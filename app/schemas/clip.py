from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ClipStatus

__all__ = ["ClipOut", "ClipUpdate"]


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    platform: str
    start_s: float
    end_s: float
    duration_s: float | None = None
    score: float | None = None
    hook_strength: float | None = None
    standalone: bool | None = None
    reason: str | None = None
    title: str | None = None
    description: str | None = None
    hashtags: list[str] = []
    captions_enabled: bool
    caption_style: str | None = None
    language: str | None = None
    status: ClipStatus
    rank: int | None = None
    error: str | None = None
    has_file: bool = False
    has_thumbnail: bool = False
    has_srt: bool = False


class ClipUpdate(BaseModel):
    """Editable fields from the review dashboard. All optional (PATCH)."""

    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = Field(default=None, max_length=512)
    description: str | None = None
    hashtags: list[str] | None = None
    captions_enabled: bool | None = None
    status: ClipStatus | None = None

    @model_validator(mode="after")
    def _check_times(self):
        if self.start_s is not None and self.end_s is not None:
            if self.end_s <= self.start_s:
                raise ValueError("end_s must be > start_s")
        return self
