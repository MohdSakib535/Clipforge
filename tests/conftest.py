from __future__ import annotations

import os
import tempfile

# Configure environment BEFORE any app module imports settings.
_TMP_STORAGE = tempfile.mkdtemp(prefix="clipforge-test-")
# Forced, not setdefault: inside the Docker containers these point at the real
# Postgres/Redis, and tests (e.g. ones that run Celery tasks in-process) must
# never touch them.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_URL"] = "memory://"
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
# Forced: the transcriber choice must not depend on the developer's .env,
# which inside the Docker containers is loaded into the environment.
os.environ["TRANSCRIBER"] = "openai"
# Always a temp dir: inside the Docker containers STORAGE_PATH is already set to
# the real storage volume, and tests must never write files there.
os.environ["STORAGE_PATH"] = _TMP_STORAGE

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tasks.celery_app import celery_app  # noqa: E402

# Some tests run Celery tasks in-process (task.apply). "memory://" is a broker,
# not a result backend, so give them an in-memory one.
celery_app.conf.result_backend = "cache+memory://"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session, monkeypatch):
    # Record enqueued tasks instead of hitting Celery/Redis.
    def _record_clip_render(cid, keep_approved=False, urgent=False):
        calls["clip"].append((cid, keep_approved))
        calls["clip_urgent"].append(urgent)

    calls: dict[str, list] = {
        "ingest": [],
        "job": [],
        "clip": [],
        "clip_urgent": [],
        "download": [],
        "music": [],
        "audio_link": [],
        "reel_script": [],
        "reel_render": [],
    }
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_reel_script", lambda rid: calls["reel_script"].append(rid)
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_reel_render", lambda rid: calls["reel_render"].append(rid)
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_ingest", lambda vid: calls["ingest"].append(vid)
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_audio_download", lambda tid: calls["audio_link"].append(tid)
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_bg_audio_mix",
        lambda ids, approved: calls["music"].append((ids, approved)),
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_download",
        lambda vid, quality: calls["download"].append((vid, quality)),
    )
    monkeypatch.setattr("app.tasks.dispatch.enqueue_job", lambda jid: calls["job"].append(jid))
    monkeypatch.setattr("app.tasks.dispatch.enqueue_clip_render", _record_clip_render)

    def _override_get_db():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db

    test_client = TestClient(app)
    test_client.enqueued = calls  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()
