from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.platforms import get_preset
from app.models.enums import JobStage, JobStatus


class CaptionSettings(BaseModel):
    enabled: bool = True
    language: str | None = None  # None => auto-detect


MAX_TRIMS = 20


class Trim(BaseModel):
    """A part of the video picked by hand ("Reel from a video")."""

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be > start_s")
        return self


class SourceRange(Trim):
    """The stretch of the video "Clip a video" cuts into clips (its Trim step)."""


def _known_platforms(v: list[str]) -> list[str]:
    for p in v:
        try:
            get_preset(p)
        except KeyError as exc:
            raise ValueError(str(exc)) from None
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in v:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


Platforms = Annotated[list[str], Field(min_length=1), AfterValidator(_known_platforms)]


class JobCreate(BaseModel):
    platforms: Platforms
    # Per-platform target seconds per clip, e.g. {"facebook_reel": 40}. Each
    # platform's video is tiled end-to-end into ~N-second clips covering the
    # whole video; the count is derived, not asked. Missing: 40 s vertical,
    # 3 min horizontal.
    coverage_lengths: dict[str, int] = Field(default_factory=dict)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)
    # False: skip speech-to-text. Much faster and free; parts are cut on time
    # at scene changes, titled "Part N", with no caption file.
    transcribe: bool = True
    # Given: make exactly these parts (one clip each per platform) instead of
    # cutting the whole video. With transcription on, only these seconds are
    # transcribed. More can be added later with POST /jobs/{id}/trims.
    trims: list[Trim] | None = Field(default=None, min_length=1, max_length=MAX_TRIMS)
    # Given: only this stretch of the video is cut into clips; with
    # transcription on, only it is transcribed (unless the whole video already
    # was). Omitted: the whole video.
    source_range: SourceRange | None = None

    @model_validator(mode="after")
    def _no_captions_without_transcript(self):
        if not self.transcribe:
            self.captions.enabled = False  # captions come from the transcript
        return self

    @model_validator(mode="after")
    def _range_or_trims(self):
        if self.trims and self.source_range:
            raise ValueError("give trims or source_range, not both")
        return self

    @field_validator("coverage_lengths")
    @classmethod
    def _valid_coverage(cls, v: dict[str, int]) -> dict[str, int]:
        for platform, seconds in v.items():
            try:
                preset = get_preset(platform)
            except KeyError as exc:
                raise ValueError(str(exc)) from None
            if not (preset.length_min_s <= int(seconds) <= preset.length_max_s):
                raise ValueError(
                    f"coverage length for {platform} must be "
                    f"{preset.length_min_s}-{preset.length_max_s}s"
                )
        return v


class TrimsAdd(BaseModel):
    """More hand-picked parts for a job made from trims."""

    trims: list[Trim] = Field(min_length=1, max_length=MAX_TRIMS)
    platforms: Platforms
    transcribe: bool = True


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    status: JobStatus
    stage: JobStage
    progress: int
    platforms: list[str]
    settings: dict
    error: str | None = None
    audio_minutes: float
    models_used: dict
    estimated_cost_usd: float
    created_at: datetime
    updated_at: datetime


class JobProgress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: JobStatus
    stage: JobStage
    progress: int
    error: str | None = None
    estimated_cost_usd: float
