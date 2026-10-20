from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.platforms import get_preset
from app.models.enums import JobStage, JobStatus


class CaptionSettings(BaseModel):
    enabled: bool = True
    language: str | None = None  # None => auto-detect


class JobCreate(BaseModel):
    platforms: list[str] = Field(min_length=1)
    # Per-platform target seconds per clip, e.g. {"facebook_reel": 40}. Each
    # platform's video is tiled end-to-end into ~N-second clips covering the
    # whole video; the count is derived, not asked. Missing: 40 s vertical,
    # 3 min horizontal.
    coverage_lengths: dict[str, int] = Field(default_factory=dict)
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
