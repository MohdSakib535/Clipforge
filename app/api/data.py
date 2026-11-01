"""Free disk space: see what is kept on this computer ("Local videos" tab),
delete one video or only its clips, or delete every video and everything made
from it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, ConflictError, NotFoundError
from app.db.session import get_db
from app.models import Job, User, Video
from app.schemas.data import DeleteAllData
from app.services.discard import (
    busy_reason,
    data_usage,
    delete_all_data,
    delete_video,
    delete_video_clips,
    local_data,
    release_stuck_clips,
    video_busy_reason,
)

router = APIRouter(tags=["data"])


@router.get("/data/usage")
def usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Counts and disk space that "delete all data" would free."""
    return data_usage(db, user.id)


@router.post("/data/delete-all")
def delete_all(
    body: DeleteAllData,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Delete every video, job, clip and transcript, with their files. The
    music library and the Whisper model are kept. Refused while work that
    writes files is still running."""
    if body.confirm != "DELETE":
        raise AppError(
            "Type DELETE to confirm", code="not_confirmed", status_code=status.HTTP_400_BAD_REQUEST
        )
    videos = select(Video.id).where(Video.user_id == user.id)
    release_stuck_clips(db, select(Job.id).where(Job.video_id.in_(videos)))
    reason = busy_reason(db, user.id)
    if reason:
        raise ConflictError(reason)
    return delete_all_data(db, user.id)


def _idle_video(db: Session, user: User, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.user_id != user.id:
        raise NotFoundError("Video not found")
    release_stuck_clips(db, select(Job.id).where(Job.video_id == video.id))
    reason = video_busy_reason(db, video)
    if reason:
        raise ConflictError(reason)
    return video


@router.get("/data/local")
def local(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Everything kept on this computer and the disk space each part uses:
    videos (uploaded or from a link) with their clips, reels made from a
    topic, music, and the speech/voice models."""
    return local_data(db, user.id)


@router.delete("/data/videos/{video_id}")
def delete_one_video(
    video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Delete the video with everything made from it: clips, reels, the
    transcript and their files. Copies in the bucket are kept."""
    return delete_video(db, _idle_video(db, user, video_id))


@router.delete("/data/videos/{video_id}/clips")
def delete_clips_of_video(
    video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Delete the clips and reels made from the video; the video and its
    transcript stay, so new clips are quick to make."""
    return delete_video_clips(db, _idle_video(db, user, video_id))
