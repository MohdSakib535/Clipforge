from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config.platforms import PlatformPreset
from app.core.logging import get_logger
from app.services import ffmpeg, reframe
from app.services.captions import write_srt

log = get_logger(__name__)


@dataclass
class RenderSpec:
    source_path: str
    out_path: str
    thumb_path: str
    start_s: float
    end_s: float
    preset: PlatformPreset
    src_width: int
    src_height: int
    words: list[dict]  # absolute-time word dicts for the whole video
    captions_enabled: bool = True
    # x264 preset for clips that must be re-encoded.
    encode_preset: str = "veryfast"
    # Face-detection samples per clip during reframe (fewer = faster).
    reframe_samples: int = 8
    # Source video can be cut without re-encoding (see ffmpeg.can_stream_copy).
    stream_copy_ok: bool = False


def srt_path_for(clip_path: str) -> str:
    return str(Path(clip_path).with_suffix(".srt"))


def _clip_relative_words(words: list[dict], start: float, end: float) -> list[dict]:
    out = []
    for w in words:
        if w["end"] <= start or w["start"] >= end:
            continue
        out.append(
            {
                "word": w["word"],
                "start": max(0.0, w["start"] - start),
                "end": max(0.0, w["end"] - start),
            }
        )
    return out


def render_clip(spec: RenderSpec) -> None:
    """Render one clip, its thumbnail and (optionally) a sidecar .srt.
    Raises FFmpegError on failure.

    Captions are never burned into the video; they go in a separate .srt file.

    Vertical (Shorts/Reels): crop around the speaker and re-encode — the crop
    makes re-encoding unavoidable.
    Horizontal: keep the full frame and cut without re-encoding whenever the
    source allows it. A copy cut starts on the keyframe at/before ``start_s``
    (typically 0.5-2s early); ``start_s`` itself is left as requested so
    repeated re-renders don't creep earlier.
    """
    Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
    if spec.preset.is_vertical:
        start = _render_vertical(spec)
    else:
        start = _render_horizontal(spec)
    _write_captions(spec, start)
    ffmpeg.run(ffmpeg.build_thumbnail_cmd(spec.out_path, spec.thumb_path, at_s=1.0))


def _render_vertical(spec: RenderSpec) -> float:
    p = spec.preset
    focus = None
    if p.reframe:
        focus = reframe.focus_x_for_clip(
            spec.source_path,
            spec.start_s,
            spec.end_s,
            spec.src_width,
            spec.src_height,
            samples=spec.reframe_samples,
        )
    vf = ffmpeg.reframe_filter(spec.src_width, spec.src_height, p.width, p.height, focus)
    ffmpeg.run(
        ffmpeg.build_render_cmd(
            spec.source_path,
            spec.out_path,
            spec.start_s,
            spec.end_s,
            vf,
            preset=spec.encode_preset,
        )
    )
    return spec.start_s


def _render_horizontal(spec: RenderSpec) -> float:
    if spec.stream_copy_ok:
        with tempfile.TemporaryDirectory() as tmp:
            cut = str(Path(tmp) / "cut.mkv")
            ffmpeg.run(ffmpeg.build_copy_remux_cmd(spec.source_path, cut, spec.start_s, spec.end_s))
            # Where the clip really begins (a keyframe) — captions follow that.
            start = ffmpeg.media_start_time(cut)
            ffmpeg.run(ffmpeg.build_audio_normalize_cmd(cut, spec.out_path))
        return start
    vf = ffmpeg.fit_filter(spec.src_width, spec.src_height, spec.preset.width, spec.preset.height)
    ffmpeg.run(
        ffmpeg.build_render_cmd(
            spec.source_path,
            spec.out_path,
            spec.start_s,
            spec.end_s,
            vf,
            preset=spec.encode_preset,
        )
    )
    return spec.start_s


def _write_captions(spec: RenderSpec, clip_start: float) -> None:
    srt_path = srt_path_for(spec.out_path)
    words = _clip_relative_words(spec.words, clip_start, spec.end_s)
    if spec.captions_enabled and words:
        write_srt(srt_path, words)
    else:
        Path(srt_path).unlink(missing_ok=True)  # captions turned off since last render
