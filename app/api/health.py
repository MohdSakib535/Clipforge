from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.config.settings import ConfigError, get_settings
from app.db.session import engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness + dependency check (DB connectivity)."""
    settings = get_settings()
    db_ok = True
    db_error: str | None = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - exercised only when DB is down
        db_ok = False
        db_error = str(exc)

    transcriber: str | None = None
    config_error: str | None = None
    try:
        settings.check_transcriber_config()
        transcriber = settings.transcriber  # "openai" or "local"
    except ConfigError as exc:
        config_error = str(exc)

    return {
        "status": "ok" if db_ok and transcriber else "degraded",
        "transcriber": transcriber,
        "config_error": config_error,
        "database": {"ok": db_ok, "error": db_error},
    }
