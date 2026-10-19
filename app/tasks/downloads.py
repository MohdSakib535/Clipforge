"""Import a video from a link. Runs on the ``downloads`` queue (its own worker),
so a long download never waits behind clip processing and never holds it up."""

from __future__ import annotations

from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models import Video, VideoStatus
from app.services import downloader
from app.services.storage import get_storage
from app.tasks.celery_app import celery_app
from app.tasks.pipeline import ingest_video

log = get_logger(__name__)

_timeout_s = get_settings().download_timeout_s


def _save_progress(video_id: str, pct: int) -> None:
    # Own short session: yt-dlp may call this from its fragment threads.
    with SessionLocal() as db:
        db.execute(update(Video).where(Video.id == video_id).values(download_progress=pct))
        db.commit()


@celery_app.task(
    name="download_video",
    soft_time_limit=_timeout_s,
    time_limit=_timeout_s + 60,
)
def download_video(video_id: str, quality: str = "best") -> None:
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        if video is None:
            log.warning("download_missing_video", extra={"video_id": video_id})
            return
        if video.status != VideoStatus.downloading:
            return  # already finished (task redelivered) or failed

        dest_dir = Path(get_storage().path_for(f"videos/{video_id}"))
        try:
            result = downloader.download(
                video.source_url or "",
                dest_dir,
                quality,
                on_progress=lambda pct: _save_progress(video_id, pct),
            )
        except Exception as exc:
            if isinstance(exc, downloader.DownloadError):
                reason = str(exc)
            elif isinstance(exc, SoftTimeLimitExceeded):
                reason = f"Download took longer than {_timeout_s // 60} minutes and was stopped."
            else:
                reason = downloader.clean_error(exc)
            db.refresh(video)
            video.status = VideoStatus.failed
            video.error = reason
            db.commit()
            log.exception("download_failed", extra={"video_id": video_id})
            return

        # Keep the real title (clip file names are derived from it); only a
        # slash would be read as a folder.
        title = result.title.replace("/", "_").replace("\\", "_").strip()[:200] or "video"
        db.refresh(video)
        video.path = str(result.path)
        video.filename = f"{title}{result.path.suffix}"
        video.size_bytes = result.path.stat().st_size
        video.download_progress = 100
        video.status = VideoStatus.uploaded
        db.commit()
        log.info("download_done", extra={"video_id": video_id, "bytes": video.size_bytes})

    # Probe right here (~1s) rather than queueing it behind a long job on the
    # main worker, so the video is ready as soon as the download ends.
    ingest_video(video_id)
