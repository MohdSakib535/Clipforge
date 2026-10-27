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
    assert reel_script.scene_count(90) == 8  # never more than 8


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
        sent.update(url=url, body=json)
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
    assert len(s["scenes"]) == 5 and s["title"] == "T"


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
