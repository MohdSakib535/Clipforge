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
    assert ". a cat on a roof at night. Natural, correct anatomy. One single vertical" in p
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


RAMAYANA = {
    "setting": "Ancient India in the Ramayana: forests, hermitages, oil lamps.",
    "characters": [
        {"name": "Ram", "look": "a young prince with a bow, in saffron robes"},
        {"name": "Sita", "look": "a young princess in a red sari"},
    ],
    "scenes": [
        {"narration": "a", "image_prompt": "Ram walks into the forest.", "characters": ["Ram"],
         "crowd": False},
        {"narration": "b", "image_prompt": "Sages bless them", "characters": ["Ram", "Sita"],
         "crowd": True},
        {"narration": "c", "image_prompt": "A river at dawn", "characters": [], "crowd": False},
    ],
}  # fmt: skip


def test_each_picture_gets_its_peoples_looks_and_the_setting():
    one, two, none = (reel_render.scene_prompt(sc, RAMAYANA) for sc in RAMAYANA["scenes"])
    assert one.startswith("Ram walks into the forest. In the picture: Ram, a young prince")
    assert "Only 1 person in the picture, nobody else: no statues, idols or paintings" in one
    assert one.endswith(
        "Time and place: Ancient India in the Ramayana: forests, hermitages, oil lamps. "
        "Everything in the picture belongs there."
    )
    assert "Ram, a young prince with a bow, in saffron robes; Sita, a young princess" in two
    assert "Other people only in the background" in two and "Only" not in two
    assert "In the picture" not in none and "Time and place: Ancient India" in none
    # A script from before characters: the picture prompt as it was.
    assert reel_render.scene_prompt({"image_prompt": "a cat."}, {"scenes": []}) == "a cat."


def test_a_new_look_redraws_only_the_pictures_with_that_character(tmp_path):
    from app.config.settings import get_settings

    settings = get_settings()
    before = reel_render.scene_pictures(tmp_path, RAMAYANA, settings, "cinematic")
    blue = {"name": "Sita", "look": "a princess in a blue sari"}
    sita = {**RAMAYANA, "characters": [RAMAYANA["characters"][0], blue]}
    after = reel_render.scene_pictures(tmp_path, sita, settings, "cinematic")
    assert [a == b for a, b in zip(before, after, strict=True)] == [True, False, True]
    elsewhere = reel_render.scene_pictures(tmp_path, {**RAMAYANA, "setting": "Mars"}, settings,
                                           "cinematic")  # fmt: skip
    assert all(a != b for a, b in zip(before, elsewhere, strict=True))  # every picture
    # Old scripts keep their pictures.
    old = {"scenes": [{"narration": "a", "image_prompt": "a cat"}]}
    assert reel_render.scene_pictures(tmp_path, old, settings, "cinematic") == [
        reel_render.picture_path(tmp_path, 0, "a cat", "cinematic", settings.reel_image_width,
                                 settings.reel_image_height, settings.reel_image_steps)
    ]  # fmt: skip


def test_what_vision_sees_tells_a_broken_picture():
    one = {"characters": ["Ram"], "crowd": False}

    def seen(faces: int, hands: int, people: int) -> dict[str, int]:
        return {"faces": faces, "hands": hands, "people": people}

    assert reel_render.picture_problems(seen(1, 2, 1), one) == []
    assert reel_render.picture_problems(None, one) == []  # nothing seen: nothing to judge
    assert reel_render.picture_problems(seen(2, 2, 1), one) == ["2 people or faces instead of 1"]
    assert reel_render.picture_problems(seen(1, 4, 1), one) == ["4 hands for 1 people"]
    crowd = {"characters": ["Ram"], "crowd": True}
    assert reel_render.picture_problems(seen(5, 9, 6), crowd) == []  # others are allowed
    assert reel_render.picture_problems(seen(2, 9, 2), crowd) == ["9 hands for 2 people"]
    old = {"image_prompt": "a market"}  # who should be there is not known: hands only
    assert reel_render.picture_problems(seen(4, 6, 4), old) == []


def test_a_warning_goes_with_its_picture(tmp_path):
    keep, old = tmp_path / "scene01-aaa.png", tmp_path / "scene01-bbb.png"
    for f in (keep, old, reel_render.warning_path(keep), reel_render.warning_path(old)):
        f.write_text("x")
    reel_render.remove_unused(tmp_path, {keep})
    assert reel_render.warning_path(keep).exists() and not reel_render.warning_path(old).exists()


def test_a_story_blends_in_the_breath_before_the_next_line():
    frames, spoken = [54, 60, 45], [30, 36, 21]  # each line plus a 0.8 s breath
    shown, blends, starts = reel_render.scene_timing(frames, spoken, story=True)
    assert blends == [24, 24]  # 0.8 s, within each breath
    assert starts == [0, 24, 24]  # each line starts once its picture is in
    assert shown == [30, 60, 69] and sum(shown) == sum(frames)  # voice and picture in step
    # A reel blends as the next line starts; nothing moves.
    assert reel_render.scene_timing(frames, spoken) == (frames, [12, 12], [0, 0, 0])
    # A breath shorter than the blend keeps the blend inside it.
    assert reel_render.scene_timing([40, 40], [30, 30], story=True)[1] == [10]


def test_a_story_drifts_and_has_calm_captions():
    assert reel_render.camera_move(1, 60, story=True) == (
        "1+0.08*on/60", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    )  # fmt: skip
    cues = caption_cues("Long ago in a quiet forest there lived a young prince.", 3.0,
                        max_words=5, max_chars=30)  # fmt: skip
    calm = build_ass(cues, "DejaVu Sans", story=True)
    assert "\\fscx80" not in calm and "Style: Caption,DejaVu Sans,80," in calm
    assert len(cues) <= 3  # a line at a time, not 2-3 words


def test_a_storys_lines_start_and_end_softly():
    np = pytest.importorskip("numpy")
    line = np.ones(2400, np.float32)
    soft = reel_render._soften(line, 24000)
    assert soft[0] == 0.0 and soft[-1] == 0.0 and soft[1200] == 1.0
    assert line[0] == 1.0  # the clip itself is untouched


@needs_ffmpeg
def test_a_story_fades_into_the_next_picture_before_its_line(tmp_path):
    red, blue = _picture(tmp_path / "a.png", "red"), _picture(tmp_path / "b.png", "blue")
    scenes = [
        SceneInput(red, "Long ago.", _voice(tmp_path / "a.wav", 1.0)),
        SceneInput(blue, "Then.", _voice(tmp_path / "b.wav", 1.0)),
    ]
    duration = reel_render.render(tmp_path, scenes, font="DejaVu Sans", story=True)
    # Scene 1: its line (30 frames) and a 0.8 s breath (24); scene 2's line starts at 54.
    assert duration == pytest.approx((54 + 54) / 30)
    assert _frame_count(tmp_path / "reel.mp4") == 54 + 54
    red_end, mixed, blue_start = (_color(tmp_path / "reel.mp4", n) for n in (28, 42, 55))
    assert red_end[0] > 3 * red_end[2]  # the line is still on red
    assert blue_start[2] > 3 * blue_start[0]  # the next line starts on blue
    assert blue_start[0] < mixed[0] < red_end[0]  # faded during the breath
