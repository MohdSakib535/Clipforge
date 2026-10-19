from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_str

if TYPE_CHECKING:
    from app.models.video import Video


class Transcript(Base, TimestampMixin):
    """Cached word-level transcript for a video. Keyed by video so re-running a
    job never re-transcribes the same source."""

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), unique=True, index=True)

    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    text: Mapped[str] = mapped_column(Text, default="")
    # List of {word, start, end} dicts (word-level timestamps).
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # List of {text, start, end} sentence/segment dicts.
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    video: Mapped[Video] = relationship(back_populates="transcript")
