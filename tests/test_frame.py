from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config.platforms import get_preset
from app.services import frame, render
from app.services.render import RenderSpec

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _frame(**kw) -> dict:
    base = {"background": "black", "bg_color": "#000000", "texts": []}
    return {**base, **kw}


def _text(text: str, **kw) -> dict:
    return {"text": text, "x": 540, "y": 960, "size": 60, "color": "#ffffff", **kw}


def test_picture_rect_landscape_fills_the_width_in_the_middle():
    assert frame.picture_rect(1920, 1080) == (0, 656, 1080, 608)
    x, y, w, h = frame.picture_rect(1920, 1080, video_y=30)
    assert (x, w, h) == (0, 1080, 608) and y < 656


def test_picture_rect_scale_and_move():
    # Half size, pushed to the right: kept fully on the canvas.
    x, y, w, h = frame.picture_rect(1920, 1080, video_y=50, video_x=100, video_scale=50)
    assert (w, h) == (540, 304) and x + w == 1080
    assert y == (960 - 152) // 2 * 2


def test_picture_rect_stays_on_the_canvas():
    assert frame.picture_rect(1920, 1080, video_y=0)[1] == 0
    _, y, _, h = frame.picture_rect(1920, 1080, video_y=100)
    assert y + h <= frame.CANVAS_H
    # A portrait source fills the height instead.
    assert frame.picture_rect(1080, 1920) == (0, 0, 1080, 1920)


def test_text_layers_anywhere_with_anchor_and_escaping():
    ass = frame.build_ass(
        _frame(
            texts=[
                _text("Line {one}\nभाग {part} 🔥", x=100, y=200, align="left", color="#ff0000"),
                _text("Right", x=1000, y=1800, align="right", italic=True),
                _text("   "),  # empty: no line
                _text("On the picture", x=540, y=960),
            ]
        ),
        {"part": 7},
    )
    events = ass.split("[Events]")[1]
    # Braces can't open ASS tags, newlines become \N, the emoji is dropped.
    assert "{\\an4\\pos(100,200)}Line (one)\\Nभाग 7" in events
    assert "🔥" not in ass
    assert "{\\an6\\pos(1000,1800)}Right" in events
    assert "{\\an5\\pos(540,960)}On the picture" in events
    assert events.count("Dialogue:") == 3
    # Red is &H000000FF in ASS (blue first); the size is scaled to libass units.
    assert ",&H000000FF," in ass and f",{round(60 * frame.ASS_SIZE_PER_PX)}," in ass
    # Later layers are drawn on top.
    assert "Dialogue: 3," in events


def test_frame_filter_backgrounds():
    black = frame.frame_filter(_frame(), 1920, 1080, "/t.ass")
    assert "pad=1080:1920:0:656:color=0x000000" in black
    assert black.endswith("ass=filename=/t.ass")
    colour = frame.frame_filter(_frame(background="color", bg_color="#1a2b3c"), 1920, 1080, "/a")
    assert "color=0x1a2b3c" in colour
    blur = frame.frame_filter(_frame(background="blur"), 1920, 1080, "/a", pre="eq=contrast=1.1")
    assert blur.startswith("eq=contrast=1.1,split=2") and "overlay=0:656" in blur
    small = frame.frame_filter(_frame(video_scale=50, video_x=0), 1920, 1080, "/a")
    assert "scale=540:304,pad=1080:1920:0:808" in small


def _source(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:d=3",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def _luma(path: Path, crop: str) -> float:
    """Mean luma of one area of the first frame."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-vf", f"crop={crop},format=gray,scale=1:1", "-f", "rawvideo", "-"],
        check=True,
        capture_output=True,
    ).stdout  # fmt: skip
    return out[0]


@needs_ffmpeg
@pytest.mark.parametrize("platform", ["facebook_video", "facebook_reel"])
def test_framed_render_is_9x16_with_the_text_where_it_was_put(tmp_path, platform):
    src, out = tmp_path / "src.mp4", tmp_path / "clip.mp4"
    _source(src)
    spec = RenderSpec(
        source_path=str(src),
        out_path=str(out),
        thumb_path=str(tmp_path / "t.jpg"),
        start_s=0.5,
        end_s=2.5,
        preset=get_preset(platform),
        src_width=640,
        src_height=360,
        words=[],
        encode_preset="ultrafast",
        stream_copy_ok=True,  # ignored: a frame always re-encodes
        frame=_frame(
            texts=[_text("Part {part}", x=540, y=328, size=120, bold=True, outline=False)]
        ),
        frame_values={"part": 1},
    )
    render.render_clip(spec)
    info = _probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.15
    # Whole picture kept (no reframe crop) at 656..1264; above it the text
    # (white on black), below it plain black.
    assert render.frame_rect(spec) == (0, 656, 1080, 608)
    assert _luma(out, "300:80:390:288") > 40  # the text, centred at y=328
    assert _luma(out, "1080:500:0:1350") < 3  # empty black below
    assert _luma(out, "1080:500:0:700") > 40  # the picture
