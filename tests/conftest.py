from __future__ import annotations

import os
import tempfile

# Configure environment BEFORE any app module imports settings.
_TMP_STORAGE = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "memory://")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TRANSCRIBER", "openai")
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
    calls: dict[str, list] = {
        "ingest": [],
        "job": [],
        "clip": [],
        "download": [],
        "music": [],
        "audio_link": [],
    }
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
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_clip_render",
        lambda cid, keep_approved=False: calls["clip"].append((cid, keep_approved)),
    )

    def _override_get_db():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db

    test_client = TestClient(app)
    test_client.enqueued = calls  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()
