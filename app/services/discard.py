"""Delete a cancelled job and everything it made."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Clip, Job
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
