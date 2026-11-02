from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.models import AudioTrack, Clip, ClipStatus, Video, VideoStatus
from app.services import bgaudio, ffmpeg
from app.services.ffmpeg import FFmpegError, MediaInfo
from app.services.storage import get_storage
from tests.test_bucket_uploads import s3  # noqa: F401 - the fake bucket fixture


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------
def test_mix_cmd_copies_video_loops_music_and_keeps_length():
    cmd = bgaudio.build_mix_cmd("c.mp4", "m.m4a", "o.mp4", volume_pct=25, duration_s=30.0)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    # -stream_loop applies to the music input (the second -i)
    assert cmd[cmd.index("-stream_loop") + 3] == "m.m4a"
    assert cmd[cmd.index("-t") + 1] == "30.000"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0.25" in graph
    assert "amix=inputs=2:duration=first" in graph and "normalize=0" in graph
    assert "afade=t=out:st=28.500:d=1.500" in graph
    assert "alimiter" in graph


def test_mix_cmd_without_clip_audio_uses_music_only():
    cmd = bgaudio.build_mix_cmd(
        "c.mp4", "m.m4a", "o.mp4", volume_pct=80, duration_s=4.0, clip_has_audio=False
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" not in graph and "[0:a]" not in graph
    assert "volume=0.80" in graph
    assert "afade=t=out:st=3.000:d=1.000" in graph  # short clip -> shorter fade


def test_prepare_track_cmd_normalises_like_clips():
    cmd = bgaudio.build_prepare_track_cmd("song.mp3", "track.m4a")
    assert cmd[cmd.index("-af") + 1] == ffmpeg.AUDIO_NORM_FILTER
    assert "aac" in cmd and "-vn" in cmd


# ---------------------------------------------------------------------------
# Real ffmpeg (synthetic tones): mix, re-mix, remove, failure
# ---------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _make_clip(path: Path, *, with_audio: bool = True, seconds: int = 4) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:d={seconds}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    # Stereo AAC, like every rendered clip
    cmd += ["-c:a", "aac", "-ac", "2"] if with_audio else []
    subprocess.run([*cmd, str(path)], check=True, capture_output=True)


def _make_music(tmp: Path) -> str:
    raw = tmp / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1.5", str(raw)],
        check=True,
        capture_output=True,
    )
    track = tmp / "track.m4a"
    info = bgaudio.prepare_track(raw, track)
    assert info.audio_tracks == 1 and 1.4 < info.duration_s < 1.7
    return str(track)


