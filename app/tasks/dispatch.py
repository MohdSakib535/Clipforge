from __future__ import annotations

"""Thin indirection over Celery ``.delay`` so the API never imports heavy task
modules directly and tests can patch enqueueing."""


def enqueue_ingest(video_id: str) -> None:
    from app.tasks.pipeline import ingest_video

    ingest_video.delay(video_id)


def enqueue_download(video_id: str, quality: str) -> None:
    from app.tasks.downloads import download_video

    download_video.delay(video_id, quality)


def enqueue_job(job_id: str) -> None:
    from app.tasks.pipeline import run_job

    run_job.delay(job_id)


def enqueue_audio_download(track_id: str) -> None:
    from app.tasks.downloads import download_audio_track

    download_audio_track.delay(track_id)


def enqueue_bg_audio_mix(clip_ids: list[str], approved_ids: list[str]) -> None:
    from app.tasks.audio import mix_background_audio

    mix_background_audio.delay(clip_ids, approved_ids)


# Redis serves lower numbers first (kombu checks priority 0 of every queue
# before priority 3, 6, 9), so an edit someone is waiting for jumps ahead of a
# batch such as a colour change on every clip.
URGENT_PRIORITY = 0
BATCH_PRIORITY = 9


def enqueue_clip_render(clip_id: str, keep_approved: bool = False, urgent: bool = False) -> None:
    """``urgent``: one clip someone is editing right now; it is rendered
    before any batch re-renders still waiting in the queue."""
    from app.tasks.pipeline import render_single_clip

    render_single_clip.apply_async(
        args=[clip_id, keep_approved], priority=URGENT_PRIORITY if urgent else BATCH_PRIORITY
    )
