from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_str


class Reel(Base, TimestampMixin):
    """A short video made from a topic ("Create a reel"): Ollama writes the
    script, FLUX.2 draws a picture per scene, Kokoro reads it out
    (services/reel_script.py, services/reel_render.py). Separate from the
    upload -> clips flow; its files live under ``reels/<id>/``."""

    __tablename__ = "reels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    platform: Mapped[str] = mapped_column(String(32))  # "facebook" | "instagram"
    language: Mapped[str] = mapped_column(String(16))  # key of tts.LANGUAGES
    voice: Mapped[str] = mapped_column(String(32))  # Kokoro voice, e.g. "hf_alpha"
    topic: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[int] = mapped_column(Integer)  # target length

    # "scripting" -> "script_ready" -> "generating" -> "ready"; "failed" from
    # either step (``script`` tells which: no script yet = the script failed).
    status: Mapped[str] = mapped_column(String(16), default="scripting")
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)  # shown while working
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # {"title", "hooks": [..], "scenes": [{"narration", "image_prompt"}],
    #  "caption", "hashtags": [..]}; editable until the video is generated.
    script: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Background music from the library. No foreign key: the music is mixed
    # into the file, so deleting the track later doesn't touch the reel.
    music_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    music_volume: Mapped[int] = mapped_column(Integer, default=20, server_default="20")

    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    video_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
