"""Music kept in the bucket: fetched only while a clip or reel is made, then
deleted again (services/music.py), in the mixer and the reel maker."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import AudioTrack, Clip, ClipStatus, Reel
from app.services import bgaudio, music, reel_render
from app.services.ffmpeg import MediaInfo
from app.services.storage import get_storage
from app.tasks import audio as audio_tasks
from app.tasks import reels
from tests.test_bucket_uploads import s3  # noqa: F401 - the fake bucket fixture

KEY = "clipforge/audio/Calm.m4a"
GONE = "clipforge/audio/Gone.m4a"  # not in the bucket


def _fake_prepare(src, dst):
    Path(dst).write_bytes(b"converted " + Path(src).read_bytes())
    return MediaInfo(duration_s=30.0, width=0, height=0, fps=0, audio_tracks=1, raw={})


def _track(db_session, **fields) -> AudioTrack:
    track = AudioTrack(user_id="u1", filename="Calm.m4a", **{"status": "ready", **fields})
    db_session.add(track)
    db_session.commit()
    return track


@pytest.fixture
def downloads(s3, monkeypatch):  # noqa: F811
    """Counts what is fetched from the fake bucket."""
    s3.objects[KEY] = (b"aac", "audio/mp4")
    monkeypatch.setattr(bgaudio, "prepare_track", _fake_prepare)
    fetched = []
    download = s3.download_file

    def counted(Bucket, Key, Filename):
        fetched.append(Key)
        download(Bucket=Bucket, Key=Key, Filename=Filename)

    monkeypatch.setattr(s3, "download_file", counted)
    return fetched


def test_a_working_copy_is_fetched_converted_and_deleted(db_session, downloads):
    track = _track(db_session, bucket_copy=music.in_bucket(KEY, converted=False))
    with music.working_copy(track) as path:
        assert Path(path).read_bytes() == b"converted aac"  # mixed like an upload
        assert not list(Path(path).parent.glob("source*"))  # only the converted file
    assert not Path(path).exists()
    assert not get_storage().path_for(f"audio/{track.id}").exists()  # nothing left here
    assert downloads == [KEY]


def test_music_the_app_converted_is_only_downloaded(db_session, downloads, monkeypatch):
    monkeypatch.setattr(bgaudio, "prepare_track", lambda *a: pytest.fail("converted twice"))
    track = _track(db_session, bucket_copy=music.in_bucket(KEY, converted=True))
    with music.working_copy(track) as path:
        assert Path(path).read_bytes() == b"aac"  # as it is in the bucket
    assert not get_storage().path_for(f"audio/{track.id}").exists() and downloads == [KEY]


def test_bucket_music_the_app_tagged_is_known_when_picked(client, db_session, s3):  # noqa: F811
    s3.objects["clipforge/audio/Ours.m4a"] = (b"aac", "audio/mp4")
    s3.metadata["clipforge/audio/Ours.m4a"] = music.CONVERTED
    s3.objects["old/Theirs.mp3"] = (b"mp3", "audio/mpeg")  # put there some other way
    ours = client.post("/audio/from-bucket", json={"key": "clipforge/audio/Ours.m4a"}).json()
    theirs = client.post("/audio/from-bucket", json={"key": "old/Theirs.mp3"}).json()
    assert db_session.get(AudioTrack, ours["id"]).bucket_copy["converted"] is True
    assert db_session.get(AudioTrack, theirs["id"]).bucket_copy["converted"] is False


def test_no_music_or_a_track_from_before(db_session, downloads, tmp_path):
    with music.working_copy(None) as path:
        assert path is None
    busy = _track(
        db_session, status="processing", bucket_copy=music.in_bucket(KEY, converted=False)
    )
    with music.working_copy(busy) as path:
        assert path is None
    kept = tmp_path / "track.m4a"
    kept.write_bytes(b"old")
    old = _track(db_session, path=str(kept))
    with music.working_copy(old) as path:
        assert path == str(kept)
    assert kept.exists() and downloads == []  # its own file: used as it is, kept


def test_a_file_gone_from_the_bucket_says_so(db_session, downloads):
    track = _track(db_session, bucket_copy=music.in_bucket(GONE, converted=False))
    with pytest.raises(music.MusicError, match='Could not get the music "Calm.m4a"'):
        with music.working_copy(track):
            pass
    assert not get_storage().path_for(f"audio/{track.id}").exists()


@pytest.fixture
def factory(db_session, monkeypatch):
    f = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(audio_tasks, "SessionLocal", f)
    monkeypatch.setattr(reels, "SessionLocal", f)
    return f


def _clip(db_session, tmp_path, n: int, track: AudioTrack | None) -> Clip:
    path = tmp_path / f"c{n}.mp4"
    path.write_bytes(b"mp4")
    clip = Clip(job_id="j1", platform="youtube_shorts", start_s=0, end_s=10, rank=n,
                status=ClipStatus.rendering, file_path=str(path),
                bg_audio_id=track.id if track else None, bg_audio_volume=30)  # fmt: skip
    db_session.add(clip)
    db_session.commit()
    return clip


def test_the_mixer_fetches_a_track_once_for_all_its_clips(
    db_session, factory, downloads, tmp_path, monkeypatch
):
    track = _track(db_session, bucket_copy=music.in_bucket(KEY, converted=False))
    clips = [_clip(db_session, tmp_path, n, track) for n in (1, 2)]
    mixed = []

    def fake_apply(clip_path, music_path, volume, *, mute_original=False):
        mixed.append((clip_path, Path(music_path).read_bytes()))

    monkeypatch.setattr(bgaudio, "apply", fake_apply)
    audio_tasks.mix_background_audio([c.id for c in clips], [clips[1].id])
    assert downloads == [KEY]  # once, for both clips
    assert sorted(mixed) == sorted((c.file_path, b"converted aac") for c in clips)
    assert not get_storage().path_for(f"audio/{track.id}").exists()  # deleted after
    for clip, status in zip(clips, (ClipStatus.ready, ClipStatus.approved), strict=True):
        db_session.refresh(clip)
        assert (clip.status, clip.error) == (status, None)


def test_the_mixer_explains_music_it_could_not_fetch(
    db_session, factory, downloads, tmp_path, monkeypatch
):
    gone = _track(db_session, bucket_copy=music.in_bucket(GONE, converted=False))
    clip = _clip(db_session, tmp_path, 1, gone)
    monkeypatch.setattr(bgaudio, "apply", lambda *a, **k: pytest.fail("nothing to mix"))
    audio_tasks.mix_background_audio([clip.id], [])
    db_session.refresh(clip)
    assert clip.status == ClipStatus.ready  # its file is untouched: still playable
    assert clip.error.startswith('Could not get the music "Calm.m4a" from the bucket')


SCRIPT = {"title": "T", "hooks": [], "caption": "", "hashtags": [],
          "scenes": [{"narration": "Drink water.", "image_prompt": "a glass"}]}  # fmt: skip


def _reel_with_music(db_session, track: AudioTrack) -> Reel:
    reel = Reel(user_id="u1", platform="instagram", language="en", voice="af_heart",
                topic="water", duration_s=15, status="generating", script=SCRIPT,
                music_id=track.id, music_volume=25)  # fmt: skip
    db_session.add(reel)
    db_session.commit()
    folder = reel_render.reel_dir(reel.id)
    folder.mkdir(parents=True, exist_ok=True)
    return reel


def _made_already(monkeypatch):
    """Every picture and voice clip is there: only the video is put together."""
    np = pytest.importorskip("numpy")

    def pictures(folder, scenes, settings, style):
        paths = [folder / f"scene{i + 1:02d}-x.png" for i in range(len(scenes))]
        for p in paths:
            p.write_bytes(b"png")
        return paths

    def voice(folder, i, text, language, voice):
        path = folder / f"voice{i + 1:02d}-x.wav"
        reel_render.write_wav(path, np.zeros(2400, np.float32), 24000)
        return path

    monkeypatch.setattr(reel_render, "scene_pictures", pictures)
    monkeypatch.setattr(reel_render, "voice_path", voice)


def test_a_reel_mixes_in_music_fetched_for_it(db_session, factory, downloads, monkeypatch):
    track = _track(db_session, bucket_copy=music.in_bucket(KEY, converted=False))
    reel = _reel_with_music(db_session, track)
    _made_already(monkeypatch)
    used = {}

    def fake_render(folder, scenes, *, font, music, music_volume, workers):
        used.update(music=Path(music).read_bytes(), volume=music_volume)
        return 3.0

    monkeypatch.setattr(reel_render, "render", fake_render)
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "ready", reel.error
    assert used == {"music": b"converted aac", "volume": 25}
    assert not get_storage().path_for(f"audio/{track.id}").exists()  # deleted after


def test_a_reel_whose_music_is_gone_explains_why(db_session, factory, downloads, monkeypatch):
    track = _track(db_session, bucket_copy=music.in_bucket(GONE, converted=False))
    reel = _reel_with_music(db_session, track)
    _made_already(monkeypatch)
    monkeypatch.setattr(reel_render, "render", lambda *a, **k: pytest.fail("not made"))
    reels.render_reel.apply(args=[reel.id])
    db_session.refresh(reel)
    assert reel.status == "failed"
    assert reel.error.startswith('Could not get the music "Calm.m4a" from the bucket')
