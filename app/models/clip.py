from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_str
from app.models.enums import ClipStatus
from app.services.bucket import copy_out

if TYPE_CHECKING:
    from app.models.job import Job


class Clip(Base, TimestampMixin):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)

    platform: Mapped[str] = mapped_column(String(64))

    # Timing on the source video (seconds).
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)

    # Metadata: "Part N - <first words>" and the part's transcript; editable.
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Caption settings for this clip.
    captions_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    caption_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Background music mixed under the clip's own audio (services/bgaudio.py).
    bg_audio_id: Mapped[str | None] = mapped_column(
        ForeignKey("audio_tracks.id"), nullable=True, index=True
    )
    bg_audio_volume: Mapped[int] = mapped_column(Integer, default=40, server_default="40")
    # Drop the clip's own audio so only the music plays ("mute original").
    bg_audio_mute_original: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # Gentle per-clip colour grade (each 0 = no change); applied on re-render.
    color_contrast: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    color_brightness: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    color_saturation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Frame (services/frame.py): 1080x1920 with the picture in the middle and
    # the user's text above/below it; None = no frame. Applied on re-render.
    frame: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Output artifacts.
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[ClipStatus] = mapped_column(
        Enum(ClipStatus), default=ClipStatus.pending, index=True
    )
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Part number: position in the video (1 = first part).
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="clips")

    @property
    def has_speech(self) -> bool | None:
        """False for a clip with no spoken words (music, silence), from the
        transcript. None for clips made before this was recorded."""
        return (self.extra or {}).get("has_speech")

    @property
    def transcript(self) -> str | None:
        """What is said in a clip made from a trim with transcription on, as
        spoken (not romanized). Its timed words are in ``extra["words"]``."""
        return (self.extra or {}).get("transcript")

    @property
    def translation(self) -> dict | None:
        """The transcript's sentences translated (POST /clips/{id}/translate)."""
        return (self.extra or {}).get("translation")

    @property
    def dub(self) -> dict | None:
        """Set on a dubbed copy: {"language", "voice", "lines", "source_clip"};
        its own audio is replaced by this voice (services/dub.py)."""
        return (self.extra or {}).get("dub")

    @property
    def dub_language(self) -> str | None:
        return (self.dub or {}).get("language")

    @property
    def color_rendered(self) -> dict[str, int] | None:
        """The colour grade the clip's FILE was last rendered with. The
        ``color_*`` columns are what the sliders were last saved as, which may
        not be applied yet. None for clips rendered before this was recorded."""
        return (self.extra or {}).get("color_rendered")

    @property
    def frame_rendered(self) -> dict | None:
        """The frame the clip's FILE was last rendered with (plus ``rect``,
        where the picture sits on it); None = the file has no frame."""
        return (self.extra or {}).get("frame_rendered")

    def rendered_grade(self) -> tuple[int, int, int]:
        """(contrast, brightness, saturation) the file has. A clip rendered
        before this was recorded is assumed to match its saved values."""
        g = self.color_rendered
        if g is None:
            return (
                self.color_contrast or 0,
                self.color_brightness or 0,
                self.color_saturation or 0,
            )
        return (g["contrast"], g["brightness"], g["saturation"])

    @property
    def has_file(self) -> bool:
        return bool(self.file_path)

    @property
    def file_version(self) -> int | None:
        """Changes whenever the clip file is rewritten (render, music added or
        removed). The dashboard puts it in preview/download URLs so a browser
        never plays a stale cached copy."""
        if not self.file_path:
            return None
        try:
            return Path(self.file_path).stat().st_mtime_ns
        except OSError:
            return None

    @property
    def bucket(self) -> dict | None:
        """Its copy in the bucket ("Upload to bucket"): {"key", "uploaded_at",
        "current"}; "current" is False once the file changed after the upload."""
        return copy_out((self.extra or {}).get("bucket"), self.file_version)

    @property
    def has_thumbnail(self) -> bool:
        return bool(self.thumbnail_path)

    @property
    def srt_path(self) -> str | None:
        """Sidecar captions written next to the clip (horizontal clips only)."""
        if not self.file_path:
            return None
        path = Path(self.file_path).with_suffix(".srt")
        return str(path) if path.exists() else None

    @property
    def has_srt(self) -> bool:
        return self.srt_path is not None
