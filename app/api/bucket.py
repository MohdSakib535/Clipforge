"""The "Bucket" section: send videos from this computer straight to the bucket,
into their own folder (<BUCKET_PREFIX>/uploads/), apart from the reels
(reels/ and clips/), and open, download or delete any video in the bucket.
Music goes straight to its own folder (audio/), a file or the audio of a link,
and any audio file can be opened, downloaded, renamed or deleted.

A file goes up in parts through the API: start -> part(s) -> complete (or
abort), so the bucket needs no CORS setup and the page shows real progress.
Nothing is kept on this server's disk; downloads come straight from the bucket."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError
from app.config.settings import get_settings
from app.db.session import get_db
from app.models import AudioTrack, Clip, Reel, User
from app.schemas.audio import AudioFromUrl, AudioTrackOut
from app.schemas.bucket import (
    BucketAudio,
    BucketFile,
    BucketPart,
    BucketRename,
    BucketRenamed,
    BucketStatus,
    BucketUploadDone,
    BucketUploadInit,
    BucketUploadRef,
    BucketUploadStart,
    BucketVideos,
)
from app.services import bucket, downloader
from app.services.naming import video_stem
from app.tasks import dispatch

router = APIRouter(prefix="/bucket", tags=["bucket"])


def _upload_key(key: str) -> str:
    """Uploads only ever write to the uploads and audio folders."""
    folders = (bucket.uploads_folder(), bucket.audio_folder())
    if not key.startswith(folders) or ".." in key.split("/"):
        raise AppError("Not a file in the uploads or audio folder", code="bad_key")
    return key


def _video_key(key: str) -> str:
    """Open / Download / Delete: any video in the bucket."""
    if Path(key).suffix.lower() not in bucket.VIDEO_EXTENSIONS or ".." in key.split("/"):
        raise AppError("Not a video in the bucket", code="bad_key")
    return key


def _audio_key(key: str) -> str:
    """Open / Download / Rename / Delete: any audio file in the bucket."""
    if Path(key).suffix.lower() not in bucket.AUDIO_EXTENSIONS or ".." in key.split("/"):
        raise AppError("Not an audio file in the bucket", code="bad_key")
    return key


def _moved(db: Session, key: str, new_key: str | None) -> None:
    """Reels, clips and music uploaded to ``key`` are now at ``new_key``
    (renamed), or not in the bucket any more (None)."""
    for reel in db.query(Reel).filter(Reel.bucket["key"].as_string() == key):
        reel.bucket = {**reel.bucket, "key": new_key} if new_key else None
    for clip in db.query(Clip).filter(Clip.extra[("bucket", "key")].as_string() == key):
        extra = {k: v for k, v in clip.extra.items() if k != "bucket"}
        if new_key:
            extra["bucket"] = {**clip.extra["bucket"], "key": new_key}
        clip.extra = extra
    for track in db.query(AudioTrack).filter(AudioTrack.bucket_copy["key"].as_string() == key):
        track.bucket_copy = {**track.bucket_copy, "key": new_key} if new_key else None
    db.commit()


@router.get("", response_model=BucketStatus)
def bucket_status() -> BucketStatus:
    """Is a bucket set up, and which folders are used (shown in the section)."""
    settings = get_settings()
    return BucketStatus(
        set_up=bool(settings.bucket_name.strip()),
        bucket=settings.bucket_name.strip(),
        endpoint=settings.bucket_endpoint_url.strip() or "AWS S3",
        uploads_folder=bucket.uploads_folder(),
        reels_folders=[bucket.key_for("reels") + "/", bucket.key_for("clips") + "/"],
        audio_folder=bucket.audio_folder(),
    )


@router.get("/videos", response_model=BucketVideos)
def list_videos(_user: User = Depends(get_current_user)) -> BucketVideos:
    """Every video in the bucket, in any folder (uploads, reels, others),
    newest first."""
    videos, truncated = bucket.list_videos()
    return BucketVideos(videos=[BucketFile(**v) for v in videos], truncated=truncated)


@router.post("/uploads", response_model=BucketUploadStart)
def start_upload(
    body: BucketUploadInit, _user: User = Depends(get_current_user)
) -> BucketUploadStart:
    """Start sending a video (or, with folder "audio", a music file); then PUT
    /bucket/uploads/part for each piece and POST /bucket/uploads/complete. It
    keeps its name (name-2... if taken)."""
    audio = body.folder == "audio"
    allowed = bucket.AUDIO_EXTENSIONS if audio else bucket.VIDEO_EXTENSIONS
    ext = Path(body.filename).suffix.lower()
    if ext not in allowed:
        raise AppError(
            f"Unsupported file type '{ext}'. Allowed: {sorted(allowed)}",
            code="unsupported_type",
        )
    if body.size_bytes > bucket.PART_SIZE * bucket.MAX_PARTS:
        raise AppError("File exceeds maximum upload size", code="too_large", status_code=413)
    folder = bucket.audio_folder() if audio else bucket.uploads_folder()
    key = bucket.free_key(folder, video_stem(body.filename), ext)
    return BucketUploadStart(
        key=key, upload_id=bucket.start_upload(key), part_size=bucket.PART_SIZE
    )


@router.put("/uploads/part", response_model=BucketPart)
async def upload_part(
    request: Request,
    key: str,
    upload_id: str,
    number: int = Query(ge=1, le=bucket.MAX_PARTS),
    _user: User = Depends(get_current_user),
) -> BucketPart:
    """Piece ``number`` (1, 2, ...) of the file, as the raw request body."""
    data = await request.body()
    if not data:
        raise AppError("Empty part", code="empty_part")
    etag = await asyncio.to_thread(bucket.upload_part, _upload_key(key), upload_id, number, data)
    return BucketPart(number=number, etag=etag)


@router.post("/uploads/complete", response_model=BucketUploadRef)
def complete_upload(
    body: BucketUploadDone, _user: User = Depends(get_current_user)
) -> BucketUploadRef:
    """Join the pieces: the video is now in the bucket."""
    parts = sorted((p.number, p.etag) for p in body.parts)
    bucket.finish_upload(_upload_key(body.key), body.upload_id, parts)
    return BucketUploadRef(key=body.key, upload_id=body.upload_id)


@router.post("/uploads/abort", status_code=status.HTTP_204_NO_CONTENT)
def abort_upload(body: BucketUploadRef, _user: User = Depends(get_current_user)) -> Response:
    """Stopped or failed: drop the pieces sent so far."""
    bucket.abort_upload(_upload_key(body.key), body.upload_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/videos/link")
def video_link(key: str, _user: User = Depends(get_current_user)) -> RedirectResponse:
    """Opens (plays) any video in the bucket."""
    return RedirectResponse(bucket.link(_video_key(key)))


@router.get("/videos/download")
def download_video(key: str, _user: User = Depends(get_current_user)) -> RedirectResponse:
    """Saves any video in the bucket on this computer, straight from the bucket."""
    return RedirectResponse(bucket.download_link(_video_key(key)))


@router.delete("/videos", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(
    key: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
) -> Response:
    """Deletes any video from the bucket, for good. Reels and clips uploaded
    there show as not in the bucket again (the files on this computer stay)."""
    bucket.delete(_video_key(key))
    _moved(db, key, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Audio: music from the library (audio/) and any other audio file ---
@router.get("/audio", response_model=BucketAudio)
def list_audio(_user: User = Depends(get_current_user)) -> BucketAudio:
    """Every audio file in the bucket, in any folder, newest first."""
    audio, truncated = bucket.list_audio()
    return BucketAudio(audio=[BucketFile(**a) for a in audio], truncated=truncated)


@router.post("/audio/from-url", response_model=AudioTrackOut, status_code=status.HTTP_202_ACCEPTED)
def audio_from_url(
    body: AudioFromUrl, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> AudioTrack:
    """The audio of a link (YouTube, SoundCloud, ...) into the bucket's audio
    folder. Returns at once, "processing" with ``to_bucket``; the downloads
    worker fetches it, sends it to the bucket and deletes it here (the row
    goes too; "failed" with ``error`` if it couldn't). Poll ``GET /audio``."""
    bucket.ensure_set_up()
    try:
        url = downloader.validate_url(body.url)
    except downloader.DownloadError as exc:
        raise AppError(str(exc), code="bad_link", status_code=400) from None
    track = AudioTrack(
        user_id=user.id,
        filename=urlparse(url).hostname or "audio",
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


@router.get("/audio/link")
def audio_link(key: str, _user: User = Depends(get_current_user)) -> RedirectResponse:
    """Opens (plays) any audio file in the bucket."""
    return RedirectResponse(bucket.link(_audio_key(key)))


@router.get("/audio/download")
def download_audio(key: str, _user: User = Depends(get_current_user)) -> RedirectResponse:
    """Saves any audio file in the bucket on this computer, straight from the bucket."""
    return RedirectResponse(bucket.download_link(_audio_key(key)))


@router.post("/audio/rename", response_model=BucketRenamed)
def rename_audio(
    body: BucketRename, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
) -> BucketRenamed:
    """A new name for an audio file, in the same folder (its extension stays).
    409 if another file has that name: nothing is ever replaced."""
    new_key = bucket.rename(_audio_key(body.key), body.name)
    if new_key != body.key:
        _moved(db, body.key, new_key)
    return BucketRenamed(key=new_key, name=new_key.rsplit("/", 1)[-1])


@router.delete("/audio", status_code=status.HTTP_204_NO_CONTENT)
def delete_audio(
    key: str, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
) -> Response:
    """Deletes any audio file from the bucket, for good (the music library on
    this computer keeps its track)."""
    bucket.delete(_audio_key(key))
    _moved(db, key, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
