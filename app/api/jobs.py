from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.settings import ConfigError, get_settings
from app.db.session import SessionLocal, get_db
from app.models import Clip, ClipStatus, Job, JobStatus, User, Video, VideoStatus
from app.schemas.clip import ClipOut, ColorGradeApply, ColorGradeResult
from app.schemas.job import JobCreate, JobOut
from app.services.discard import discard_job
from app.services.naming import clip_basename, video_stem
from app.tasks import dispatch

router = APIRouter(tags=["jobs"])


def _get_job(db: Session, user: User, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError("Job not found")
    video = db.get(Video, job.video_id)
    if video is None or video.user_id != user.id:
        raise NotFoundError("Job not found")
    return job


def _check_transcriber() -> None:
    """Refuse to queue a job the worker could not run (a bad TRANSCRIBER, or
    OpenAI chosen without a key). Tells the user at once instead of after a
    failed job."""
    try:
        get_settings().check_transcriber_config()
    except ConfigError as exc:
        raise AppError(
            str(exc),
            code="transcriber_unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from None


@router.post(
    "/videos/{video_id}/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    video_id: str,
    body: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Job:
    video = db.get(Video, video_id)
    if video is None or video.user_id != user.id:
        raise NotFoundError("Video not found")
    if not video.rights_confirmed:
        raise AppError(
            "Rights must be confirmed before processing.",
            code="rights_not_confirmed",
            status_code=status.HTTP_409_CONFLICT,
        )
    if video.status != VideoStatus.ready:
        raise AppError(
            f"Video is not ready for processing (status={video.status.value}).",
            code="video_not_ready",
            status_code=status.HTTP_409_CONFLICT,
        )
    _check_transcriber()

    job = Job(
        video_id=video.id,
        platforms=body.platforms,
        settings=body.model_dump(mode="json"),
        status=JobStatus.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    dispatch.enqueue_job(job.id)
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Start over while a job is still queued or running: stop it and delete
    what it made (its clips and their files). The video and its transcript
    are kept, so processing it again is quick. A finished (or failed) job is
    left alone."""
    job = _get_job(db, user, job_id)
    if job.status == JobStatus.queued:
        # Not started: delete now; the worker skips a job that no longer exists.
        discard_job(db, job.id)
        return {"status": "cancelled", "deleted": True}
    if job.status == JobStatus.running:
        # The pipeline checks this between steps (within seconds), stops,
        # and deletes the job and its clips itself.
        job.status = JobStatus.cancelled
        db.commit()
        return {"status": "cancelling", "deleted": False}
    return {"status": job.status.value, "deleted": False}


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Job:
    return _get_job(db, user, job_id)


@router.get("/jobs/{job_id}/clips", response_model=list[ClipOut])
def list_clips(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Clip]:
    job = _get_job(db, user, job_id)
    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job.id, Clip.status != ClipStatus.deleted)
        .order_by(Clip.platform, Clip.rank)
        .all()
    )
    return clips


@router.post("/jobs/{job_id}/color", response_model=ColorGradeResult)
def apply_color_grade(
    job_id: str,
    body: ColorGradeApply,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ColorGradeResult:
    """Apply one colour grade to some or all of a job's clips (all three 0 =
    remove the grade). Returns at once; each clip re-renders in the background
    (its background music is re-applied automatically) and comes back ready.

    A colour change edits pixels, so every affected clip is fully re-rendered —
    slower than a music change, which only redoes audio."""
    job = _get_job(db, user, job_id)

    query = db.query(Clip).filter(Clip.job_id == job.id, Clip.status != ClipStatus.deleted)
    if body.clip_ids is not None:
        query = query.filter(Clip.id.in_(body.clip_ids))
    clips = query.order_by(Clip.platform, Clip.rank).all()
    if body.clip_ids is not None and len(clips) != len(set(body.clip_ids)):
        raise NotFoundError("Clip not found")

    want = (body.contrast, body.brightness, body.saturation)
    queued: list[tuple[Clip, bool]] = []  # (clip, was_approved)
    for clip in clips:
        ready = clip.file_path and clip.status in (ClipStatus.ready, ClipStatus.approved)
        if not ready:
            continue  # still rendering / failed
        # Compare with the grade the FILE has, not the saved sliders: sliders
        # saved without a re-render must not make a clip look already done.
        if clip.rendered_grade() == want:
            # Already looks like this; just bring saved sliders back in line.
            clip.color_contrast, clip.color_brightness, clip.color_saturation = want
            continue
        keep_approved = clip.status == ClipStatus.approved
        clip.color_contrast, clip.color_brightness, clip.color_saturation = want
        clip.status = ClipStatus.rendering
        clip.error = None
        queued.append((clip, keep_approved))

    if not queued:
        db.commit()  # keep any sliders brought back in line above
        raise AppError(
            "No ready clips to change (clips still rendering, or already at this grade).",
            code="nothing_to_apply",
            status_code=status.HTTP_409_CONFLICT,
        )
    db.commit()
    for clip, keep_approved in queued:
        # One clip = an edit in progress: jump ahead of batch re-renders.
        dispatch.enqueue_clip_render(clip.id, keep_approved, urgent=len(queued) == 1)
    return ColorGradeResult(
        queued=len(queued), skipped=len(clips) - len(queued), clips=[c for c, _ in queued]
    )


def _metadata_dict(clip: Clip, source_video: str) -> dict:
    return {
        "source_video": source_video,
        "part": clip.rank,
        "platform": clip.platform,
        "title": clip.title,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "start_s": clip.start_s,
        "end_s": clip.end_s,
        "duration_s": clip.duration_s,
        "has_speech": clip.has_speech,
    }


@router.get("/jobs/{job_id}/export.zip")
def export_zip(
    job_id: str,
    only: Literal["approved"] | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """Every rendered clip, or with ``?only=approved`` just the approved ones."""
    job = _get_job(db, user, job_id)
    wanted = [ClipStatus.approved]
    if only != "approved":
        wanted.append(ClipStatus.ready)
    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job.id, Clip.status.in_(wanted))
        .order_by(Clip.platform, Clip.rank)
        .all()
    )
    ready = [c for c in clips if c.file_path and Path(c.file_path).exists()]
    if not ready:
        message = "No approved clips to export yet." if only else "No rendered clips to export yet."
        raise AppError(message, code="nothing_to_export", status_code=409)

    video_name = job.video.filename
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in ready:
            base = f"{c.platform}/{clip_basename(c.rank)}"
            zf.write(c.file_path, f"{base}.mp4")
            if c.srt_path:
                zf.write(c.srt_path, f"{base}.srt")
            meta = _metadata_dict(c, video_name)
            zf.writestr(f"{base}.json", json.dumps(meta, indent=2))
            txt = (
                f"Source video: {video_name}\n"
                f"Part: {c.rank}\n\n"
                f"Title: {c.title or ''}\n\n"
                f"Description:\n{c.description or ''}\n\n"
                f"Hashtags: {' '.join('#' + h for h in (c.hashtags or []))}\n"
            )
            zf.writestr(f"{base}.txt", txt)

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{video_stem(video_name)}_{'approved' if only else 'clips'}.zip",
    )


@router.websocket("/ws/jobs/{job_id}")
async def job_progress_ws(websocket: WebSocket, job_id: str) -> None:
    """Push job progress every second until the job reaches a terminal state."""
    await websocket.accept()
    try:
        while True:
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None:
                    await websocket.send_json({"error": "not_found"})
                    break
                await websocket.send_json(
                    {
                        "id": job.id,
                        "status": job.status.value,
                        "stage": job.stage.value,
                        "progress": job.progress,
                        "estimated_cost_usd": job.estimated_cost_usd,
                        "error": job.error,
                    }
                )
                if job.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
                    break
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
