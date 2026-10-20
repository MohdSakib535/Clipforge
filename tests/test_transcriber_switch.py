from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config.settings import ConfigError, Settings, get_settings
from app.models import Video, VideoStatus
from app.services.transcriber import (
    LocalWhisperTranscriber,
    OpenAITranscriber,
    get_transcriber,
)


def _settings(**kw) -> Settings:
    kw.setdefault("openai_api_key", "sk-test")
    return Settings(_env_file=None, **kw)


# --- TRANSCRIBER picks who transcribes ---------------------------------------
def test_default_is_openai_whisper():
    s = _settings()
    assert s.transcriber == "openai" and not s.transcribe_is_local
    assert s.transcribe_model == "whisper-1"
    assert isinstance(get_transcriber(s), OpenAITranscriber)


def test_local_uses_faster_whisper():
    s = _settings(transcriber_choice="local")
    assert s.transcriber == "local" and s.transcribe_is_local
    assert s.transcribe_model == s.local_whisper_model == "large-v3-turbo"
    assert isinstance(get_transcriber(s), LocalWhisperTranscriber)


def test_transcriber_read_from_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIBER", "Local")
    assert Settings(_env_file=None).transcriber == "local"


def test_unknown_transcriber_fails_loudly():
    with pytest.raises(ConfigError, match="openai or local"):
        _settings(transcriber_choice="whisper").transcriber  # noqa: B018


def test_openai_key_needed_only_for_openai():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY is empty"):
        _settings(openai_api_key="").check_transcriber_config()
    _settings(openai_api_key="", transcriber_choice="local").check_transcriber_config()


# --- API / health -------------------------------------------------------------
def _ready_video(client, db_session) -> str:
    vid = client.post("/videos/upload", files={"file": ("s.mp4", b"x" * 10, "video/mp4")}).json()[
        "id"
    ]
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    db_session.commit()
    return vid


def test_job_refused_when_openai_has_no_key(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    vid = _ready_video(client, db_session)
    resp = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "transcriber_unavailable"
    assert client.enqueued["job"] == []  # nothing was queued


def test_local_job_needs_no_key(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("TRANSCRIBER", "local")
    vid = _ready_video(client, db_session)
    resp = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]})
    assert resp.status_code == 201


def test_health_reports_transcriber_and_config_error(client, monkeypatch):
    body = client.get("/health").json()
    assert (body["status"], body["transcriber"], body["config_error"]) == ("ok", "openai", None)
    assert "backend" not in body  # there is no LLM backend any more
    monkeypatch.setenv("TRANSCRIBER", "nope")
    get_settings.cache_clear()  # settings are cached after the first read
    body = client.get("/health").json()
    assert body["status"] == "degraded" and body["transcriber"] is None
    assert "openai or local" in body["config_error"]


# --- local Whisper: language is voted across the audio, not read from 30 seconds ---
class _FakeWhisperModel:
    def __init__(self, per_window):
        self.per_window, self.calls = list(per_window), 0

    def detect_language(self, audio=None, **_):
        probs = self.per_window[self.calls]
        self.calls += 1
        return max(probs, key=probs.get), max(probs.values()), list(probs.items())


def _patch_audio(monkeypatch, duration_s):
    import numpy as np

    from app.services import ffmpeg as ff

    class _Info:
        pass

    info = _Info()
    info.duration_s = duration_s
    monkeypatch.setattr(ff, "ffprobe_metadata", lambda p: info)
    monkeypatch.setattr(ff, "run_capture", lambda cmd: np.zeros(16000 * 30, np.float32).tobytes())


def test_language_vote_beats_a_misleading_first_window(monkeypatch):
    _patch_audio(monkeypatch, 3600)  # 1 hour -> 5 windows
    # The opening says "en" (an English logo jingle) but 4 of 5 windows are Hindi.
    model = _FakeWhisperModel([{"en": 0.8, "hi": 0.2}] + [{"hi": 0.9, "en": 0.1}] * 4)
    lang = LocalWhisperTranscriber(_settings(transcriber_choice="local"))
    assert lang._detect_language(model, "a.mp3") == "hi"
    assert model.calls == 5


def test_short_audio_uses_a_single_window(monkeypatch):
    _patch_audio(monkeypatch, 60)
    model = _FakeWhisperModel([{"en": 0.9, "hi": 0.1}])
    lang = LocalWhisperTranscriber(_settings(transcriber_choice="local"))
    assert lang._detect_language(model, "a.mp3") == "en"
    assert model.calls == 1
