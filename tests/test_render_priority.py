"""A clip someone is editing right now must not wait behind a batch of
re-renders (e.g. a colour change on every clip)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Clip, ClipStatus, Video, VideoStatus
from app.tasks import dispatch


def test_dispatch_marks_edits_urgent(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.tasks.pipeline.render_single_clip.apply_async",
        lambda args, priority: sent.append((args, priority)),
    )
    dispatch.enqueue_clip_render("c1", True, urgent=True)
    dispatch.enqueue_clip_render("c2")
    # Redis serves lower numbers first
    assert sent == [(["c1", True], 0), (["c2", False], 9)]


def _job_with_clips(client, db_session, n):
    vid = client.post("/videos/upload", files={"file": ("v.mp4", b"\x00fake", "video/mp4")}).json()[
        "id"
    ]
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]
    clips = []
    for i in range(n):
        c = Clip(
            job_id=job_id, platform="facebook_video", start_s=i * 60, end_s=i * 60 + 60,
            rank=i + 1, status=ClipStatus.ready, file_path=f"/x/{i}.mp4",
        )  # fmt: skip
        db_session.add(c)
        clips.append(c)
    db_session.commit()
    return job_id, clips


def test_single_clip_rerender_is_urgent(client, db_session):
    _, clips = _job_with_clips(client, db_session, 1)
    assert client.post(f"/clips/{clips[0].id}/render").status_code == 200
    assert client.enqueued["clip_urgent"] == [True]


def test_colour_on_all_clips_is_a_batch_but_one_clip_is_urgent(client, db_session):
    job_id, clips = _job_with_clips(client, db_session, 3)
    r = client.post(f"/jobs/{job_id}/color", json={"contrast": 10})
    assert r.status_code == 200 and r.json()["queued"] == 3
    assert client.enqueued["clip_urgent"] == [False, False, False]

    for c in clips:
        c.status = ClipStatus.ready
    db_session.commit()
    one = client.post(f"/jobs/{job_id}/color", json={"contrast": 20, "clip_ids": [clips[0].id]})
    assert one.status_code == 200
    assert client.enqueued["clip_urgent"][-1] is True
