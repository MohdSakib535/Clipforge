"""Putting a reel together: captions, FFmpeg commands and a real render."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import reel_render
from app.services.reel_render import SceneInput, build_ass, caption_cues
from tests.synthetic_media import make_media

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_captions_show_a_few_words_at_a_time_over_the_whole_line():
    cues = caption_cues("Do you wake up tired every morning? These habits help.", 4.0)
    texts = [c.text for c in cues]
    assert texts == ["Do you wake", "up tired every", "morning?", "These habits", "help."]
    assert cues[0].start == 0.0 and cues[-1].end == pytest.approx(4.0)
    for a, b in zip(cues, cues[1:], strict=False):
        assert a.end == pytest.approx(b.start)  # no gaps, no overlaps
    # Longer chunks stay on screen longer.
    assert cues[1].end - cues[1].start > cues[4].end - cues[4].start


def test_hindi_captions_break_after_the_danda():
    cues = caption_cues("ये तीन आदतें। आज से शुरू करें।", 3.0)
    assert [c.text for c in cues] == ["ये तीन आदतें।", "आज से शुरू", "करें।"]


def test_ass_file_has_style_font_and_safe_text():
    ass = build_ass(caption_cues("Hi {there} \\N", 1.5), "Noto Sans Devanagari")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Caption,Noto Sans Devanagari," in ass
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert dialogue[0].startswith("Dialogue: 0,0:00:00.00,")
    assert "(there)" in ass and "{there}" not in ass  # braces would be override tags
    assert reel_render._ass_time(3661.5) == "1:01:01.50"


def test_scene_cmd_zooms_both_ways_and_escapes_the_caption_path():
    cmd = reel_render.build_scene_cmd(
        "p.png", "s.mp4", frames=90, zoom_in=True, ass="C:/x/cap.ass", preset="veryfast"
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "zoompan=z=1+0.15*on/90:" in vf and "d=90:s=1080x1920:fps=30" in vf
    assert "ass=C\\:/x/cap.ass" in vf
    assert cmd[cmd.index("-frames:v") + 1] == "90" and "-an" in cmd
    out = reel_render.build_scene_cmd(
        "p.png", "s.mp4", frames=90, zoom_in=False, ass=None, preset="veryfast"
    )
    vf = out[out.index("-vf") + 1]
    assert "zoompan=z=1.15-0.15*on/90:" in vf and "ass=" not in vf


def test_cached_files_are_named_after_what_made_them(tmp_path):
    a = reel_render.picture_path(tmp_path, 0, "a cat", 384, 672, 4)
    assert a.name.startswith("scene01-") and a.suffix == ".png"
    assert a == reel_render.picture_path(tmp_path, 0, "a cat", 384, 672, 4)
    assert a != reel_render.picture_path(tmp_path, 0, "a dog", 384, 672, 4)
    assert a != reel_render.picture_path(tmp_path, 0, "a cat", 576, 1024, 4)
    v = reel_render.voice_path(tmp_path, 2, "hello", "en", "af_heart")
    assert v.name.startswith("voice03-")
    assert v != reel_render.voice_path(tmp_path, 2, "hello", "en", "am_michael")


def _picture(path: Path, color: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=384x672",
         "-frames:v", "1", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _voice(path: Path, seconds: float) -> Path:
    np = pytest.importorskip("numpy")
    t = np.arange(int(24000 * seconds)) / 24000
    reel_render.write_wav(path, 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32), 24000)
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


@needs_ffmpeg
def test_render_makes_a_vertical_reel_with_voice_and_music(tmp_path):
    red, blue = _picture(tmp_path / "a.png", "red"), _picture(tmp_path / "b.png", "blue")
    scenes = [
        SceneInput(red, "Do you wake up tired?", _voice(tmp_path / "a.wav", 1.0)),
        SceneInput(blue, "Try this.", _voice(tmp_path / "b.wav", 1.5)),
    ]
    music = make_media(tmp_path / "music.m4a", seed=2, seconds=2, video=False)

    duration = reel_render.render(tmp_path, scenes, font="DejaVu Sans", music=str(music))

    # Each scene: its line + a breath (0.25 s; 0.8 s after the last), in whole frames.
    assert duration == pytest.approx((38 + 69) / 30)
    info = _probe(tmp_path / "reel.mp4")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(duration, abs=0.1)
    assert (tmp_path / "thumb.jpg").exists()
    assert not (tmp_path / "tmp").exists()  # temporary files are cleaned up


def test_remove_unused_keeps_only_current_pictures_and_voices(tmp_path):
    keep = tmp_path / "scene01-aaa.png"
    old = tmp_path / "scene01-bbb.png"
    voice = tmp_path / "voice01-ccc.wav"
    video = tmp_path / "reel.mp4"
    for f in (keep, old, voice, video):
        f.write_bytes(b"x")
    reel_render.remove_unused(tmp_path, {keep, voice})
    assert keep.exists() and voice.exists() and video.exists()
    assert not old.exists()
