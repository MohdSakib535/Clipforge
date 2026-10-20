from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config.platforms import get_preset
from app.services import ffmpeg, render
from app.services.render import RenderSpec, _color_filter, _join_filters

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_join_filters_drops_empty():
    assert _join_filters("scale=2:2", None, "eq=contrast=1.1") == "scale=2:2,eq=contrast=1.1"
    assert _join_filters(None, None) is None


def test_color_filter_from_spec():
    base = dict(
        source_path="s",
        out_path="o",
        thumb_path="t",
        start_s=0,
        end_s=1,
        preset=get_preset("youtube_clip"),
        src_width=1920,
        src_height=1080,
        words=[],
    )
    assert _color_filter(RenderSpec(**base)) is None
    assert _color_filter(RenderSpec(**base, color_contrast=10)) == (
        "eq=contrast=1.1:brightness=0.0:saturation=1.0"
    )


def _make_source(path: Path, seconds: int = 3, *, gop: int | None = None) -> None:
    """640x360 test video with audio; ``gop`` = frames between keyframes."""
    keyframes = ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"] if gop else []
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=25:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=300:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            *keyframes,
            "-c:a",
            "aac",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _yavg(path: Path) -> float:
    out = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            "signalstats,metadata=print",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stderr
    vals = [float(m) for m in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", out)]
    return sum(vals) / len(vals)


def _spec(src: Path, out: Path, **kw) -> RenderSpec:
    base = dict(
        source_path=str(src),
        out_path=str(out),
        thumb_path=str(out.with_suffix(".jpg")),
        start_s=0.0,
        end_s=2.0,
        preset=get_preset("youtube_clip"),
        src_width=640,
        src_height=360,
        words=[],
        encode_preset="ultrafast",
    )
    return RenderSpec(**{**base, **kw})


@needs_ffmpeg
def test_brightness_grade_lightens_the_picture(tmp_path):
    src = tmp_path / "src.mp4"
    _make_source(src)
    plain, bright = tmp_path / "plain.mp4", tmp_path / "bright.mp4"
    render.render_clip(_spec(src, plain))
    render.render_clip(_spec(src, bright, color_brightness=40))
    assert _yavg(bright) > _yavg(plain) + 5  # clearly brighter


@needs_ffmpeg
def test_colour_forces_reencode_even_when_stream_copy_ok(tmp_path):
    # Source is 8-bit H.264 -> normally stream-copied unchanged. With a grade,
    # the only way the luma can change is a re-encode through the eq filter.
    src = tmp_path / "src.mp4"
    _make_source(src)
    assert ffmpeg.can_stream_copy("h264", "yuv420p")

    copy_out = tmp_path / "copy.mp4"
    render.render_clip(_spec(src, copy_out, stream_copy_ok=True))

    # Brightness shifts the mean luma; a stream copy cannot do that, so a
    # measurable change proves the clip was re-encoded through the eq filter.
    graded = tmp_path / "graded.mp4"
    render.render_clip(_spec(src, graded, stream_copy_ok=True, color_brightness=40))
    assert _yavg(graded) > _yavg(copy_out) + 5


@needs_ffmpeg
def test_graded_horizontal_clip_keeps_the_size_and_timing_of_the_copy_cut(tmp_path):
    # Keyframes every second and a start between two of them: the copy cut
    # begins on the keyframe before start_s. A grade must not change the frame
    # size (it used to jump to 1920x1080), the start or the length.
    src = tmp_path / "src.mp4"
    _make_source(src, seconds=5, gop=25)
    window = dict(start_s=1.5, end_s=3.5, stream_copy_ok=True)
    plain, graded = tmp_path / "plain.mp4", tmp_path / "graded.mp4"
    render.render_clip(_spec(src, plain, **window))
    render.render_clip(_spec(src, graded, color_contrast=30, color_brightness=20, **window))

    p, g = ffmpeg.ffprobe_metadata(plain), ffmpeg.ffprobe_metadata(graded)
    assert (g.width, g.height) == (p.width, p.height) == (640, 360)
    assert abs(g.duration_s - p.duration_s) < 0.1
    assert p.duration_s > 2.3  # the cut really starts on the keyframe at 1.0 s
    assert _yavg(graded) > _yavg(plain) + 5  # and the grade is in the picture


def test_odd_frame_size_is_trimmed_to_even_for_the_encoder():
    base = dict(source_path="s", out_path="o", thumb_path="t", start_s=0, end_s=1,
                preset=get_preset("youtube_clip"), words=[])  # fmt: skip
    assert render._even_size(RenderSpec(src_width=1920, src_height=1080, **base)) is None
    assert render._even_size(RenderSpec(src_width=1919, src_height=1080, **base)) == (
        "crop=trunc(iw/2)*2:trunc(ih/2)*2"
    )
