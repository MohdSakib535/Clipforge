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


def test_scene_cmd_moves_the_camera_and_escapes_the_caption_path():
    cmd = reel_render.build_scene_cmd(
        "p.png", "s.mp4", frames=90, move=0, ass="C:/x/cap.ass", preset="veryfast"
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "zoompan=z=1+0.15*on/90:" in vf and "d=90:s=1080x1920:fps=30" in vf
    assert "vignette" in vf and vf.index("vignette") < vf.index("ass=")  # captions not darkened
    assert "ass=C\\:/x/cap.ass" in vf
    assert cmd[cmd.index("-frames:v") + 1] == "90" and "-an" in cmd
    out = reel_render.build_scene_cmd(
        "p.png", "s.mp4", frames=90, move=2, ass=None, preset="veryfast"
    )
    vf = out[out.index("-vf") + 1]
    assert "zoompan=z=1.15-0.15*on/90:" in vf and "ass=" not in vf


def test_camera_moves_take_turns():
    moves = [reel_render.camera_move(i, 60) for i in range(7)]
    assert len(set(moves[:6])) == 6  # six different moves...
    assert moves[6] == moves[0]  # ...then round again
    z, x, _ = moves[1]
    assert z == "1.15" and x == "(iw-iw/zoom)*on/60"  # pans right while zoomed in


def test_a_scene_is_cut_into_pieces_on_keyframes():
    cmd = reel_render.build_scene_cmd(
        "p.png", "s-%d.mp4", frames=100, move=0, ass=None, preset="veryfast", splits=[12, 88]
    )
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:eq(n,12)+eq(n,88)"
    assert cmd[cmd.index("-segment_frames") + 1] == "12,88"
    assert cmd[cmd.index("-f") + 1] == "segment" and cmd[-1] == "s-%d.mp4"
    whole = reel_render.build_scene_cmd(
        "p.png", "s.mp4", frames=100, move=0, ass=None, preset="veryfast"
    )
    assert "-f" not in whole and whole[-1] == "s.mp4"


def test_a_blend_takes_only_the_two_pieces_it_joins():
    cmd = reel_render.build_blend_cmd(
        "tail.mp4", "head.mp4", "b.mp4", kind="smoothleft", frames=12, preset="veryfast"
    )
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"] == ["tail.mp4", "head.mp4"]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == "[0:v][1:v]xfade=transition=smoothleft:duration=0.400000:offset=0[v]"
    assert cmd[cmd.index("-frames:v") + 1] == "12"


def test_captions_light_up_the_word_being_spoken():
    ass = build_ass([reel_render.Cue(0.0, 1.0, "Do it now")], "DejaVu Sans")
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue) == 3  # one per word, the whole chunk on screen each time
    yellow = "{\\c&H0000D7FF&}"
    assert f"{yellow}Do{{\\c&HFFFFFF&}} it now" in dialogue[0]
    assert f"Do {yellow}it{{\\c&HFFFFFF&}} now" in dialogue[1]
    assert "\\fscx80" in dialogue[0] and "\\fscx80" not in dialogue[1]  # pops in once
    assert dialogue[-1].split(",")[2] == "0:00:01.00"  # the last word ends with the chunk


def test_picture_prompt_starts_with_the_style():
    p = reel_render.picture_prompt("a cat on a roof at night.", "anime")
    assert p.startswith("Anime style illustration, ")
    assert ". a cat on a roof at night. One single vertical 9:16 picture, not a collage" in p
    assert reel_render.DEFAULT_STYLE in reel_render.STYLES


def test_cached_files_are_named_after_what_made_them(tmp_path):
    a = reel_render.picture_path(tmp_path, 0, "a cat", "cinematic", 384, 672, 4)
    assert a.name.startswith("scene01-") and a.suffix == ".png"
    assert a == reel_render.picture_path(tmp_path, 0, "a cat", "cinematic", 384, 672, 4)
    assert a != reel_render.picture_path(tmp_path, 0, "a dog", "cinematic", 384, 672, 4)
    assert a != reel_render.picture_path(tmp_path, 0, "a cat", "anime", 384, 672, 4)
    assert a != reel_render.picture_path(tmp_path, 0, "a cat", "cinematic", 576, 1024, 4)
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
    assert _frame_count(tmp_path / "reel.mp4") == 38 + 69  # every frame, none lost or added
    # Red until the blue scene's voice starts (frame 38), then a 12-frame blend.
    red, mixed, blue = (_color(tmp_path / "reel.mp4", n) for n in (36, 44, 52))
    assert red[0] > 3 * red[2] and blue[2] > 3 * blue[0]
    assert blue[0] < mixed[0] < red[0] and red[2] < mixed[2] < blue[2]
    assert (tmp_path / "thumb.jpg").exists()
    assert not (tmp_path / "tmp").exists()  # temporary files are cleaned up


@needs_ffmpeg
def test_a_very_short_scene_gets_a_shorter_blend(tmp_path):
    pics = [_picture(tmp_path / f"{c}.png", c) for c in ("red", "green", "blue")]
    voices = [_voice(tmp_path / "a.wav", 1.0), _voice(tmp_path / "b.wav", 0.05),
              _voice(tmp_path / "c.wav", 1.0)]  # fmt: skip
    scenes = [SceneInput(p, "Hi.", v) for p, v in zip(pics, voices, strict=True)]
    duration = reel_render.render(tmp_path, scenes, font="DejaVu Sans")
    # The middle scene is 9 frames: the blend into it takes 8, leaving it one.
    assert duration == pytest.approx((38 + 9 + 54) / 30)
    assert _frame_count(tmp_path / "reel.mp4") == 38 + 9 + 54


def _frame_count(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return int(out.stdout.strip())


def _color(path: Path, frame: int) -> tuple[int, int, int]:
    """The average colour of one frame (R, G, B)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame}),scale=1:1",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    return tuple(out.stdout[:3])


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
