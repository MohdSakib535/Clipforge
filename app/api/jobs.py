from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.platforms import get_preset
from app.config.settings import ConfigError, get_settings
from app.db.session import SessionLocal, get_db
from app.models import Clip, ClipStatus, Job, JobStage, JobStatus, User, Video, VideoStatus
from app.schemas.clip import ClipOut, ColorGradeApply, ColorGradeResult
from app.schemas.job import MAX_TRIMS, JobCreate, JobOut, Trim, TrimsAdd
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


def _trim_settings(
    video: Video, trims: list[Trim], platforms: list[str], transcribe: bool
) -> list[dict]:
    """Check hand-picked parts against the video and each platform's allowed
    length; return them as stored in ``job.settings["trims"]``."""
    out = []
    for n, trim in enumerate(trims, start=1):
        end = trim.end_s
        if video.duration_s:
            if end > video.duration_s + 0.5:
                raise AppError(
                    f"Clip {n} ends after the video ({video.duration_s:.1f} s).",
                    code="out_of_range",
                )
            end = min(end, video.duration_s)  # the player can overshoot a little
        length = end - trim.start_s
        for platform in platforms:
            preset = get_preset(platform)
            if not preset.length_min_s <= round(length, 1) <= preset.length_max_s:
                raise AppError(
                    f"Clip {n} is {length:.1f} s long, but {preset.label} allows "
                    f"{preset.length_min_s}-{preset.length_max_s} s.",
                    code="bad_length",
                )
        out.append(
            {
                "start_s": trim.start_s,
                "end_s": end,
                "platforms": platforms,
                "transcribe": transcribe,
            }
        )
    return out


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

    settings = body.model_dump(mode="json")
    if body.trims:
        settings["trims"] = _trim_settings(video, body.trims, body.platforms, body.transcribe)
    job = Job(
        video_id=video.id,
        platforms=body.platforms,
        settings=settings,
        status=JobStatus.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    dispatch.enqueue_job(job.id)
    return job


@router.post("/jobs/{job_id}/trims", response_model=JobOut)
def add_trims(
    job_id: str,
    body: TrimsAdd,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Job:
    """Make more hand-picked parts in a job made from trims, so every reel of
    the video stays in one list. The job runs again for the new parts only."""
    job = _get_job(db, user, job_id)
    if (job.settings or {}).get("trims") is None:
        raise AppError(
            "This job cuts the whole video; it has no trims.",
            code="not_a_trim_job",
            status_code=status.HTTP_409_CONFLICT,
        )
    if job.status in (JobStatus.queued, JobStatus.running):
        raise AppError(
            "The reels before these are still being made. Add more when they are done.",
            code="job_busy",
            status_code=status.HTTP_409_CONFLICT,
        )
    _check_transcriber()
    added = _trim_settings(job.video, body.trims, body.platforms, body.transcribe)
    trims = job.settings["trims"] + added
    if len(trims) > MAX_TRIMS * 5:
        raise AppError("Too many clips in one job. Start over for more.", code="too_many")
    # New dicts: the JSON columns only notice a reassignment.
    job.settings = {**job.settings, "trims": trims}
    job.platforms = list(dict.fromkeys([*job.platforms, *body.platforms]))
    job.status = JobStatus.queued
    job.stage = JobStage.queued
    job.progress = 0
    job.error = None
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


@router.get("/jobs/{job_id}/export.zip")
def export_zip(
    job_id: str,
    only: Literal["approved"] | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """Every rendered clip, or with ``?only=approved`` just the approved ones.
    Only the videos: captions are downloaded per clip."""
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
    # MP4 is already compressed: store, don't deflate (much faster, same size).
    names: set[str] = set()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
        for c in ready:
            stem = f"{c.platform}/{clip_basename(c.rank, c.dub_language)}"
            name, n = f"{stem}.mp4", 1
            while name in names:  # e.g. a reel dubbed twice (two voices)
                n += 1
                name = f"{stem}-{n}.mp4"
            names.add(name)
            zf.write(c.file_path, name)

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{video_stem(video_name)}_{'approved' if only else 'clips'}.zip",
        background=BackgroundTask(os.unlink, tmp.name),  # delete once sent
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
