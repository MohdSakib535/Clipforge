from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_str


class AudioTrack(Base, TimestampMixin):
    """A user's background-music track (see services/bgaudio.py): uploaded, or
    grabbed from a link (audio only)."""

    __tablename__ = "audio_tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    filename: Mapped[str] = mapped_column(String(512))
    # Converted track (stereo AAC at the clips' loudness); set once ready.
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    # "processing" (upload/link import running) -> "ready" | "failed"
    status: Mapped[str] = mapped_column(String(16), default="ready", server_default="ready")
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)  # link imports

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and bool(self.path)
