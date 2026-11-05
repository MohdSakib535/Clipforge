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


def enqueue_reel_script(
    reel_id: str, before: dict | None = None, then_render: bool = False
) -> None:
    from app.tasks.reels import write_reel_script

    write_reel_script.delay(reel_id, before, then_render)


def enqueue_reel_render(reel_id: str) -> None:
    from app.tasks.reels import render_reel

    render_reel.delay(reel_id)


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


# Work that re-makes a clip already there: a re-render (trim, colour) on the
# "renders" queue and the background-music mix on the "audio" queue.
CLIP_WORK_QUEUES = ("renders", "audio")
CLIP_WORK_TASKS = {"render_single_clip", "mix_background_audio"}


def clip_work_pending() -> bool:
    """Is a clip re-render or music mix waiting in its queue or held by a
    worker? If not, a clip still marked "rendering" was cut off (its worker
    restarted mid-render) and nothing will ever finish it. True when it
    can't be told (the broker can't be reached)."""
    from app.tasks.celery_app import celery_app

    try:
        # Queue, then reserved, then active: a task moving along that way
        # while we look is still seen.
        with celery_app.connection_for_read() as conn:
            conn.ensure_connection(max_retries=1)
            channel = conn.default_channel
            for queue in CLIP_WORK_QUEUES:
                if channel.queue_declare(queue=queue, passive=False).message_count:
                    return True
        inspect = celery_app.control.inspect(timeout=1.0)
        for replies in (inspect.reserved(), inspect.active()):
            for tasks in (replies or {}).values():
                if any(t.get("name") in CLIP_WORK_TASKS for t in tasks):
                    return True
    except Exception:
        return True
    return False
