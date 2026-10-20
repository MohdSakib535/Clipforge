from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ClipStatus

__all__ = ["ClipOut", "ClipUpdate", "ColorGrade", "ColorGradeApply", "ColorGradeResult"]


class ColorGrade(BaseModel):
    contrast: int = 0
    brightness: int = 0
    saturation: int = 0


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    platform: str
    start_s: float
    end_s: float
    duration_s: float | None = None
    title: str | None = None
    description: str | None = None
    hashtags: list[str] = []
    captions_enabled: bool
    color_contrast: int = 0
    color_brightness: int = 0
    color_saturation: int = 0
    # The grade the clip file actually has (the three above may be saved but
    # not applied yet). None: rendered before this was recorded.
    color_rendered: ColorGrade | None = None
    caption_style: str | None = None
    language: str | None = None
    status: ClipStatus
    rank: int | None = None
    error: str | None = None
    has_file: bool = False
    file_version: int | None = None
    has_thumbnail: bool = False
    has_srt: bool = False
    has_speech: bool | None = None
    bg_audio_id: str | None = None
    bg_audio_volume: int = 40
    bg_audio_mute_original: bool = False


class ClipUpdate(BaseModel):
    """Editable fields from the review dashboard. All optional (PATCH)."""

    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = Field(default=None, max_length=512)
    description: str | None = None
    hashtags: list[str] | None = None
    captions_enabled: bool | None = None
    color_contrast: int | None = Field(default=None, ge=-50, le=50)
    color_brightness: int | None = Field(default=None, ge=-50, le=50)
    color_saturation: int | None = Field(default=None, ge=-50, le=50)
    status: ClipStatus | None = None

    @model_validator(mode="after")
    def _check_times(self):
        if self.start_s is not None and self.end_s is not None:
            if self.end_s <= self.start_s:
                raise ValueError("end_s must be > start_s")
        return self


class ColorGradeApply(BaseModel):
    """Apply one colour grade to some or all of a job's clips (each 0 = reset)."""

    contrast: int = Field(default=0, ge=-50, le=50)
    brightness: int = Field(default=0, ge=-50, le=50)
    saturation: int = Field(default=0, ge=-50, le=50)
    clip_ids: list[str] | None = Field(
        default=None, description="Clips to change; omit for every ready clip of the job."
    )


class ColorGradeResult(BaseModel):
    queued: int
    skipped: int  # still rendering / failed / unchanged
    clips: list[ClipOut]  # the queued clips, now "rendering"
