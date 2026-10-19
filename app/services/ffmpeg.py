from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration_s: float
    width: int
    height: int
    fps: float
    audio_tracks: int
    raw: dict
    video_codec: str | None = None
    pix_fmt: str | None = None


# Keep exactly one video + the first audio track; drop the source's own
# subtitle/data/chapter streams (movies often carry several, which would
# otherwise leak into every clip).
_MAP_ARGS = ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_chapters", "-1"]

# Downmix to stereo BEFORE loudnorm: 5.1 movie audio costs 3x the work otherwise,
# and social platforms expect stereo. loudnorm resamples internally, so resample
# back to 48 kHz afterwards.
AUDIO_NORM_FILTER = "aformat=channel_layouts=stereo,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def ffprobe_metadata(path: str | Path) -> MediaInfo:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {proc.stderr.strip()}")
    raw = json.loads(proc.stdout)
    return _parse_probe(raw)


def _parse_probe(raw: dict) -> MediaInfo:
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_tracks = sum(1 for s in streams if s.get("codec_type") == "audio")
    duration = float(raw.get("format", {}).get("duration", 0.0) or 0.0)

    width = height = 0
    fps = 0.0
    codec = pix_fmt = None
    if video is not None:
        width = int(video.get("width", 0) or 0)
        height = int(video.get("height", 0) or 0)
        if not duration:
            duration = float(video.get("duration", 0.0) or 0.0)
        fps = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
        codec = video.get("codec_name")
        pix_fmt = video.get("pix_fmt")

    return MediaInfo(
        duration_s=duration,
        width=width,
        height=height,
        fps=fps,
        audio_tracks=audio_tracks,
        raw=raw,
        video_codec=codec,
        pix_fmt=pix_fmt,
    )


def _parse_fraction(frac: str) -> float:
    try:
        num, _, den = frac.partition("/")
        den_f = float(den) if den else 1.0
        return float(num) / den_f if den_f else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def can_stream_copy(video_codec: str | None, pix_fmt: str | None) -> bool:
    """Whether the source video can be cut without re-encoding and still play
    everywhere (browsers, Facebook, YouTube). 8-bit 4:2:0 H.264 only — HEVC and
    10-bit H.264 don't play reliably in browsers, so those get re-encoded."""
    return video_codec == "h264" and pix_fmt == "yuv420p"


def media_start_time(path: str | Path) -> float:
    """Start time of a file cut with ``build_copy_remux_cmd`` (which keeps the
    source timestamps), i.e. where the clip really begins in the source.
    Prefers the audio track, since captions must line up with speech."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,start_time:format=start_time",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    by_type = {s.get("codec_type"): s for s in data.get("streams", [])}
    for kind in ("audio", "video"):
        if by_type.get(kind, {}).get("start_time") not in (None, "N/A"):
            return float(by_type[kind]["start_time"])
    return float(data.get("format", {}).get("start_time") or 0.0)


# ---------------------------------------------------------------------------
# Command builders (pure functions -> unit-testable)
# ---------------------------------------------------------------------------
def build_audio_chunk_cmd(
    src: str, dst: str, start_s: float, duration_s: float, bitrate: str = "64k"
) -> list[str]:
    """Compressed mono MP3 [start, start+duration] for the transcription API.
    Used once per video to extract the whole audio track."""
    return [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", src,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        dst,
    ]  # fmt: skip


def build_audio_copy_chunk_cmd(src: str, dst: str, start_s: float, duration_s: float) -> list[str]:
    """Slice an already-extracted MP3 without re-encoding (near-instant)."""
    return [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", src,
        "-c", "copy",
        dst,
    ]  # fmt: skip


def build_scene_detect_cmd(src: str, metadata_path: str) -> list[str]:
    """Score scene changes by decoding ONLY keyframes (-skip_frame nokey).

    Decoding every frame of a long HD video takes minutes; keyframes are a few
    percent of frames, so this takes seconds. Each keyframe's scene score
    (difference from the previous keyframe) is written to ``metadata_path``."""
    safe = metadata_path.replace("\\", "\\\\").replace(":", "\\:")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-skip_frame", "nokey",
        "-i", src,
        "-an", "-sn", "-dn",
        "-vf", f"scale=320:-2,select='gte(scene,0)',metadata=print:file={safe}",
        "-fps_mode", "passthrough",
        "-f", "null", "-",
    ]  # fmt: skip


_SCENE_RE = re.compile(r"pts_time:([\d.]+)\s+lavfi\.scene_score=([\d.]+)")


def parse_scene_scores(text: str) -> list[tuple[float, float]]:
    """Parse ``metadata=print`` output into [(pts_time, scene_score), ...]."""
    return [(float(t), float(s)) for t, s in _SCENE_RE.findall(text)]


def build_frame_grab_cmd(
    src: str, start_s: float, duration_s: float, width: int, height: int
) -> list[str]:
    """Raw RGB frames of every keyframe in [start, start+duration], scaled to
    width x height. Used for face detection without decoding every frame."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-skip_frame", "nokey",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", src,
        "-an", "-sn", "-dn",
        "-vf", f"scale={width}:{height}",
        "-pix_fmt", "rgb24",
        "-fps_mode", "passthrough",
        "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip


