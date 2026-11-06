"""Create a reel from a topic: Ollama writes the script (editable), FLUX.2
draws a picture per scene, Kokoro reads it out. Separate from the
upload -> clips flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, ConflictError, NotFoundError
from app.config.settings import get_settings
from app.db.session import get_db
from app.models import AudioTrack, Reel, User
from app.schemas.reel import (
    ReelCreate,
    ReelGenerate,
    ReelOut,
    ReelRewrite,
    ReelScript,
    SceneRephrase,
    SceneRephrased,
)
from app.services import bucket, reel_render, reel_script, tts
from app.services.naming import video_stem
from app.services.storage import get_storage
from app.tasks import dispatch

router = APIRouter(prefix="/reels", tags=["reels"])

BUSY = ("scripting", "generating")
# Working with no progress for this long means it was cut off (the worker or
# the Mac restarted mid-way); such a reel can be started again. A picture takes
# under a minute, a script under five.
STALLED_AFTER = timedelta(minutes=10)


def _get(db: Session, user: User, reel_id: str) -> Reel:
    reel = db.get(Reel, reel_id)
    if reel is None or reel.user_id != user.id:
        raise NotFoundError("Reel not found")
    return reel


def _stalled(reel: Reel) -> bool:
    if reel.status not in BUSY:
        return False
    updated = reel.updated_at
    if updated.tzinfo is None:  # SQLite (tests) keeps UTC without a zone
        updated = updated.replace(tzinfo=UTC)
    return datetime.now(UTC) - updated > STALLED_AFTER


def _not_busy(reel: Reel) -> None:
    if reel.status in BUSY and not _stalled(reel):
        raise ConflictError("This reel is still being worked on. Wait until it has finished.")


def _pictures(reel: Reel) -> list[Path]:
    if not reel.script:
        return []
    folder = reel_render.reel_dir(reel.id)
    return reel_render.scene_pictures(folder, reel.script, get_settings(), reel.picture_style)


def _warning(picture: Path) -> str | None:
    warn = reel_render.warning_path(picture)
    if not picture.exists() or not warn.exists():
        return None
    return warn.read_text(encoding="utf-8").strip() or None


def _video(reel: Reel) -> Path | None:
    """The reel's video file, if there is a usable one (a crash can leave it empty)."""
    video = Path(reel.file_path) if reel.file_path else None
    if video is None or not video.exists() or video.stat().st_size == 0:
        return None
    return video


def _out(reel: Reel) -> ReelOut:
    video = _video(reel)
    has_video = video is not None
    pictures = _pictures(reel)
    return ReelOut(
        id=reel.id,
        platform=reel.platform,
        language=reel.language,
        voice=reel.voice,
        topic=reel.topic,
        script_type=reel.script_type,
        description=reel.description or "",
        script_model=reel.script_model or get_settings().reel_script_model,
        duration_s=reel.duration_s,
        style=reel.style,
        brief_id=reel.brief_id,
        status=reel.status,
        stalled=_stalled(reel),
        stage=reel.stage,
        progress=reel.progress,
        error=reel.error,
        script=reel.script,
        music_id=reel.music_id,
        music_volume=reel.music_volume,
        has_video=has_video,
        video_version=int(video.stat().st_mtime) if has_video else None,
        video_duration_s=reel.video_duration_s,
        scene_pictures=[p.exists() for p in pictures],
        scene_warnings=[_warning(p) for p in pictures],
        bucket=bucket.copy_out(reel.bucket, video.stat().st_mtime_ns if has_video else None),
        created_at=reel.created_at,
        updated_at=reel.updated_at,
    )


def _check_voice(language: str, voice: str | None) -> str:
    if language not in tts.LANGUAGES:
        raise AppError(f"Unknown language '{language}'", code="bad_language")
    voice = voice or tts.default_voice(language)
    if voice not in tts.LANGUAGES[language].voices:
        raise AppError(f"Unknown voice '{voice}' for {language}", code="bad_voice")
    return voice


def _check_script_type(script_type: str) -> str:
    if script_type not in reel_script.SCRIPT_TYPES:
        raise AppError(f"Unknown script type '{script_type}'", code="bad_script_type")
    return script_type


def _check_script_model(name: str | None) -> str | None:
    """A model to write with, or None for the default."""
    if name is None or name == get_settings().reel_script_model:
        return None
    if name not in reel_script.SCRIPT_MODELS:
        raise AppError(f"Unknown script writer '{name}'", code="bad_script_model")
    return name


