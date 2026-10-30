from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Response, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.settings import get_settings
from app.db.session import get_db
from app.models import AudioTrack, Clip, ClipStatus, Job, Reel, User, Video
from app.schemas.audio import (
    AudioFromBucket,
    AudioFromUrl,
    AudioTrackOut,
    BackgroundAudioApply,
    BackgroundAudioResult,
)
from app.services import bgaudio, bucket, downloader, music
from app.services.ffmpeg import FFmpegError
from app.services.naming import video_stem
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
    clips' loudness) in a thread, so other requests are never held up, then
    goes to the bucket's audio folder; nothing is kept on this computer."""
    bucket.ensure_set_up()
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
        key = await asyncio.to_thread(
            bucket.free_key, bucket.audio_folder(), video_stem(Path(name).stem), ".m4a"
        )
        await asyncio.to_thread(
            functools.partial(bucket.upload, dst, key, metadata=music.CONVERTED)
        )
        size_bytes = dst.stat().st_size
    except Exception:
        db.delete(track)
        db.commit()
        raise
    finally:
        storage.delete(f"audio/{track.id}")  # in the bucket now (or failed): nothing kept here

    track.filename = key.rsplit("/", 1)[-1]
    track.size_bytes = size_bytes
    track.duration_s = round(info.duration_s, 3)
    track.bucket_copy = music.in_bucket(key, converted=True)
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
    track, kept in the bucket. Returns at once with status "processing"; the
    download runs on the downloads worker. Poll ``GET /audio`` until it is
    "ready"."""
    bucket.ensure_set_up()
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
        to_bucket=True,
    )
    db.add(track)
    db.commit()
    dispatch.enqueue_audio_download(track.id)
    db.refresh(track)
    return track


@router.post(
    "/audio/from-bucket", response_model=AudioTrackOut, status_code=status.HTTP_201_CREATED
)
async def audio_from_bucket(
    body: AudioFromBucket,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AudioTrack:
    """Add an audio file in the bucket to the music library, so it can be
    picked as background music. Nothing is downloaded: the file stays in the
    bucket and is fetched while a clip or reel is made. A file already in the
    library gives its track."""
    key = body.key
    ext = Path(key).suffix.lower()
    if ext not in bgaudio.AUDIO_EXTS or ".." in key.split("/"):
        raise AppError("Not an audio file in the bucket", code="bad_key")
    kept = (
        db.query(AudioTrack)
        .filter(
            AudioTrack.user_id == user.id,
            AudioTrack.status == "ready",
            AudioTrack.bucket_copy["key"].as_string() == key,
        )
        .first()
    )
    if kept is not None:
        return kept
    limit = get_settings().max_audio_upload_bytes
    about = await asyncio.to_thread(bucket.head, key)
    size_bytes = about["ContentLength"]
    if size_bytes > limit:
        raise AppError(
            f"Audio file is larger than {limit // (1024 * 1024)} MB",
            code="too_large",
            status_code=413,
        )
    # The pickers don't offer the bucket file again: it is this track now.
    track = AudioTrack(
        user_id=user.id,
        filename=key.rsplit("/", 1)[-1],
        size_bytes=size_bytes,
        status="ready",
        bucket_copy=music.in_bucket(key, converted=music.is_converted(about.get("Metadata"))),
    )
    db.add(track)
    db.commit()
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
) -> Response:
    """The track for the preview player: played straight from the bucket (a
    track from before, kept on this computer, from here)."""
    track = _get_track(db, user, audio_id)
    if track.path and Path(track.path).exists():
        return FileResponse(track.path, media_type="audio/mp4", content_disposition_type="inline")
    key = music.bucket_key(track)
    if not key:
        raise NotFoundError("Audio track not found")
    return RedirectResponse(bucket.link(key))


@router.delete("/audio/{audio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_audio(
    audio_id: str,
    from_bucket: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Drop a track from the music library; ``from_bucket`` deletes its file
    in the bucket too, for good (the music boxes' Delete)."""
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
    key = music.bucket_key(track)
    if from_bucket and key:
        bucket.delete(key)  # first: if the bucket refuses, nothing has changed
    music.forget(db, track)
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