def build_single_frame_cmd(src: str, at_s: float, width: int, height: int) -> list[str]:
    """One raw RGB frame at ``at_s`` (fallback when a clip has no keyframe)."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at_s:.3f}",
        "-i", src,
        "-an", "-sn", "-dn",
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}",
        "-pix_fmt", "rgb24",
        "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip


def reframe_filter(
    src_w: int, src_h: int, out_w: int, out_h: int, focus_x: float | None = None
) -> str:
    """Build a crop+scale filter turning a source frame into out_w x out_h.

    For 16:9 -> 9:16 we crop a vertical window (optionally centred on focus_x,
    the subject's horizontal centre in source pixels) then scale to target.
    """
    target_ar = out_w / out_h
    src_ar = src_w / src_h

    if abs(src_ar - target_ar) < 1e-3:
        return f"scale={out_w}:{out_h}"

    if src_ar > target_ar:
        # Source is wider: crop horizontally.
        crop_w = max(2, int(round(src_h * target_ar)))
        crop_w -= crop_w % 2
        if focus_x is None:
            x_expr = "(in_w-out_w)/2"
        else:
            half = crop_w / 2
            x = min(max(focus_x, half), src_w - half)
            x_expr = str(int(round(x - half)))
        return f"crop={crop_w}:{src_h}:{x_expr}:0,scale={out_w}:{out_h}"
    else:
        # Source is taller: crop vertically (centre).
        crop_h = max(2, int(round(src_w / target_ar)))
        crop_h -= crop_h % 2
        return f"crop={src_w}:{crop_h}:0:(in_h-out_h)/2,scale={out_w}:{out_h}"


def fit_filter(src_w: int, src_h: int, box_w: int, box_h: int) -> str:
    """Scale to fit inside box_w x box_h keeping the source aspect ratio — no
    cropping, so widescreen movies keep their full frame."""
    scale = min(box_w / src_w, box_h / src_h)
    w = max(2, int(round(src_w * scale / 2)) * 2)
    h = max(2, int(round(src_h * scale / 2)) * 2)
    return f"scale={w}:{h}"


def build_render_cmd(
    src: str,
    dst: str,
    start_s: float,
    end_s: float,
    video_filter: str | None,
    *,
    loudnorm: bool = True,
    fps: int | None = None,
    preset: str = "veryfast",
) -> list[str]:
    """Cut [start,end], apply the video filter chain, normalise audio, encode
    H.264/AAC with faststart."""
    duration = max(0.0, end_s - start_s)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration:.3f}",
        "-i", src,
        *_MAP_ARGS,
    ]  # fmt: skip

    filters = []
    if video_filter:
        filters.append(video_filter)
    if fps:
        filters.append(f"fps={fps}")
    if filters:
        cmd += ["-vf", ",".join(filters)]

    cmd += ["-af", AUDIO_NORM_FILTER] if loudnorm else ["-ac", "2"]

    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        dst,
    ]  # fmt: skip
    return cmd


def build_copy_remux_cmd(src: str, dst_mkv: str, start_s: float, end_s: float) -> list[str]:
    """Step 1 of a no-re-encode cut: copy video AND audio (~0.1s).

    A copied clip must begin on a keyframe, so it starts at the keyframe at or
    before ``start_s``. Copying both streams keeps them in sync; re-encoding
    the audio in this same step does not — ffmpeg then trims the audio to its
    own start/length, giving a silent tail and misaligned captions. Source
    timestamps are kept (-copyts) so :func:`media_start_time` can report where
    the clip really starts. Write to .mkv; step 2 produces the final MP4."""
    duration = max(0.0, end_s - start_s)
    return [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration:.3f}",
        "-i", src,
        *_MAP_ARGS,
        "-c", "copy",
        "-copyts",
        dst_mkv,
    ]  # fmt: skip


def build_audio_normalize_cmd(src_mkv: str, dst: str) -> list[str]:
    """Step 2 of a no-re-encode cut: keep the video as-is, normalise the audio
    to stereo/loudnorm/AAC, and write a web-ready MP4 starting at t=0."""
    return [
        "ffmpeg", "-y",
        "-i", src_mkv,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy",
        "-af", AUDIO_NORM_FILTER,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        dst,
    ]  # fmt: skip


def build_thumbnail_cmd(src: str, dst: str, at_s: float = 0.0) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-ss", f"{at_s:.3f}",
        "-i", src,
        "-frames:v", "1",
        "-q:v", "3",
        dst,
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def run(cmd: list[str], *, timeout: int | None = None) -> None:
    log.info("ffmpeg_run", extra={"cmd": cmd[0], "args_len": len(cmd)})
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-5:]
        raise FFmpegError(f"{cmd[0]} failed (rc={proc.returncode}): {' / '.join(tail)}")


def run_capture(cmd: list[str], *, timeout: int | None = None) -> bytes:
    """Run and return raw stdout bytes (e.g. rawvideo frames)."""
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-5:]
        raise FFmpegError(f"{cmd[0]} failed (rc={proc.returncode}): {' / '.join(tail)}")
    return proc.stdout
