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
from app.models import AudioTrack, Brief, Reel
from app.services import brief_script, imagegen, music, reel_render, reel_script, tts
from app.services.ffmpeg import FFmpegError
from app.services.storage import get_storage
from app.tasks import dispatch
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
def write_reel_script(reel_id: str, before: dict | None = None, then_render: bool = False) -> None:
    """``before``: what the current script was written for (script type,
    length, description, writer), put back if no new script comes;
    ``then_render``: make the video from the new script straight after
    ("Generate video" with another script type or length)."""
    with SessionLocal() as db:
        reel = db.get(Reel, reel_id)
        if reel is None:
            return
        look = None  # a reel from a brief: the brief's look, as it is now
        try:
            # What it does next; also shows it is still at work (a 12B
            # model takes minutes, past which a reel looks cut off).
            def on_step(stage: str) -> bool:
                return _update(db, reel_id, stage=stage)

            if reel.brief_id:
                brief = db.get(Brief, reel.brief_id)
                if brief is None:
                    raise brief_script.BriefMissing(
                        "The brief of this reel was deleted, so its script can't be written again."
                    )
                look = brief.visual_style or None
                script = brief_script.generate_script(
                    {"niche": brief.niche, "rules": brief.rules, "visual_style": look or ""},
                    reel.topic,
                    reel.platform,
                    reel.language,
                    reel.duration_s,
                    reel.description or "",
                    reel.voice,
                    model=reel.script_model,
                    on_step=on_step,
                )
            else:
                script = reel_script.generate_script(
                    reel.topic,
                    reel.platform,
                    reel.language,
                    reel.duration_s,
                    reel.script_type,
                    reel.description or "",
                    reel.voice,
                    model=reel.script_model,
                    on_step=on_step,
                )
        except reel_script.ScriptError as exc:
            reel = db.get(Reel, reel_id, populate_existing=True)
            if reel is not None:
                # A failed "write again" keeps the script there was, and what
                # it was written for.
                status = "script_ready" if reel.script else "failed"
                kept = (before or {}) if reel.script else {}
                _update(db, reel_id, status=status, stage=None, error=str(exc)[:2000], **kept)
            return
        fields = {"script": script, "error": None}
        if look:
            fields["custom_style"] = look
        if not then_render:
            _update(db, reel_id, status="script_ready", stage=None, **fields)
            return
        fields |= {"status": "generating", "stage": "Waiting to start", "progress": 0}
        if _update(db, reel_id, **fields):
            dispatch.enqueue_reel_render(reel_id)


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
        script = reel.script
        scenes = script["scenes"]
        language, voice, style = reel.language, reel.voice, reel.picture_style
        # Told like a story: read a little slower, with its own voice clips.
        story = reel.script_type == "story"
        speed = reel_render.STORY_SPEED if story else 1.0
        track = db.get(AudioTrack, reel.music_id) if reel.music_id else None
        if track is not None and not track.is_ready:
            track = None
        music_volume = reel.music_volume

        folder = reel_render.reel_dir(reel_id)
        folder.mkdir(parents=True, exist_ok=True)
        pictures = reel_render.scene_pictures(folder, script, settings, style)
        voices = [
            reel_render.voice_path(
                folder,
                i,
                sc["narration"],
                language,
                tts.voice_key(voice) + (f"@{speed}" if story else ""),
            )  # fmt: skip
            for i, sc in enumerate(scenes)
        ]

        def _make_voices() -> None:
            for path, sc in zip(voices, scenes, strict=True):
                if not path.exists():
                    samples, sample_rate = tts.synthesize(
                        sc["narration"], language, voice, speed=speed, by_sentence=True
                    )
                    reel_render.write_wav(path, samples, sample_rate)

        missing = [i for i, p in enumerate(pictures) if not p.exists()]

        def _draw(n: int, i: int) -> bool:
            """Scene i's picture (the n-th to draw), drawn again with a new seed
            while Vision sees something wrong in it (an extra person, face or
            hands), up to reel_picture_redraws times. The best one is kept, with
            a warning beside it if it still looks wrong. False if the reel was
            deleted meanwhile."""
            prompt = reel_render.picture_prompt(reel_render.scene_prompt(scenes[i], script), style)
            best: tuple[imagegen.Picture, list[str]] | None = None
            for _ in range(1 + settings.reel_picture_redraws):
                stage = f"Picture {n + 1} of {len(missing)}"
                if best is not None:
                    stage += " (drawing it again)"
                if not _update(db, reel_id, stage=stage, progress=5 + 80 * n // len(missing)):
                    return False
                picture = imagegen.generate(
                    prompt,
                    width=settings.reel_image_width,
                    height=settings.reel_image_height,
                    seed=random.randrange(2**31),
                    steps=settings.reel_image_steps,
                )
                problems = reel_render.picture_problems(picture.counts, scenes[i])
                if best is None or len(problems) < len(best[1]):
                    best = (picture, problems)
                if not problems:
                    break
                log.info("picture_looks_wrong", extra={"reel_id": reel_id, "problems": problems})
            picture, problems = best
            warning = reel_render.warning_path(pictures[i])
            if problems:
                warning.write_text("; ".join(problems), encoding="utf-8")
            else:
                warning.unlink(missing_ok=True)
            part = pictures[i].with_name(pictures[i].name + ".part")
            part.write_bytes(picture.png)
            os.replace(part, pictures[i])
            return True

        try:
            if missing:
                imagegen.check()
            with ThreadPoolExecutor(max_workers=1) as pool:
                voice_job = pool.submit(_make_voices)
                try:
                    for n, i in enumerate(missing):
                        if not _draw(n, i):
                            break
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
                    story=story,
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
