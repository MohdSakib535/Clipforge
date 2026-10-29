"""Delete a cancelled job and everything it made; list what is kept on this
computer ("Local videos" tab) and delete one video, its clips, or all data."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import (
    AudioTrack,
    Clip,
    ClipStatus,
    Job,
    JobStatus,
    Reel,
    Transcript,
    Video,
    VideoStatus,
)
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


# ---------------------------------------------------------------------------
# "Local videos": what is kept on this computer, and deleting one video
# ---------------------------------------------------------------------------
# Kept for every video; shown in the list but never deleted from it.
MODEL_DIRS = {"whisper": ".hf-cache", "voice": ".kokoro"}


def _clip_ids(db: Session, video: Video) -> list[str]:
    jobs = select(Job.id).where(Job.video_id == video.id)
    return [cid for (cid,) in db.query(Clip.id).filter(Clip.job_id.in_(jobs))]


def video_busy_reason(db: Session, video: Video) -> str | None:
    """Why this video can't be deleted right now (work still writing its
    files). An unfinished upload or download can be: it stops, as on Start over."""
    running = [JobStatus.queued, JobStatus.running]
    if db.query(Job).filter(Job.video_id == video.id, Job.status.in_(running)).count():
        return "Clips are still being made from this video. Wait, or use Start over first."
    jobs = select(Job.id).where(Job.video_id == video.id)
    if db.query(Clip).filter(Clip.job_id.in_(jobs), Clip.status == ClipStatus.rendering).count():
        return "Some clips of this video are still re-rendering (colour, trim or music)."
    if video.status in (VideoStatus.uploaded, VideoStatus.ingesting):
        return "This video is still being prepared. Try again in a moment."
    return None


def local_data(db: Session, user_id: str) -> dict:
    """Everything kept on this computer with the disk space it uses: videos
    (with the clips and reels made from them), reels made from a topic, the
    music library, and the speech/voice models."""
    storage = get_storage()

    def size(key: str) -> int:
        return _folder_bytes(storage.path_for(key))

    videos = []
    for v in db.query(Video).filter(Video.user_id == user_id).order_by(Video.created_at.desc()):
        jobs = select(Job.id).where(Job.video_id == v.id)
        clips = db.query(Clip).filter(Clip.job_id.in_(jobs)).all()
        videos.append(
            {
                "id": v.id,
                "filename": v.filename,
                "from_link": bool(v.source_url),
                "source_url": v.source_url,
                "status": v.status.value,
                "duration_s": v.duration_s,
                "created_at": v.created_at,
                "video_bytes": size(f"videos/{v.id}"),  # the file and its caches
                "clips": sum(c.status != ClipStatus.deleted for c in clips),
                "clips_bytes": sum(size(f"clips/{c.id}") for c in clips),
                "transcribed": db.query(Transcript).filter(Transcript.video_id == v.id).count() > 0,
                "busy": video_busy_reason(db, v),
            }
        )
    reels = [
        {
            "id": r.id,
            "title": (r.script or {}).get("title") or r.topic,
            "status": r.status,
            "duration_s": r.video_duration_s,
            "created_at": r.created_at,
            "bytes": size(f"reels/{r.id}"),
        }
        for r in db.query(Reel).filter(Reel.user_id == user_id).order_by(Reel.created_at.desc())
    ]
    music = [
        {
            "id": t.id,
            "filename": t.filename,
            "status": t.status,
            "duration_s": t.duration_s,
            "created_at": t.created_at,
            "bytes": size(f"audio/{t.id}"),
        }
        for t in db.query(AudioTrack)
        .filter(AudioTrack.user_id == user_id)
        .order_by(AudioTrack.created_at.desc())
    ]
    return {
        "videos": videos,
        "reels": reels,
        "music": music,
        "models": {name: size(folder) for name, folder in MODEL_DIRS.items()},
    }


def delete_video(db: Session, video: Video) -> dict:
    """Delete one video with its jobs, clips, transcript and files."""
    storage = get_storage()
    clip_ids = _clip_ids(db, video)
    keys = [f"videos/{video.id}"] + [f"clips/{cid}" for cid in clip_ids]
    freed = sum(_folder_bytes(storage.path_for(k)) for k in keys)
    jobs = select(Job.id).where(Job.video_id == video.id)
    db.query(Clip).filter(Clip.job_id.in_(jobs)).delete(synchronize_session=False)
    db.query(Job).filter(Job.video_id == video.id).delete(synchronize_session=False)
    db.query(Transcript).filter(Transcript.video_id == video.id).delete(synchronize_session=False)
    db.query(Video).filter(Video.id == video.id).delete(synchronize_session=False)
    db.commit()
    for key in keys:  # rows first: a file left behind is swept up by "delete all data"
        storage.delete(key)
    log.info("video_deleted", extra={"clips": len(clip_ids), "bytes_freed": freed})
    return {"clips": len(clip_ids), "bytes_freed": freed}


def delete_video_clips(db: Session, video: Video) -> dict:
    """Delete every clip and reel made from the video, keeping the video and
    its transcript, so new clips are quick to make."""
    storage = get_storage()
    freed = sum(_folder_bytes(storage.path_for(f"clips/{cid}")) for cid in _clip_ids(db, video))
    clips = sum(
        discard_job(db, jid) for (jid,) in db.query(Job.id).filter(Job.video_id == video.id)
    )
    return {"clips": clips, "bytes_freed": freed}
