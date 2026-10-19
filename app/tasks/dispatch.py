from __future__ import annotations

"""Thin indirection over Celery ``.delay`` so the API never imports heavy task
modules directly and tests can patch enqueueing."""


def enqueue_ingest(video_id: str) -> None:
    from app.tasks.pipeline import ingest_video

    ingest_video.delay(video_id)


def enqueue_job(job_id: str) -> None:
    from app.tasks.pipeline import run_job

    run_job.delay(job_id)


def enqueue_clip_render(clip_id: str) -> None:
    from app.tasks.pipeline import render_single_clip

    render_single_clip.delay(clip_id)
