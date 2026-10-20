from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_str
from app.models.enums import JobStage, JobStatus

if TYPE_CHECKING:
    from app.models.clip import Clip
    from app.models.video import Video


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued, index=True)
    stage: Mapped[JobStage] = mapped_column(Enum(JobStage), default=JobStage.queued)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Full job settings (platforms, seconds per clip, caption settings).
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Cost tracking (transcription).
    audio_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    models_used: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    video: Mapped[Video] = relationship(back_populates="jobs")
    clips: Mapped[list[Clip]] = relationship(back_populates="job", cascade="all, delete-orphan")
