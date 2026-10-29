"""The strip of frames behind "Clip a video"'s trim bar, so the user sees
where things happen while dragging the start and end."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from app.services import ffmpeg

FRAMES = 12  # spread evenly over the video
WIDTH, HEIGHT = 160, 90  # each frame, cropped to fill


def make(src: str, dst: Path, duration_s: float) -> Path:
    """``FRAMES`` frames of the video side by side in one JPEG at ``dst``.
    One ffmpeg run per frame (a fast seek each), so a long or 4K film still
    takes only a few seconds and little memory."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for i in range(FRAMES):
            frame = str(Path(tmp) / f"{i:02d}.jpg")
            at = duration_s * (i + 0.5) / FRAMES  # the middle of each slice
            ffmpeg.run(ffmpeg.build_strip_frame_cmd(src, frame, at, WIDTH, HEIGHT), timeout=60)
            frames.append(frame)
        part = dst.with_name(f"{dst.stem}.part-{uuid.uuid4().hex}.jpg")
        try:
            ffmpeg.run(ffmpeg.build_hstack_cmd(frames, str(part)), timeout=60)
            os.replace(part, dst)  # never a half-written strip
        finally:
            part.unlink(missing_ok=True)
    return dst
