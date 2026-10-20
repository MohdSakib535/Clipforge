from __future__ import annotations

from datetime import UTC, datetime

from app.models import Clip, ClipStatus, Video, VideoStatus


def _upload_sample(client) -> str:
    resp = client.post(
        "/videos/upload",
        files={"file": ("sample.mp4", b"\x00\x01\x02fake-bytes", "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_health(client):
    assert client.get("/health").status_code == 200


def test_upload_rejects_bad_extension(client):
    resp = client.post(
        "/videos/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_type"


def test_upload_enqueues_ingest(client):
    vid = _upload_sample(client)
    assert vid in client.enqueued["ingest"]
    body = client.get(f"/videos/{vid}").json()
    assert body["status"] == VideoStatus.uploaded.value


def test_confirm_rights_required_true(client):
    vid = _upload_sample(client)
    resp = client.post(f"/videos/{vid}/confirm-rights", json={"confirmed": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "rights_not_confirmed"

    ok = client.post(f"/videos/{vid}/confirm-rights", json={"confirmed": True})
    assert ok.status_code == 200
    assert ok.json()["rights_confirmed_at"] is not None


def test_create_job_blocked_without_rights(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    db_session.commit()

    resp = client.post(
        f"/videos/{vid}/jobs",
        json={"platforms": ["youtube_shorts"], "clip_count": 3},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "rights_not_confirmed"


def test_create_job_blocked_when_not_ready(client):
    vid = _upload_sample(client)
    client.post(f"/videos/{vid}/confirm-rights", json={"confirmed": True})
    resp = client.post(
        f"/videos/{vid}/jobs",
        json={"platforms": ["youtube_shorts"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "video_not_ready"


def test_create_job_success_enqueues(client, db_session):
    vid = _upload_sample(client)
    client.post(f"/videos/{vid}/confirm-rights", json={"confirmed": True})
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()

    resp = client.post(
        f"/videos/{vid}/jobs",
        json={
            "platforms": ["youtube_shorts", "youtube_clip"],
            "clip_count": 4,
            "captions": {"enabled": True, "style": "word_highlight"},
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["platforms"] == ["youtube_shorts", "youtube_clip"]
    assert job["id"] in client.enqueued["job"]
    assert job["status"] == "queued"


def test_create_job_with_per_platform_clip_counts(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    resp = client.post(
        f"/videos/{vid}/jobs",
        json={
            "platforms": ["youtube_shorts", "facebook_reel"],
            "clip_counts": {"youtube_shorts": 3, "facebook_reel": 8},
        },
    )
    assert resp.status_code == 201, resp.text
    settings = resp.json()["settings"]
    assert settings["clip_counts"] == {"youtube_shorts": 3, "facebook_reel": 8}


def test_create_job_coverage_mode(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    resp = client.post(
        f"/videos/{vid}/jobs",
        json={
            "platforms": ["youtube_shorts"],
            "coverage_lengths": {"youtube_shorts": 40},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["settings"]["coverage_lengths"] == {"youtube_shorts": 40}


def test_create_job_rejects_bad_coverage_length(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    # youtube_shorts max is 60s; 500s is out of range.
    resp = client.post(
        f"/videos/{vid}/jobs",
        json={"platforms": ["youtube_shorts"], "coverage_lengths": {"youtube_shorts": 500}},
    )
    assert resp.status_code == 422


def test_create_job_rejects_bad_clip_count(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    resp = client.post(
        f"/videos/{vid}/jobs",
        json={"platforms": ["youtube_shorts"], "clip_counts": {"youtube_shorts": 99}},
    )
    assert resp.status_code == 422


def test_create_job_rejects_unknown_platform(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    resp = client.post(f"/videos/{vid}/jobs", json={"platforms": ["tiktok"]})
    assert resp.status_code == 422


def test_clip_patch_and_download_flow(client, db_session):
    # Build a job + clip directly, then exercise the clip endpoints.
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_resp = client.post(
        f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"], "clip_count": 2}
    )
    job_id = job_resp.json()["id"]

    clip = Clip(
        job_id=job_id,
        platform="youtube_shorts",
        start_s=10.0,
        end_s=40.0,
        status=ClipStatus.ready,
    )
    db_session.add(clip)
    db_session.commit()

    # PATCH trim + title
    patch = client.patch(
        f"/clips/{clip.id}", json={"start_s": 12.0, "end_s": 42.0, "title": "New Title"}
    )
    assert patch.status_code == 200
    assert patch.json()["title"] == "New Title"
    assert patch.json()["duration_s"] == 30.0

    # PATCH beyond duration rejected
    bad = client.patch(f"/clips/{clip.id}", json={"end_s": 9999.0})
    assert bad.status_code == 400

    # Download before render -> 409
    dl = client.get(f"/clips/{clip.id}/download")
    assert dl.status_code == 409

    # No sidecar captions yet -> 404, and the flag says so.
    assert client.get(f"/clips/{clip.id}/captions.srt").status_code == 404
    assert client.get(f"/jobs/{job_id}/clips").json()[0]["has_srt"] is False

    # Re-render enqueues
    rr = client.post(f"/clips/{clip.id}/render")
    assert rr.status_code == 200
    assert clip.id in [cid for cid, _ in client.enqueued["clip"]]

    # Clips list for job
    listed = client.get(f"/jobs/{job_id}/clips")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_clip_color_grade_patch(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_clip"]}).json()["id"]
    clip = Clip(
        job_id=job_id, platform="youtube_clip", start_s=0, end_s=30, status=ClipStatus.ready
    )
    db_session.add(clip)
    db_session.commit()

    # Defaults are 0 (no grade).
    got = client.get(f"/jobs/{job_id}/clips").json()[0]
    assert (got["color_contrast"], got["color_brightness"], got["color_saturation"]) == (0, 0, 0)

    ok = client.patch(
        f"/clips/{clip.id}",
        json={"color_contrast": 15, "color_brightness": -10, "color_saturation": 20},
    )
    assert ok.status_code == 200
    assert ok.json()["color_contrast"] == 15 and ok.json()["color_saturation"] == 20
    db_session.refresh(clip)
    assert clip.color_contrast == 15 and clip.color_brightness == -10

    # Out-of-range is rejected; a re-render still enqueues.
    assert client.patch(f"/clips/{clip.id}", json={"color_contrast": 99}).status_code == 422
    assert client.post(f"/clips/{clip.id}/render").status_code == 200
    assert clip.id in [cid for cid, _ in client.enqueued["clip"]]


def _color_job(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_clip"]}).json()["id"]
    clips = {}
    for i, st in enumerate([ClipStatus.ready, ClipStatus.approved, ClipStatus.rendering], 1):
        c = Clip(
            job_id=job_id,
            platform="youtube_clip",
            start_s=0,
            end_s=30,
            rank=i,
            status=st,
            file_path=f"/x/{i}.mp4",
        )
        db_session.add(c)
        clips[st.value] = c
    db_session.commit()
    return job_id, clips


def test_color_grade_all_clips(client, db_session):
    job_id, clips = _color_job(client, db_session)
    client.enqueued["clip"].clear()

    r = client.post(f"/jobs/{job_id}/color", json={"contrast": 10, "brightness": -5})
    assert r.status_code == 200, r.text
    body = r.json()
    # ready + approved queued; the rendering one skipped
    assert body["queued"] == 2 and body["skipped"] == 1
    ready, approved = clips["ready"], clips["approved"]
    assert {cid for cid, _ in client.enqueued["clip"]} == {ready.id, approved.id}
    keep = dict(client.enqueued["clip"])
    assert keep[approved.id] is True and keep[ready.id] is False
    for c in (ready, approved):
        db_session.refresh(c)
        assert c.status == ClipStatus.rendering
        assert (c.color_contrast, c.color_brightness, c.color_saturation) == (10, -5, 0)
    db_session.refresh(clips["rendering"])
    assert clips["rendering"].color_contrast == 0  # untouched

    # Same grade again (pretend the renders finished) changes nothing -> 409.
    ready.status = approved.status = ClipStatus.ready
    db_session.commit()
    again = client.post(f"/jobs/{job_id}/color", json={"contrast": 10, "brightness": -5})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "nothing_to_apply"


def test_color_grade_specific_clip_and_reset(client, db_session):
    job_id, clips = _color_job(client, db_session)
    one = clips["ready"]
    client.enqueued["clip"].clear()

    r = client.post(f"/jobs/{job_id}/color", json={"saturation": 20, "clip_ids": [one.id]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    assert client.enqueued["clip"] == [(one.id, False)]
    db_session.refresh(one)
    assert one.color_saturation == 20

    # Reset (all zeros) only re-renders clips that currently have a grade.
    one.status = ClipStatus.ready
    db_session.commit()
    client.enqueued["clip"].clear()
    reset = client.post(f"/jobs/{job_id}/color", json={"contrast": 0, "saturation": 0})
    assert reset.status_code == 200 and reset.json()["queued"] == 1
    db_session.refresh(one)
    assert one.color_saturation == 0


def test_color_grade_validation(client, db_session):
    job_id, _ = _color_job(client, db_session)
    assert client.post(f"/jobs/{job_id}/color", json={"contrast": 99}).status_code == 422
    assert (
        client.post(f"/jobs/{job_id}/color", json={"clip_ids": ["x"], "contrast": 5}).status_code
        == 404
    )
    assert client.post("/jobs/missing/color", json={"contrast": 5}).status_code == 404


def test_export_zip_empty_is_409(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]}).json()["id"]
    resp = client.get(f"/jobs/{job_id}/export.zip")
    assert resp.status_code == 409


def test_unknown_video_404(client):
    assert client.get("/videos/does-not-exist").status_code == 404


def test_download_and_zip_names_are_just_the_part_number(client, db_session, tmp_path):
    import zipfile

    vid = _upload_sample(client)  # uploaded as "sample.mp4"
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]

    mp4, srt = tmp_path / "c.mp4", tmp_path / "c.srt"
    mp4.write_bytes(b"x" * 10)
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    clip = Clip(
        job_id=job_id, platform="facebook_video", start_s=1, end_s=61, rank=41,
        status=ClipStatus.ready, file_path=str(mp4), title="T",
    )  # fmt: skip
    db_session.add(clip)
    db_session.commit()

    dl = client.get(f"/clips/{clip.id}/download")
    assert 'filename="part-041.mp4"' in dl.headers["content-disposition"]
    cap = client.get(f"/clips/{clip.id}/captions.srt")
    assert 'filename="part-041.srt"' in cap.headers["content-disposition"]

    z = client.get(f"/jobs/{job_id}/export.zip")
    assert 'filename="sample_clips.zip"' in z.headers["content-disposition"]
    out = tmp_path / "out.zip"
    out.write_bytes(z.content)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        base = "facebook_video/part-041"
        assert names == {f"{base}.mp4", f"{base}.srt", f"{base}.json", f"{base}.txt"}
        assert "Source video: sample.mp4" in zf.read(f"{base}.txt").decode()
        assert '"part": 41' in zf.read(f"{base}.json").decode()
