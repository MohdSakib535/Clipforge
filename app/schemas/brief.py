from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.brief_script import DEFAULT_DURATION_S, MAX_BRIEF, MAX_PART
from app.services.reel_script import MAX_DESCRIPTION, MAX_DURATION_S, MIN_DURATION_S


class BriefCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(
        default=None, max_length=120, description="Omit: its first line is the name."
    )
    content: str = Field(min_length=20, max_length=MAX_BRIEF, description="The whole pasted brief.")


class BriefUpdate(BaseModel):
    """Omit what stays. The three parts are what scripts and pictures use."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=20, max_length=MAX_BRIEF)
    niche: str | None = Field(default=None, max_length=MAX_PART)
    rules: str | None = Field(default=None, min_length=1, max_length=MAX_PART)
    visual_style: str | None = Field(default=None, min_length=1, max_length=MAX_PART)


class BriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    content: str
    niche: str
    rules: str
    visual_style: str
    created_at: datetime
    updated_at: datetime


class BriefReelCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    topic: str = Field(default="", max_length=500, description="Empty: the writer picks one.")
    platform: str = Field(description='"instagram" or "facebook"')
    language: str = Field(description="A key from GET /reels/options")
    voice: str | None = Field(default=None, description="Omit for the language's first voice.")
    duration_s: int = Field(default=DEFAULT_DURATION_S, ge=MIN_DURATION_S, le=MAX_DURATION_S)
    description: str = Field(default="", max_length=MAX_DESCRIPTION, description="A note for it.")
    script_model: str | None = Field(default=None, description="Omit for the default writer.")
