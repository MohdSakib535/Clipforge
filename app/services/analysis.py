from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.logging import get_logger
from app.services import ffmpeg

log = get_logger(__name__)


SCENE_THRESHOLD = 0.12


def detect_scenes(video_path: str, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Scene-change timestamps, found by comparing consecutive keyframes only.

    Runs directly on the source — no proxy needed. On a 104-min 1080p film this
    takes ~18s, versus several minutes to decode every frame.

    ``threshold`` is ffmpeg's scene score (0-1). Keyframes are a few seconds
    apart, so scores run much lower than frame-to-frame (median ~0.04 on a
    feature film); 0.12 gives roughly one cut every ~25s, while the usual 0.3
    finds almost none.
    """
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / "scenes.txt"
        ffmpeg.run(ffmpeg.build_scene_detect_cmd(video_path, str(meta)))
        text = meta.read_text() if meta.exists() else ""
    cuts = [round(t, 3) for t, score in ffmpeg.parse_scene_scores(text) if score > threshold]
    log.info("scene_detect", extra={"cuts": len(cuts)})
    return cuts
