from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_str
from app.services.bucket import copy_out


class AudioTrack(Base, TimestampMixin):
    """A user's background-music track (see services/bgaudio.py): uploaded, or
    grabbed from a link (audio only). Its audio is kept in the bucket
    (services/music.py), fetched only while a clip or reel is being made."""

    __tablename__ = "audio_tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    filename: Mapped[str] = mapped_column(String(512))
    # Converted track (stereo AAC at the clips' loudness) on this computer:
    # only tracks from before music was kept in the bucket.
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # "processing" (upload/link import running) -> "ready" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="ready", server_default="ready")
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)  # link imports
    # The bucket file with its audio: {"key", "version", "uploaded_at"};
    # "version" is the mtime of a copy here (None: no copy here).
    bucket_copy: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # A link import: once downloaded, its audio goes to the bucket and its
    # files here are deleted (nothing kept here).
    to_bucket: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    @property
    def is_ready(self) -> bool:
        """Done, with its audio in the bucket (or, from before, on this computer)."""
        return self.status == "ready" and bool(self.path or (self.bucket_copy or {}).get("key"))

    @property
    def bucket(self) -> dict | None:
        """Its copy in the bucket: {"key", "uploaded_at", "current"}."""
        try:
            version = Path(self.path).stat().st_mtime_ns if self.path else None
        except OSError:
            version = None
        return copy_out(self.bucket_copy, version)
