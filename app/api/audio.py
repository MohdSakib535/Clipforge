from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.settings import get_settings
from app.db.session import get_db
from app.models import AudioTrack, Clip, ClipStatus, Job, Reel, User, Video
from app.schemas.audio import (
    AudioFromUrl,
    AudioTrackOut,
    BackgroundAudioApply,
    BackgroundAudioResult,
)
from app.services import bgaudio, downloader
from app.services.ffmpeg import FFmpegError
from app.services.storage import get_storage
from app.tasks import dispatch

router = APIRouter(tags=["audio"])

CHUNK_SIZE = 1024 * 1024


def _get_track(db: Session, user: User, audio_id: str, *, ready: bool = True) -> AudioTrack:
    track = db.get(AudioTrack, audio_id)
    if track is None or track.user_id != user.id or (ready and not track.is_ready):
        raise NotFoundError("Audio track not found")
    return track


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------
@router.post("/audio/upload", response_model=AudioTrackOut, status_code=status.HTTP_201_CREATED)
async def upload_audio(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AudioTrack:
    """Upload a background-music file. It is converted once (stereo AAC at the
    clips' loudness) in a thread, so other requests are never held up."""
    name = file.filename or "audio.mp3"
    ext = Path(name).suffix.lower()
    if ext not in bgaudio.AUDIO_EXTS:
        raise AppError(
            f"Unsupported audio type '{ext}'. Allowed: {sorted(bgaudio.AUDIO_EXTS)}",
            code="unsupported_type",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    limit = get_settings().max_audio_upload_bytes
    storage = get_storage()

    track = AudioTrack(user_id=user.id, filename=name, status="processing")
    db.add(track)
    db.commit()
    db.refresh(track)

    raw = storage.path_for(f"audio/{track.id}/upload{ext}")
    dst = storage.path_for(f"audio/{track.id}/track.m4a")
    raw.parent.mkdir(parents=True, exist_ok=True)
    try:
        written = 0
        with open(raw, "wb") as out:
            while chunk := await file.read(CHUNK_SIZE):
                written += len(chunk)
                if written > limit:
                    raise AppError(
                        f"Audio file is larger than {limit // (1024 * 1024)} MB",
                        code="too_large",
                        status_code=413,
                    )
                out.write(chunk)
        try:
            info = await asyncio.to_thread(bgaudio.prepare_track, raw, dst)
        except FFmpegError:
            raise AppError(
                "Could not read this audio file.", code="bad_audio", status_code=400
            ) from None
    except AppError:
        storage.delete(f"audio/{track.id}")
        db.delete(track)
        db.commit()
        raise
    finally:
        raw.unlink(missing_ok=True)

    track.path = str(dst)
    track.size_bytes = dst.stat().st_size
    track.duration_s = round(info.duration_s, 3)
    track.status = "ready"
    db.commit()
    db.refresh(track)
    return track


@router.post("/audio/from-url", response_model=AudioTrackOut, status_code=status.HTTP_202_ACCEPTED)
def audio_from_url(
    body: AudioFromUrl,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AudioTrack:
    """Grab just the audio of a link (YouTube, SoundCloud, ...) as a music
    track. Returns at once with status "processing"; the download runs on the
    downloads worker. Poll ``GET /audio`` until it is "ready"."""
    try:
        url = downloader.validate_url(body.url)
    except downloader.DownloadError as exc:
        raise AppError(str(exc), code="bad_link", status_code=400) from None
    track = AudioTrack(
        user_id=user.id,
        filename=urlparse(url).hostname or "audio",  # replaced by the title when done
        status="processing",
        source_url=url,
        progress=0,
    )
    db.add(track)
    db.commit()
    dispatch.enqueue_audio_download(track.id)
    db.refresh(track)
    return track


@router.get("/audio", response_model=list[AudioTrackOut])
def list_audio(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AudioTrack]:
    """Every track, newest first, including link imports still downloading
    ("processing") or failed (with ``error``)."""
    return (
        db.query(AudioTrack)
        .filter(AudioTrack.user_id == user.id)
        .order_by(AudioTrack.created_at.desc())
        .all()
    )


@router.get("/audio/{audio_id}/file")
def audio_file(
    audio_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """The converted track, for the preview player."""
    track = _get_track(db, user, audio_id)
    return FileResponse(track.path, media_type="audio/mp4", content_disposition_type="inline")


@router.delete("/audio/{audio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_audio(
    audio_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    track = _get_track(db, user, audio_id, ready=False)
    in_use = (
        db.query(Clip)
        .filter(Clip.bg_audio_id == track.id, Clip.status != ClipStatus.deleted)
        .count()
    )
    if in_use:
        raise AppError(
            f"This music is on {in_use} clip(s). Remove it from them first.",
            code="audio_in_use",
            status_code=status.HTTP_409_CONFLICT,
        )
    if db.query(Reel).filter(Reel.music_id == track.id, Reel.status == "generating").count():
        raise AppError(
            "A reel with this music is being made right now. Try again when it has finished.",
            code="audio_in_use",
            status_code=status.HTTP_409_CONFLICT,
        )
    db.query(Clip).filter(Clip.bg_audio_id == track.id).update({Clip.bg_audio_id: None})
    # Reels keep the music already mixed into their video; the next render has none.
    db.query(Reel).filter(Reel.music_id == track.id).update({Reel.music_id: None})
    get_storage().delete(f"audio/{track.id}")
    db.delete(track)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Put music under clips (one, some or all of a job's clips)
# ---------------------------------------------------------------------------
@router.post("/jobs/{job_id}/background-audio", response_model=BackgroundAudioResult)
def apply_background_audio(
    job_id: str,
    body: BackgroundAudioApply,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BackgroundAudioResult:
    """Returns at once; the clips go to "rendering" and are mixed in the
    background (a second or two each), then come back "ready"/"approved"."""
    job = db.get(Job, job_id)
    video = db.get(Video, job.video_id) if job else None
    if video is None or video.user_id != user.id:
        raise NotFoundError("Job not found")
    if body.audio_id is not None:
        _get_track(db, user, body.audio_id)

    query = db.query(Clip).filter(Clip.job_id == job_id, Clip.status != ClipStatus.deleted)
    if body.clip_ids is not None:
        query = query.filter(Clip.id.in_(body.clip_ids))
    clips = query.order_by(Clip.platform, Clip.rank).all()
    if body.clip_ids is not None and len(clips) != len(set(body.clip_ids)):
        raise NotFoundError("Clip not found")

    queued: list[Clip] = []
    approved: list[str] = []
    for clip in clips:
        ready = clip.file_path and clip.status in (ClipStatus.ready, ClipStatus.approved)
        if not ready or (body.audio_id is None and clip.bg_audio_id is None):
            continue  # still rendering / failed, or no music to remove
        if clip.status == ClipStatus.approved:
            approved.append(clip.id)
        clip.bg_audio_id = body.audio_id
        clip.bg_audio_volume = body.volume
        clip.bg_audio_mute_original = body.mute_original if body.audio_id else False
        clip.status = ClipStatus.rendering
        clip.error = None
        queued.append(clip)

    if not queued:
        raise AppError(
            "No ready clips to change. Clips that are still rendering are skipped.",
            code="nothing_to_apply",
            status_code=status.HTTP_409_CONFLICT,
        )
    db.commit()
    dispatch.enqueue_bg_audio_mix([c.id for c in queued], approved)
    return BackgroundAudioResult(queued=len(queued), skipped=len(clips) - len(queued), clips=queued)
