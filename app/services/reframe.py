from __future__ import annotations

from app.core.logging import get_logger
from app.services import ffmpeg

log = get_logger(__name__)

# Face detection runs on small frames; 640px wide is plenty for MediaPipe.
_GRAB_WIDTH = 640


def _grab_frames(
    video_path: str, start_s: float, end_s: float, src_w: int, src_h: int, samples: int
) -> tuple[list, int]:
    """Decode up to ``samples`` small RGB frames from the clip window, reading
    keyframes only. Returns (frames, frame_width)."""
    import numpy as np

    w = _GRAB_WIDTH
    h = max(2, int(round(w * src_h / src_w / 2)) * 2)
    frame_bytes = w * h * 3

    raw = ffmpeg.run_capture(
        ffmpeg.build_frame_grab_cmd(video_path, start_s, max(0.1, end_s - start_s), w, h)
    )
    if len(raw) < frame_bytes:
        # Clip shorter than one keyframe interval: grab a single mid-clip frame.
        raw = ffmpeg.run_capture(
            ffmpeg.build_single_frame_cmd(video_path, (start_s + end_s) / 2, w, h)
        )
    n = len(raw) // frame_bytes
    if n == 0:
        return [], w
    frames = np.frombuffer(raw[: n * frame_bytes], dtype=np.uint8).reshape(n, h, w, 3)
    if n > samples:
        idx = np.linspace(0, n - 1, samples).round().astype(int)
        frames = frames[idx]
    return list(frames), w


def focus_x_for_clip(
    video_path: str,
    start_s: float,
    end_s: float,
    src_width: int,
    src_height: int,
    *,
    samples: int = 8,
) -> float | None:
    """Estimate the horizontal centre (in source pixels) to keep in frame when
    cropping 16:9 -> 9:16, using MediaPipe face detection on frames sampled
    across the clip. Returns the median face-centre x, or None if no face is
    found (caller falls back to a centre crop)."""
    try:
        import mediapipe as mp
    except Exception as exc:  # pragma: no cover - only when deps missing
        log.warning("reframe_deps_missing", extra={"error": str(exc)})
        return None

    frames, frame_w = _grab_frames(video_path, start_s, end_s, src_width, src_height, samples)
    if not frames:
        return None

    centers: list[float] = []
    # model_selection=0 (short-range). The full-range model (1) returns no
    # detections at all with mediapipe 0.10.18 in this image, even on large,
    # well-lit faces — it silently disabled face tracking.
    with mp.solutions.face_detection.FaceDetection(
        model_selection=0, min_detection_confidence=0.5
    ) as detector:
        for frame in frames:
            result = detector.process(frame)
            if not result.detections:
                continue
            best = max(result.detections, key=lambda d: d.location_data.relative_bounding_box.width)
            box = best.location_data.relative_bounding_box
            cx = (box.xmin + box.width / 2.0) * frame_w
            centers.append(cx * src_width / frame_w)

    if not centers:
        return None

    centers.sort()
    mid = centers[len(centers) // 2]
    log.info("reframe_focus", extra={"samples": len(centers), "focus_x": round(mid, 1)})
    return mid
