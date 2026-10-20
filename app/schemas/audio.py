from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.clip import ClipOut


class AudioTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: str
    size_bytes: int | None = None
    duration_s: float | None = None
    error: str | None = None
    source_url: str | None = None
    progress: int | None = None
    created_at: datetime


class AudioFromUrl(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class BackgroundAudioApply(BaseModel):
    """Put a track under some or all of a job's clips (or remove it)."""

    audio_id: str | None = Field(description="Track to add; null removes the music.")
    volume: int = Field(default=40, ge=0, le=100, description="100 = as loud as the speech.")
    mute_original: bool = Field(
        default=False, description="Drop the clip's own audio so only the music plays."
    )
    clip_ids: list[str] | None = Field(
        default=None, description="Clips to change; omit for every ready clip of the job."
    )


class BackgroundAudioResult(BaseModel):
    queued: int
    skipped: int  # still rendering / failed / unchanged
    clips: list[ClipOut]  # the queued clips, now "rendering"
