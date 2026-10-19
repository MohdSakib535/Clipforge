from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.platforms import get_preset
from app.models.enums import JobStage, JobStatus


class CaptionSettings(BaseModel):
    enabled: bool = True
    language: str | None = None  # None => auto-detect


class LengthOverride(BaseModel):
    """Optional per-platform override of the preset's clip length window."""

    min_s: int = Field(gt=0)
    max_s: int = Field(gt=0)

    @field_validator("max_s")
    @classmethod
    def _max_ge_min(cls, v: int, info):
        mn = info.data.get("min_s")
        if mn is not None and v < mn:
            raise ValueError("max_s must be >= min_s")
        return v


class JobCreate(BaseModel):
    platforms: list[str] = Field(min_length=1)
    # Job-wide default clip count; used for any platform not in clip_counts.
    clip_count: int = Field(default=5, ge=1, le=20)
    # Per-platform clip count (Top-highlights mode), e.g. {"facebook_reel": 8}.
    clip_counts: dict[str, int] = Field(default_factory=dict)
    # Per-platform target seconds per clip (Cover-whole-video mode), e.g.
    # {"facebook_reel": 40}. A platform listed here is tiled end-to-end into
    # ~N-second clips covering the whole video; the count is derived, not asked.
    coverage_lengths: dict[str, int] = Field(default_factory=dict)
    length_overrides: dict[str, LengthOverride] = Field(default_factory=dict)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, v: list[str]) -> list[str]:
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

    @field_validator("clip_counts")
    @classmethod
    def _valid_counts(cls, v: dict[str, int]) -> dict[str, int]:
        for platform, count in v.items():
            try:
                get_preset(platform)
            except KeyError as exc:
                raise ValueError(str(exc)) from None
            if not (1 <= int(count) <= 20):
                raise ValueError(f"clip count for {platform} must be between 1 and 20")
        return v

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
    llm_tokens_in: int
    llm_tokens_out: int
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
