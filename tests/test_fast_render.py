"""Fast horizontal cuts: the source's audio is loudness-normalised once and each
copy cut takes its slice with one measured gain, instead of running loudnorm
per clip. These tests check A/V sync matches the old per-clip path, loudness
lands on target with safe peaks, and the source's chapters stay out."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.config.platforms import get_preset
from app.models import Video
from app.services import analysis, ffmpeg, render
from app.services.render import RenderSpec
from app.tasks import pipeline

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
PERIOD = 3.3  # a white flash and a beep start together every 3.3 s


def test_commands_copy_the_picture_and_normalise_once():
    norm = ffmpeg.build_normalized_audio_cmd("src.mkv", "norm.m4a")
    assert norm[norm.index("-af") + 1] == ffmpeg.AUDIO_NORM_FILTER and "0:a:0" in norm
    assert norm[norm.index("-map_chapters") + 1] == "-1"
    cut = ffmpeg.build_copy_video_cut_cmd("src.mkv", "cut.mkv", 10.0, 70.0)
    assert "-an" in cut and "-copyts" in cut and cut[cut.index("-c") + 1] == "copy"
    mux = ffmpeg.build_mux_normalized_audio_cmd(
        "cut.mkv", "norm.m4a", "out.mp4", 9.92, 60.08, gain_db=3.5
    )
    assert mux[mux.index("-c:v") + 1] == "copy"  # picture never re-encoded
    assert mux[mux.index("-af") + 1].startswith("volume=3.50dB,alimiter=")
    assert mux[mux.index("-map_chapters") + 1] == "-1"
    assert mux[mux.index("-ss") + 1] == "9.920" and mux[mux.index("-ss") + 5] == "norm.m4a"


def test_clip_gain_targets_minus_14_within_limits():
    assert ffmpeg.clip_gain_db(-18.2) == pytest.approx(4.2)
    assert ffmpeg.clip_gain_db(-11.0) == pytest.approx(-3.0)
    assert ffmpeg.clip_gain_db(-35.0) == 10.0  # never more than +/-10 dB
    assert ffmpeg.clip_gain_db(-70.0) == 0.0  # silence: leave it
    assert ffmpeg.clip_gain_db(None) == 0.0


# ---------------------------------------------------------------------------
# Real FFmpeg: sync + loudness vs the per-clip loudnorm path
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def flash_beep(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("fast_render")
    src = d / "src.mp4"
    chapters = d / "chapters.txt"
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=15000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=15000\nEND=30000\ntitle=Two\n"
    )
    flash = f"drawbox=x=0:y=0:w=320:h=180:color=white:t=fill:enable='lt(mod(t,{PERIOD}),0.12)'"
    beep = f"if(lt(mod(t,{PERIOD}),0.12),0.5*sin(2*PI*1000*t),0)+0.05*sin(2*PI*220*t)"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=black:s=320x180:r=25:d=30,{flash}",
         "-f", "lavfi", "-i", f"aevalsrc='{beep}':s=48000:d=30",
         "-i", str(chapters), "-map", "0:v", "-map", "1:a", "-map_chapters", "2",
         # keyframes every 2 s, so copy cuts really snap back to a keyframe
         "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(src)],
        check=True, capture_output=True,
    )  # fmt: skip
    norm = d / "norm.m4a"
    ffmpeg.run(ffmpeg.build_normalized_audio_cmd(str(src), str(norm)))
    return d, src, norm


def _render(d: Path, src: Path, start: float, normalized: Path | None) -> tuple[float, Path]:
    out = d / f"{'fast' if normalized else 'old'}_{start}.mp4"
    spec = RenderSpec(
        source_path=str(src), out_path=str(out), thumb_path=str(out.with_suffix(".jpg")),
        start_s=start, end_s=start + 12, preset=get_preset("facebook_video"),
        src_width=320, src_height=180, words=[], stream_copy_ok=True,
        normalized_audio=str(normalized) if normalized else None,
    )  # fmt: skip
    return render._render_horizontal(spec), out


def _av_offsets_ms(path: Path) -> list[float]:
    """Beep onset minus flash onset, for each flash in the clip."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vf", "signalstats,metadata=print",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    t = [float(x) for x in re.findall(r"pts_time:([\d.]+)", out)]
    y = [float(x) for x in re.findall(r"YAVG=([\d.]+)", out)]
    flashes = [t[i] for i in range(1, len(y)) if y[i] > 100 >= y[i - 1]]
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True,
    ).stdout
    env = np.convolve(np.abs(np.frombuffer(raw, np.float32)), np.ones(48) / 48, "same")
    thr = 0.5 * env.max()
    beeps = (np.flatnonzero((env[1:] > thr) & (env[:-1] <= thr)) + 1) / 48000
    return [(b - f) * 1000 for f in flashes for b in beeps if abs(b - f) < 0.5]


