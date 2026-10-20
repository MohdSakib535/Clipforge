from __future__ import annotations

from celery import Celery

from app.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "clipforge",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.pipeline", "app.tasks.downloads", "app.tasks.audio"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Laptop mode: one worker, concurrency 1 (set via CLI / compose command too).
    worker_concurrency=1,
    broker_connection_retry_on_startup=True,
    # Link imports and background-music mixing get their own queues + workers
    # ("downloader" and "mixer" in compose), so they never wait behind a long
    # clip job on the main worker, nor hold it up.
    task_routes={
        "download_video": {"queue": "downloads"},
        "download_audio_track": {"queue": "downloads"},
        "mix_background_audio": {"queue": "audio"},
    },
)
