from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_str
from app.models.enums import VideoStatus

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.transcript import Transcript
    from app.models.user import User


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    filename: Mapped[str] = mapped_column(String(512))
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ff_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus), default=VideoStatus.uploading, index=True
    )
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Rights confirmation (user, video, timestamp) — processing is blocked until set.
    rights_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="videos")
    jobs: Mapped[list[Job]] = relationship(back_populates="video")
    transcript: Mapped[Transcript | None] = relationship(back_populates="video", uselist=False)

    @property
    def rights_confirmed(self) -> bool:
        return self.rights_confirmed_at is not None
