"""Import a video, or just the audio for background music, from a link. Runs
on the ``downloads`` queue (its own worker), so a long download never waits
behind clip processing and never holds it up."""

from __future__ import annotations

from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models import AudioTrack, Video, VideoStatus
from app.services import bgaudio, bucket, downloader
from app.services.naming import video_stem
from app.services.storage import get_storage
from app.tasks.celery_app import celery_app
from app.tasks.pipeline import ingest_video

log = get_logger(__name__)

_timeout_s = get_settings().download_timeout_s


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, downloader.DownloadError):
        return str(exc)
    if isinstance(exc, SoftTimeLimitExceeded):
        return f"Download took longer than {_timeout_s // 60} minutes and was stopped."
    return downloader.clean_error(exc)


class DownloadCancelled(Exception):
    """The video was deleted (Start over) while downloading."""


def _save_progress(video_id: str, pct: int) -> None:
    # Own short session: yt-dlp may call this from its fragment threads.
    with SessionLocal() as db:
        res = db.execute(update(Video).where(Video.id == video_id).values(download_progress=pct))
        db.commit()
    if res.rowcount == 0:  # deleted by Start over: stop downloading
        raise DownloadCancelled


def _video_gone(video_id: str) -> bool:
    with SessionLocal() as db:
        return db.get(Video, video_id) is None


def _drop_cancelled_download(video_id: str) -> None:
    get_storage().delete(f"videos/{video_id}")
    log.info("download_cancelled", extra={"video_id": video_id})


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
            if _video_gone(video_id):  # Start over: remove the partial file
                _drop_cancelled_download(video_id)
                return
            db.refresh(video)
            video.status = VideoStatus.failed
            video.error = _failure_reason(exc)
            db.commit()
            log.exception("download_failed", extra={"video_id": video_id})
            return
        if _video_gone(video_id):  # Start over just as it finished
            _drop_cancelled_download(video_id)
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


# ---------------------------------------------------------------------------
# Background music from a link (audio only)
# ---------------------------------------------------------------------------
def _save_track_progress(track_id: str, pct: int) -> None:
    with SessionLocal() as db:
        db.execute(update(AudioTrack).where(AudioTrack.id == track_id).values(progress=pct))
        db.commit()


@celery_app.task(
    name="download_audio_track",
    soft_time_limit=_timeout_s,
    time_limit=_timeout_s + 60,
)
def download_audio_track(track_id: str) -> None:
    """Grab only the audio of a link, then convert it like an uploaded track.
    A link imported from the Bucket tab (``to_bucket``) goes to the bucket."""
    with SessionLocal() as db:
        track = db.get(AudioTrack, track_id)
        if track is None or track.status != "processing":
            return  # deleted meanwhile, or already done

        dest_dir = Path(get_storage().path_for(f"audio/{track_id}"))
        try:
            result = downloader.download(
                track.source_url or "",
                dest_dir,
                on_progress=lambda pct: _save_track_progress(track_id, pct),
                audio_only=True,
                max_bytes=get_settings().max_audio_upload_bytes,
            )
            info = bgaudio.prepare_track(result.path, dest_dir / "track.m4a")
        except Exception as exc:
            db.refresh(track)
            track.status = "failed"
            track.error = _failure_reason(exc)[:2000]
            db.commit()
            log.exception("audio_download_failed", extra={"track_id": track_id})
            return
        finally:
            for f in dest_dir.glob("source.*"):  # keep only the converted track
                f.unlink(missing_ok=True)

        track_path = dest_dir / "track.m4a"
        title = result.title.replace("/", "_").replace("\\", "_").strip()[:200] or "audio"
        db.refresh(track)
        if track.to_bucket:
            _send_to_bucket(db, track, track_path, title)
            return
        track.filename = f"{title}.m4a"
        track.path = str(track_path)
        track.size_bytes = track_path.stat().st_size
        track.duration_s = round(info.duration_s, 3)
        track.progress = 100
        track.status = "ready"
        db.commit()
        log.info("audio_download_done", extra={"track_id": track_id})


def _send_to_bucket(db, track: AudioTrack, path: Path, title: str) -> None:
    """The audio goes to the bucket's audio folder under its title (title-2...
    if taken); its files here are deleted either way. Done: the row goes too.
    Failed: the row stays, "failed", saying why."""
    track_id = track.id
    try:
        key = bucket.free_key(bucket.audio_folder(), video_stem(title), ".m4a")
        bucket.upload(path, key)
    except Exception as exc:
        track.status = "failed"
        track.error = f"Could not save it to the bucket: {exc}"[:2000]
        db.commit()
        log.exception("audio_to_bucket_failed", extra={"track_id": track_id})
        return
    finally:
        get_storage().delete(f"audio/{track_id}")
    db.delete(track)
    db.commit()
    log.info("audio_to_bucket_done", extra={"track_id": track_id, "key": key})
