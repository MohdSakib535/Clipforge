"""Reel tasks in-process: the script step, and a whole render with a fake
image generator and voice (real FFmpeg)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Reel
from app.services import imagegen, reel_render, reel_script, tts
from app.tasks import reels

SCRIPT = {
    "title": "Water",
    "hooks": [],
    "scenes": [
        {"narration": "Drink water first.", "image_prompt": "a glass of water"},
        {"narration": "Then stretch.", "image_prompt": "a woman stretching"},
    ],
    "caption": "",
    "hashtags": [],
}


@pytest.fixture
def factory(db_session, monkeypatch):
    f = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(reels, "SessionLocal", f)
    return f


def _reel(db_session, **fields) -> Reel:
    fields.setdefault("script_type", "tips")  # paced as a reel (a story is slower)
    reel = Reel(user_id="u1", platform="instagram", language="en", voice="af_heart",
                topic="water", duration_s=20, **fields)  # fmt: skip
    db_session.add(reel)
    db_session.commit()
    return reel


def test_script_task_saves_the_script(db_session, factory, monkeypatch):
    reel = _reel(db_session, status="scripting", script_type="facts", description="Keep it calm.")
    asked = []
    monkeypatch.setattr(
        reel_script, "generate_script", lambda *a, **k: asked.append((a, k)) or SCRIPT
    )
    reels.write_reel_script.apply(args=[reel.id])
    db_session.refresh(reel)
    assert (reel.status, reel.script) == ("script_ready", SCRIPT)
    assert reel.error is None and reel.stage is None
    args, kwargs = asked[0]
    assert args[4:] == ("facts", "Keep it calm.", "af_heart")  # type, description, voice
    assert kwargs["model"] is None  # the default writer
    kwargs["on_step"]("Checking its length")  # each step shows on the reel
    db_session.refresh(reel)
    assert reel.stage == "Checking its length"


def test_script_failure_keeps_an_existing_script(db_session, factory, monkeypatch):
    def broken(*a, **k):
        raise reel_script.ScriptError("Ollama isn't running")

    monkeypatch.setattr(reel_script, "generate_script", broken)
    first = _reel(db_session, status="scripting")
    again = _reel(db_session, status="scripting", script=SCRIPT)
    reels.write_reel_script.apply(args=[first.id])
    reels.write_reel_script.apply(args=[again.id])
    db_session.refresh(first)
    db_session.refresh(again)
    assert (first.status, first.error) == ("failed", "Ollama isn't running")
    assert (again.status, again.script) == ("script_ready", SCRIPT)
    assert again.error == "Ollama isn't running"


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """A blue PNG for every picture, a 1 s tone for every line; counts calls."""
    np = pytest.importorskip("numpy")
    png = tmp_path / "blue.png"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=384x672",
         "-frames:v", "1", str(png)],
        check=True,
    )  # fmt: skip
    calls = {"pictures": [], "voices": [], "unloads": 0, "counts": [], "speeds": []}

    def fake_generate(prompt, *, width, height, seed, steps):
        calls["pictures"].append(prompt)
        counts = calls["counts"].pop(0) if calls["counts"] else None  # what Vision "sees"
        return imagegen.Picture(png.read_bytes(), counts)

    def fake_synthesize(text, language, voice, speed=1.0):
        calls["voices"].append((text, voice))
        calls["speeds"].append(speed)
        t = np.arange(24000) / 24000
        return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 24000

    def fake_unload():
        calls["unloads"] += 1

    monkeypatch.setattr(imagegen, "check", lambda: None)
    monkeypatch.setattr(imagegen, "generate", fake_generate)
    monkeypatch.setattr(imagegen, "unload", fake_unload)
    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    return calls


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_render_task_makes_the_reel_and_reuses_unchanged_scenes(db_session, factory, fakes):
    reel = _reel(db_session, status="generating", script=SCRIPT)
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "ready", reel.error
    assert (reel.progress, reel.stage, reel.error) == (100, None, None)
    assert Path(reel.file_path).exists() and Path(reel.thumbnail_path).exists()
    assert reel.video_duration_s == pytest.approx((38 + 54) / 30, abs=0.01)  # stored rounded
    assert len(fakes["pictures"]) == 2 and len(fakes["voices"]) == 2
    assert fakes["pictures"][0].startswith("Cinematic film still, ")  # the reel's style
    assert "a glass of water. Natural, correct anatomy. One single vertical" in fakes["pictures"][0]
    assert fakes["unloads"] == 1  # the image model is freed once its pictures are done

    # Change one line: only its voice is redone; the pictures are reused.
    first, second = SCRIPT["scenes"]
    edited = {**SCRIPT, "scenes": [first, {**second, "narration": "Now stretch."}]}
    reel.script, reel.status = edited, "generating"
    db_session.commit()
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "ready"
    assert len(fakes["pictures"]) == 2
    assert fakes["unloads"] == 1  # no new pictures: the generator wasn't used at all
    assert fakes["voices"][2:] == [("Now stretch.", "af_heart")]
    folder = Path(reel.file_path).parent
    assert len(list(folder.glob("voice*.wav"))) == 2  # the old line's voice is gone


def test_render_task_explains_a_missing_image_generator(db_session, factory, monkeypatch):
    def not_running():
        raise imagegen.ImageGenError(f"The image generator isn't running. {imagegen.START_HINT}")

    monkeypatch.setattr(imagegen, "check", not_running)
    reel = _reel(db_session, status="generating", script=SCRIPT)
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "failed"
    assert "uv run imagegen/server.py" in reel.error


def test_render_stops_when_the_reel_was_deleted(db_session, factory, fakes, monkeypatch):
    rid = _reel(db_session, status="generating", script=SCRIPT).id

    def generate_then_delete(prompt, **kw):
        with factory() as other:  # the user deletes the reel meanwhile
            other.delete(other.get(Reel, rid))
            other.commit()
        return imagegen.Picture(b"png")

    monkeypatch.setattr(imagegen, "generate", generate_then_delete)
    reels.render_reel.apply(args=[rid])
    assert db_session.get(Reel, rid, populate_existing=True) is None
    assert not reel_render.reel_dir(rid).exists()  # stopped before the video; files removed


PEOPLE = {
    **SCRIPT,
    "setting": "A sunny modern kitchen.",
    "characters": [{"name": "Riya", "look": "a young woman in a yellow kurta"}],
    "scenes": [
        {**SCRIPT["scenes"][0], "characters": ["Riya"], "crowd": False},
        {**SCRIPT["scenes"][1], "characters": ["Riya"], "crowd": False},
    ],
}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_picture_with_an_extra_person_is_drawn_again(db_session, factory, fakes):
    reel = _reel(db_session, status="generating", script=PEOPLE)
    two_people = {"faces": 2, "hands": 4, "people": 2}
    right = {"faces": 1, "hands": 2, "people": 1}
    # Scene 1: wrong, then right. Scene 2: wrong every time.
    fakes["counts"] += [two_people, right, two_people, two_people, two_people]
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "ready", reel.error
    assert len(fakes["pictures"]) == 2 + 3  # drawn again: once, then twice (the limit)
    assert "In the picture: Riya, a young woman in a yellow kurta" in fakes["pictures"][0]
    assert "Time and place: A sunny modern kitchen" in fakes["pictures"][0]
    folder = Path(reel.file_path).parent
    first, second = sorted(folder.glob("scene*.png"))
    assert not reel_render.warning_path(first).exists()
    assert reel_render.warning_path(second).read_text() == "2 people or faces instead of 1"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_story_is_read_slower_and_told_as_one(db_session, factory, fakes, monkeypatch):
    told = {}
    render = reel_render.render

    def spy(*args, **kwargs):
        told["story"] = kwargs.get("story")
        return render(*args, **kwargs)

    monkeypatch.setattr(reel_render, "render", spy)
    reel = _reel(db_session, status="generating", script=SCRIPT, script_type="story")
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "ready", reel.error
    assert fakes["speeds"] == [reel_render.STORY_SPEED] * 2 and told["story"] is True
    # Its voice clips are its own: the same lines in a "tips" reel are read at 1.0.
    tips = _reel(db_session, status="generating", script=SCRIPT, script_type="tips")
    reels.render_reel.apply(args=[tips.id])
    db_session.refresh(tips)
    assert fakes["speeds"][2:] == [1.0, 1.0] and told["story"] is False

    def clips(r):
        return sorted(p.name for p in Path(r.file_path).parent.glob("voice*.wav"))

    assert clips(reel) != clips(tips)  # named after the pace they were read at
