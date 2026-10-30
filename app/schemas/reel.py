from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bucket import BucketCopy
from app.services.reel_script import MAX_DURATION_S, MAX_SCENES, MIN_DURATION_S


class ReelCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    topic: str = Field(min_length=3, max_length=500)
    platform: str = Field(description='"instagram" or "facebook"')
    language: str = Field(description="A key from GET /reels/options")
    voice: str | None = Field(default=None, description="Omit for the language's first voice.")
    duration_s: int = Field(default=35, ge=MIN_DURATION_S, le=MAX_DURATION_S)
    style: str = Field(default="cinematic", description="A picture style from GET /reels/options")


class ReelScene(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    narration: str = Field(min_length=1, max_length=800)
    image_prompt: str = Field(min_length=1, max_length=1000)


class ReelMusic(BaseModel):
    """Suggested background music (the user finds a track like it online)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mood: str = Field(default="", max_length=120)
    genre: str = Field(default="", max_length=120)
    tempo: str = Field(default="", max_length=120)
    instruments: str = Field(default="", max_length=120)
    search: str = Field(default="", max_length=200)


class ReelScript(BaseModel):
    """The script as edited in the UI."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default="", max_length=200)
    hooks: list[str] = Field(default_factory=list, max_length=5)
    scenes: list[ReelScene] = Field(min_length=1, max_length=MAX_SCENES)
    caption: str = Field(default="", max_length=2200)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    music: ReelMusic | None = None  # last, as in the generated script


class ReelGenerate(BaseModel):
    voice: str | None = Field(default=None, description="Omit to keep the current voice.")
    style: str | None = Field(default=None, description="Omit to keep the current style.")
    music_id: str | None = Field(default=None, description="Library track; null = no music.")
    music_volume: int = Field(default=20, ge=0, le=100, description="100 = as loud as the voice.")


class ReelOut(BaseModel):
    id: str
    platform: str
    language: str
    voice: str
    topic: str
    duration_s: int
    style: str
    status: str  # scripting | script_ready | generating | ready | failed
    # Still "scripting"/"generating" but no progress for a long time (cut off
    # by a restart): it can be started again.
    stalled: bool = False
    stage: str | None = None
    progress: int
    error: str | None = None
    script: dict | None = None
    music_id: str | None = None
    music_volume: int
    has_video: bool
    video_version: int | None = None  # changes whenever the file does (cache-busting)
    video_duration_s: float | None = None
    scene_pictures: list[bool]  # per scene: is its picture made yet?
    bucket: BucketCopy | None = None  # its copy in the bucket, once uploaded
    created_at: datetime
    updated_at: datetime
