from __future__ import annotations

import logging
from contextvars import ContextVar

from pythonjsonlogger import json as jsonlogger

from app.config.settings import get_settings

# job_id is bound per-task / per-request and injected into every log line.
_job_id_ctx: ContextVar[str | None] = ContextVar("job_id", default=None)


def bind_job_id(job_id: str | None) -> None:
    _job_id_ctx.set(job_id)


class _JobIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = _job_id_ctx.get()
        return True


_configured = False


def configure_logging() -> None:
    """Install a JSON formatter on the root logger. Idempotent."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(job_id)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )
    handler.setFormatter(formatter)
    handler.addFilter(_JobIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Quiet noisy third-party loggers.
    for noisy in ("uvicorn.access", "multipart.multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
