from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

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
        json={"platforms": ["youtube_shorts"]},
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
            "captions": {"enabled": True, "style": "word_highlight"},
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["platforms"] == ["youtube_shorts", "youtube_clip"]
    assert job["id"] in client.enqueued["job"]
    assert job["status"] == "queued"


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
    job_resp = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]})
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

    zips = lambda: set(Path(tempfile.gettempdir()).glob("*.zip"))  # noqa: E731
    before = zips()
    z = client.get(f"/jobs/{job_id}/export.zip")
    assert 'filename="sample_clips.zip"' in z.headers["content-disposition"]
    assert zips() <= before  # the temp ZIP is deleted once sent
    out = tmp_path / "out.zip"
    out.write_bytes(z.content)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert names == {"facebook_video/part-041.mp4"}  # only the videos
        assert zf.read("facebook_video/part-041.mp4") == b"x" * 10


def test_approved_export_numbers_each_platform_from_one(client, db_session, tmp_path):
    """Approved parts 2 and 3 of one platform and part 2 of another: each
    platform's folder starts at part-001; a dubbed copy keeps its part's number."""
    import zipfile

    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]

    def clip(platform, rank, status, dub=None):
        mp4 = tmp_path / f"{platform}-{rank}{dub or ''}.mp4"
        mp4.write_bytes(f"{platform} {rank} {dub}".encode())
        c = Clip(
            job_id=job_id, platform=platform, start_s=rank * 60, end_s=rank * 60 + 50,
            rank=rank, status=status, file_path=str(mp4),
            extra={"dub": {"language": dub}} if dub else None,
        )  # fmt: skip
        db_session.add(c)

    ready, approved = ClipStatus.ready, ClipStatus.approved
    for platform, rank, status in [
        ("facebook_video", 1, ready),
        ("facebook_video", 2, approved),
        ("facebook_video", 3, approved),
        ("youtube_shorts", 1, ready),
        ("youtube_shorts", 2, approved),
    ]:
        clip(platform, rank, status)
    clip("facebook_video", 3, approved, dub="hi")
    db_session.commit()

    out = tmp_path / "approved.zip"
    out.write_bytes(client.get(f"/jobs/{job_id}/export.zip?only=approved").content)
    with zipfile.ZipFile(out) as zf:
        assert {n: zf.read(n) for n in zf.namelist()} == {
            "facebook_video/part-001.mp4": b"facebook_video 2 None",
            "facebook_video/part-002.mp4": b"facebook_video 3 None",
            "facebook_video/part-002-hi.mp4": b"facebook_video 3 hi",
            "youtube_shorts/part-001.mp4": b"youtube_shorts 2 None",
        }


def test_export_only_approved_and_approve_can_be_undone(client, db_session, tmp_path):
    import zipfile

    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]

    clips = []
    for rank in (1, 2, 3):
        mp4 = tmp_path / f"c{rank}.mp4"
        mp4.write_bytes(f"clip {rank}".encode())
        clip = Clip(
            job_id=job_id, platform="facebook_video", start_s=rank * 60, end_s=rank * 60 + 50,
            rank=rank, status=ClipStatus.ready, file_path=str(mp4),
        )  # fmt: skip
        db_session.add(clip)
        clips.append(clip)
    db_session.commit()

    # Nothing approved yet: the approved-only export says so.
    resp = client.get(f"/jobs/{job_id}/export.zip?only=approved")
    assert resp.status_code == 409 and "approved" in resp.json()["error"]["message"]

    for c in (clips[0], clips[2]):
        assert client.patch(f"/clips/{c.id}", json={"status": "approved"}).status_code == 200
    z = client.get(f"/jobs/{job_id}/export.zip?only=approved")
    assert 'filename="sample_approved.zip"' in z.headers["content-disposition"]
    out = tmp_path / "approved.zip"
    out.write_bytes(z.content)
    # Numbered again, in order: parts 1 and 3 come out as part-001 and part-002.
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == [
            "facebook_video/part-001.mp4",
            "facebook_video/part-002.mp4",
        ]
        assert zf.read("facebook_video/part-002.mp4") == b"clip 3"

    # Un-approve: back to ready, and gone from the approved export.
    undo = client.patch(f"/clips/{clips[2].id}", json={"status": "ready"})
    assert undo.json()["status"] == "ready"
    z = client.get(f"/jobs/{job_id}/export.zip?only=approved")
    out.write_bytes(z.content)
    with zipfile.ZipFile(out) as zf:
        assert {n.split("/")[1].split(".")[0] for n in zf.namelist()} == {"part-001"}

    # The plain export still has every rendered clip, with its own number.
    out.write_bytes(client.get(f"/jobs/{job_id}/export.zip").content)
    with zipfile.ZipFile(out) as zf:
        parts = {n.split("/")[1].split(".")[0] for n in zf.namelist()}
        assert parts == {"part-001", "part-002", "part-003"}
    assert client.get(f"/jobs/{job_id}/export.zip?only=junk").status_code == 422


