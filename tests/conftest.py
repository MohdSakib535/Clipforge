from __future__ import annotations

import os
import tempfile

# Configure environment BEFORE any app module imports settings.
_TMP_STORAGE = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "memory://")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TRANSCRIBER", "openai")
os.environ.setdefault("STORAGE_PATH", _TMP_STORAGE)

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
    calls: dict[str, list] = {"ingest": [], "job": [], "clip": [], "download": []}
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_ingest", lambda vid: calls["ingest"].append(vid)
    )
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_download",
        lambda vid, quality: calls["download"].append((vid, quality)),
    )
    monkeypatch.setattr("app.tasks.dispatch.enqueue_job", lambda jid: calls["job"].append(jid))
    monkeypatch.setattr(
        "app.tasks.dispatch.enqueue_clip_render", lambda cid: calls["clip"].append(cid)
    )

    def _override_get_db():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db

    test_client = TestClient(app)
    test_client.enqueued = calls  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()
