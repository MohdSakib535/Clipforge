"""Mix background music under clips. Runs on the ``audio`` queue (the
``mixer`` worker), so it never waits behind a long render on the main worker."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config.settings import get_settings
from app.core.logging import bind_job_id, get_logger
from app.db.session import SessionLocal
from app.models import AudioTrack, Clip, ClipStatus
from app.services import bgaudio
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="mix_background_audio")
def mix_background_audio(clip_ids: list[str], approved_ids: list[str]) -> None:
    """Each clip's settings (track + volume) are already saved; this applies
    them. Clips are mixed in parallel and each is marked done as it finishes.
    Only ffmpeg runs in the threads; DB writes stay on this thread."""
    approved = set(approved_ids)
    with SessionLocal() as db:
        work: list[tuple[str, str, str | None, int, bool]] = []
        for clip_id in clip_ids:
            clip = db.get(Clip, clip_id)
            if clip is None or not clip.file_path:
                continue
            bind_job_id(clip.job_id)
            track = db.get(AudioTrack, clip.bg_audio_id) if clip.bg_audio_id else None
            music = track.path if track else None
            work.append(
                (clip.id, clip.file_path, music, clip.bg_audio_volume, clip.bg_audio_mute_original)
            )

        workers = max(1, min(get_settings().copy_concurrency, len(work)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(bgaudio.apply, path, music, volume, mute_original=mute): clip_id
                for clip_id, path, music, volume, mute in work
            }
            for future in as_completed(futures):
                clip = db.get(Clip, futures[future])
                try:
                    future.result()
                except Exception as exc:
                    # The clip file is untouched on failure, so it stays playable.
                    log.exception("bg_audio_failed", extra={"clip_id": clip.id})
                    clip.error = f"Background music could not be applied: {exc}"[:2000]
                clip.status = ClipStatus.approved if clip.id in approved else ClipStatus.ready
                db.commit()
        bind_job_id(None)