def test_clip_reports_whether_it_has_speech(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]
    for rank, extra in ((1, {"has_speech": False}), (2, {"has_speech": True}), (3, {})):
        db_session.add(
            Clip(
                job_id=job_id, platform="facebook_video", start_s=rank, end_s=rank + 60,
                rank=rank, status=ClipStatus.ready, extra=extra,
            )
        )  # fmt: skip
    db_session.commit()
    got = [c["has_speech"] for c in client.get(f"/jobs/{job_id}/clips").json()]
    assert got == [False, True, None]  # None: clip made before this was recorded


def test_color_all_compares_with_the_grade_the_file_really_has(client, db_session):
    job_id, clips = _color_job(client, db_session)
    ready, approved = clips["ready"], clips["approved"]
    # Sliders saved at +10 but never applied: the file has no grade yet.
    ready.color_contrast = 10
    ready.extra = {"color_rendered": {"contrast": 0, "brightness": 0, "saturation": 0}}
    # The file already has +10; its sliders were later saved back to 0.
    approved.extra = {"color_rendered": {"contrast": 10, "brightness": 0, "saturation": 0}}
    db_session.commit()
    client.enqueued["clip"].clear()

    r = client.post(f"/jobs/{job_id}/color", json={"contrast": 10})
    assert r.status_code == 200 and r.json()["queued"] == 1
    # Re-rendered even though its saved sliders already said +10.
    assert [cid for cid, _ in client.enqueued["clip"]] == [ready.id]
    # Not re-rendered (the file is right); only its sliders were brought in line.
    db_session.refresh(approved)
    assert approved.status == ClipStatus.approved and approved.color_contrast == 10

    got = {c["id"]: c for c in client.get(f"/jobs/{job_id}/clips").json()}
    assert got[approved.id]["color_rendered"] == {"contrast": 10, "brightness": 0, "saturation": 0}
    assert got[clips["rendering"].id]["color_rendered"] is None  # never recorded


def test_clip_frame_patch(client, db_session):
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]
    clip = Clip(
        job_id=job_id, platform="facebook_video", start_s=0, end_s=30, status=ClipStatus.ready
    )
    db_session.add(clip)
    db_session.commit()
    assert client.get(f"/jobs/{job_id}/clips").json()[0]["frame"] is None

    body = {
        "background": "color",
        "bg_color": "#112233",
        "texts": [{"text": "Part {part}", "x": 100, "y": 1800, "align": "left"}],
    }
    ok = client.patch(f"/clips/{clip.id}", json={"frame": body})
    assert ok.status_code == 200
    got = ok.json()["frame"]
    assert got["bg_color"] == "#112233" and got["texts"][0]["text"] == "Part {part}"
    assert got["video_scale"] == 100 and got["texts"][0]["size"] == 60  # defaults filled in
    db_session.refresh(clip)
    assert clip.frame["texts"][0]["align"] == "left" and clip.frame["texts"][0]["outline"] is True

    # Bad colour / place / alignment rejected; null removes the frame.
    for bad in (
        {"texts": [{"color": "red"}]},
        {"texts": [{"x": 2000}]},
        {"texts": [{"align": "middle"}]},
        {"video_scale": 10},
    ):
        assert client.patch(f"/clips/{clip.id}", json={"frame": bad}).status_code == 422
    assert client.patch(f"/clips/{clip.id}", json={"frame": None}).json()["frame"] is None


def test_last_part_rounded_past_the_video_end_can_still_be_saved(client, db_session):
    # A clip's end is stored rounded to 3 decimals: the last part of a
    # 317.2078 s video ends at 317.208. Saving it (e.g. "Apply" in the mobile
    # preview sends start/end too) must not fail as "past the end".
    vid = _upload_sample(client)
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 317.2078
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()["id"]
    clip = Clip(
        job_id=job_id,
        platform="facebook_video",
        start_s=270,
        end_s=317.208,
        status=ClipStatus.ready,
    )
    db_session.add(clip)
    db_session.commit()
    ok = client.patch(f"/clips/{clip.id}", json={"start_s": 270, "end_s": 317.208})
    assert ok.status_code == 200
    # Really past the end is still refused.
    assert client.patch(f"/clips/{clip.id}", json={"end_s": 318.0}).status_code == 400
