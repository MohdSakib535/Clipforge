"""Reel scripts: the prompt sent to Ollama and the clean-up of its reply."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import reel_render, reel_script, tts
from app.services.reel_script import ScriptError, normalize_script


def test_scene_count_follows_the_length():
    assert reel_script.scene_count(35) == 5
    assert reel_script.scene_count(15) == 3  # never fewer than 3
    assert reel_script.scene_count(90) == 8  # no more than 8 up to ~3 minutes
    assert reel_script.scene_count(300) == 15  # then one picture every 20 s


def test_a_slower_voice_gets_fewer_words():
    usual = reel_script.words_per_scene(40, "hi", 6, "hm_psi")
    assert reel_script.words_per_scene(40, "hi", 6, "hm_calm") < usual  # speaks at 0.9
    assert reel_script.words_per_scene(40, "hi", 6, "hm_young") > usual  # at 1.08
    assert reel_script.words_per_scene(40, "hi", 6, "hf_beta") > usual  # a faster voice


def test_a_reel_may_run_a_little_long_but_hardly_short():
    assert reel_script.length_window(300) == (291, 345)  # 5 min: up to 45 s more
    assert reel_script.length_window(240) == pytest.approx((232.8, 276))  # 4 min: 15%
    assert reel_script.length_window(35) == pytest.approx((32, 40.25))
    assert reel_script.length_window(15) == (12, 18)
    assert reel_script.aim_s(300) == 322.5  # written a bit long: the model writes short


def test_the_words_fill_the_length_but_the_pauses_and_call_to_action():
    wps = tts.words_per_s("hi", "hf_alpha")
    # 15 scenes: 14 breaths of 0.8 s and 0.8 s after the last one.
    words = reel_script.script_words(322.5, "hi", 15, "hf_alpha", story=True)
    assert words == pytest.approx((322.5 - 14 * 0.8 - 0.8) * wps * reel_render.STORY_PACE)
    per_scene = reel_script.words_per_scene(300, "hi", 15, "hf_alpha", story=True)
    assert per_scene == round((words - reel_script.CALL_TO_ACTION_WORDS) / 15)


def test_prompt_has_language_platform_and_size():
    system, user = reel_script.build_messages("morning habits", "instagram", "hi", 35)
    text = system["content"]
    assert "Devanagari" in text
    assert "Exactly 5 scenes" in text
    assert "save this reel" in text
    assert "Instagram Reel" in text
    assert "morning habits" in user["content"] and "35 seconds" in user["content"]

    fb = reel_script.build_messages("x", "facebook", "en", 35)[0]["content"]
    assert "share it with someone" in fb and "Facebook Reel" in fb


def test_script_types_shape_the_scenes():
    story = reel_script.build_messages("a lost dog", "instagram", "en", 35)[0]["content"]
    assert "one story with a beginning, middle and end" in story  # the default
    assert "Scenes 2 to 4 each carry on straight from the scene before" in story
    assert "Scene 5 gives the story a clear ending" in story and "save this reel" in story
    tips = reel_script.build_messages("x", "instagram", "en", 35, "tips")[0]["content"]
    assert "Scenes 2 to 4 give one point each" in tips and "one story" not in tips
    facts = reel_script.build_messages("x", "facebook", "en", 35, "facts")[0]["content"]
    assert "explain the topic step by step" in facts and "share it with someone" in facts
    # A character drawn in several pictures must look the same in each.
    assert "the look is written once, in full, here, and is added to every picture" in story


def test_call_to_action_closes_the_last_scene():
    assert "call_to_action" in reel_script.response_schema(3)["required"]
    scenes = [{"narration": "Moti is home.", "image_prompt": "a dog"},
              {"narration": "The end.", "image_prompt": "a door"}]  # fmt: skip
    raw = {"scenes": scenes, "call_to_action": " Save this reel! "}
    s = normalize_script(raw, topic="dog", language="en", scenes=2)
    assert [sc["narration"] for sc in s["scenes"]] == ["Moti is home.", "The end. Save this reel!"]
    # Already said in the last scene: not said twice.
    again = {"scenes": [{"narration": "The end. Save this reel!", "image_prompt": "a door"}],
             "call_to_action": "Save this reel!"}  # fmt: skip
    s = normalize_script(again, topic="dog", language="en", scenes=1)
    assert s["scenes"][0]["narration"] == "The end. Save this reel!"


def test_call_to_action_already_in_the_last_scene_is_not_said_twice():
    def last(narration, language="hi"):
        raw = {"scenes": [{"narration": narration, "image_prompt": "p"}],
               "call_to_action": "इस रील को सेव करें और फॉलो करें!"}  # fmt: skip
        script = normalize_script(raw, topic="t", language=language, scenes=1)
        return script["scenes"][0]["narration"]

    own = "कहानी खत्म। ऐसे और वीडियो के लिए हमें फ़ॉलो करो!"
    assert last(own) == own
    # "Follow your dreams" is part of the story, not a call to action.
    story_end = last("Follow your dreams.", "en")
    assert story_end == "Follow your dreams. इस रील को सेव करें और फॉलो करें!"


def test_description_goes_to_the_model_after_the_topic():
    _, user = reel_script.build_messages(
        "a lost dog", "instagram", "hi", 35, "story", "Start sad, end happy. Dog is named Moti."
    )
    text = user["content"]
    assert text.startswith("Topic: a lost dog\nLength: 35 seconds.\n")
    assert "within the rules above:\nStart sad, end happy. Dog is named Moti." in text
    assert "No plot was given" not in text
    # Always last: picture prompts in English (they once came back in Hindi).
    reminder = (
        "\nReminder: every narration in Hindi, written in Devanagari script, "
        "every image_prompt in English."
    )
    assert text.endswith(reminder)
    _, plain = reel_script.build_messages("a lost dog", "instagram", "hi", 35)
    assert "No plot was given: invent a specific one" in plain["content"]  # a story needs a plot
    assert plain["content"].endswith(reminder)
    _, tips = reel_script.build_messages("habits", "instagram", "hi", 35, "tips")
    assert "No plot was given" not in tips["content"]


def test_writing_rules_against_mixed_languages_filler_and_looks_in_narration():
    hindi = reel_script.build_messages("x", "instagram", "hi", 35)[0]["content"]
    assert "never words from any other language" in hindi
    assert "never in Roman letters" in hindi  # no Hinglish in a Hindi script
    english = reel_script.build_messages("x", "instagram", "en", 35)[0]["content"]
    assert "never words from any other language" in english
    assert "Roman letters" not in english  # that rule is for Hindi
    assert "never repeat a line or an idea" in hindi and "one point of view" in hindi
    assert "never says anyone's age, hair, build, face or clothes" in hindi
    assert "never a summary" in hindi  # stories show moments


def test_spoken_length_counts_words_pace_and_pauses():
    texts = [" ".join(["शब्द"] * 15), " ".join(["शब्द"] * 10)]
    # 25 words at 2.5 a second, 0.25 s after each scene and 0.55 s more at the end.
    assert reel_script.spoken_seconds(texts, "hi") == pytest.approx(10 + 0.5 + 0.55)
    calm = tts.words_per_s("hi", "hm_calm")  # measured
    assert reel_script.spoken_seconds(texts, "hi", "hm_calm") == pytest.approx(25 / calm + 1.05)


def _scenes(*word_counts):
    return [{"narration": " ".join(["शब्द"] * c), "image_prompt": "p"} for c in word_counts]


def test_a_script_of_the_right_length_is_left_alone(monkeypatch):
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: pytest.fail("no rewrite needed"))
    scenes = _scenes(24, 24, 24, 24)  # ~38 s of words + 1.55 s of pauses, for 40 s
    reel_script.fit_length(scenes, "hi", 40)
    assert [len(sc["narration"].split()) for sc in scenes] == [24] * 4


def test_a_too_long_script_is_rewritten_shorter(monkeypatch):
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append(messages[-1]["content"])
        return {k: " ".join(["छोटा"] * 24) for k in schema["required"]}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    scenes = _scenes(40, 40, 40, 40)  # ~66 s for a 40 s reel
    reel_script.fit_length(scenes, "hi", 40)
    assert "they must take 43 seconds. Rewrite all of them shorter" in asked[0]
    assert len(asked) == 1  # it fits now
    assert [len(sc["narration"].split()) for sc in scenes] == [24] * 4
    assert all(sc["image_prompt"] == "p" for sc in scenes)  # the pictures stay


def test_a_rewrite_that_does_not_help_is_dropped(monkeypatch):
    scenes = _scenes(40, 40, 40, 40)
    longer = {f"scene_{i}": " ".join(["x"] * 60) for i in range(1, 5)}
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: longer)
    reel_script.fit_length(scenes, "hi", 40)
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"scene_1": "x"})
    reel_script.fit_length(scenes, "hi", 40)  # scenes left out

    def broken(*a, **k):
        raise ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "_chat", broken)
    reel_script.fit_length(scenes, "hi", 40)
    assert [len(sc["narration"].split()) for sc in scenes] == [40] * 4


def test_a_script_a_little_long_is_kept_but_a_short_one_is_not(monkeypatch):
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: pytest.fail("no rewrite needed"))
    scenes = _scenes(*[54] * 15)  # ~328 s for 5 min: 28 s long is fine
    reel_script.fit_length(scenes, "hi", 300)
    assert [len(sc["narration"].split()) for sc in scenes] == [54] * 15

    asked = []
    monkeypatch.setattr(reel_script, "_chat", lambda m, s, **o: asked.append(s) or {})
    reel_script.fit_length(_scenes(*[46] * 15), "hi", 300)  # ~280 s: 20 s short
    assert asked


def test_only_the_scenes_too_short_are_written_longer(monkeypatch):
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append((messages[-1]["content"], schema["required"]))
        return {k: " ".join(["लंबा"] * 39) for k in schema["required"]}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    scenes = _scenes(40, 10, 40, 10)  # ~42 s for a 60 s reel: scenes 2 and 4 fell short
    reel_script.fit_length(scenes, "hi", 60)
    [(prompt, keys)] = asked
    assert keys == ["scene_2", "scene_4"]
    # Written for 64.5 s, halfway into the 9 s a 60 s reel may run long.
    assert "they must take 64 seconds. Rewrite only scenes 2, 4 longer" in prompt
    assert "about 39 words each" in prompt
    assert "Scene 1: " in prompt and "Scene 4: " in prompt  # the whole story is given
    assert [len(sc["narration"].split()) for sc in scenes] == [40, 39, 40, 39]


def test_a_script_still_too_short_is_rewritten_once_more(monkeypatch):
    scene_2 = iter([15, 39])  # the first rewrite still falls short there

    def fake_chat(messages, schema, **options):
        return {"scene_2": " ".join(["लंबा"] * next(scene_2)), "scene_4": " ".join(["लंबा"] * 39)}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    scenes = _scenes(40, 10, 40, 10)
    reel_script.fit_length(scenes, "hi", 60)
    assert [len(sc["narration"].split()) for sc in scenes] == [40, 39, 40, 39]


def test_the_length_is_rewritten_at_most_twice(monkeypatch):
    calls = []

    def no_better(messages, schema, **options):
        calls.append(schema["required"])
        return {k: " ".join(["x"] * 10) for k in schema["required"]}

    monkeypatch.setattr(reel_script, "_chat", no_better)
    scenes = _scenes(40, 10, 40, 10)
    reel_script.fit_length(scenes, "hi", 60)
    assert len(calls) == reel_script.LENGTH_TRIES == 2
    assert [len(sc["narration"].split()) for sc in scenes] == [40, 10, 40, 10]


def test_the_call_to_action_goes_on_after_the_length_fix(monkeypatch):
    script = {
        "scenes": [
            {"narration": " ".join(["लंबा"] * 30), "image_prompt": "A dog"} for _ in range(3)
        ],
        "call_to_action": "रील सेव करें और फॉलो करें!",
    }
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append(messages[-1]["content"])
        if "scene_1" in schema["properties"]:
            return {k: " ".join(["छोटा"] * 8) for k in schema["required"]}
        return script

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    s = reel_script.generate_script("dog", "instagram", "hi", 15)  # 3 scenes
    assert "फॉलो" not in asked[1]  # the rewrite never sees it...
    assert s["scenes"][-1]["narration"] == " ".join(["छोटा"] * 8) + " रील सेव करें और फॉलो करें!"


def test_reply_is_cleaned_up():
    raw = {
        "title": " सुबह की आदतें ",
        "hooks": ["क्या आप थके हैं? (Question hook)", "", "बड़ा दावा"],
        "scenes": [
            {"narration": "उठें जल्दी (Wake up early) और पानी पिएं।", "image_prompt": "sunrise"},
            {"narration": "  ", "image_prompt": "dropped: no narration"},
            {"narration": "ध्यान करें।", "image_prompt": ""},
        ],
        "caption": "सेव करें!",
        "hashtags": ["#Morning Routine", "morningroutine", "#सुबह", "health!"],
    }
    s = normalize_script(raw, topic="morning habits", language="hi", scenes=2)
    assert s["title"] == "सुबह की आदतें"
    assert s["hooks"] == ["क्या आप थके हैं?", "बड़ा दावा"]
    assert [sc["narration"] for sc in s["scenes"]] == ["उठें जल्दी और पानी पिएं।", "ध्यान करें।"]
    assert s["scenes"][1]["image_prompt"] == "morning habits"  # empty prompt -> topic
    assert s["hashtags"] == ["morningroutine", "सुबह", "health"]


def test_english_keeps_its_brackets_and_missing_title_uses_topic():
    raw = {"scenes": [{"narration": "Drink water (a full glass).", "image_prompt": "glass"}]}
    s = normalize_script(raw, topic="water", language="en", scenes=1)
    assert s["scenes"][0]["narration"] == "Drink water (a full glass)."
    assert s["title"] == "water"


def test_script_crammed_into_one_scene_is_spread_out():
    # gemma3:4b once put the whole script into scene 1.
    sentences = [f"Sentence number {i} has a few words." for i in range(1, 7)]
    raw = {
        "scenes": [
            {"narration": " ".join(sentences), "image_prompt": "p1"},
            {"narration": "Short.", "image_prompt": "p2"},
        ]
    }
    s = normalize_script(raw, topic="t", language="en", scenes=4)
    assert len(s["scenes"]) == 4
    assert all(sc["narration"] for sc in s["scenes"])
    joined = " ".join(sc["narration"] for sc in s["scenes"])
    assert joined == " ".join([*sentences, "Short."])  # nothing lost, order kept
    assert [sc["image_prompt"] for sc in s["scenes"]] == ["p1", "p2", "p2", "p2"]


def test_empty_reply_is_an_error():
    with pytest.raises(ScriptError):
        normalize_script({"scenes": []}, topic="t", language="en", scenes=3)
    with pytest.raises(ScriptError):
        normalize_script("not a dict", topic="t", language="en", scenes=3)


def _reply(content: dict, status: int = 200) -> httpx.Response:
    body = {"message": {"role": "assistant", "content": json.dumps(content)}}
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://x"))


def test_generate_script_asks_ollama_for_json(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.setdefault("url", url)  # the first call: the script
        sent.setdefault("body", json)
        scenes = [{"narration": f"Line {i}.", "image_prompt": f"p{i}"} for i in range(5)]
        return _reply({"title": "T", "hooks": ["a", "b", "c"], "scenes": scenes,
                       "caption": "c", "hashtags": ["x"]})  # fmt: skip

    monkeypatch.setattr(reel_script.httpx, "post", fake_post)
    s = reel_script.generate_script("topic", "facebook", "en", 35)
    assert sent["url"].endswith("/api/chat")
    assert sent["body"]["model"] == "gemma3:4b"
    assert sent["body"]["keep_alive"] == 0
    assert sent["body"]["stream"] is False
    assert sent["body"]["format"]["properties"]["scenes"]["minItems"] == 5
    assert sent["body"]["options"]["num_ctx"] == 4096
    assert len(s["scenes"]) == 5 and s["title"] == "T"


def test_long_reel_gets_room_for_a_long_reply(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        if "scenes" in json["format"]["properties"]:
            sent.setdefault("body", json)  # the script itself
        scenes = [{"narration": f"Line {i}.", "image_prompt": f"p{i}"} for i in range(15)]
        return _reply({"title": "T", "scenes": scenes})

    monkeypatch.setattr(reel_script.httpx, "post", fake_post)
    s = reel_script.generate_script("topic", "instagram", "hi", 300)
    assert sent["body"]["options"]["num_predict"] == 5000
    assert sent["body"]["options"]["num_ctx"] == 7048
    assert len(s["scenes"]) == 15
    assert "a few short spoken sentences" in sent["body"]["messages"][0]["content"]


def test_music_suggestion_is_cleaned_up():
    raw = {"mood": " calm ", "genre": "lo-fi", "tempo": "slow, 70-85 BPM", "instruments": "piano"}
    music = reel_script.clean_music(raw)
    assert music == {
        "mood": "calm", "genre": "lo-fi", "tempo": "slow, 70-85 BPM", "instruments": "piano",
        "search": "calm lo-fi no copyright background music",  # built when missing
    }  # fmt: skip
    assert reel_script.clean_music({"tempo": "fast"}) is None  # no mood or genre
    assert reel_script.clean_music("nope") is None


def test_script_carries_its_music_suggestion():
    raw = {
        "scenes": [{"narration": "Hi.", "image_prompt": "p"}],
        "music": {"mood": "upbeat", "genre": "pop", "tempo": "120 BPM", "instruments": "synth",
                  "search": "upbeat pop no copyright background music"},
    }  # fmt: skip
    s = normalize_script(raw, topic="t", language="en", scenes=1)
    order = ["title", "hooks", "setting", "characters", "scenes", "caption", "hashtags", "music"]
    assert list(s) == order
    from app.schemas.reel import ReelScene, ReelScript  # the page compares the two: same order

    assert list(ReelScript.model_fields) == order
    assert list(s["scenes"][0]) == list(ReelScene.model_fields)
    assert s["music"]["genre"] == "pop"
    assert "music" not in normalize_script({**raw, "music": {}}, topic="t", language="en", scenes=1)
    assert "music" in reel_script.response_schema(3)["required"]
    assert (
        "no copyright background music"
        in reel_script.build_messages("t", "facebook", "hi", 30)[0]["content"]
    )


def test_suggest_music_for_an_existing_script(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(body=json)
        return _reply(
            {
                "mood": "hopeful",
                "genre": "acoustic",
                "tempo": "medium, 95 BPM",
                "instruments": "guitar, light claps",
                "search": "hopeful acoustic guitar",
            }
        )

    monkeypatch.setattr(reel_script.httpx, "post", fake_post)
    script = {
        "title": "Walk",
        "scenes": [{"narration": "Walk after meals."}, {"narration": "Try it."}],
    }
    music = reel_script.suggest_music(script, "instagram")
    assert music["genre"] == "acoustic" and music["search"] == "hopeful acoustic guitar"
    assert sent["body"]["format"] == reel_script.music_schema()
    assert sent["body"]["keep_alive"] == 0
    prompt = sent["body"]["messages"][1]["content"]
    assert "Walk after meals. Try it." in prompt and "Instagram Reel" in prompt

    monkeypatch.setattr(
        reel_script.httpx, "post", lambda *a, **k: _reply({"mood": "", "genre": ""})
    )
    with pytest.raises(ScriptError, match="didn't suggest"):
        reel_script.suggest_music(script, "instagram")


def test_generate_script_explains_ollama_problems(monkeypatch):
    def refused(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(reel_script.httpx, "post", refused)
    with pytest.raises(ScriptError, match="Ollama isn't running"):
        reel_script.generate_script("topic", "facebook", "en", 35)

    monkeypatch.setattr(
        reel_script.httpx, "post", lambda *a, **k: httpx.Response(404, text="model not found")
    )
    with pytest.raises(ScriptError, match="ollama pull gemma3:4b"):
        reel_script.generate_script("topic", "facebook", "en", 35)

    garbage = httpx.Response(200, json={"message": {"content": "not json"}})
    monkeypatch.setattr(reel_script.httpx, "post", lambda *a, **k: garbage)
    with pytest.raises(ScriptError, match="unreadable"):
        reel_script.generate_script("topic", "facebook", "en", 35)


def test_picture_prompts_in_hindi_are_put_into_english(monkeypatch):
    replies = [
        {"scenes": [{"narration": "मोती खो गया।", "image_prompt": "बारिश में एक भूरा कुत्ता"},
                    {"narration": "घर मिला।", "image_prompt": "A brown dog at a red door"}],
         "call_to_action": ""},
        {"prompts": ["A small brown dog in the rain"]},
    ]  # fmt: skip
    asked = []

    def fake_chat(messages, schema, **options):
        if {"scene_1", "topic", "lines"} & set(schema["properties"]):
            return {}  # the length and Hindi fixes: nothing usable, all is kept
        asked.append(messages[-1]["content"])
        return replies[len(asked) - 1]

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    s = reel_script.generate_script("dog", "instagram", "hi", 15)
    assert [sc["image_prompt"] for sc in s["scenes"]] == [
        "A small brown dog in the rain", "A brown dog at a red door"
    ]  # fmt: skip
    # Only the Hindi one is sent to be translated.
    assert "1. बारिश में एक भूरा कुत्ता" in asked[1] and "red door" not in asked[1]


def test_a_failed_translation_keeps_the_picture_prompts(monkeypatch):
    scenes = [{"narration": "a", "image_prompt": "बारिश"}]

    def broken(messages, schema, **options):
        raise ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "_chat", broken)
    reel_script.english_picture_texts({"scenes": scenes})
    assert scenes[0]["image_prompt"] == "बारिश"
    # Still not English, or the wrong count: kept too.
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"prompts": ["बारिश में"]})
    reel_script.english_picture_texts({"scenes": scenes})
    assert scenes[0]["image_prompt"] == "बारिश"


def test_a_stuck_model_gets_one_more_try(monkeypatch):
    good = {"scenes": [{"narration": " ".join(["word"] * 12), "image_prompt": "A dog"}
                       for _ in range(3)]}  # fmt: skip
    stuck = [
        httpx.Response(200, json={"message": {"content": '{"title": "T", "scenes": [{"narr'}}),
        httpx.Response(500, json={"error": "prediction aborted, token repeat limit reached"}),
    ]
    for first in stuck:
        calls = []

        def fake_post(url, json, timeout, first=first, calls=calls):
            calls.append(json)
            return first if len(calls) == 1 else _reply(good)

        monkeypatch.setattr(reel_script.httpx, "post", fake_post)
        s = reel_script.generate_script("dog", "instagram", "en", 15)  # 3 scenes, ~15 s
        assert len(calls) == 2 and len(s["scenes"]) == 3

    # Stuck twice: the error goes to the UI ("Try again").
    monkeypatch.setattr(reel_script.httpx, "post", lambda *a, **k: stuck[1])
    with pytest.raises(ScriptError, match="repeat limit"):
        reel_script.generate_script("dog", "instagram", "en", 15)


def test_no_second_try_when_ollama_is_not_there(monkeypatch):
    calls = []

    def refused(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(reel_script.httpx, "post", refused)
    with pytest.raises(ScriptError, match="Ollama isn't running"):
        reel_script.generate_script("dog", "instagram", "en", 15)
    assert len(calls) == 1


def test_json_bits_leaked_into_a_text_are_cleaned_up():
    leaked = "चैनल को फॉलो करो!”, “और प्रेरणा के लिए नए वीडियो देखना मत भूलना।” ,"
    assert reel_script._clean(leaked) == ("चैनल को फॉलो करो! और प्रेरणा के लिए नए वीडियो देखना मत भूलना।")
    # Real quotes in a line stay, and so does a list of quoted words.
    assert reel_script._clean('Say "hello", "thanks" and "sorry".') == (
        'Say "hello", "thanks" and "sorry".'
    )
    assert reel_script._clean("करण कहता है, ‘राहुल, रुको!’") == "करण कहता है, ‘राहुल, रुको!’"
    assert reel_script._clean("He said “run” and ran.") == "He said “run” and ran."


def test_the_model_settles_the_setting_and_looks_before_the_scenes():
    schema = reel_script.response_schema(3)
    props = list(schema["properties"])
    assert props.index("setting") < props.index("scenes")
    assert props.index("characters") < props.index("scenes")
    scene = schema["properties"]["scenes"]["items"]
    assert scene["properties"]["characters"]["maxItems"] == 3
    assert {"characters", "crowd"} <= set(scene["required"])
    system = reel_script.build_messages("Ramayana", "instagram", "hi", 35)[0]["content"]
    assert "never modern things such as libraries" in system  # the time of the story
    assert "call the characters by their name only" in system
    assert "at most 3 (2 or fewer come out best)" in system
    assert "a name in English letters" in system


def test_characters_and_who_is_in_each_picture_are_cleaned_up():
    raw = {
        "setting": " Ancient India: forests, hermitages. ",
        "characters": [
            {"name": "Ram", "look": "a young prince in saffron robes, carrying a bow"},
            {"name": "Ram", "look": "a second Ram: dropped"},
            {"name": "Sita", "look": ""},  # no look: dropped
            {"name": "Lakshman", "look": "Ram's younger brother in white"},
        ],
        "scenes": [
            {"narration": "Ram enters the forest.", "image_prompt": "Ram walks",
             "characters": ["ram", "Sita", "Ravan", "Lakshman"], "crowd": False},
            {"narration": "Sages greet them.", "image_prompt": "a hermitage",
             "characters": [], "crowd": True},
        ],
    }  # fmt: skip
    s = normalize_script(raw, topic="Ramayana", language="en", scenes=2)
    assert s["setting"] == "Ancient India: forests, hermitages."
    assert [c["name"] for c in s["characters"]] == ["Ram", "Lakshman"]
    # Known names only (any case), at most 2.
    assert s["scenes"][0]["characters"] == ["Ram", "Lakshman"]
    assert (s["scenes"][1]["characters"], s["scenes"][1]["crowd"]) == ([], True)


def test_looks_and_setting_are_put_into_english_too(monkeypatch):
    script = {
        "setting": "प्राचीन भारत का जंगल",
        "characters": [{"name": "Ram", "look": "भगवा वस्त्र में युवा राजकुमार"}],
        "scenes": [{"narration": "n", "image_prompt": "Ram in the forest"}],
    }
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append(messages[-1]["content"])
        return {"prompts": ["a young prince in saffron robes", "an ancient Indian forest"]}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    reel_script.english_picture_texts(script)
    assert "Ram in the forest" not in asked[0]  # already English: not sent
    assert script["characters"][0]["look"] == "a young prince in saffron robes"
    assert script["setting"] == "an ancient Indian forest"


def test_hindi_names_and_people_the_prompt_draws_are_matched():
    # Seen with gemma3: names in Devanagari while the picture prompts say "Ram",
    # and a third person drawn but left out of the scene's list (max 2 then).
    raw = {
        "characters": [
            {"name": "राम", "look": "a young prince in saffron robes"},
            {"name": "सीता", "look": "a young woman in a white sari"},
            {"name": "लक्ष्मण", "look": "a young man in grey"},
        ],
        "scenes": [
            {"narration": "a", "image_prompt": "Ram, Sita and Lakshman walk through the jungle",
             "characters": ["राम", "लक्ष्मण"], "crowd": False},
            {"narration": "b", "image_prompt": "A river at dawn", "characters": ["Raam"],
             "crowd": False},
        ],
    }  # fmt: skip
    s = normalize_script(raw, topic="Ramayana", language="hi", scenes=2)
    assert [c["name"] for c in s["characters"]] == ["Ram", "Sita", "Lakshman"]  # as the prompts say
    assert s["scenes"][0]["characters"] == ["Ram", "Sita", "Lakshman"]  # Sita: named in the prompt
    assert s["scenes"][1]["characters"] == ["Ram"]  # "Raam" is Ram


def test_the_setting_never_brings_the_characters_into_every_picture():
    # Seen: "Ram, Sita, and Lakshman are dressed in..." in the setting drew Ram
    # into a picture of Sita and Lakshman, three times running.
    raw = {
        "setting": "An ancient forest by a river. Ram, Sita, and Lakshman are dressed in cotton "
        "- Ram in ochre. A mud hut stands in a clearing.",
        "characters": [{"name": "Ram", "look": "ochre robes"}, {"name": "Sita", "look": "white"}],
        "scenes": [{"narration": "a", "image_prompt": "Sita by the hut", "characters": ["Sita"],
                    "crowd": False}],
    }  # fmt: skip
    s = normalize_script(raw, topic="Ramayana", language="en", scenes=1)
    assert s["setting"] == "An ancient forest by a river. A mud hut stands in a clearing."
    system = reel_script.build_messages("Ramayana", "instagram", "hi", 35)[0]["content"]
    assert "Never the characters themselves" in system


def test_no_extra_figures_and_clothes_of_the_time():
    # Seen: "praying towards a Vishnu figure" drew a fourth person three times
    # running, and Lakshman came out in Roman-looking armour.
    system = reel_script.build_messages("Ramayana", "instagram", "hi", 35)[0]["content"]
    assert "never adds anyone else: no other person, god, statue, idol" in system
    assert "show a lamp, flowers, a symbol or a temple instead" in system
    assert "never western, Roman or medieval European armour" in system


def test_word_counts_after_a_narration_are_dropped():
    # Seen: the model copied the word count it was asked for into the line.
    seen = (
        "देखो, राजकुमार राम, अपने बगीचे में खेल रहा है, जीवन कितना सरल और खुशहाल है।" "”, 20 words. 17 words."
    )
    assert reel_script._spoken(seen) == (
        "देखो, राजकुमार राम, अपने बगीचे में खेल रहा है, जीवन कितना सरल और खुशहाल है।"
    )
    assert reel_script._spoken("Ram goes to the forest. (18 words)") == "Ram goes to the forest."
    assert reel_script._spoken("राम वन गए। - 12 शब्द") == "राम वन गए।"
    # In the middle of a line it is meant.
    assert reel_script._spoken("Learn 5 words a day.") == "Learn 5 words a day."
    raw = {"scenes": [{"narration": "Ram smiles.”, 20 words.", "image_prompt": "p"}]}
    assert normalize_script(raw, topic="t", language="en", scenes=1)["scenes"][0]["narration"] == (
        "Ram smiles."
    )
    system = reel_script.build_messages("x", "instagram", "hi", 35)[0]["content"]
    assert "never word counts, labels or notes" in system


def test_a_story_flows_and_is_paced_like_one():
    story = reel_script.build_messages("x", "instagram", "hi", 40, "story")[0]["content"]
    assert "one flowing story told aloud, not separate captions" in story
    assert "each opening the story in a different way" in story  # its hooks
    tips = reel_script.build_messages("x", "instagram", "hi", 40, "tips")[0]["content"]
    assert "flowing story" not in tips and "(question, bold claim, surprising fact)" in tips
    # Read slower, with longer breaths: fewer words for the same length.
    assert reel_script.words_per_scene(40, "hi", 6, story=True) < reel_script.words_per_scene(
        40, "hi", 6
    )
    lines = ["शब्द " * 20] * 3
    longer = reel_script.spoken_seconds(lines, "hi", story=True)
    assert longer == pytest.approx(60 / (2.5 * reel_render.STORY_PACE) + 2 * 0.8 + 0.8)
    assert reel_script.spoken_seconds(lines, "hi") == pytest.approx(60 / 2.5 + 2 * 0.25 + 0.8)


def test_a_hinglish_idea_is_given_to_the_model_in_devanagari(monkeypatch):
    # Given "Ravan ka bachpan, Lanka ka raja", the model wrote "Lanka का राजा".
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append(options.get("model"))
        return {"topic": "रावण", "description": "रावण का बचपन, लंका का राजा"}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    idea = reel_script.devanagari_idea("Ravan", "Ravan ka bachpan, Lanka ka raja", model="m")
    assert idea == ("रावण", "रावण का बचपन, लंका का राजा") and asked == ["m"]
    # Already Devanagari: no call.
    assert reel_script.devanagari_idea("रावण", "") == ("रावण", "") and len(asked) == 1

    def broken(*a, **k):
        raise ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "_chat", broken)
    assert reel_script.devanagari_idea("Ravan", "x") == ("Ravan", "x")  # as it was


def test_hindi_lines_with_english_words_are_written_again(monkeypatch):
    script = {
        "title": "रावण की कहानी",
        "hooks": ["रावण कौन था?", "Lanka का राजा कौन बना?"],
        "caption": "Reels के लिए फॉलो करें",
        "scenes": [
            {"narration": "रावण वेद पढ़ता था।", "image_prompt": "Ravan reads"},
            {"narration": "वह Lanka का राजा बना।", "image_prompt": "Ravan in Lanka"},
        ],
        "hashtags": ["ramayana"],
    }
    asked = []

    def fake_chat(messages, schema, **options):
        asked.append(messages[-1]["content"])
        return {"lines": ["रील्स के लिए फॉलो करें", "लंका का राजा कौन बना?", "वह Lanka का राजा।"]}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    reel_script.hindi_only(script)
    assert "1. Reels के लिए फॉलो करें" in asked[0] and "रावण वेद" not in asked[0]  # only those
    assert script["caption"] == "रील्स के लिए फॉलो करें"
    assert script["hooks"][1] == "लंका का राजा कौन बना?"
    assert script["scenes"][1]["narration"] == "वह Lanka का राजा बना।"  # still English: kept
    assert script["scenes"][1]["image_prompt"] == "Ravan in Lanka"  # pictures stay English
    system = reel_script.build_messages("x", "instagram", "hi", 35)[0]["content"]
    assert "names and places too (लंका, राम)" in system and "may stay in English" not in system


def test_the_chosen_writer_writes_every_part(monkeypatch):
    models, steps = [], []

    def fake_chat(messages, schema, **options):
        models.append(options.get("model"))
        if "scenes" in schema["properties"]:
            return {"scenes": [{"narration": "Lanka " + " ".join(["शब्द"] * 30), "image_prompt": "ग"}
                               for _ in range(3)]}  # fmt: skip
        return {}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    reel_script.generate_script("Ravan", "instagram", "hi", 15, model="gemma3:12b",
                                on_step=steps.append)  # fmt: skip
    assert models and set(models) == {"gemma3:12b"}  # idea, script, length, Hindi, pictures
    assert steps == ["Checking its length", "Putting every word in Hindi",
                     "Checking the pictures' words"]  # fmt: skip


# --- One scene's spoken text rephrased (a word is replaced on the page, without the model) ---
def test_rephrase_sends_only_this_scene_never_grows_and_stays_in_hindi(monkeypatch):
    replies = [
        {"narration": "दूसरा सीन यहाँ अब बहुत लंबा हो गया है।"},  # more words: asked again
        {"narration": "दूसरा सीन ये है।"},
    ]
    seen = []

    def fake_chat(messages, schema, **options):
        seen.append([m["content"] for m in messages])
        return replies[len(seen) - 1]

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    text = reel_script.rephrase_narration("दूसरा सीन यहाँ है।", "simple banao", "hi")
    assert text == "दूसरा सीन ये है।"
    system, user = seen[0]
    assert "You rephrase one line" in system
    assert "Write in Hindi, written in Devanagari script only" in system
    assert "3 to 4 words and at most" in system  # never longer, not much shorter
    # Only this scene and the hint: no title, no other scene.
    assert user.splitlines()[:2] == [
        "The line: दूसरा सीन यहाँ है।",
        "How to rephrase it: simple banao",
    ]
    assert "too long, it needs 3 to 4 words" in seen[1][-1]  # told why, then asked again

    seen.clear()
    replies[:] = [{"narration": "दूसरा सीन ये है।"}]
    reel_script.rephrase_narration("दूसरा सीन यहाँ है।", "", "hi")
    assert not any(line.startswith("How to") for line in seen[0][1].splitlines())  # no hint


def test_rephrase_may_spell_a_little_longer_but_never_has_more_words(monkeypatch):
    narration = "Have some water now"  # 4 words, 19 characters: at most 21 back
    reply = {}
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: reply)
    reply["narration"] = "Have some waters now!"
    assert reel_script.rephrase_narration(narration, "", "en") == "Have some waters now!"
    reply["narration"] = "Have some waters now!!"
    with pytest.raises(ScriptError, match="at most 21 characters"):
        reel_script.rephrase_narration(narration, "", "en")


def test_rephrase_gives_up_after_asking_again(monkeypatch):
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"narration": "Raju ka सीन"})
    with pytest.raises(ScriptError, match="Roman letters"):
        reel_script.rephrase_narration("राजू का सीन", "", "hi")

    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"narration": "Drink a big glass."})
    with pytest.raises(ScriptError, match="needs 2 to 3 words"):
        reel_script.rephrase_narration("Drink water first.", "", "en")


def test_rephrase_that_changes_nothing_is_asked_again(monkeypatch):
    seen = []
    replies = iter([{"narration": "Drink  water first."}, {"narration": "First, drink water."}])

    def fake_chat(messages, schema, **options):
        seen.append(messages[-1]["content"])
        return next(replies)

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    assert reel_script.rephrase_narration("Drink water first.", "", "en") == "First, drink water."
    assert seen[1].startswith("That is the line as it was")


def test_rephrase_tries_again_after_an_unreadable_reply(monkeypatch):
    replies = iter([reel_script.BadReplyError("stuck"), {"narration": "First, drink water."}])

    def fake_chat(messages, schema, **options):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    assert reel_script.rephrase_narration("Drink water first.", "", "en") == "First, drink water."


def test_rephrase_is_not_much_shorter_either(monkeypatch):
    seen = []
    replies = iter([{"narration": "Drink water."}, {"narration": "First drink a glass of water."}])

    def fake_chat(messages, schema, **options):
        seen.append(messages[-1]["content"])
        return next(replies)

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    narration = "Drink a glass of water first."  # 6 words: 5 or 6 back
    text = reel_script.rephrase_narration(narration, "", "en")
    assert text == "First drink a glass of water."
    assert seen[1] == "That has 2 words: too short, it needs 5 to 6 words. Write it again."
