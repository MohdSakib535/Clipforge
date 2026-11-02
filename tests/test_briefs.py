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
    assert client.enqueued["reel_script"] == [reel["id"]]
    assert client.get("/reels").json() == []  # not in "Create a reel"
    assert [r["id"] for r in client.get("/briefs/reels/all").json()] == [reel["id"]]
    # An ordinary reel is the other way round.
    plain = client.post(
        "/reels", json={"topic": "3 habits", "platform": "instagram", "language": "hi"}
    )
    assert [r["id"] for r in client.get("/reels").json()] == [plain.json()["id"]]
    assert [r["id"] for r in client.get("/briefs/reels/all").json()] == [reel["id"]]


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
    fake, asked = _chat([first, plan])
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
    assert fitted == [(4, {"story": True, "model": None})]  # told calmly, its length fitted
    assert steps[:2] == ["Checking its length", "Planning the pictures"]
    # The brief is in the writer's prompt, and the look is in the picture planner's.
    system = asked[0][0][0]["content"]
    assert PARTS["niche"] in system and PARTS["rules"] in system
    assert PARTS["visual_style"] in asked[1][0][0]["content"]
    assert "Topic: greed" in asked[0][0][1]["content"]


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
    assert args[0] == PARTS and args[1:5] == ("greed", "instagram", "en", 60)
    assert kwargs["model"] is None


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


def test_a_look_that_is_already_short_is_kept_and_a_useless_split_fails(monkeypatch):
    fake, asked = _chat([{"niche": "", "rules": "r", "visual_style": "Ink drawing, sepia"}])
    monkeypatch.setattr(reel_script, "_chat", fake)
    assert brief_script.split_brief("x" * 30)["visual_style"] == "Ink drawing, sepia"
    assert len(asked) == 1
    fake, _ = _chat([{"niche": "n", "rules": "", "visual_style": "Ink"}])
    monkeypatch.setattr(reel_script, "_chat", fake)
    with pytest.raises(reel_script.BadReplyError):
        brief_script.split_brief("x" * 30)


def test_every_paragraph_has_a_role_in_the_story():
    assert "Paragraph 8 is the closing thought" in brief_script._roles(8)
    assert "Paragraphs 3 to 6" in brief_script._roles(8)
    assert "Paragraph 4 is the closing" in brief_script._roles(4)
    assert "relatable real-life situation" in brief_script._roles(5)


def test_a_brief_pasted_without_a_name_is_named_by_its_first_line(client, split):
    heading = "# Dark Human Psychology: videos about greed and death"
    resp = client.post("/briefs", json={"content": f"\n{heading}\n\n" + BRIEF})
    assert resp.json()["name"] == "Dark Human Psychology: videos about greed and death"
    long = client.post("/briefs", json={"content": "word " * 40 + "\n" + BRIEF}).json()["name"]
    assert len(long) == 60 and long.endswith("…")
    assert client.post("/briefs", json={"name": "Mine", "content": BRIEF}).json()["name"] == "Mine"


def test_a_short_reel_gets_about_one_picture_for_every_13_seconds():
    assert [brief_script.scene_count(d) for d in (30, 60, 75, 90, 120, 300)] == [3, 5, 6, 7, 8, 15]