def _script_models(default: str) -> list[dict]:
    """The script writers to pick from: whether Ollama has each, and how much
    slower than gemma3:4b it writes (for the time estimates)."""
    installed = _installed_models()
    models = dict(reel_script.SCRIPT_MODELS)
    if default not in models:  # REEL_SCRIPT_MODEL set to another model
        models = {default: reel_script.ScriptModel(default, 1.0), **models}
    return [
        {
            "key": key,
            "label": m.label,
            "slowness": m.slowness,
            "installed": None if installed is None else key in installed,
        }
        for key, m in models.items()
    ]


def _installed_models() -> set[str] | None:
    """The models Ollama has (None if it can't be asked), for the picker."""
    try:
        r = httpx.get(f"{get_settings().ollama_url}/api/tags", timeout=2)
        return {m["name"] for m in r.json().get("models", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def _check_style(style: str) -> str:
    if style not in reel_render.STYLES:
        raise AppError(f"Unknown picture style '{style}'", code="bad_style")
    return style


def _start_render(db: Session, reel: Reel) -> None:
    reel.status = "generating"
    reel.stage = "Waiting to start"
    reel.progress = 0
    reel.error = None
    db.commit()
    dispatch.enqueue_reel_render(reel.id)


# What a script is written for; put back if a new one doesn't come.
_IDEA = ("script_type", "duration_s", "description", "script_model")


def _start_script(
    db: Session, reel: Reel, idea: ReelRewrite | None, then_render: bool = False
) -> None:
    """Ask the LLM for a new script (replaces the current one), optionally
    with another script type, length, description or writer; with
    ``then_render`` the video is made from it straight after."""
    before = {k: getattr(reel, k) for k in _IDEA} if reel.script else None
    if (
        idea is not None and idea.script_type is not None and not reel.brief_id
    ):  # told as its brief says
        reel.script_type = _check_script_type(idea.script_type)
    if idea is not None and idea.duration_s is not None:
        reel.duration_s = idea.duration_s
    if idea is not None and idea.description is not None:
        reel.description = idea.description or None
    if idea is not None and idea.script_model is not None:
        reel.script_model = _check_script_model(idea.script_model)
    reel.status = "scripting"
    reel.stage = "Writing the script"
    reel.error = None
    db.commit()
    dispatch.enqueue_reel_script(reel.id, before=before, then_render=then_render)


@router.get("/options")
def options() -> dict:
    """Platforms, languages with their voices, picture styles, the allowed length
    and rough timings."""
    settings = get_settings()
    return {
        "platforms": [{"key": k, "label": p.label} for k, p in reel_script.PLATFORMS.items()],
        "languages": [
            {
                "key": k,
                "label": lang.label,
                "voices": [
                    {"key": v, "label": label, "words_per_s": tts.words_per_s(k, v)}
                    for v, label in lang.voices.items()
                ],
            }
            for k, lang in tts.LANGUAGES.items()
        ],
        "story_pace": reel_render.STORY_PACE,
        "styles": [{"key": k, "label": s.label} for k, s in reel_render.STYLES.items()],
        "script_types": [{"key": k, "label": t.label} for k, t in reel_script.SCRIPT_TYPES.items()],
        "description_max": reel_script.MAX_DESCRIPTION,
        "script_models": _script_models(settings.reel_script_model),
        "script_model_default": settings.reel_script_model,
        "brief_script_model_default": settings.brief_script_model,
        "duration": {
            "min": reel_script.MIN_DURATION_S,
            "max": reel_script.MAX_DURATION_S,
            "default": 35,
        },
        "max_scenes": reel_script.MAX_SCENES,
        # Rough timings (seconds) for the time estimates in the picture style menus.
        "timing": {
            "picture_s": settings.reel_picture_seconds,
            "load_s": settings.reel_model_load_seconds,
            # Writing the script (gemma3:4b, loaded each time): base + per second of reel.
            "script_s": 25,
            "script_s_per_s": 0.5,
            # Voice and putting the video together, per second of reel (measured 15 s for 48 s).
            "render_s_per_s": 0.35,
        },
    }


@router.get("/voices/{language}/{voice}/sample")
def voice_sample(language: str, voice: str, user: User = Depends(get_current_user)) -> FileResponse:
    """A short line read by ``voice``, for the "Listen" button. Made on the
    first request (~2-3 s), then kept."""
    voice = _check_voice(language, voice)
    line = tts.LANGUAGES[language].sample
    folder = Path(get_settings().storage_path) / ".kokoro" / "samples"
    path = folder / f"{language}-{voice}-{reel_render._key(line, tts.voice_key(voice))}.wav"
    if not path.exists():
        try:
            samples, sample_rate = tts.synthesize(line, language, voice)
        except tts.TTSError as exc:
            raise AppError(str(exc), code="voice_failed", status_code=502) from exc
        folder.mkdir(parents=True, exist_ok=True)
        reel_render.write_wav(path, samples, sample_rate)
    return FileResponse(path, media_type="audio/wav")


@router.post("", response_model=ReelOut, status_code=status.HTTP_202_ACCEPTED)
def create_reel(
    body: ReelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReelOut:
    """Returns at once with status "scripting"; poll until "script_ready"."""
    if body.platform not in reel_script.PLATFORMS:
        raise AppError(f"Unknown platform '{body.platform}'", code="bad_platform")
    voice = _check_voice(body.language, body.voice)
    style = _check_style(body.style)
    script_type = _check_script_type(body.script_type)
    script_model = _check_script_model(body.script_model)
    reel = Reel(
        user_id=user.id,
        platform=body.platform,
        language=body.language,
        voice=voice,
        topic=body.topic,
        script_type=script_type,
        description=body.description or None,
        script_model=script_model,
        duration_s=body.duration_s,
        style=style,
        status="scripting",
        stage="Writing the script",
    )
    db.add(reel)
    db.commit()
    dispatch.enqueue_reel_script(reel.id)
    db.refresh(reel)
    return _out(reel)


@router.get("", response_model=list[ReelOut])
def list_reels(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reels = (
        db.query(Reel)
        .filter(Reel.user_id == user.id, Reel.brief_id.is_(None))  # not the Brief reels tab's
        .order_by(Reel.created_at.desc())
        .all()
    )
    return [_out(r) for r in reels]


@router.get("/{reel_id}", response_model=ReelOut)
def get_reel(reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _out(_get(db, user, reel_id))


@router.put("/{reel_id}/script", response_model=ReelOut)
def save_script(
    reel_id: str,
    body: ReelScript,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReelOut:
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    script = body.model_dump(exclude_none=True)  # no "music": null on reels without one
    script["hashtags"] = reel_script.clean_hashtags(script["hashtags"])
    if script != reel.script:
        reel.script = script
        # The video (if any) no longer matches; it stays playable until redone.
        reel.status = "script_ready"
    reel.error = None
    db.commit()
    db.refresh(reel)
    return _out(reel)


@router.post("/{reel_id}/music-suggestion", response_model=ReelOut)
def music_suggestion(
    reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ReelOut:
    """Ask the LLM (again) what background music fits the saved script: mood,
    genre, tempo, instruments and a YouTube search phrase. Saved in the
    script's "music"; waits for the answer (~5-15 s)."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    if not reel.script:
        raise ConflictError("Write the script first.")
    try:
        music = reel_script.suggest_music(reel.script, reel.platform, model=reel.script_model)
    except reel_script.ScriptError as exc:
        raise AppError(str(exc), code="music_suggestion_failed", status_code=502) from None
    db.refresh(reel)  # the call took a while: build on what is saved now
    reel.script = {**reel.script, "music": music}
    db.commit()
    db.refresh(reel)
    return _out(reel)


@router.post("/{reel_id}/scenes/{index}/rephrase", response_model=SceneRephrased)
def rephrase_scene(
    reel_id: str,
    index: int,
    body: SceneRephrase,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SceneRephrased:
    """Scene ``index``'s (0-based) spoken text said in other words, by the
    script writer picked (else the reel's); the other scenes play no part.
    Never more words than sent. Not saved: the page puts it in the script
    being edited. Waits for the answer (~5-45 s)."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    if not reel.script:
        raise ConflictError("Write the script first.")
    if not 0 <= index < reel_script.MAX_SCENES:
        raise NotFoundError("Scene not found")
    model = reel.script_model
    if body.model is not None:
        _check_script_model(body.model)  # an unknown one is refused
        model = body.model
    try:
        text = reel_script.rephrase_narration(body.narration, body.hint, reel.language, model=model)
    except reel_script.ScriptError as exc:
        raise AppError(str(exc), code="rephrase_failed", status_code=502) from None
    return SceneRephrased(narration=text, words=len(text.split()))


@router.post("/{reel_id}/rewrite", response_model=ReelOut, status_code=status.HTTP_202_ACCEPTED)
def rewrite_script(
    reel_id: str,
    body: ReelRewrite | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReelOut:
    """Ask the LLM for a new script (replaces the current one), optionally
    with another script type, length or description."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    _start_script(db, reel, body)
    db.refresh(reel)
    return _out(reel)


@router.post("/{reel_id}/generate", response_model=ReelOut, status_code=status.HTTP_202_ACCEPTED)
def generate_video(
    reel_id: str,
    body: ReelGenerate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReelOut:
    """Make the video from the saved script. Returns at once with status
    "generating"; poll until "ready" (about 20 s per new picture). With
    ``rewrite`` (another script type or length), a new script is written
    first ("scripting") and the video is made from it."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    if not reel.script:
        raise ConflictError("Write the script first.")
    reel.voice = _check_voice(reel.language, body.voice or reel.voice)
    reel.style = _check_style(body.style or reel.style)  # another style redraws every picture
    if body.music_id is not None:
        track = db.get(AudioTrack, body.music_id)
        if track is None or track.user_id != user.id or not track.is_ready:
            raise NotFoundError("Music track not found")
    reel.music_id = body.music_id
    reel.music_volume = body.music_volume
    if body.rewrite is not None:
        _start_script(db, reel, body.rewrite, then_render=True)  # the new voice sets its pace
    else:
        _start_render(db, reel)
    db.refresh(reel)
    return _out(reel)


@router.post(
    "/{reel_id}/scenes/{index}/new-picture",
    response_model=ReelOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def new_picture(
    reel_id: str, index: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ReelOut:
    """Draw scene ``index`` (0-based) again and redo the video; the other
    scenes' pictures and voice are reused."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    pictures = _pictures(reel)
    if not 0 <= index < len(pictures):
        raise NotFoundError("Scene not found")
    pictures[index].unlink(missing_ok=True)
    reel_render.warning_path(pictures[index]).unlink(missing_ok=True)
    if reel.brief_id and pictures[index].parent.is_dir():
        # Its pictures start from the reel's seed, which would draw this one again.
        reel_render.reseed_path(pictures[index]).touch()
    _start_render(db, reel)
    db.refresh(reel)
    return _out(reel)


@router.get("/{reel_id}/video")
def reel_video(
    reel_id: str,
    download: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    reel = _get(db, user, reel_id)
    if _video(reel) is None:
        raise NotFoundError("This reel has no video yet")
    if download:
        title = (reel.script or {}).get("title") or reel.topic
        return FileResponse(
            reel.file_path, media_type="video/mp4", filename=f"{video_stem(title)}.mp4"
        )
    return FileResponse(reel.file_path, media_type="video/mp4", content_disposition_type="inline")


@router.post("/{reel_id}/upload-to-bucket", response_model=ReelOut)
def upload_to_bucket(
    reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ReelOut:
    """Copy the reel's video to the bucket (BUCKET_* settings); waits for the
    upload (a few seconds). Uploading again replaces the copy."""
    reel = _get(db, user, reel_id)
    _not_busy(reel)
    video = _video(reel)
    if video is None:
        raise NotFoundError("This reel has no video yet")
    title = (reel.script or {}).get("title") or reel.topic
    key = (reel.bucket or {}).get("key") or bucket.key_for(
        "reels", f"{video_stem(title)}-{reel.id[:8]}.mp4"
    )
    saved = bucket.upload(video, key)
    db.refresh(reel)  # the upload took a while: build on what is saved now
    reel.bucket = saved
    db.commit()
    db.refresh(reel)
    return _out(reel)


@router.get("/{reel_id}/bucket")
def bucket_link(
    reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> RedirectResponse:
    """Opens the reel's copy in the bucket."""
    reel = _get(db, user, reel_id)
    if not reel.bucket:
        raise NotFoundError("This reel is not in the bucket")
    return RedirectResponse(bucket.link(reel.bucket["key"]))


@router.get("/{reel_id}/thumbnail")
def reel_thumbnail(
    reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FileResponse:
    reel = _get(db, user, reel_id)
    if not reel.thumbnail_path or not Path(reel.thumbnail_path).exists():
        raise NotFoundError("No thumbnail yet")
    return FileResponse(reel.thumbnail_path, media_type="image/jpeg")


@router.get("/{reel_id}/scenes/{index}/picture")
def scene_picture(
    reel_id: str, index: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FileResponse:
    pictures = _pictures(_get(db, user, reel_id))
    if not 0 <= index < len(pictures) or not pictures[index].exists():
        raise NotFoundError("No picture for this scene yet")
    return FileResponse(pictures[index], media_type="image/png")


@router.delete("/{reel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reel(
    reel_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    """Also allowed while it's being made: the worker stops at its next step.
    Its copy in the bucket, if any, is kept."""
    reel = _get(db, user, reel_id)
    db.delete(reel)
    db.commit()
    get_storage().delete(f"reels/{reel_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
