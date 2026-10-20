from __future__ import annotations

import enum


class VideoStatus(str, enum.Enum):
    uploading = "uploading"
    downloading = "downloading"  # being imported from a link
    uploaded = "uploaded"
    ingesting = "ingesting"
    ready = "ready"
    failed = "failed"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class JobStage(str, enum.Enum):
    """Ordered pipeline stages, surfaced to the progress screen."""

    queued = "queued"
    transcribing = "transcribing"
    analyzing = "analyzing"
    planning_clips = "planning_clips"  # cutting the video into whole-video parts
    cutting = "cutting"
    reframing = "reframing"
    captioning = "captioning"
    rendering = "rendering"
    finalizing = "finalizing"
    done = "done"


class ClipStatus(str, enum.Enum):
    pending = "pending"
    rendering = "rendering"
    ready = "ready"
    approved = "approved"
    failed = "failed"
    deleted = "deleted"
