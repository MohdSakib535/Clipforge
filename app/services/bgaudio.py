"""Background music under a clip.

Uploaded tracks are converted once to stereo AAC at the same loudness as the
clips' own audio (-14 LUFS), so the volume setting means the same thing for
every track: 100% = as loud as the speech, 25% ≈ 12 dB quieter.

Mixing never re-encodes the video (stream copy) - only the audio, so it takes
a second or two per clip. The clip as rendered is kept next to it
(``clip.nomusic.mp4``) while music is on, so music can be changed or removed
without re-rendering. Invariant: that file exists <=> ``clip.mp4`` has music.
"""

from __future__ import annotations

import fcntl
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.services import ffmpeg

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
UNMIXED_NAME = "clip.nomusic.mp4"
DEFAULT_VOLUME = 40

_FADE_IN_S = 0.5
_FADE_OUT_S = 1.5
_STEREO = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


# ---------------------------------------------------------------------------
# Command builders (pure functions -> unit-testable)
# ---------------------------------------------------------------------------
def build_prepare_track_cmd(src: str, dst_m4a: str) -> list[str]:
    """Uploaded audio -> stereo 48 kHz AAC at the clips' loudness."""
    return [
        "ffmpeg", "-y",
        "-i", src,
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", ffmpeg.AUDIO_NORM_FILTER,
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        dst_m4a,
    ]  # fmt: skip


def build_mix_cmd(
    clip: str,
    music: str,
    dst: str,
    *,
    volume_pct: int,
    duration_s: float,
    clip_has_audio: bool = True,
) -> list[str]:
    """Clip + looping music -> new MP4. The video is copied as-is; the music
    fades in/out and a limiter stops speech + music from clipping."""
    fade_out = min(_FADE_OUT_S, duration_s / 4)
    music_chain = (
        f"[1:a]{_STEREO},volume={volume_pct / 100:.2f},"
        f"afade=t=in:d={_FADE_IN_S},"
        f"afade=t=out:st={max(0.0, duration_s - fade_out):.3f}:d={fade_out:.3f}"
    )
    if clip_has_audio:
        graph = (
            f"{music_chain}[bg];[0:a]{_STEREO}[fg];"
            "[fg][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95:level=0[a]"
        )
    else:
        graph = f"{music_chain}[a]"
    return [
        "ffmpeg", "-y",
        "-i", clip,
        "-stream_loop", "-1", "-i", music,
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration_s:.3f}",
        "-movflags", "+faststart",
        dst,
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def prepare_track(src: str | Path, dst: str | Path) -> ffmpeg.MediaInfo:
    """Convert an uploaded file; raises FFmpegError if it isn't usable audio."""
    ffmpeg.run(build_prepare_track_cmd(str(src), str(dst)))
    info = ffmpeg.ffprobe_metadata(dst)
    if info.audio_tracks < 1 or info.duration_s <= 0:
        raise ffmpeg.FFmpegError("no audio found")
    return info


@contextmanager
def clip_lock(clip_path: str | Path) -> Iterator[None]:
    """Serialise work on one clip's files across workers (re-render vs. mix)."""
    lock = Path(clip_path).with_name(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def apply(
    clip_path: str | Path,
    music_path: str | None,
    volume_pct: int,
    *,
    mute_original: bool = False,
) -> None:
    """Put ``music_path`` under the clip at ``volume_pct``; ``None`` removes it.
    ``mute_original`` drops the clip's own audio so only the music plays."""
    with clip_lock(clip_path):
        _apply_unlocked(Path(clip_path), music_path, volume_pct, mute_original)


def after_render(
    clip_path: str | Path,
    music_path: str | None,
    volume_pct: int,
    *,
    mute_original: bool = False,
) -> None:
    """Call (holding ``clip_lock``) after a fresh render wrote ``clip_path``:
    the old music-free copy is stale, then music is added again if set."""
    clip = Path(clip_path)
    clip.with_name(UNMIXED_NAME).unlink(missing_ok=True)
    if music_path:
        _apply_unlocked(clip, music_path, volume_pct, mute_original)


def _apply_unlocked(
    clip: Path, music_path: str | None, volume_pct: int, mute_original: bool = False
) -> None:
    unmixed = clip.with_name(UNMIXED_NAME)
    if music_path is None:
        if unmixed.exists():
            os.replace(unmixed, clip)
        return

    src = unmixed if unmixed.exists() else clip
    info = ffmpeg.ffprobe_metadata(src)
    tmp = clip.with_name(f".mix-{uuid.uuid4().hex}.mp4")
    try:
        ffmpeg.run(
            build_mix_cmd(
                str(src),
                music_path,
                str(tmp),
                volume_pct=volume_pct,
                duration_s=info.duration_s,
                clip_has_audio=info.audio_tracks > 0 and not mute_original,
            )
        )
        # Only touch the real files once the mix succeeded.
        if src == clip:
            os.replace(clip, unmixed)
        os.replace(tmp, clip)
    finally:
        tmp.unlink(missing_ok=True)
