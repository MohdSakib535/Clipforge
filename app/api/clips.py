from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.platforms import get_preset
from app.db.session import get_db
from app.models import Clip, ClipStatus, Job, User, Video
from app.schemas.clip import ClipOut, ClipUpdate
from app.services.naming import clip_basename
from app.tasks import dispatch

router = APIRouter(prefix="/clips", tags=["clips"])


def _download_name(clip: Clip) -> str:
    """e.g. The_Popes_Exorcist_part-041_facebook_video"""
    return clip_basename(clip.job.video.filename, clip.platform, clip.rank)


def _get_clip(db: Session, user: User, clip_id: str) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise NotFoundError("Clip not found")
    job = db.get(Job, clip.job_id)
    video = db.get(Video, job.video_id) if job else None
    if video is None or video.user_id != user.id:
        raise NotFoundError("Clip not found")
    return clip


@router.patch("/{clip_id}", response_model=ClipOut)
def update_clip(
    clip_id: str,
    body: ClipUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Clip:
    clip = _get_clip(db, user, clip_id)
    data = body.model_dump(exclude_unset=True)

    # Bound trim edits to the source video duration.
    video = db.get(Video, db.get(Job, clip.job_id).video_id)
    new_start = data.get("start_s", clip.start_s)
    new_end = data.get("end_s", clip.end_s)
    if video.duration_s and new_end > video.duration_s:
        raise AppError("end_s exceeds video duration", code="out_of_range", status_code=400)
    if new_end <= new_start:
        raise AppError("end_s must be > start_s", code="bad_range", status_code=400)

    for field, value in data.items():
        setattr(clip, field, value)
    if "start_s" in data or "end_s" in data:
        clip.duration_s = round(new_end - new_start, 3)
    db.commit()
    db.refresh(clip)
    return clip


@router.post("/{clip_id}/render", response_model=ClipOut)
def render_clip_endpoint(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Clip:
    clip = _get_clip(db, user, clip_id)
    get_preset(clip.platform)  # validate platform still known
    clip.status = ClipStatus.rendering
    clip.error = None
    db.commit()
    dispatch.enqueue_clip_render(clip.id)
    db.refresh(clip)
    return clip


@router.get("/{clip_id}/download")
def download_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    clip = _get_clip(db, user, clip_id)
    if not clip.file_path or not Path(clip.file_path).exists():
        raise AppError(
            "Clip not rendered yet", code="not_rendered", status_code=status.HTTP_409_CONFLICT
        )
    filename = f"{_download_name(clip)}.mp4"
    return FileResponse(clip.file_path, media_type="video/mp4", filename=filename)


@router.get("/{clip_id}/preview")
def preview_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """Serve the clip inline for the review player (supports range requests)."""
    clip = _get_clip(db, user, clip_id)
    if not clip.file_path or not Path(clip.file_path).exists():
        raise AppError(
            "Clip not rendered yet", code="not_rendered", status_code=status.HTTP_409_CONFLICT
        )
    return FileResponse(clip.file_path, media_type="video/mp4", content_disposition_type="inline")


@router.get("/{clip_id}/thumbnail")
def clip_thumbnail(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    clip = _get_clip(db, user, clip_id)
    if not clip.thumbnail_path or not Path(clip.thumbnail_path).exists():
        raise NotFoundError("No thumbnail")
    return FileResponse(clip.thumbnail_path, media_type="image/jpeg")


@router.get("/{clip_id}/captions.srt")
def clip_captions(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """Sidecar captions (upload alongside the video; never burned into it)."""
    clip = _get_clip(db, user, clip_id)
    srt = clip.srt_path
    if srt is None:
        raise NotFoundError("No caption file for this clip")
    filename = f"{_download_name(clip)}.srt"
    return FileResponse(srt, media_type="application/x-subrip", filename=filename)
