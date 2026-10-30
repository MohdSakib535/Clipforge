"""Reel scripts: the prompt sent to Ollama and the clean-up of its reply."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import reel_script
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
    assert reel_script.words_per_scene(40, "hi", 6) == usual


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
    assert "described in full, the same way, in every image_prompt" in story


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
    assert "never words from any other language" in english and "Roman" not in english
    assert "never repeat a line or an idea" in hindi and "one point of view" in hindi
    assert "a narration never says anyone's age, hair, build, face or clothes" in hindi
    assert "never a summary" in hindi  # stories show moments


def test_spoken_length_counts_words_pace_and_pauses():
    texts = [" ".join(["शब्द"] * 15), " ".join(["शब्द"] * 10)]
    # 25 words at 2.5 a second, 0.25 s after each scene and 0.55 s more at the end.
    assert reel_script.spoken_seconds(texts, "hi") == pytest.approx(10 + 0.5 + 0.55)
    assert reel_script.spoken_seconds(texts, "hi", "hm_calm") == pytest.approx(25 / 2.25 + 1.05)


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
        return {"narrations": [" ".join(["छोटा"] * 22) for _ in range(4)]}

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    scenes = _scenes(40, 40, 40, 40)  # ~66 s for a 40 s reel
    reel_script.fit_length(scenes, "hi", 40)
    assert "they must take 40 seconds. Rewrite them shorter" in asked[0]
    assert [len(sc["narration"].split()) for sc in scenes] == [22] * 4
    assert all(sc["image_prompt"] == "p" for sc in scenes)  # the pictures stay


def test_a_rewrite_that_does_not_help_is_dropped(monkeypatch):
    scenes = _scenes(40, 40, 40, 40)
    longer = {"narrations": [" ".join(["x"] * 60) for _ in range(4)]}
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: longer)
    reel_script.fit_length(scenes, "hi", 40)
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"narrations": ["x"] * 3})
    reel_script.fit_length(scenes, "hi", 40)  # the wrong count

    def broken(*a, **k):
        raise ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "_chat", broken)
    reel_script.fit_length(scenes, "hi", 40)
    assert [len(sc["narration"].split()) for sc in scenes] == [40] * 4


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
        if "narrations" in schema["properties"]:
            return {"narrations": [" ".join(["छोटा"] * 8) for _ in range(3)]}
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
        sent.setdefault("body", json)  # the first call: the script
        scenes = [{"narration": f"Line {i}.", "image_prompt": f"p{i}"} for i in range(15)]
        return _reply({"title": "T", "scenes": scenes})

    monkeypatch.setattr(reel_script.httpx, "post", fake_post)
    s = reel_script.generate_script("topic", "instagram", "hi", 300)
    assert sent["body"]["options"]["num_predict"] == 4500
    assert sent["body"]["options"]["num_ctx"] == 6548
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
    assert list(s) == ["title", "hooks", "scenes", "caption", "hashtags", "music"]
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
        if "narrations" in schema["properties"]:
            return {}  # the length fix: nothing usable, the script is kept
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
    reel_script.english_picture_prompts(scenes)
    assert scenes[0]["image_prompt"] == "बारिश"
    # Still not English, or the wrong count: kept too.
    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"prompts": ["बारिश में"]})
    reel_script.english_picture_prompts(scenes)
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
