from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, ConflictError, NotFoundError
from app.config.platforms import get_preset
from app.db.session import get_db
from app.models import Clip, ClipStatus, Job, Transcript, User, Video
from app.schemas.clip import (
    ClipOut,
    ClipUpdate,
    DubRequest,
    TranslateRequest,
    TranslationLine,
    TranslationOut,
    VoiceOption,
)
from app.services import dub, tts
from app.services.naming import clip_basename
from app.services.romanize import contains_devanagari, is_mostly_devanagari, romanize
from app.tasks import dispatch

router = APIRouter(prefix="/clips", tags=["clips"])


def _download_name(clip: Clip) -> str:
    """e.g. part-041, or part-041-hi for a Hindi dub"""
    return clip_basename(clip.rank, clip.dub_language)


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
    keep_approved = clip.status == ClipStatus.approved
    clip.status = ClipStatus.rendering
    clip.error = None
    db.commit()
    dispatch.enqueue_clip_render(clip.id, keep_approved, urgent=True)
    db.refresh(clip)
    return clip


def _clip_words(db: Session, clip: Clip) -> list[dict]:
    """Timed words said in the clip: its own (made from a trim with the
    transcript on), else the video's transcript."""
    own = (clip.extra or {}).get("words")
    if own is not None:
        return own
    video_id = db.get(Job, clip.job_id).video_id
    transcript = db.query(Transcript).filter(Transcript.video_id == video_id).one_or_none()
    words = transcript.words if transcript else []
    return [w for w in words if w["end"] > clip.start_s and w["start"] < clip.end_s]


def _dub_language(language: str) -> tts.Language:
    if language not in dub.LANGUAGES:
        raise AppError(f"Dubbing into '{language}' isn't available.", code="bad_language")
    return tts.LANGUAGES[language]


def _not_dubbed(clip: Clip) -> None:
    if clip.dub:
        raise ConflictError("This reel is already dubbed. Use the original reel.")


@router.post("/{clip_id}/translate", response_model=TranslationOut)
def translate_clip(
    clip_id: str,
    body: TranslateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TranslationOut:
    """Translate what is said in the clip, sentence by sentence, with the
    local LLM (waits for the answer, ~5-20 s). Kept on the clip. Edit the
    lines if needed, then POST /clips/{id}/dub."""
    clip = _get_clip(db, user, clip_id)
    lang = _dub_language(body.language)
    _not_dubbed(clip)
    lines = dub.sentences(_clip_words(db, clip))
    if not lines:
        raise ConflictError(
            "This reel has no transcript to translate. Make it with the transcript on."
        )
    try:
        translated = dub.translate(lines, body.language)
    except dub.DubError as exc:
        raise AppError(str(exc), code="translation_failed", status_code=502) from None
    out = [{**line, "translation": text} for line, text in zip(lines, translated, strict=True)]
    db.refresh(clip)  # the call took a while: build on what is saved now
    clip.extra = {**(clip.extra or {}), "translation": {"language": body.language, "lines": out}}
    db.commit()
    return TranslationOut(
        language=body.language,
        label=lang.label,
        voices=[VoiceOption(key=k, label=v) for k, v in lang.voices.items()],
        lines=[
            TranslationLine(
                **line,
                roman=(
                    romanize(line["translation"])
                    if contains_devanagari(line["translation"])
                    else None
                ),
            )
            for line in out
        ],
    )


@router.post("/{clip_id}/dub", response_model=ClipOut, status_code=status.HTTP_202_ACCEPTED)
def dub_clip(
    clip_id: str,
    body: DubRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Clip:
    """Make a copy of the clip whose audio is ``lines`` read out in
    ``language``; the original audio is dropped, the original clip is kept.
    Returns the copy at once ("rendering"); it keeps the colour and music."""
    clip = _get_clip(db, user, clip_id)
    lang = _dub_language(body.language)
    _not_dubbed(clip)
    voice = body.voice or tts.default_voice(body.language)
    if voice not in lang.voices:
        raise AppError(f"Unknown voice '{voice}' for {lang.label}", code="bad_voice")
    for n, line in enumerate(body.lines, start=1):
        inside = clip.start_s - 0.5 <= line.start_s < clip.end_s
        if line.end_s <= line.start_s or not inside:
            raise AppError(f"Line {n} is outside this reel.", code="out_of_range")

    lines = [line.model_dump() for line in body.lines]
    text = " ".join(line["text"] for line in lines)
    roman = romanize(text) if is_mostly_devanagari(text) else text
    snippet = " ".join(roman.split()[:9])
    copy = Clip(
        job_id=clip.job_id,
        platform=clip.platform,
        start_s=clip.start_s,
        end_s=clip.end_s,
        duration_s=clip.duration_s,
        rank=clip.rank,
        title=(f"Reel {clip.rank} ({lang.label})" + (f" - {snippet}" if snippet else ""))[:120],
        description=roman[:800],
        hashtags=[],
        captions_enabled=True,
        language=body.language,
        color_contrast=clip.color_contrast,
        color_brightness=clip.color_brightness,
        color_saturation=clip.color_saturation,
        bg_audio_id=clip.bg_audio_id,
        bg_audio_volume=clip.bg_audio_volume,
        status=ClipStatus.rendering,
        extra={
            "has_speech": True,
            "transcript": text,
            "dub": {
                "language": body.language,
                "voice": voice,
                "lines": lines,
                "source_clip": clip.id,
            },
        },
    )
    db.add(copy)
    db.commit()
    dispatch.enqueue_clip_render(copy.id, urgent=True)
    db.refresh(copy)
    return copy


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