@needs_ffmpeg
def test_apply_change_and_remove_music(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_clip(clip)
    original = clip.read_bytes()
    music = _make_music(tmp_path)
    unmixed = tmp_path / bgaudio.UNMIXED_NAME

    bgaudio.apply(clip, music, 30)
    assert unmixed.read_bytes() == original  # rendered clip kept aside
    mixed = ffmpeg.ffprobe_metadata(clip)
    assert mixed.audio_tracks == 1 and mixed.video_codec == "h264"
    # 1.5s of music looped to cover the whole 4s clip
    assert abs(mixed.duration_s - 4.0) < 0.2

    first_mix = clip.read_bytes()
    bgaudio.apply(clip, music, 90)  # change volume: re-mixed from the kept copy
    assert clip.read_bytes() != first_mix
    assert unmixed.read_bytes() == original

    bgaudio.apply(clip, None, 25)  # remove
    assert clip.read_bytes() == original
    assert not unmixed.exists()


def _mean_volume_db(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr
    return float(re.search(r"mean_volume: ([-\d.]+) dB", out).group(1))


@needs_ffmpeg
def test_music_never_makes_the_speech_quieter(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_clip(clip)
    before = _mean_volume_db(clip)
    music = _make_music(tmp_path)

    bgaudio.apply(clip, music, 0)
    assert abs(_mean_volume_db(clip) - before) < 0.3  # speech level untouched
    bgaudio.apply(clip, music, 100)
    assert _mean_volume_db(clip) > before + 1  # music audibly added on top


def _band_db(path: Path, freq: int) -> float:
    out = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"bandpass=f={freq}:width_type=h:w=40,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stderr
    return float(re.search(r"mean_volume: ([-\d.]+) dB", out).group(1))


@needs_ffmpeg
def test_mute_original_plays_music_only(tmp_path):
    # "speech" = 300 Hz, "music" = 880 Hz, in separate bands so we can tell
    # which survives the mix.
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=25:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    raw = tmp_path / "m.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )
    music = str(bgaudio.prepare_track(raw, tmp_path / "track.m4a") and tmp_path / "track.m4a")

    speech0, music0 = _band_db(clip, 300), _band_db(clip, 880)
    assert music0 < -40  # no music to start

    # Normal mix: both speech and music present
    bgaudio.apply(clip, music, 100)
    assert _band_db(clip, 300) > speech0 - 2  # speech kept
    assert _band_db(clip, 880) > music0 + 15  # music added

    # Mute original: speech gone, music still there
    bgaudio.apply(clip, music, 100, mute_original=True)
    assert _band_db(clip, 300) < speech0 - 15  # speech removed
    assert _band_db(clip, 880) > music0 + 15  # music present

    # Untick mute (still music on): speech returns
    bgaudio.apply(clip, music, 100, mute_original=False)
    assert _band_db(clip, 300) > speech0 - 2

    # Remove music entirely: back to the original
    bgaudio.apply(clip, None, 100)
    assert abs(_band_db(clip, 300) - speech0) < 1.0
    assert _band_db(clip, 880) < -40


@needs_ffmpeg
def test_music_on_clip_without_audio(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_clip(clip, with_audio=False)
    bgaudio.apply(clip, _make_music(tmp_path), 50)
    assert ffmpeg.ffprobe_metadata(clip).audio_tracks == 1


@needs_ffmpeg
def test_failed_mix_leaves_clip_untouched(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_clip(clip)
    original = clip.read_bytes()
    with pytest.raises(FFmpegError):
        bgaudio.apply(clip, str(tmp_path / "missing.m4a"), 25)
    assert clip.read_bytes() == original
    assert not (tmp_path / bgaudio.UNMIXED_NAME).exists()
    assert not list(tmp_path.glob(".mix-*"))


@needs_ffmpeg
def test_after_render_drops_stale_copy_and_remixes(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_clip(clip)
    music = _make_music(tmp_path)
    bgaudio.apply(clip, music, 30)

    _make_clip(clip, seconds=3)  # a re-render writes a fresh clip.mp4
    fresh = clip.read_bytes()
    bgaudio.after_render(clip, music, 30)
    assert (tmp_path / bgaudio.UNMIXED_NAME).read_bytes() == fresh
    assert abs(ffmpeg.ffprobe_metadata(clip).duration_s - 3.0) < 0.2

    _make_clip(clip)
    bgaudio.after_render(clip, None, 30)  # music since removed
    assert not (tmp_path / bgaudio.UNMIXED_NAME).exists()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _fake_prepare(src, dst):
    Path(dst).write_bytes(b"m4a")
    return MediaInfo(duration_s=95.5, width=0, height=0, fps=0, audio_tracks=1, raw={})


def _upload_track(client, monkeypatch, name="song.mp3") -> str:
    """Needs the ``s3`` fixture: uploaded music goes to the bucket."""
    monkeypatch.setattr(bgaudio, "prepare_track", _fake_prepare)
    resp = client.post("/audio/upload", files={"file": (name, b"ID3fake", "audio/mpeg")})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _job_with_clips(client, db_session, tmp_path) -> tuple[str, dict[str, Clip]]:
    resp = client.post("/videos/upload", files={"file": ("v.mp4", b"\x00fake", "video/mp4")})
    vid = resp.json()["id"]
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 600.0
    db_session.commit()
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]}).json()["id"]
    clips = {}
    for i, st in enumerate(
        [ClipStatus.ready, ClipStatus.approved, ClipStatus.rendering, ClipStatus.deleted], 1
    ):
        f = tmp_path / f"c{i}.mp4"
        f.write_bytes(b"mp4")
        clips[st.value] = Clip(
            job_id=job_id,
            platform="youtube_shorts",
            start_s=0,
            end_s=30,
            rank=i,
            status=st,
            file_path=str(f),
        )
        db_session.add(clips[st.value])
    db_session.commit()
    return job_id, clips


def test_upload_goes_to_the_bucket_and_plays_from_there(client, monkeypatch, s3):  # noqa: F811
    tid = _upload_track(client, monkeypatch, "My song.mp3")
    listed = client.get("/audio").json()
    assert [t["id"] for t in listed] == [tid]
    track = listed[0]
    assert (track["filename"], track["duration_s"], track["status"]) == (
        "My_song.m4a",
        95.5,
        "ready",
    )
    assert track["bucket"]["key"] == "clipforge/audio/My_song.m4a"
    assert s3.objects == {"clipforge/audio/My_song.m4a": (b"m4a", "audio/mp4")}  # converted
    assert s3.metadata["clipforge/audio/My_song.m4a"] == {"clipforge": "converted-music"}
    assert not get_storage().path_for(f"audio/{tid}").exists()  # nothing kept here
    played = client.get(f"/audio/{tid}/file", follow_redirects=False)
    assert played.headers["location"].endswith("/clipforge/audio/My_song.m4a")
    # A name in use is never replaced.
    other = _upload_track(client, monkeypatch, "My song.wav")
    assert "clipforge/audio/My_song-2.m4a" in s3.objects and other != tid


def test_music_needs_a_bucket(client, monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "")
    get_settings.cache_clear()
    resp = client.post("/audio/upload", files={"file": ("song.mp3", b"ID3", "audio/mpeg")})
    assert resp.status_code == 409
    resp = client.post("/audio/from-url", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert resp.status_code == 409
    assert client.get("/audio").json() == []


def test_a_failed_bucket_upload_keeps_nothing(client, monkeypatch, s3):  # noqa: F811
    def refused(*args, **kwargs):
        raise RuntimeError("access denied")

    monkeypatch.setattr(s3, "upload_file", refused)
    monkeypatch.setattr(bgaudio, "prepare_track", _fake_prepare)
    resp = client.post("/audio/upload", files={"file": ("song.mp3", b"ID3", "audio/mpeg")})
    assert resp.status_code == 502
    assert client.get("/audio").json() == [] and s3.objects == {}
    assert not any(get_storage().path_for("audio").glob("*/*"))


def test_delete_keeps_the_bucket_file_unless_asked(client, monkeypatch, s3):  # noqa: F811
    tid = _upload_track(client, monkeypatch)
    assert client.delete(f"/audio/{tid}").status_code == 204  # e.g. the Local tab
    assert "clipforge/audio/song.m4a" in s3.objects
    tid = _upload_track(client, monkeypatch)
    assert client.delete(f"/audio/{tid}", params={"from_bucket": True}).status_code == 204
    assert "clipforge/audio/song-2.m4a" not in s3.objects  # the music boxes' Delete
    assert client.get("/audio").json() == []


def test_upload_rejects_non_audio(client, monkeypatch, s3):  # noqa: F811
    bad_ext = client.post("/audio/upload", files={"file": ("x.txt", b"hi", "text/plain")})
    assert bad_ext.status_code == 400

    def broken(src, dst):
        raise FFmpegError("Invalid data found when processing input")

    monkeypatch.setattr(bgaudio, "prepare_track", broken)
    resp = client.post("/audio/upload", files={"file": ("fake.mp3", b"nope", "audio/mpeg")})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_audio"
    assert client.get("/audio").json() == []


def test_add_music_to_all_ready_clips(client, db_session, monkeypatch, tmp_path, s3):  # noqa: F811
    tid = _upload_track(client, monkeypatch)
    job_id, clips = _job_with_clips(client, db_session, tmp_path)

    resp = client.post(f"/jobs/{job_id}/background-audio", json={"audio_id": tid, "volume": 40})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ready + approved queued; the rendering one skipped; deleted ignored
    assert body["queued"] == 2 and body["skipped"] == 1
    ready, approved = clips["ready"], clips["approved"]
    assert client.enqueued["music"] == [([ready.id, approved.id], [approved.id])]
    for c in (ready, approved):
        db_session.refresh(c)
        assert c.status == ClipStatus.rendering
        assert c.bg_audio_id == tid and c.bg_audio_volume == 40
    assert clips["rendering"].bg_audio_id is None


def test_music_on_one_clip_and_remove(client, db_session, monkeypatch, tmp_path, s3):  # noqa: F811
    tid = _upload_track(client, monkeypatch)
    job_id, clips = _job_with_clips(client, db_session, tmp_path)
    one = clips["ready"]
    url = f"/jobs/{job_id}/background-audio"

    # Nothing to remove yet
    assert client.post(url, json={"audio_id": None, "clip_ids": [one.id]}).status_code == 409
    # Still rendering -> can't change it
    busy = client.post(url, json={"audio_id": tid, "clip_ids": [clips["rendering"].id]})
    assert busy.status_code == 409

    resp = client.post(url, json={"audio_id": tid, "volume": 10, "clip_ids": [one.id]})
    assert resp.json()["queued"] == 1
    assert client.enqueued["music"][-1] == ([one.id], [])

    # The track can't be deleted while a clip uses it
    assert client.delete(f"/audio/{tid}").status_code == 409

    one.status = ClipStatus.ready  # the mix finished
    db_session.commit()
    resp = client.post(url, json={"audio_id": None, "clip_ids": [one.id]})
    assert resp.json()["queued"] == 1
    db_session.refresh(one)
    assert one.bg_audio_id is None
    assert client.delete(f"/audio/{tid}").status_code == 204
    assert db_session.get(AudioTrack, tid) is None


def test_mute_original_flag_is_stored(client, db_session, monkeypatch, tmp_path, s3):  # noqa: F811
    tid = _upload_track(client, monkeypatch)
    job_id, clips = _job_with_clips(client, db_session, tmp_path)
    one = clips["ready"]
    url = f"/jobs/{job_id}/background-audio"

    resp = client.post(
        url, json={"audio_id": tid, "volume": 100, "mute_original": True, "clip_ids": [one.id]}
    )
    assert resp.status_code == 200
    assert resp.json()["clips"][0]["bg_audio_mute_original"] is True
    db_session.refresh(one)
    assert one.bg_audio_mute_original is True

    # Removing the music clears the mute flag too.
    one.status = ClipStatus.ready
    db_session.commit()
    client.post(url, json={"audio_id": None, "clip_ids": [one.id]})
    db_session.refresh(one)
    assert one.bg_audio_mute_original is False


def test_apply_validates_input(client, db_session, monkeypatch, tmp_path, s3):  # noqa: F811
    job_id, clips = _job_with_clips(client, db_session, tmp_path)
    url = f"/jobs/{job_id}/background-audio"
    assert client.post(url, json={"audio_id": "nope"}).status_code == 404
    tid = _upload_track(client, monkeypatch)
    assert client.post(url, json={"audio_id": tid, "volume": 150}).status_code == 422
    assert client.post(url, json={"audio_id": tid, "clip_ids": ["x"]}).status_code == 404
    assert client.post("/jobs/missing/background-audio", json={"audio_id": tid}).status_code == 404


def test_grab_audio_from_link_runs_in_background(client, db_session, s3):  # noqa: F811
    resp = client.post("/audio/from-url", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert resp.status_code == 202, resp.text
    track = resp.json()
    assert track["status"] == "processing" and track["progress"] == 0
    assert track["to_bucket"] is True  # kept in the bucket once downloaded
    assert client.enqueued["audio_link"] == [track["id"]]
    # listed while downloading, but can't be put on clips until ready
    assert [t["status"] for t in client.get("/audio").json()] == ["processing"]
    assert client.get(f"/audio/{track['id']}/file").status_code == 404

    bad = client.post("/audio/from-url", json={"url": "not a link at all"})
    assert bad.status_code == 400
    # a failed/stuck import can still be removed
    assert client.delete(f"/audio/{track['id']}").status_code == 204


def test_uploaded_track_is_ready(client, monkeypatch, s3):  # noqa: F811
    _upload_track(client, monkeypatch)
    assert [t["status"] for t in client.get("/audio").json()] == ["ready"]


def test_clip_file_version_changes_when_file_changes(client, db_session, monkeypatch, tmp_path):
    job_id, clips = _job_with_clips(client, db_session, tmp_path)
    clip = clips["ready"]

    def version():
        rows = client.get(f"/jobs/{job_id}/clips").json()
        return next(c["file_version"] for c in rows if c["id"] == clip.id)

    v1 = version()
    assert v1
    path = Path(clip.file_path)
    path.write_bytes(b"mixed")  # e.g. music added
    os.utime(path, ns=(v1 + 10**9, v1 + 10**9))
    assert version() != v1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_music_keeps_its_volume_while_the_voice_speaks(tmp_path):
    clip, music, out = tmp_path / "clip.mp4", tmp_path / "music.wav", tmp_path / "out.mp4"
    # A "voice" at 300 Hz for 2 s, then 1 s of silence, three times; music at 1 kHz.
    voice = "sine=f=300:d=9,volume=0.3,volume=enable='between(mod(t,3),2,3)':volume=0"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=9",
                    "-f", "lavfi", "-i", voice, "-shortest", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", str(clip)], check=True)  # fmt: skip
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=1000:d=9",
                    str(music)], check=True)  # fmt: skip
    ffmpeg.run(bgaudio.build_mix_cmd(str(clip), str(music), str(out), volume_pct=30,
                                     duration_s=9.0))  # fmt: skip

    def music_level(start: float, end: float) -> float:
        err = subprocess.run(
            ["ffmpeg", "-i", str(out), "-af", f"atrim={start}:{end},bandpass=f=1000:"
             "width_type=h:w=60,volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        ).stderr  # fmt: skip
        return float(re.search(r"mean_volume: (-?[\d.]+)", err).group(1))

    # The set volume all through: not pushed down under the voice.
    assert abs(music_level(2.3, 2.9) - music_level(0.5, 1.5)) < 1
