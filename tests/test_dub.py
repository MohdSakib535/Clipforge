"""Translate & dub a reel into Hindi: sentences -> local LLM translation ->
Kokoro voice placed where each sentence was said, replacing the clip's audio."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Clip, ClipStatus, Job, JobStatus, Video, VideoStatus
from app.services import dub, reel_script, tts
from app.services.naming import clip_basename
from app.tasks import pipeline
from tests.synthetic_media import make_media

WORDS = [
    {"word": " Plant", "start": 11.0, "end": 11.4},
    {"word": " tomatoes", "start": 11.4, "end": 12.0},
    {"word": " today.", "start": 12.0, "end": 12.5},
    {"word": " Water", "start": 15.0, "end": 15.4},
    {"word": " daily.", "start": 15.4, "end": 16.0},
]


def test_words_become_one_line_per_sentence():
    assert dub.sentences(WORDS) == [
        {"start_s": 11.0, "end_s": 12.5, "text": "Plant tomatoes today."},
        {"start_s": 15.0, "end_s": 16.0, "text": "Water daily."},
    ]


def test_translation_is_asked_again_when_a_line_is_missing(monkeypatch):
    replies = [
        {"lines": ["सब एक में"]},
        {"lines": ["{बिज़नेस नाम} आज  टमाटर लगाओ।", "रोज़Daily पानी दो।"]},
    ]
    asked = []

    def fake_chat(messages, schema, *, temperature, num_predict):
        asked.append(messages[1]["content"])
        return replies.pop(0)

    monkeypatch.setattr(reel_script, "_chat", fake_chat)
    lines = dub.sentences(WORDS)
    assert dub.translate(lines, "hi") == ["आज टमाटर लगाओ।", "रोज़ Daily पानी दो।"]
    assert len(asked) == 2
    assert "Hindi, written in Devanagari" in asked[0]
    assert "1. (1.5 s) Plant tomatoes today." in asked[0]

    monkeypatch.setattr(reel_script, "_chat", lambda *a, **k: {"lines": ["one"]})
    with pytest.raises(dub.DubError, match="incomplete"):
        dub.translate(lines, "hi")


def _fake_voice(monkeypatch, sample_rate=100, s_per_char=0.1):
    """Kokoro stand-in: 0.1 s of sound per character, shorter when faster."""
    calls = []

    def synthesize(text, language, voice, speed=1.0):
        calls.append((text, speed))
        n = int(round(len(text) * s_per_char * sample_rate / speed))
        return np.full(n, 0.5, dtype=np.float32), sample_rate

    monkeypatch.setattr(tts, "synthesize", synthesize)
    return calls


def test_lines_are_placed_where_they_were_said(monkeypatch):
    calls = _fake_voice(monkeypatch)
    lines = [
        {"start_s": 10.0, "end_s": 12.0, "text": "x" * 30},  # 3.0 s in a 2.0 s slot
        {"start_s": 12.0, "end_s": 13.0, "text": "y" * 10},  # 1.0 s
        {"start_s": 16.0, "end_s": 17.0, "text": "z" * 10},  # after the reel's end
    ]
    track, sr, cues = dub.voice_track(lines, "hi", "hf_alpha", clip_start=10.0, duration=5.0)

    assert sr == 100 and len(track) == 500  # as long as the clip
    # Too long: read again 1.3x faster (the most allowed); the next line waits.
    assert calls[:3] == [("x" * 30, 1.0), ("x" * 30, dub.MAX_SPEEDUP), ("y" * 10, 1.0)]
    assert [(round(s, 2), round(e, 2), t[0]) for s, e, t in cues] == [
        (0.0, 2.31, "x"),
        (2.36, 3.36, "y"),
    ]
    assert track[0] == 0.5 and track[240] == 0.5 and track[450] == 0.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_dub_replaces_the_audio_and_writes_hindi_captions(tmp_path, monkeypatch):
    calls = []

    def synthesize(text, language, voice, speed=1.0):  # 1 s of 440 Hz
        calls.append(text)
        t = np.arange(24000) / 24000
        return (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 24000

    monkeypatch.setattr(tts, "synthesize", synthesize)
    (tmp_path / "clips" / "c1").mkdir(parents=True)
    clip = make_media(tmp_path / "clips" / "c1" / "clip.mp4", seed=2, seconds=4)
    plan = {
        "language": "hi",
        "voice": "hf_alpha",
        "lines": [{"start_s": 21.0, "end_s": 22.0, "text": "नमस्ते दोस्तों"}],
    }

    dub.apply(plan, str(clip), clip_start=20.0, duration=4.0)
    dub.apply(plan, str(clip), clip_start=20.0, duration=4.0)  # e.g. after a colour change

    assert calls == ["नमस्ते दोस्तों"]  # read out once, then reused
    srt = (tmp_path / "clips" / "c1" / "clip.srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,000\nनमस्ते दोस्तों" in srt
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "csv=p=0", str(clip)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "video" in probe and "audio" in probe
    assert abs(float(probe.split()[-1]) - 4.0) < 0.1
    # Only the voice is heard: the tone, then silence (the original sound is gone).
    tone = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", "1.1", "-t", "0.8", "-i", str(clip),
         "-af", "bandpass=f=440:width_type=h:w=40,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    quiet = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", "2.5", "-t", "1.0", "-i", str(clip),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    assert float(tone.split("mean_volume: ")[1].split(" dB")[0]) > -30
    assert float(quiet.split("max_volume: ")[1].split(" dB")[0]) < -60


# --- API ---------------------------------------------------------------------
def _reel(client, db_session, **extra) -> Clip:
    resp = client.post("/videos/upload", files={"file": ("film.mp4", b"x" * 10, "video/mp4")})
    video = db_session.get(Video, resp.json()["id"])
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 60.0
    job = Job(video_id=video.id, platforms=["facebook_reel"], status=JobStatus.completed)
    db_session.add(job)
    db_session.commit()
    clip = Clip(
        job_id=job.id, platform="facebook_reel", start_s=10.0, end_s=20.0, rank=1,
        status=ClipStatus.ready, file_path="/nowhere/clip.mp4", color_contrast=15,
        extra={"trim": 0, "words": WORDS, "transcript": "Plant tomatoes today. Water daily.",
               **extra},
    )  # fmt: skip
    db_session.add(clip)
    db_session.commit()
    return clip


def test_translate_gives_editable_hindi_lines(client, db_session, monkeypatch):
    clip = _reel(client, db_session)
    monkeypatch.setattr(
        dub, "translate", lambda lines, language: ["आज टमाटर लगाओ।", "रोज़ पानी दो।"]
    )

    resp = client.post(f"/clips/{clip.id}/translate", json={"language": "hi"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["label"] == "Hindi" and out["voices"][0] == {"key": "hf_alpha", "label": "Female 1"}
    first = out["lines"][0]
    assert (first["start_s"], first["end_s"], first["text"]) == (
        11.0,
        12.5,
        "Plant tomatoes today.",
    )
    assert first["translation"] == "आज टमाटर लगाओ।" and first["roman"]
    db_session.expire_all()
    saved = db_session.get(Clip, clip.id)
    assert saved.translation["language"] == "hi" and len(saved.translation["lines"]) == 2
    assert (
        client.get(f"/jobs/{clip.job_id}/clips").json()[0]["translation"]["lines"][1]["translation"]
        == "रोज़ पानी दो।"
    )


def test_translate_needs_a_transcript_and_a_known_language(client, db_session, monkeypatch):
    clip = _reel(client, db_session)
    assert client.post(f"/clips/{clip.id}/translate", json={"language": "xx"}).status_code == 400

    def offline(lines, language):
        raise dub.DubError("Ollama isn't running on this Mac.")

    monkeypatch.setattr(dub, "translate", offline)
    resp = client.post(f"/clips/{clip.id}/translate", json={})
    assert resp.status_code == 502 and "Ollama" in resp.json()["error"]["message"]

    clip.extra = {"trim": 0, "has_speech": None}  # made without the transcript
    db_session.commit()
    resp = client.post(f"/clips/{clip.id}/translate", json={})
    assert resp.status_code == 409 and "transcript" in resp.json()["error"]["message"]


def test_dub_makes_a_hindi_copy_and_keeps_the_original(client, db_session):
    clip = _reel(client, db_session)
    lines = [
        {"start_s": 11.0, "end_s": 12.5, "text": " आज टमाटर लगाओ। "},
        {"start_s": 15.0, "end_s": 16.0, "text": "रोज़ पानी दो।"},
    ]
    resp = client.post(f"/clips/{clip.id}/dub", json={"voice": "hm_omega", "lines": lines})
    assert resp.status_code == 202, resp.text
    copy = resp.json()
    assert copy["id"] != clip.id and copy["status"] == "rendering"
    assert copy["dub_language"] == "hi" and copy["rank"] == 1 and copy["color_contrast"] == 15
    assert (copy["start_s"], copy["end_s"], copy["platform"]) == (10.0, 20.0, "facebook_reel")
    assert copy["transcript"] == "आज टमाटर लगाओ। रोज़ पानी दो।"
    assert copy["title"].startswith("Reel 1 (Hindi) - Aaj tamaatar")  # in Roman letters
    assert client.enqueued["clip"] == [(copy["id"], False)] and client.enqueued["clip_urgent"] == [
        True
    ]
    saved = db_session.get(Clip, copy["id"])
    assert saved.dub["voice"] == "hm_omega" and saved.dub["source_clip"] == clip.id
    assert saved.dub["lines"][0]["text"] == "आज टमाटर लगाओ।"
    assert db_session.get(Clip, clip.id).dub is None  # the original is untouched
    assert clip_basename(saved.rank, saved.dub_language) == "part-001-hi"

    # A dub can't be dubbed again.
    resp = client.post(f"/clips/{copy['id']}/dub", json={"lines": lines})
    assert resp.status_code == 409


def test_dub_checks_voice_and_line_times(client, db_session):
    clip = _reel(client, db_session)
    line = {"start_s": 11.0, "end_s": 12.0, "text": "नमस्ते"}
    resp = client.post(f"/clips/{clip.id}/dub", json={"voice": "af_heart", "lines": [line]})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "bad_voice"
    outside = {**line, "start_s": 25.0, "end_s": 26.0}
    resp = client.post(f"/clips/{clip.id}/dub", json={"lines": [outside]})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "out_of_range"
    empty = {**line, "text": "   "}
    assert client.post(f"/clips/{clip.id}/dub", json={"lines": [empty]}).status_code == 422


def test_zip_keeps_every_dub_of_a_reel(client, db_session, tmp_path):
    clip = _reel(client, db_session)
    line = {"start_s": 11.0, "end_s": 12.0, "text": "नमस्ते"}
    for n, voice in enumerate(("hf_alpha", "hm_omega")):
        copy = client.post(f"/clips/{clip.id}/dub", json={"voice": voice, "lines": [line]}).json()
        saved = db_session.get(Clip, copy["id"])
        saved.file_path = str(tmp_path / f"dub{n}.mp4")
        Path(saved.file_path).write_bytes(voice.encode())
        saved.status = ClipStatus.ready
    db_session.commit()

    resp = client.get(f"/jobs/{clip.job_id}/export.zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["facebook_reel/part-001-hi-2.mp4", "facebook_reel/part-001-hi.mp4"]
        assert {zf.read(n) for n in names} == {b"hf_alpha", b"hm_omega"}


def test_rerender_lays_the_hindi_voice_over_the_new_picture(db_session, tmp_path, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    rendered, dubbed = [], []

    def fake_render(spec):
        Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.out_path).write_bytes(b"mp4")
        rendered.append(spec)

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    monkeypatch.setattr(pipeline.dub, "apply", lambda *args: dubbed.append(args))
    video = Video(user_id="u1", filename="f.mp4", path=str(tmp_path / "f.mp4"),
                  status=VideoStatus.ready, duration_s=60, width=160, height=90)  # fmt: skip
    db_session.add(video)
    db_session.commit()
    job = Job(video_id=video.id, platforms=["facebook_reel"], status=JobStatus.completed)
    db_session.add(job)
    db_session.commit()
    plan = {"language": "hi", "voice": "hf_alpha", "lines": [], "source_clip": "x"}
    clip = Clip(job_id=job.id, platform="facebook_reel", start_s=10, end_s=20, rank=1,
                status=ClipStatus.rendering, extra={"has_speech": True, "dub": plan})  # fmt: skip
    db_session.add(clip)
    db_session.commit()

    pipeline.render_single_clip.apply(args=[clip.id])

    db_session.expire_all()
    clip = db_session.get(Clip, clip.id)
    assert clip.status == ClipStatus.ready, clip.error
    assert rendered[0].words == []  # no English captions
    assert dubbed == [(plan, rendered[0].out_path, 10.0, 10.0)]
    assert clip.has_speech is True
