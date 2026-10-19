from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.config.settings import get_settings
from app.db.session import get_db
from app.models import User, Video, VideoStatus
from app.schemas.video import (
    RightsConfirm,
    UploadInit,
    UploadInitOut,
    UrlImport,
    UrlProbe,
    UrlProbeOut,
    VideoOut,
)
from app.services import downloader
from app.services.storage import get_storage
from app.tasks import dispatch

router = APIRouter(prefix="/videos", tags=["videos"])

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
_ALLOWED_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm"}


def _source_key(video_id: str, filename: str) -> str:
    ext = Path(filename).suffix.lower() or ".mp4"
    return f"videos/{video_id}/source{ext}"


def _check_ext(filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise AppError(
            f"Unsupported file type '{ext}'. Allowed: {sorted(_ALLOWED_EXT)}",
            code="unsupported_type",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def _get_video(db: Session, user: User, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.user_id != user.id:
        raise NotFoundError("Video not found")
    return video


# ---------------------------------------------------------------------------
# Chunked / resumable upload: init -> chunk(s) -> complete
# ---------------------------------------------------------------------------
@router.post("/upload/init", response_model=UploadInitOut)
def upload_init(
    body: UploadInit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadInitOut:
    _check_ext(body.filename)
    settings = get_settings()
    if body.size_bytes > settings.max_upload_bytes:
        raise AppError("File exceeds maximum upload size", code="too_large", status_code=413)

    video = Video(
        user_id=user.id,
        filename=body.filename,
        size_bytes=body.size_bytes,
        status=VideoStatus.uploading,
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    key = _source_key(video.id, body.filename)
    video.path = str(get_storage().path_for(key))
    db.commit()

    return UploadInitOut(video_id=video.id, upload_key=key, chunk_size=CHUNK_SIZE)


@router.put("/{video_id}/upload/chunk")
async def upload_chunk(
    video_id: str,
    request: Request,
    offset: int = 0,
    content_range: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    video = _get_video(db, user, video_id)
    if video.status != VideoStatus.uploading:
        raise AppError("Upload already finalized", code="bad_state", status_code=409)

    # Content-Range: bytes <start>-<end>/<total> takes precedence over ?offset.
    if content_range and content_range.startswith("bytes "):
        try:
            offset = int(content_range.split(" ", 1)[1].split("-", 1)[0])
        except (ValueError, IndexError):
            raise AppError("Malformed Content-Range", code="bad_range", status_code=400) from None

    data = await request.body()
    key = _source_key(video_id, video.filename)
    total = get_storage().save_upload_chunk(key, offset, data)
    return {"received": len(data), "offset": offset, "size": total}


@router.post("/{video_id}/upload/complete", response_model=VideoOut)
def upload_complete(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Video:
    video = _get_video(db, user, video_id)
    storage = get_storage()
    key = _source_key(video_id, video.filename)
    if not storage.exists(key) or storage.size(key) == 0:
        raise AppError("No uploaded data found", code="empty_upload", status_code=400)

    video.status = VideoStatus.uploaded
    video.size_bytes = storage.size(key)
    db.commit()
    dispatch.enqueue_ingest(video.id)
    db.refresh(video)
    return video


# ---------------------------------------------------------------------------
# Single-shot upload (convenient for small sample videos)
# ---------------------------------------------------------------------------
@router.post("/upload", response_model=VideoOut, status_code=status.HTTP_201_CREATED)
async def upload_single(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Video:
    _check_ext(file.filename or "video.mp4")
    settings = get_settings()
    storage = get_storage()

    video = Video(
        user_id=user.id,
        filename=file.filename or "video.mp4",
        status=VideoStatus.uploading,
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    key = _source_key(video.id, video.filename)
    dest = storage.path_for(key)
    dest.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(CHUNK_SIZE):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                out.close()
                storage.delete(key)
                raise AppError(
                    "File exceeds maximum upload size", code="too_large", status_code=413
                )
            out.write(chunk)

    video.path = str(dest)
    video.size_bytes = written
    video.status = VideoStatus.uploaded
    db.commit()
    dispatch.enqueue_ingest(video.id)
    db.refresh(video)
    return video


# ---------------------------------------------------------------------------
# Import from a link (YouTube, Vimeo, Facebook, direct .mp4, ...)
# check link -> pick quality -> import (downloads in the background)
# ---------------------------------------------------------------------------
@router.post("/from-url/qualities", response_model=UrlProbeOut)
async def probe_url(body: UrlProbe, _: User = Depends(get_current_user)) -> UrlProbeOut:
    """List the qualities a link offers. The site lookup runs in a thread, so a
    slow site never blocks other requests."""
    settings = get_settings()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(downloader.probe, body.url), timeout=settings.url_probe_timeout_s
        )
    except TimeoutError:
        raise AppError(
            "The site took too long to answer. Try again.", code="probe_timeout", status_code=504
        ) from None
    except downloader.DownloadError as exc:
        raise AppError(str(exc), code="bad_link", status_code=400) from None
    return UrlProbeOut.model_validate(result)


@router.post("/from-url", response_model=VideoOut, status_code=status.HTTP_202_ACCEPTED)
def import_from_url(
    body: UrlImport,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Video:
    """Start importing a link. Returns at once with status ``downloading``; poll
    ``GET /videos/{id}`` for ``download_progress`` until it is ``ready``."""
    try:
        url = downloader.validate_url(body.url)
        downloader.parse_quality(body.quality)
    except downloader.DownloadError as exc:
        raise AppError(str(exc), code="bad_link", status_code=400) from None

    video = Video(
        user_id=user.id,
        filename=urlparse(url).hostname or "video",  # replaced by the title once downloaded
        status=VideoStatus.downloading,
        source_url=url,
        download_progress=0,
    )
    db.add(video)
    db.commit()
    dispatch.enqueue_download(video.id, body.quality)
    db.refresh(video)
    return video


# ---------------------------------------------------------------------------
# Rights confirmation + fetch
# ---------------------------------------------------------------------------
@router.post("/{video_id}/confirm-rights", response_model=VideoOut)
def confirm_rights(
    video_id: str,
    body: RightsConfirm,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Video:
    video = _get_video(db, user, video_id)
    if not body.confirmed:
        raise AppError(
            "You must confirm you own or are licensed to edit and publish this video.",
            code="rights_not_confirmed",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    video.rights_confirmed_at = datetime.now(UTC)
    db.commit()
    db.refresh(video)
    return video


@router.get("/{video_id}", response_model=VideoOut)
def get_video(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Video:
    return _get_video(db, user, video_id)
