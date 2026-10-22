"""Delete a cancelled job and everything it made; or delete all data."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Clip, ClipStatus, Job, JobStatus, Transcript, Video, VideoStatus
from app.services.storage import get_storage

log = get_logger(__name__)


def discard_job(db: Session, job_id: str) -> int:
    """Delete the job, its clip rows and their files; return how many clips.
    The video, its transcript and its caches (normalised audio, scene cuts)
    are kept, so processing it again is quick."""
    db.rollback()  # drop anything half-written by the cancelled work
    storage = get_storage()
    clip_ids = [cid for (cid,) in db.query(Clip.id).filter(Clip.job_id == job_id)]
    for cid in clip_ids:
        storage.delete(f"clips/{cid}")
    db.query(Clip).filter(Clip.job_id == job_id).delete(synchronize_session=False)
    db.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
    db.commit()
    log.info("job_discarded", extra={"job_id": job_id, "clips_deleted": len(clip_ids)})
    return len(clip_ids)


# ---------------------------------------------------------------------------
# Delete all data (free disk space)
# ---------------------------------------------------------------------------
# Storage folders that belong to a video or a clip. The music library (audio/)
# and the Whisper model (.hf-cache/) live elsewhere, so they are kept.
DATA_DIRS = ("videos", "clips")


def _folder_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # deleted while we walked
    return total


def _data_folders(db: Session, user_id: str) -> list[str]:
    """Storage keys to delete: the user's video and clip folders, plus folders
    no row points to any more (left over from earlier deletes)."""
    mine = {f"videos/{v}" for (v,) in db.query(Video.id).filter(Video.user_id == user_id)}
    mine |= {
        f"clips/{c}"
        for (c,) in db.query(Clip.id)
        .join(Job, Clip.job_id == Job.id)
        .join(Video, Job.video_id == Video.id)
        .filter(Video.user_id == user_id)
    }
    known = {f"videos/{v}" for (v,) in db.query(Video.id)}
    known |= {f"clips/{c}" for (c,) in db.query(Clip.id)}
    storage = get_storage()
    keys = []
    for d in DATA_DIRS:
        base = storage.path_for(d)
        if base.is_dir():
            for p in base.iterdir():
                key = f"{d}/{p.name}"
                if key in mine or key not in known:
                    keys.append(key)
    return keys


def data_usage(db: Session, user_id: str) -> dict:
    """What "delete all data" would delete."""
    videos = select(Video.id).where(Video.user_id == user_id)
    jobs = select(Job.id).where(Job.video_id.in_(videos))
    storage = get_storage()
    return {
        "videos": db.query(Video).filter(Video.user_id == user_id).count(),
        "jobs": db.query(Job).filter(Job.video_id.in_(videos)).count(),
        "clips": db.query(Clip).filter(Clip.job_id.in_(jobs)).count(),
        "bytes": sum(_folder_bytes(storage.path_for(k)) for k in _data_folders(db, user_id)),
    }


def busy_reason(db: Session, user_id: str) -> str | None:
    """Why all data can't be deleted right now (work still writing files)."""
    videos = select(Video.id).where(Video.user_id == user_id)
    jobs = select(Job.id).where(Job.video_id.in_(videos))
    running = [JobStatus.queued, JobStatus.running]
    if db.query(Job).filter(Job.video_id.in_(videos), Job.status.in_(running)).count():
        return "A job is still processing. Wait for it to finish or use Start over, then try again."
    if db.query(Clip).filter(Clip.job_id.in_(jobs), Clip.status == ClipStatus.rendering).count():
        return "Some clips are still re-rendering (colour, trim or music). Try again soon."
    preparing = [VideoStatus.downloading, VideoStatus.uploaded, VideoStatus.ingesting]
    if db.query(Video).filter(Video.user_id == user_id, Video.status.in_(preparing)).count():
        return "A video is still downloading or being prepared. Try again when it is ready."
    return None


def delete_all_data(db: Session, user_id: str) -> dict:
    """Delete every video of the user with its jobs, clips, transcript and
    files. The music library and the Whisper model are kept."""
    keys = _data_folders(db, user_id)
    storage = get_storage()
    freed = sum(_folder_bytes(storage.path_for(k)) for k in keys)
    videos = select(Video.id).where(Video.user_id == user_id)
    jobs = select(Job.id).where(Job.video_id.in_(videos))
    n_clips = db.query(Clip).filter(Clip.job_id.in_(jobs)).delete(synchronize_session=False)
    n_jobs = db.query(Job).filter(Job.video_id.in_(videos)).delete(synchronize_session=False)
    db.query(Transcript).filter(Transcript.video_id.in_(videos)).delete(synchronize_session=False)
    n_videos = db.query(Video).filter(Video.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    for key in keys:  # rows first: a file left behind is swept up next time
        storage.delete(key)
    log.info(
        "all_data_deleted",
        extra={"videos": n_videos, "jobs": n_jobs, "clips": n_clips, "bytes_freed": freed},
    )
    return {"videos": n_videos, "jobs": n_jobs, "clips": n_clips, "bytes_freed": freed}
