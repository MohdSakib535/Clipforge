"""The "Create a reel" API: options, script editing, generating, files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import get_settings
from app.models import AudioTrack, Reel
from app.services import reel_render, tts

SCRIPT = {
    "title": "Morning habits",
    "hooks": ["Tired every morning?", "One habit changed my life"],
    "scenes": [
        {"narration": "Tired every morning?", "image_prompt": "a tired man in bed"},
        {"narration": "Drink water first.", "image_prompt": "a glass of water"},
        {"narration": "Follow for more.", "image_prompt": "a smiling woman"},
    ],
    "caption": "Save this!",
    "hashtags": ["morning", "habits"],
}


def _create(client, **overrides) -> dict:
    body = {"topic": "3 morning habits", "platform": "instagram", "language": "hi"}
    resp = client.post("/reels", json={**body, **overrides})
    assert resp.status_code == 202, resp.text
    return resp.json()


def _with_script(db_session, reel_id: str, status: str = "script_ready") -> Reel:
    reel = db_session.get(Reel, reel_id)
    reel.script = SCRIPT
    reel.status = status
    db_session.commit()
    return reel


def test_options_list_platforms_languages_and_voices(client):
    body = client.get("/reels/options").json()
    assert [p["key"] for p in body["platforms"]] == ["instagram", "facebook"]
    hindi = next(lang for lang in body["languages"] if lang["key"] == "hi")
    assert hindi["label"] == "Hindi" and hindi["voices"][0]["key"] == "hf_alpha"
    calm = next(v for v in hindi["voices"] if v["key"] == "hm_calm")
    assert calm["label"] == "Male 5 · calm"
    assert calm["words_per_s"] == tts.words_per_s("hi", "hm_calm")  # its own, measured pace
    assert body["story_pace"] == reel_render.STORY_PACE
    assert body["duration"] == {"min": 15, "max": 300, "default": 35}
    assert body["max_scenes"] == 20
    assert [s["key"] for s in body["styles"]] == ["cinematic", "realistic", "anime", "3d"]
    assert [t["key"] for t in body["script_types"]] == ["story", "tips", "facts"]
    assert body["description_max"] == 1500
    timing = body["timing"]  # for the time estimates in the style menus
    assert (timing["picture_s"], timing["load_s"]) == (58, 5)
    assert {"script_s", "script_s_per_s", "render_s_per_s"} <= timing.keys()


def test_create_starts_writing_the_script(client):
    reel = _create(client)
    assert reel["status"] == "scripting"
    assert reel["voice"] == "hf_alpha"  # the language's first voice
    assert reel["duration_s"] == 35 and reel["script"] is None
    assert reel["style"] == "cinematic"  # the default
    assert (reel["script_type"], reel["description"]) == ("story", "")  # the defaults
    assert client.enqueued["reel_script"] == [reel["id"]]
    assert [r["id"] for r in client.get("/reels").json()] == [reel["id"]]
    assert _create(client, style="anime")["style"] == "anime"
    idea = _create(client, script_type="tips", description="  Funny tone, 3 quick tips.  ")
    assert (idea["script_type"], idea["description"]) == ("tips", "Funny tone, 3 quick tips.")


def test_create_validates_choices(client):
    def code(**body):
        base = {"topic": "a topic", "platform": "instagram", "language": "en"}
        return client.post("/reels", json={**base, **body}).status_code

    assert code(platform="tiktok") == 400
    assert code(style="pixel-art") == 400
    assert code(script_type="poem") == 400
    assert code(description="x" * 1501) == 422
    assert code(language="xx") == 400
    assert code(voice="hf_alpha") == 400  # a Hindi voice for English
    assert code(topic="hi") == 422
    assert code(duration_s=5) == 422
    assert code(duration_s=301) == 422
    assert code(duration_s=300) == 202  # up to 5 minutes
    assert code(voice="am_michael", duration_s=40) == 202


def test_script_cannot_be_edited_while_it_is_written(client):
    reel = _create(client)
    resp = client.put(f"/reels/{reel['id']}/script", json=SCRIPT)
    assert resp.status_code == 409


def test_save_script_cleans_hashtags_and_marks_video_out_of_date(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")

    same = client.put(f"/reels/{reel['id']}/script", json=SCRIPT).json()
    assert same["status"] == "ready"  # nothing changed: the video still matches

    edited = {**SCRIPT, "hashtags": ["#Morning Routine", "#morningroutine"], "title": " New "}
    body = client.put(f"/reels/{reel['id']}/script", json=edited).json()
    assert body["status"] == "script_ready"
    assert body["script"]["hashtags"] == ["morningroutine"]
    assert body["script"]["title"] == "New"

    bad = {**SCRIPT, "scenes": []}
    assert client.put(f"/reels/{reel['id']}/script", json=bad).status_code == 422


def test_generate_needs_a_script_and_valid_music(client, db_session):
    reel = _create(client)
    db_session.get(Reel, reel["id"]).status = "failed"
    db_session.commit()
    assert client.post(f"/reels/{reel['id']}/generate", json={}).status_code == 409

    _with_script(db_session, reel["id"])
    resp = client.post(f"/reels/{reel['id']}/generate", json={"music_id": "nope"})
    assert resp.status_code == 404
    resp = client.post(f"/reels/{reel['id']}/generate", json={"voice": "af_heart"})
    assert resp.status_code == 400  # English voice for a Hindi reel


def test_generate_queues_the_render_with_voice_and_music(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"])
    track = AudioTrack(user_id=db_session.get(Reel, reel["id"]).user_id, filename="song.mp3",
                       path="/tmp/song.m4a", status="ready")  # fmt: skip
    db_session.add(track)
    db_session.commit()

    resp = client.post(
        f"/reels/{reel['id']}/generate",
        json={"voice": "hm_omega", "music_id": track.id, "music_volume": 30},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "generating" and body["progress"] == 0
    assert (body["voice"], body["music_id"], body["music_volume"]) == ("hm_omega", track.id, 30)
    assert client.enqueued["reel_render"] == [reel["id"]]
    # Busy now: no edits, no second render.
    assert client.post(f"/reels/{reel['id']}/generate", json={}).status_code == 409
    assert client.post(f"/reels/{reel['id']}/rewrite").status_code == 409


def test_new_picture_drops_that_scene_picture_only(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")
    folder = reel_render.reel_dir(reel["id"])
    folder.mkdir(parents=True, exist_ok=True)
    pictures = reel_render.scene_pictures(folder, SCRIPT, get_settings(), "cinematic")
    for p in pictures:
        p.write_bytes(b"png")
    assert client.get(f"/reels/{reel['id']}").json()["scene_pictures"] == [True, True, True]
    assert client.get(f"/reels/{reel['id']}/scenes/1/picture").status_code == 200

    resp = client.post(f"/reels/{reel['id']}/scenes/1/new-picture")
    assert resp.status_code == 202
    assert resp.json()["scene_pictures"] == [True, False, True]
    assert resp.json()["status"] == "generating"
    assert client.enqueued["reel_render"] == [reel["id"]]

    db_session.get(Reel, reel["id"]).status = "ready"
    db_session.commit()
    assert client.post(f"/reels/{reel['id']}/scenes/9/new-picture").status_code == 404


def test_another_style_needs_every_picture_again(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")
    folder = reel_render.reel_dir(reel["id"])
    folder.mkdir(parents=True, exist_ok=True)
    for p in reel_render.scene_pictures(folder, SCRIPT, get_settings(), "cinematic"):
        p.write_bytes(b"png")
    assert client.get(f"/reels/{reel['id']}").json()["scene_pictures"] == [True, True, True]

    resp = client.post(f"/reels/{reel['id']}/generate", json={"style": "3d"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["style"] == "3d"
    assert resp.json()["scene_pictures"] == [False, False, False]

    db_session.get(Reel, reel["id"]).status = "ready"
    db_session.commit()
    resp = client.post(f"/reels/{reel['id']}/generate", json={"style": "oil"})
    assert resp.status_code == 400
    # Leaving the style out keeps it.
    assert client.post(f"/reels/{reel['id']}/generate", json={}).json()["style"] == "3d"


def test_video_files_and_delete(client, db_session):
    reel = _create(client)
    rid = reel["id"]
    assert client.get(f"/reels/{rid}/video").status_code == 404
    assert client.get(f"/reels/{rid}/thumbnail").status_code == 404

    folder = reel_render.reel_dir(rid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reel.mp4").write_bytes(b"mp4")
    (folder / "thumb.jpg").write_bytes(b"jpg")
    r = _with_script(db_session, rid, status="ready")
    r.file_path = str(folder / "reel.mp4")
    r.thumbnail_path = str(folder / "thumb.jpg")
    db_session.commit()

    body = client.get(f"/reels/{rid}").json()
    assert body["has_video"] is True and body["video_version"]
    assert client.get(f"/reels/{rid}/video").status_code == 200
    download = client.get(f"/reels/{rid}/video?download=1")
    assert 'filename="Morning_habits.mp4"' in download.headers["content-disposition"]
    assert client.get(f"/reels/{rid}/thumbnail").status_code == 200

    (folder / "reel.mp4").write_bytes(b"")  # emptied by a crash
    assert client.get(f"/reels/{rid}").json()["has_video"] is False
    assert client.get(f"/reels/{rid}/video").status_code == 404

    assert client.delete(f"/reels/{rid}").status_code == 204
    assert client.get(f"/reels/{rid}").status_code == 404
    assert not folder.exists()


def test_reel_cut_off_by_a_restart_can_be_started_again(client, db_session):
    reel = _create(client)
    r = _with_script(db_session, reel["id"], status="generating")
    assert client.get(f"/reels/{reel['id']}").json()["stalled"] is False
    assert client.post(f"/reels/{reel['id']}/generate", json={}).status_code == 409

    r.updated_at = datetime.now(UTC) - timedelta(minutes=30)  # no progress for 30 min
    db_session.commit()
    assert client.get(f"/reels/{reel['id']}").json()["stalled"] is True
    resp = client.post(f"/reels/{reel['id']}/generate", json={})
    assert resp.status_code == 202
    assert resp.json()["stalled"] is False and resp.json()["status"] == "generating"


def test_deleting_music_used_by_a_reel(client, db_session):
    reel = _create(client)
    r = _with_script(db_session, reel["id"], status="generating")
    track = AudioTrack(user_id=r.user_id, filename="song.m4a", path="/tmp/x.m4a", status="ready")
    db_session.add(track)
    db_session.commit()
    r.music_id = track.id
    db_session.commit()

    # Not while a reel is being made with it.
    resp = client.delete(f"/audio/{track.id}")
    assert resp.status_code == 409 and "being made" in resp.json()["error"]["message"]

    # Afterwards: the reel keeps its video, its next render has no music.
    r.status = "ready"
    db_session.commit()
    assert client.delete(f"/audio/{track.id}").status_code == 204
    assert client.get(f"/reels/{reel['id']}").json()["music_id"] is None


MUSIC = {
    "mood": "calm", "genre": "lo-fi", "tempo": "slow, 80 BPM", "instruments": "piano",
    "search": "calm lo-fi piano no copyright background music",
}  # fmt: skip


def test_music_suggestion_is_saved_in_the_script(client, db_session, monkeypatch):
    from app.services import reel_script

    reel = _create(client)
    assert client.post(f"/reels/{reel['id']}/music-suggestion").status_code == 409  # busy

    _with_script(db_session, reel["id"], status="ready")
    monkeypatch.setattr(reel_script, "suggest_music", lambda script, platform, model=None: MUSIC)
    body = client.post(f"/reels/{reel['id']}/music-suggestion").json()
    assert body["script"]["music"] == MUSIC
    assert body["script"]["scenes"] == SCRIPT["scenes"]  # the rest is untouched
    assert body["status"] == "ready"  # the video still matches the script

    # Saving the edited script keeps the suggestion.
    saved = client.put(f"/reels/{reel['id']}/script", json=body["script"]).json()
    assert saved["script"]["music"] == MUSIC and saved["status"] == "ready"

    def down(script, platform, model=None):
        raise reel_script.ScriptError("Ollama isn't running on this Mac.")

    monkeypatch.setattr(reel_script, "suggest_music", down)
    resp = client.post(f"/reels/{reel['id']}/music-suggestion")
    assert resp.status_code == 502 and "Ollama" in resp.json()["error"]["message"]


def test_rephrase_one_scene_without_saving(client, db_session, monkeypatch):
    from app.services import reel_script

    reel = _create(client)
    url = f"/reels/{reel['id']}/scenes/1/rephrase"
    body = {"narration": "Drink water first.", "hint": "friendlier"}
    assert client.post(url, json=body).status_code == 409  # still writing the script

    _with_script(db_session, reel["id"], status="ready")
    calls = []

    def fake(narration, hint, language, model=None):
        calls.append((narration, hint, language, model))
        return "First, drink some water."

    monkeypatch.setattr(reel_script, "rephrase_narration", fake)
    resp = client.post(url, json=body)
    assert resp.json() == {"narration": "First, drink some water.", "words": 4}
    db_session.expire_all()
    stored = db_session.get(Reel, reel["id"])
    assert stored.script == SCRIPT and stored.status == "ready"  # not saved: the page keeps it

    # The writer picked on the page, else the reel's; the hint is optional.
    assert client.post(url, json={"narration": "Hi.", "model": "gemma3:12b"}).status_code == 200
    assert calls == [
        ("Drink water first.", "friendlier", "hi", None),
        ("Hi.", "", "hi", "gemma3:12b"),
    ]
    resp = client.post(url, json={**body, "model": "no-such-model"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "bad_script_model"
    assert client.post(url, json={**body, "narration": "  "}).status_code == 422
    assert client.post(f"/reels/{reel['id']}/scenes/99/rephrase", json=body).status_code == 404

    def down(*args, **kwargs):
        raise reel_script.ScriptError("Ollama isn't running on this Mac.")

    monkeypatch.setattr(reel_script, "rephrase_narration", down)
    resp = client.post(url, json=body)
    assert resp.status_code == 502 and "Ollama" in resp.json()["error"]["message"]


def test_saving_a_script_without_music_adds_no_empty_music(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")
    body = client.put(f"/reels/{reel['id']}/script", json=SCRIPT).json()
    assert "music" not in body["script"]
    assert body["status"] == "ready"  # unchanged script: still matches the video


def test_rewrite_asks_for_a_new_script(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"])
    body = client.post(f"/reels/{reel['id']}/rewrite").json()
    assert body["status"] == "scripting"
    assert client.enqueued["reel_script"] == [reel["id"], reel["id"]]
    assert (body["script_type"], body["description"]) == ("story", "")  # no body: kept


def test_rewrite_with_another_script_idea(client, db_session):
    reel = _create(client, description="Old idea")
    _with_script(db_session, reel["id"])
    rewrite = f"/reels/{reel['id']}/rewrite"
    assert client.post(rewrite, json={"script_type": "poem"}).status_code == 400

    body = client.post(rewrite, json={"script_type": "facts", "description": "New idea"}).json()
    assert (body["status"], body["script_type"], body["description"]) == (
        "scripting", "facts", "New idea"
    )  # fmt: skip
    _with_script(db_session, reel["id"])
    body = client.post(rewrite, json={"description": ""}).json()  # empty drops it
    assert (body["script_type"], body["description"]) == ("facts", "")
    # What the script was written for goes along, to put back if the new one fails.
    assert client.enqueued["reel_script_kw"][-1] == {
        "before": {"script_type": "facts", "duration_s": 35, "description": "New idea",
                   "script_model": None},
        "then_render": False,
    }  # fmt: skip
    _with_script(db_session, reel["id"])
    assert client.post(rewrite, json={"duration_s": 5}).status_code == 422
    assert client.post(rewrite, json={"duration_s": 60}).json()["duration_s"] == 60


def test_generate_with_another_script_type_or_length_writes_a_new_script_first(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")
    generate = f"/reels/{reel['id']}/generate"
    bad = {"rewrite": {"script_type": "poem"}}
    assert client.post(generate, json=bad).status_code == 400
    assert client.post(generate, json={"rewrite": {"duration_s": 900}}).status_code == 422

    resp = client.post(
        generate,
        json={"voice": "hm_omega", "style": "3d",
              "rewrite": {"script_type": "facts", "duration_s": 120}},
    )  # fmt: skip
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["script_type"], body["duration_s"]) == ("scripting", "facts", 120)
    assert (body["voice"], body["style"]) == ("hm_omega", "3d")  # the new script's pace
    assert client.enqueued["reel_script"][-1] == reel["id"]
    assert client.enqueued["reel_script_kw"][-1] == {
        "before": {"script_type": "story", "duration_s": 35, "description": None,
                   "script_model": None},
        "then_render": True,
    }  # fmt: skip
    assert client.enqueued["reel_render"] == []  # the script task starts it
    assert client.post(generate, json={}).status_code == 409  # busy


def test_listen_to_a_voice_makes_the_sample_once(client, tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    from app.services import tts

    made = []

    def fake_synthesize(text, language, voice):
        made.append((text, voice))
        return np.zeros(2400, np.float32), 24000

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    resp = client.get("/reels/voices/hi/hm_deep/sample")
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"
    assert client.get("/reels/voices/hi/hm_deep/sample").status_code == 200
    assert made == [(tts.LANGUAGES["hi"].sample, "hm_deep")]  # kept after the first time
    assert client.get("/reels/voices/hi/af_heart/sample").status_code == 400  # not Hindi
    assert client.get("/reels/voices/xx/hm_deep/sample").status_code == 400


def test_a_script_with_characters_is_saved_as_it_is(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"])
    script = {
        **SCRIPT,
        "setting": "Ancient India: forests and hermitages.",
        "characters": [{"name": "Ram", "look": "a young prince with a bow"}],
        "scenes": [{**sc, "characters": ["Ram"], "crowd": False} for sc in SCRIPT["scenes"]],
    }
    saved = client.put(f"/reels/{reel['id']}/script", json=script).json()["script"]
    assert saved["characters"] == [{"name": "Ram", "look": "a young prince with a bow"}]
    assert list(saved) == ["title", "hooks", "setting", "characters", "scenes", "caption",
                           "hashtags"]  # fmt: skip
    assert saved["scenes"][0] == {**SCRIPT["scenes"][0], "characters": ["Ram"], "crowd": False}
    too_many = {**script, "scenes": [{**SCRIPT["scenes"][0], "characters": ["A", "B", "C", "D"]}]}
    assert client.put(f"/reels/{reel['id']}/script", json=too_many).status_code == 422
    # A script from before characters gets none added.
    old = client.put(f"/reels/{reel['id']}/script", json=SCRIPT).json()["script"]
    assert "characters" not in old and "characters" not in old["scenes"][0]


def test_a_picture_that_may_look_wrong_says_so(client, db_session):
    reel = _create(client)
    _with_script(db_session, reel["id"], status="ready")
    folder = reel_render.reel_dir(reel["id"])
    folder.mkdir(parents=True, exist_ok=True)
    pictures = reel_render.scene_pictures(folder, SCRIPT, get_settings(), "cinematic")
    for p in pictures:
        p.write_bytes(b"png")
    reel_render.warning_path(pictures[1]).write_text("2 people or faces instead of 1")
    body = client.get(f"/reels/{reel['id']}").json()
    assert body["scene_warnings"] == [None, "2 people or faces instead of 1", None]
    client.post(f"/reels/{reel['id']}/scenes/1/new-picture")  # drawn again: the warning goes
    assert not reel_render.warning_path(pictures[1]).exists()


def test_the_image_client_reads_what_vision_saw(monkeypatch):
    import httpx

    from app.services import imagegen

    def reply(headers):
        return lambda *a, **k: httpx.Response(200, content=b"png", headers=headers)

    monkeypatch.setattr(imagegen.httpx, "post", reply({"X-Faces": "2", "X-Hands": "3",
                                                       "X-People": "2"}))  # fmt: skip
    pic = imagegen.generate("p", width=16, height=16, seed=1, steps=1)
    assert (pic.png, pic.counts) == (b"png", {"faces": 2, "hands": 3, "people": 2})
    monkeypatch.setattr(imagegen.httpx, "post", reply({}))  # an older generator
    assert imagegen.generate("p", width=16, height=16, seed=1, steps=1).counts is None


def test_pick_the_script_writer(client, db_session, monkeypatch):
    from app.api import reels as reels_api

    monkeypatch.setattr(reels_api, "_installed_models", lambda: {"gemma3:4b"})
    models = {m["key"]: m for m in client.get("/reels/options").json()["script_models"]}
    assert models["gemma3:4b"]["installed"] is True
    assert models["gemma3:12b"]["installed"] is False and models["gemma3:12b"]["slowness"] > 1
    assert client.get("/reels/options").json()["script_model_default"] == "gemma3:4b"

    reel = _create(client, script_model="gemma3:12b")
    assert reel["script_model"] == "gemma3:12b"
    assert db_session.get(Reel, reel["id"]).script_model == "gemma3:12b"
    plain = _create(client, script_model="gemma3:4b")  # the default: stored as None
    assert plain["script_model"] == "gemma3:4b"
    assert db_session.get(Reel, plain["id"]).script_model is None
    unknown = {
        "topic": "a topic",
        "platform": "instagram",
        "language": "hi",
        "script_model": "x:1b",
    }
    assert client.post("/reels", json=unknown).status_code == 400
    # "Write again" with another writer.
    db_session.get(Reel, reel["id"]).status = "script_ready"
    db_session.commit()
    body = client.post(f"/reels/{reel['id']}/rewrite", json={"script_model": "gemma3:4b"}).json()
    assert body["script_model"] == "gemma3:4b"