def _true_peak(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    return float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", out)[-1])


def _lufs(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", out)[-1])


@needs_ffmpeg
@pytest.mark.parametrize("start", [5.0, 7.3, 11.1])
def test_fast_cut_keeps_sync_and_safe_loudness(flash_beep, start):
    d, src, norm = flash_beep
    _, old = _render(d, src, start, None)
    _, fast = _render(d, src, start, norm)

    offsets = _av_offsets_ms(fast)
    assert len(offsets) >= 3
    assert all(abs(o) <= 45 for o in offsets), offsets  # within about one frame (40 ms)
    # Loudness as with per-clip loudnorm (this peaky beep signal misses -14 with
    # both paths; on a real film the new path lands closer to it), and peaks
    # kept safe (per-clip loudnorm left them near -0.7 dBTP).
    assert abs(_lufs(fast) - _lufs(old)) <= 1.0
    assert _true_peak(fast) <= -1.4
    assert (
        abs(ffmpeg.ffprobe_metadata(fast).duration_s - ffmpeg.ffprobe_metadata(old).duration_s)
        < 0.3
    )
    info = ffmpeg.ffprobe_metadata(fast)
    assert info.audio_tracks == 1 and info.video_codec == "h264"
    kinds = [st["codec_type"] for st in info.raw["streams"]]
    assert kinds == ["video", "audio"]  # the source's chapters are not copied


@needs_ffmpeg
def test_fast_cut_starts_on_the_keyframe(flash_beep):
    d, src, norm = flash_beep
    spec_start = 7.3
    out = d / "kf.mp4"
    spec = RenderSpec(
        source_path=str(src), out_path=str(out), thumb_path=str(out.with_suffix(".jpg")),
        start_s=spec_start, end_s=spec_start + 6, preset=get_preset("facebook_video"),
        src_width=320, src_height=180, words=[], stream_copy_ok=True, normalized_audio=str(norm),
    )  # fmt: skip
    assert render._render_horizontal(spec) == pytest.approx(6.0, abs=0.01)  # keyframes every 2 s


@needs_ffmpeg
def test_graded_clips_use_the_normalised_track_too(flash_beep, monkeypatch):
    d, src, norm = flash_beep
    used = []
    monkeypatch.setattr(render, "_copy_cut_with_normalized_audio", lambda s: used.append(s) or 0.0)
    base = dict(
        source_path=str(src), thumb_path=str(d / "x.jpg"), start_s=5.0, end_s=9.0,
        preset=get_preset("facebook_video"), src_width=320, src_height=180, words=[],
        stream_copy_ok=True,
    )  # fmt: skip
    graded = RenderSpec(out_path=str(d / "g.mp4"), normalized_audio=str(norm),
                        color_brightness=20, **base)  # fmt: skip
    render._render_horizontal(graded)
    render._render_horizontal(RenderSpec(out_path=str(d / "n.mp4"), **base))
    # A grade re-encodes the picture but no longer runs loudnorm per clip;
    # without the normalised track it is still the old per-clip path.
    assert used == [graded]


def test_graded_mux_reencodes_the_picture_only():
    mux = ffmpeg.build_mux_normalized_audio_cmd(
        "cut.mkv", "norm.m4a", "out.mp4", 9.92, 60.08, gain_db=1.0,
        video_filter="eq=contrast=1.2", preset="ultrafast",
    )  # fmt: skip
    assert mux[mux.index("-vf") + 1] == "eq=contrast=1.2"
    assert mux[mux.index("-c:v") + 1] == "libx264" and mux[mux.index("-preset") + 1] == "ultrafast"
    assert mux[mux.index("-af") + 1].startswith("volume=1.00dB,alimiter=")  # no loudnorm
    assert "loudnorm" not in " ".join(mux)


@needs_ffmpeg
@pytest.mark.parametrize("start", [5.0, 7.3])
def test_graded_fast_cut_matches_the_ungraded_clip(flash_beep, start):
    """Colour changes the picture only: same keyframe start, length, frame
    size, sync and loudness as the ungraded fast cut."""
    d, src, norm = flash_beep
    outs = {}
    for name, grade in (("plain", 0), ("graded", 30)):
        out = d / f"{name}_{start}.mp4"
        spec = RenderSpec(
            source_path=str(src), out_path=str(out), thumb_path=str(out.with_suffix(".jpg")),
            start_s=start, end_s=start + 12, preset=get_preset("facebook_video"),
            src_width=320, src_height=180, words=[], stream_copy_ok=True,
            normalized_audio=str(norm), color_contrast=grade, color_saturation=grade,
            encode_preset="ultrafast",
        )  # fmt: skip
        outs[name] = (render._render_horizontal(spec), out)
    (p_start, plain), (g_start, graded) = outs["plain"], outs["graded"]
    assert g_start == p_start  # same keyframe
    p, g = ffmpeg.ffprobe_metadata(plain), ffmpeg.ffprobe_metadata(graded)
    assert (g.width, g.height) == (p.width, p.height) == (320, 180)
    assert abs(g.duration_s - p.duration_s) < 0.1
    offsets = _av_offsets_ms(graded)
    assert len(offsets) >= 3 and all(abs(o) <= 45 for o in offsets), offsets
    assert abs(_lufs(graded) - _lufs(plain)) <= 0.2  # same audio


# ---------------------------------------------------------------------------
# Pipeline caches
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_normalized_audio_is_made_once(flash_beep, tmp_path, monkeypatch):
    _, src, _ = flash_beep
    target = tmp_path / "audio_norm.m4a"
    assert pipeline._ensure_normalized_audio(str(target), str(src)) == str(target)
    assert ffmpeg.ffprobe_metadata(target).audio_tracks == 1
    calls = []
    monkeypatch.setattr(pipeline.ffmpeg, "run", lambda cmd: calls.append(cmd))
    assert pipeline._ensure_normalized_audio(str(target), str(src)) == str(target)
    assert calls == []  # reused, not redone


def test_normalized_audio_failure_falls_back(tmp_path):
    target = tmp_path / "audio_norm.m4a"
    assert pipeline._ensure_normalized_audio(str(target), str(tmp_path / "missing.mp4")) is None
    assert list(tmp_path.iterdir()) == []  # no half-written track left behind


def test_scene_cuts_cache_respects_threshold():
    video = Video(filename="v.mp4", ff_metadata={"video_codec": "h264"})
    assert pipeline._cached_scene_cuts(video) is None
    video.ff_metadata = {
        **video.ff_metadata,
        "scene_cuts": {"threshold": analysis.SCENE_THRESHOLD, "cuts": [1.0, 2.5]},
    }
    assert pipeline._cached_scene_cuts(video) == [1.0, 2.5]
    video.ff_metadata = {
        "scene_cuts": {"threshold": 0.3, "cuts": [1.0]}
    }  # made with another setting
    assert pipeline._cached_scene_cuts(video) is None
