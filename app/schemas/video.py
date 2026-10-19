from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VideoStatus


class UploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)


class UploadInitOut(BaseModel):
    video_id: str
    upload_key: str
    chunk_size: int


class UrlProbe(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class UrlImport(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    # "best", or the frame's short side in pixels ("1080", "720", ...).
    quality: str = Field(default="best", pattern=r"^(best|\d{3,4})$")


class QualityOptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    height: int | None = None
    approx_size_bytes: int | None = None


class UrlProbeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    title: str
    duration_s: float | None = None
    thumbnail: str | None = None
    site: str | None = None
    qualities: list[QualityOptionOut]


class RightsConfirm(BaseModel):
    """User must affirm they own or are licensed to edit/publish the video."""

    confirmed: bool = Field(description="Must be true to proceed.")


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: VideoStatus
    size_bytes: int | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_tracks: int | None = None
    rights_confirmed_at: datetime | None = None
    error: str | None = None
    source_url: str | None = None
    download_progress: int | None = None
    created_at: datetime
