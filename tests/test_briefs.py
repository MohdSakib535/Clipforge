"""The "Brief reels" tab: saved briefs, reels made from them (listed apart from
"Create a reel"), the script written from a brief and its own picture look."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Brief, Reel
from app.services import brief_script, reel_render, reel_script
from app.tasks import reels

BRIEF = "Dark psychology videos. " * 5
PARTS = {
    "niche": "Human psychology and the reality of life.",
    "rules": "Hook, a real situation, the hidden truth, a thought-provoking end. Calm voice.",
    "visual_style": "Dark hand-drawn 2D cartoon, muted sepia palette, symbolic characters",
}


@pytest.fixture
def split(monkeypatch):
    calls = []

    def fake(content, *, model=None):
        calls.append(content)
        return dict(PARTS)

    monkeypatch.setattr(brief_script, "split_brief", fake)
    return calls


def _brief(client, name="Dark psychology") -> dict:
    resp = client.post("/briefs", json={"name": name, "content": BRIEF})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_pasted_brief_is_split_into_its_parts_and_saved(client, split):
    brief = _brief(client)
    assert {k: brief[k] for k in PARTS} == PARTS
    assert (brief["name"], brief["content"]) == ("Dark psychology", BRIEF.strip())
    assert split == [BRIEF.strip()]
    assert [b["id"] for b in client.get("/briefs").json()] == [brief["id"]]


def test_a_brief_the_writer_cannot_split_is_not_saved(client, monkeypatch):
    def broken(content, *, model=None):
        raise reel_script.ScriptError("Ollama isn't running")

    monkeypatch.setattr(brief_script, "split_brief", broken)
    resp = client.post("/briefs", json={"name": "x", "content": BRIEF})
    assert resp.status_code == 502 and "Ollama" in resp.json()["error"]["message"]
    assert client.get("/briefs").json() == []
    assert client.post("/briefs", json={"name": "x", "content": "short"}).status_code == 422


def test_its_parts_can_be_edited_and_split_again_and_it_can_be_deleted(client, split):
    brief = _brief(client)
    url = f"/briefs/{brief['id']}"
    body = client.put(url, json={"name": "Dark", "visual_style": "Ink drawing"}).json()
    assert (body["name"], body["visual_style"], body["rules"]) == (
        "Dark",
        "Ink drawing",
        PARTS["rules"],
    )
    assert client.put(url, json={"rules": ""}).status_code == 422
    assert client.post(f"{url}/split").json()["visual_style"] == PARTS["visual_style"]
    assert client.delete(url).status_code == 204
    assert client.put(url, json={"name": "x"}).status_code == 404
    assert client.get("/briefs").json() == []


def test_a_reel_from_a_brief_has_its_look_and_stays_out_of_create_a_reel(client, split):
    brief = _brief(client)
    resp = client.post(
        f"/briefs/{brief['id']}/reels",
        json={"platform": "instagram", "language": "hi", "duration_s": 80},
    )
    assert resp.status_code == 202, resp.text
    reel = resp.json()
    assert (reel["status"], reel["brief_id"], reel["script_type"]) == (
        "scripting",
        brief["id"],
        "story",
    )
    assert (reel["topic"], reel["duration_s"]) == ("", 80)
    assert reel["script_model"] == "gemma3:12b"  # written by the better writer unless picked
    assert client.enqueued["reel_script"] == [reel["id"]]
    assert client.get("/reels").json() == []  # not in "Create a reel"
    assert [r["id"] for r in client.get("/briefs/reels/all").json()] == [reel["id"]]
    # An ordinary reel is the other way round.
    plain = client.post(
        "/reels", json={"topic": "3 habits", "platform": "instagram", "language": "hi"}
    )
    assert [r["id"] for r in client.get("/reels").json()] == [plain.json()["id"]]
    assert [r["id"] for r in client.get("/briefs/reels/all").json()] == [reel["id"]]
    # The fast writer can still be picked.
    fast = client.post(
        f"/briefs/{brief['id']}/reels",
        json={"platform": "instagram", "language": "hi", "script_model": "gemma3:4b"},
    ).json()
    assert fast["script_model"] == "gemma3:4b"
    assert client.get("/reels/options").json()["brief_script_model_default"] == "gemma3:12b"


def test_a_reel_from_a_brief_checks_its_choices(client, split):
    url = f"/briefs/{_brief(client)['id']}/reels"
    assert client.post(url, json={"platform": "tiktok", "language": "hi"}).status_code == 400
    assert client.post(url, json={"platform": "instagram", "language": "xx"}).status_code == 400
    assert (
        client.post(
            "/briefs/nope/reels", json={"platform": "instagram", "language": "hi"}
        ).status_code
        == 404
    )


def test_pictures_use_the_brief_look_when_there_is_one():
    assert reel_render.style_prompt("anime") == reel_render.STYLES["anime"].prompt
    assert reel_render.style_prompt("Dark ink drawing") == "Dark ink drawing"
    prompt = reel_render.picture_prompt("a man at a mirror", "Dark ink drawing")
    assert prompt.startswith("Dark ink drawing. a man at a mirror.")
    plain = Reel(style="cinematic", custom_style=None)
    assert plain.picture_style == "cinematic"
    assert Reel(style="cinematic", custom_style="Dark ink").picture_style == "Dark ink"


# ----- The script -----
def test_a_script_with_one_paragraph_per_scene_is_cut_where_they_end():
    script = "One. Two.\n\nThree is here.\n\nFour.\n\nFive."
    assert brief_script.split_scenes(script, 4) == ["One. Two.", "Three is here.", "Four.", "Five."]
    assert brief_script.paragraphs("a\nb\nc") == ["a", "b", "c"]  # no blank lines


def test_other_scripts_are_cut_into_scenes_of_about_the_same_length():
    paras = "One two three. Four five six.\n\nSeven eight nine. Ten eleven twelve. Thirteen."
    scenes = brief_script.split_scenes(paras, 3)
    assert len(scenes) == 3 and " ".join(scenes).split() == paras.split()
    assert scenes[0].endswith("six.")  # cut at the end of a paragraph, not inside one
    assert len(brief_script.split_scenes("Only one sentence here.", 4)) == 1


def _chat(replies):
    asked = []

    def fake(messages, schema, **kwargs):
        asked.append((messages, schema))
        return replies[len(asked) - 1]

    return fake, asked


def test_a_script_is_written_in_one_piece_then_cut_and_given_pictures(monkeypatch):
    script = "A question.\n\nA situation.\n\nThe hidden truth.\n\nThe end."
    first = {
        "title": "Money", "hooks": ["a", "b", "c"], "script": script,
        "call_to_action": "Follow for more.", "caption": "cap", "hashtags": ["greed"],
        "music": {"mood": "dark", "genre": "ambient", "tempo": "slow 60 BPM",
                  "instruments": "piano", "search": "dark ambient no copyright background music"},
    }  # fmt: skip
    plan = {
        "setting": "A grey city street at night",
        "characters": [{"name": "The Man", "look": "a thin man in a worn grey suit"}],
        "scenes": [
            {"image_prompt": f"The Man holds a coin {i}", "characters": ["The Man"], "crowd": False}
            for i in range(4)
        ],
    }
    fine = {"paragraphs": [{"problem": "", "text": p} for p in script.split("\n\n")]}
    fake, asked = _chat([first, fine, plan])
    monkeypatch.setattr(reel_script, "_chat", fake)
    fitted = []
    monkeypatch.setattr(
        reel_script, "fit_length", lambda scenes, *a, **k: fitted.append((len(scenes), k))
    )
    monkeypatch.setattr(brief_script, "scene_count", lambda d: 4)
    steps = []
    out = brief_script.generate_script(
        PARTS, "greed", "instagram", "en", 40, voice="af_heart", on_step=steps.append
    )
    assert [sc["narration"] for sc in out["scenes"]][:3] == [
        "A question.",
        "A situation.",
        "The hidden truth.",
    ]
    assert out["scenes"][-1]["narration"] == "The end. Follow for more."  # the call to action
    assert [sc["image_prompt"] for sc in out["scenes"]][1] == "The Man holds a coin 1"
    assert out["characters"] == [{"name": "The Man", "look": "a thin man in a worn grey suit"}]
    assert out["setting"] == "A grey city street at night"
    assert out["title"] == "Money" and out["music"]["mood"] == "dark"
    assert len(fitted) == 1 and fitted[0][0] == 4  # its length fitted...
    assert fitted[0][1]["story"] is True and callable(fitted[0][1]["measure"])  # ...read aloud
    assert steps[:3] == ["Checking it against the brief", "Checking its length",
                         "Planning the pictures"]  # fmt: skip
    # The brief is in the writer's prompt and the editor's, the look in the picture planner's.
    system = asked[0][0][0]["content"]
    assert PARTS["niche"] in system and PARTS["rules"] in system
    assert PARTS["rules"] in asked[1][0][0]["content"]
    assert PARTS["visual_style"] in asked[2][0][0]["content"]
    assert "Topic: greed" in asked[0][0][1]["content"]


def test_the_writer_follows_the_brief_as_written_not_one_niche():
    brief = {
        "niche": "Quick kitchen hacks for busy people.",
        "rules": "The problem, then the hack, then a tip to try. Cheerful voice.",
        "visual_style": "Bright flat illustration",
        "content": "# Kitchen hacks\nEnd every video with a quick tip to try.",
    }
    messages = brief_script._script_messages(brief, "", "facebook", "hi", 75, 6, 180, "")
    system = messages[0]["content"]
    assert brief["content"] in system  # the creator's own words, not only the summary
    assert "where they differ from the brief above, follow these" in system
    # Nothing of one niche is forced on every brief: its own rules decide.
    for word in ("psychology", "advice", "graves", "made-up names"):
        assert word not in system
    # Without the pasted text (a reel made before), the parts alone.
    assert "<<<" not in brief_script._script_messages(PARTS, "", "facebook", "hi", 75, 6, 180,
                                                      "")[0]["content"]  # fmt: skip


def test_without_a_topic_the_writer_is_told_the_ideas_already_made():
    done = ["झूठी मुस्कान का रहस्य", "मृगतृष्णा"]
    fresh = brief_script._script_messages(PARTS, "", "facebook", "hi", 75, 6, 180, "", done)
    assert "pick an idea none of them had" in fresh[1]["content"] and done[1] in fresh[1]["content"]
    given = brief_script._script_messages(PARTS, "greed", "facebook", "hi", 75, 6, 180, "", done)
    assert "already made" not in given[1]["content"]  # the creator's own topic wins


def test_the_length_is_measured_with_the_reels_voice(monkeypatch):
    read = []

    def fake_synthesize(text, language, voice, speed=1.0, by_sentence=False):
        read.append((text, voice, speed, by_sentence))
        return [0.0] * (len(text.split()) * 1000), 1000  # a word a second

    monkeypatch.setattr(brief_script.tts, "synthesize", fake_synthesize)
    seconds = brief_script._read_aloud("hi", None)
    assert seconds(["एक दो", "तीन"]) == pytest.approx(3 + 0.8 + 0.8)  # words + pauses
    assert read[0][1:] == (brief_script.tts.default_voice("hi"), 0.92, True)  # as reels read
    seconds(["एक दो", "चार पाँच"])
    assert [r[0] for r in read] == ["एक दो", "तीन", "चार पाँच"]  # each line read once

    def broken(*a, **k):
        raise brief_script.tts.TTSError("no voice model")

    monkeypatch.setattr(brief_script.tts, "synthesize", broken)
    fallback = brief_script._read_aloud("hi", "hm_omega")(["नया वाक्य"])
    assert fallback == pytest.approx(
        reel_script.spoken_seconds(["नया वाक्य"], "hi", "hm_omega", True)
    )


def test_the_editor_rewrites_only_the_paragraphs_that_break_the_brief(monkeypatch):
    texts = ["Why do we chase money?", "A man counts coins at night in his small shop.",
             "So you should save more and spend less every month."]  # fmt: skip
    review = {"paragraphs": [
        {"problem": "", "text": "Something else"},  # fine: kept as it was
        {"problem": "weak", "text": "No."},  # far shorter: the length would break
        {"problem": "advice", "text": "And still, the more he saved, the less he lived."},
    ]}  # fmt: skip
    fake, asked = _chat([review])
    monkeypatch.setattr(reel_script, "_chat", fake)
    out = brief_script._review({**PARTS, "content": BRIEF}, texts, "en", model="m")
    assert out == [texts[0], texts[1], "And still, the more he saved, the less he lived."]
    assert BRIEF.strip() in asked[0][0][0]["content"] and texts[2] in asked[0][0][1]["content"]
    # It checks each sentence; an order to the viewer breaks a brief that forbids advice.
    assert "tells them what to do, remember" in asked[0][0][1]["content"]

    def down(*a, **k):
        raise reel_script.ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "_chat", down)
    assert brief_script._review(PARTS, texts, "en", model=None) == texts  # best effort
    fake, _ = _chat([{"paragraphs": [{"problem": "x", "text": "y"}]}])  # wrong count
    monkeypatch.setattr(reel_script, "_chat", fake)
    assert brief_script._review(PARTS, texts, "en", model=None) == texts


def test_without_a_topic_the_writer_invents_one(monkeypatch):
    fake, asked = _chat([{"script": ""}, {"script": ""}])
    monkeypatch.setattr(reel_script, "_chat", fake)
    with pytest.raises(reel_script.BadReplyError):
        brief_script.generate_script(PARTS, "", "instagram", "en", 40)
    assert len(asked) == 2  # tried twice
    assert "invent one original concept" in asked[0][0][1]["content"]


# ----- The task -----
@pytest.fixture
def factory(db_session, monkeypatch):
    f = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(reels, "SessionLocal", f)
    return f


SCRIPT = {
    "title": "Money", "hooks": [],
    "scenes": [{"narration": "One.", "image_prompt": "a coin"}],
    "caption": "", "hashtags": [],
}  # fmt: skip


def _brief_reel(db_session, **fields) -> Reel:
    brief = Brief(user_id="u1", name="Dark", content=BRIEF, **PARTS)
    db_session.add(brief)
    db_session.commit()
    reel = Reel(user_id="u1", platform="instagram", language="en", voice="af_heart", topic="greed",
                duration_s=60, script_type="story", brief_id=brief.id, status="scripting",
                custom_style="An old look", **fields)  # fmt: skip
    db_session.add(reel)
    db_session.commit()
    return reel


def test_the_script_task_writes_from_the_brief_and_takes_its_look(db_session, factory, monkeypatch):
    reel = _brief_reel(db_session)
    asked = []
    monkeypatch.setattr(
        brief_script, "generate_script", lambda *a, **k: asked.append((a, k)) or SCRIPT
    )
    monkeypatch.setattr(
        reel_script, "generate_script", lambda *a, **k: pytest.fail("not an ordinary reel")
    )
    reels.write_reel_script.apply(args=[reel.id])
    db_session.refresh(reel)
    assert (reel.status, reel.script, reel.custom_style) == (
        "script_ready",
        SCRIPT,
        PARTS["visual_style"],
    )
    args, kwargs = asked[0]
    assert kwargs["earlier"] == []  # nothing else made from this brief yet
    assert args[0] == {**PARTS, "content": BRIEF}  # the brief as pasted, too
    assert args[1:5] == ("greed", "instagram", "en", 60)
    assert kwargs["model"] is None


def test_the_writer_hears_of_the_reels_already_made_from_the_brief(
    db_session, factory, monkeypatch
):
    first = _brief_reel(db_session, script={**SCRIPT, "title": "Money"})
    same = dict(user_id="u1", platform="instagram", language="en", voice="af_heart",
                duration_s=60, script_type="story", status="scripting")  # fmt: skip
    later = Reel(topic="", brief_id=first.brief_id, **same)
    other = Reel(topic="Habits", **same)  # not from a brief
    db_session.add_all([later, other])
    db_session.commit()
    asked = []
    monkeypatch.setattr(
        brief_script, "generate_script", lambda *a, **k: asked.append(k["earlier"]) or SCRIPT
    )
    reels.write_reel_script.apply(args=[later.id])
    assert asked == [["Money"]]  # its brief's other reels only, not itself or others


def test_a_reel_whose_brief_was_deleted_cannot_be_written_again(db_session, factory):
    reel = _brief_reel(db_session)
    db_session.delete(db_session.get(Brief, reel.brief_id))
    db_session.commit()
    reels.write_reel_script.apply(args=[reel.id])
    db_session.refresh(reel)
    assert (reel.status, reel.custom_style) == ("failed", "An old look")
    assert "brief of this reel was deleted" in reel.error


# ----- Splitting a brief -----
def test_a_brief_is_split_by_the_writer_and_a_long_look_is_made_short(monkeypatch):
    long_look = "The look will be " + "dark and muted, " * 40
    replies = [
        {"niche": "n", "rules": "r", "visual_style": long_look},
        {"style": "Dark hand-drawn 2D cartoon, muted sepia"},
    ]
    fake, asked = _chat(replies)
    monkeypatch.setattr(reel_script, "_chat", fake)
    parts = brief_script.split_brief("A long pasted brief " * 5)
    assert parts == {
        "niche": "n",
        "rules": "r",
        "visual_style": "Dark hand-drawn 2D cartoon, muted sepia",
    }
    assert len(asked) == 2 and "A long pasted brief" in asked[0][0][1]["content"]
    assert long_look.strip()[:40] in asked[1][0][1]["content"]


def test_the_look_leaves_the_symbols_to_each_picture(monkeypatch):
    # The writers kept them in the look (12B too), which drew all of them into every picture.
    look = (
        "Dark hand-drawn 2D cartoon, muted palette (sepia, charcoal, faded olive), "
        "symbolic imagery – money, clocks, graves, masks – detailed linework, moody light"
    )
    reply = {"niche": "n", "rules": "r", "visual_style": look,
             "symbols": ["money", "clock", "grave", "mask"]}  # fmt: skip
    fake, asked = _chat([reply])
    monkeypatch.setattr(reel_script, "_chat", fake)
    assert brief_script.split_brief("A pasted brief " * 5)["visual_style"] == (
        "Dark hand-drawn 2D cartoon, muted palette (sepia, charcoal, faded olive), "
        "detailed linework, moody light"
    )
    assert "symbols" in asked[0][1]["properties"]
    assert brief_script.without_symbols("glasses of water, flat vector", ["glass"]) == "flat vector"
    assert brief_script.without_symbols("money, clocks", ["money", "clock"]) == "money, clocks"


def test_a_look_that_is_already_short_is_kept_and_a_useless_split_fails(monkeypatch):
    fake, asked = _chat([{"niche": "", "rules": "r", "visual_style": "Ink drawing, sepia"}])
    monkeypatch.setattr(reel_script, "_chat", fake)
    assert brief_script.split_brief("x" * 30)["visual_style"] == "Ink drawing, sepia"
    assert len(asked) == 1
    fake, _ = _chat([{"niche": "n", "rules": "", "visual_style": "Ink"}])
    monkeypatch.setattr(reel_script, "_chat", fake)
    with pytest.raises(reel_script.BadReplyError):
        brief_script.split_brief("x" * 30)


def test_orders_to_the_viewer_are_found_in_hindi():
    orders = {
        "यात्रा को महसूस करें, सिर्फ गंतव्य को नहीं।": "करें",
        "इसलिए, अगली बार किसी की मुस्कान देखें, याद रखें: हर मुस्कान सच्ची नहीं होती।": "देखें",
        "उसे खुद को बदलना चाहिए।": "चाहिए",
        "डर को मत पालो।": "मत",
        "खुद पर भरोसा रखो।": "रखो",
    }
    for sentence, word in orders.items():
        assert brief_script.order_word(sentence) == word, sentence
    for fine in (
        "उसकी आँखें खाली थीं।",  # a plural noun, not "do it"
        "सोचिए, एक अधिकारी निराश होकर भी मुस्कुरा रहा है।",  # a story's opener
        "हम सभी दुःख छुपाते हैं, दूसरों को खुश करने के लिए।",
        "क्या हम सच में खुश हैं?",  # a question is what the brief wants
        "भीड़ में एक महिला मुस्कुरा रही थी।",
        "उसे पैसा चाहिए।",  # "needs", not "should"
        "सबसे बड़ा नुकसान यह है कि हम अपनी क्षमता को भूल जाते हैं।",
        "क्या हमें बदलना चाहिए?",  # "should we?" asks, it doesn't tell
    ):
        assert brief_script.order_word(fine) is None, fine
    # An order before a question is still one.
    asked = "अगली बार 'धन्यवाद' कहते समय विचार कीजिए; क्या यह आभार है, या छिपी हुई शर्त?"
    assert brief_script.order_word(asked) == "कीजिए"


def test_words_meant_to_be_written_in_a_picture_are_left_out():
    w = brief_script.without_written_words
    assert w("A desk. Behind him, files labelled 'Next Steps' are piled up.") == (
        "A desk. Behind him, files are piled up."
    )
    assert (
        w('A shop with a sign reading "Sale", dusty street.') == "A shop with a sign, dusty street."
    )
    assert w("The employee’s hand trembles; The Merchant's smile is thin.") == (
        "The employee’s hand trembles; The Merchant's smile is thin."
    )  # apostrophes are not quotes


def test_a_brief_without_advice_gets_its_orders_said_as_questions(monkeypatch):
    assert brief_script.forbids_advice({"rules": "Avoid offering advice, lecturing."})
    assert brief_script.forbids_advice({"rules": "r", "content": "- No advice, no lectures."})
    assert not brief_script.forbids_advice({"rules": "End with a quick tip to try."})
    texts = ["वो रोज़ मेहनत करता है। फिर भी कुछ नहीं बदलता।",
             "हम अपनी क्षमता भूल जाते हैं। यात्रा को महसूस करें, सिर्फ गंतव्य को नहीं।",
             "याद रखें, हर मुस्कान सच्ची नहीं होती।"]  # fmt: skip
    rewrites = {"sentences": ["क्या यात्रा का अनुभव ही असली है, या सिर्फ मंज़िल?",
                              "हर मुस्कान सच्ची नहीं होती, ये याद रखें।"]}  # fmt: skip
    fake, asked = _chat([rewrites])
    monkeypatch.setattr(reel_script, "_chat", fake)
    brief = {**PARTS, "rules": "No advice, no lectures."}
    out = brief_script._no_orders(brief, texts, "hi", model="m")
    assert out[0] == texts[0]  # no order in it: never sent
    assert out[1] == "हम अपनी क्षमता भूल जाते हैं। क्या यात्रा का अनुभव ही असली है, या सिर्फ मंज़िल?"
    assert out[2] == texts[2]  # its new line still gives an order: kept as it was
    prompt = asked[0][0][1]["content"]
    assert '(the order: "करें")' in prompt and "वो रोज़ मेहनत करता है" not in prompt
    # A brief that allows advice, or another language: nothing is asked.
    assert brief_script._no_orders(PARTS, texts, "hi", model="m") == texts
    assert brief_script._no_orders(brief, ["Remember this."], "en", model="m") == ["Remember this."]
    assert len(asked) == 1


def test_a_hindi_reel_is_pictured_where_its_viewers_live(monkeypatch):
    plan = {"setting": "s", "characters": [], "scenes": [{"image_prompt": "p", "characters": [],
                                                          "crowd": False}]}  # fmt: skip
    fake, asked = _chat([plan, plan])
    monkeypatch.setattr(reel_script, "_chat", fake)
    brief = {**PARTS, "content": "Symbols such as money, clocks and chains."}
    brief_script._pictures(brief, ["एक"], "greed", language="hi", model=None)
    brief_script._pictures(PARTS, ["One"], "greed", language="en", model=None)
    hindi = asked[0][0][1]["content"]
    assert "The viewers are in India" in hindi and "money is Indian rupee notes" in hindi
    assert "viewers are in" not in asked[1][0][1]["content"]
    # The brief's symbols reach the planner, which puts at most one in a picture
    # (in the look, they were drawn into every picture).
    assert brief["content"] in asked[0][0][0]["content"] and "at most one main symbol" in hindi
    assert "no shop signs, signboards, posters" in hindi  # drawn with nonsense letters
    # The Hindi writing note gives only plain examples: "आईएएस" became an IAS officer.
    assert "आईएएस" not in reel_script.LANGUAGE_NOTES["hi"]


def test_every_paragraph_has_a_role_in_the_story():
    roles = brief_script._roles(8)
    assert "its ending is paragraph 8" in roles and "fill paragraphs 2 to 7" in roles
    assert "the hook that stops the scroll" in roles
    assert "fill paragraph 2," in brief_script._roles(3)
    assert "the last one is the ending" in brief_script._roles(2)


def test_a_brief_pasted_without_a_name_is_named_by_its_first_line(client, split):
    heading = "# Dark Human Psychology: videos about greed and death"
    resp = client.post("/briefs", json={"content": f"\n{heading}\n\n" + BRIEF})
    assert resp.json()["name"] == "Dark Human Psychology: videos about greed and death"
    long = client.post("/briefs", json={"content": "word " * 40 + "\n" + BRIEF}).json()["name"]
    assert len(long) == 60 and long.endswith("…")
    assert client.post("/briefs", json={"name": "Mine", "content": BRIEF}).json()["name"] == "Mine"


def test_a_short_reel_gets_about_one_picture_for_every_13_seconds():
    assert [brief_script.scene_count(d) for d in (30, 60, 75, 90, 120, 300)] == [3, 5, 6, 7, 8, 15]
