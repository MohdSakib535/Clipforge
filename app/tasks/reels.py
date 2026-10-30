"""Reel maker tasks. They run on the ``reels`` queue (the ``reelmaker``
worker), one at a time: every reel shares the Mac's GPU and RAM, and they
never wait behind (or hold up) clip jobs on the main worker."""

from __future__ import annotations

import os
import random
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models import AudioTrack, Reel
from app.services import imagegen, music, reel_render, reel_script, tts
from app.services.ffmpeg import FFmpegError
from app.services.storage import get_storage
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


def _update(db: Session, reel_id: str, **fields) -> bool:
    """Save fields on the reel; False if it was deleted meanwhile."""
    reel = db.get(Reel, reel_id, populate_existing=True)
    if reel is None:
        return False
    for name, value in fields.items():
        setattr(reel, name, value)
    db.commit()
    return True


@celery_app.task(name="write_reel_script")
def write_reel_script(reel_id: str) -> None:
    with SessionLocal() as db:
        reel = db.get(Reel, reel_id)
        if reel is None:
            return
        try:
            script = reel_script.generate_script(
                reel.topic,
                reel.platform,
                reel.language,
                reel.duration_s,
                reel.script_type,
                reel.description or "",
                reel.voice,
            )
        except reel_script.ScriptError as exc:
            reel = db.get(Reel, reel_id, populate_existing=True)
            if reel is not None:
                # A failed "write again" keeps the script there was.
                status = "script_ready" if reel.script else "failed"
                _update(db, reel_id, status=status, stage=None, error=str(exc)[:2000])
            return
        _update(db, reel_id, script=script, status="script_ready", stage=None, error=None)


@celery_app.task(name="render_reel")
def render_reel(reel_id: str) -> None:
    """Pictures (on the Mac's GPU, one by one) while the voice-over is made
    on the CPU, then the video. Pictures and voice clips already made for the
    same text are reused, so changing one scene only redoes that scene."""
    settings = get_settings()
    with SessionLocal() as db:
        reel = db.get(Reel, reel_id)
        if reel is None or not reel.script:
            return
        scenes = reel.script["scenes"]
        language, voice, style = reel.language, reel.voice, reel.style
        track = db.get(AudioTrack, reel.music_id) if reel.music_id else None
        if track is not None and not track.is_ready:
            track = None
        music_volume = reel.music_volume

        folder = reel_render.reel_dir(reel_id)
        folder.mkdir(parents=True, exist_ok=True)
        pictures = reel_render.scene_pictures(folder, scenes, settings, style)
        voices = [
            reel_render.voice_path(folder, i, sc["narration"], language, tts.voice_key(voice))
            for i, sc in enumerate(scenes)
        ]

        def _make_voices() -> None:
            for path, sc in zip(voices, scenes, strict=True):
                if not path.exists():
                    samples, sample_rate = tts.synthesize(sc["narration"], language, voice)
                    reel_render.write_wav(path, samples, sample_rate)

        try:
            missing = [i for i, p in enumerate(pictures) if not p.exists()]
            if missing:
                imagegen.check()
            with ThreadPoolExecutor(max_workers=1) as pool:
                voice_job = pool.submit(_make_voices)
                try:
                    for done, i in enumerate(missing):
                        stage = f"Picture {done + 1} of {len(missing)}"
                        progress = 5 + 80 * done // len(missing)
                        if not _update(db, reel_id, stage=stage, progress=progress):
                            break
                        png = imagegen.generate(
                            reel_render.picture_prompt(scenes[i]["image_prompt"], style),
                            width=settings.reel_image_width,
                            height=settings.reel_image_height,
                            seed=random.randrange(2**31),
                            steps=settings.reel_image_steps,
                        )
                        part = pictures[i].with_name(pictures[i].name + ".part")
                        part.write_bytes(png)
                        os.replace(part, pictures[i])
                finally:
                    if missing:
                        # Holding the model with Docker running swamped a 16 GB Mac.
                        imagegen.unload()
                voice_job.result()
            if not _update(db, reel_id, stage="Putting the video together", progress=90):
                get_storage().delete(f"reels/{reel_id}")  # deleted while it was working
                return
            # The music comes from the bucket for this render only.
            with music.working_copy(track) as music_path:
                duration = reel_render.render(
                    folder,
                    [
                        reel_render.SceneInput(p, sc["narration"], v)
                        for p, sc, v in zip(pictures, scenes, voices, strict=True)
                    ],
                    font=tts.LANGUAGES[language].caption_font,
                    music=music_path,
                    music_volume=music_volume,
                    workers=settings.render_concurrency,
                )
            reel_render.remove_unused(folder, set(pictures) | set(voices))
        except (imagegen.ImageGenError, tts.TTSError, FFmpegError, music.MusicError) as exc:
            _update(db, reel_id, status="failed", stage=None, error=str(exc)[:2000])
            return
        except Exception as exc:
            log.exception("reel_render_failed", extra={"reel_id": reel_id})
            error = f"Unexpected error: {exc}"[:2000]
            _update(db, reel_id, status="failed", stage=None, error=error)
            return

        _update(
            db,
            reel_id,
            status="ready",
            stage=None,
            progress=100,
            error=None,
            file_path=str(folder / reel_render.VIDEO_NAME),
            thumbnail_path=str(folder / reel_render.THUMB_NAME),
            video_duration_s=round(duration, 2),
        )
