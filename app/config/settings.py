from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (app/config/settings.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings, loaded from environment / .env.

    The OpenAI API key is intentionally excluded from ``__repr__`` output so it
    never leaks into logs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # OpenAI
    openai_api_key: str = Field(default="", repr=False)
    transcriber: str = "openai"  # "openai" | "local"
    openai_transcribe_model: str = "whisper-1"
    openai_scoring_model: str = "gpt-4o-mini"
    openai_metadata_model: str = "gpt-4o"

    # Local whisper
    local_whisper_model: str = "base"
    local_whisper_compute_type: str = "int8"
    local_whisper_device: str = "auto"

    # Cost control
    max_job_cost_usd: float = 5.0

    # Infrastructure
    database_url: str = "postgresql+psycopg://clipforge:clipforge@localhost:5433/clipforge"
    redis_url: str = "redis://localhost:6380/0"

    # Storage
    storage_path: str = str(REPO_ROOT / "storage")

    # App
    log_level: str = "INFO"
    max_upload_bytes: int = 5 * 1024 * 1024 * 1024
    max_audio_upload_bytes: int = 200 * 1024 * 1024  # background-music files

    # --- Import from a link (yt-dlp) ---
    # Longest a single download may run before it is stopped and marked failed.
    download_timeout_s: int = 3 * 3600
    # How long "check link" waits for the site before giving up.
    url_probe_timeout_s: int = 45
    # Optional Netscape cookies.txt (e.g. exported from your browser) for videos
    # that need a login or when YouTube asks to "confirm you're not a bot".
    ytdlp_cookies_file: str = ""

    # --- Performance tuning (speed-first defaults) ---
    # I/O-bound stages (network) run concurrently; CPU encode runs in parallel
    # subprocesses. Defaults favour speed on a ~10-core / 16 GB laptop.
    transcribe_concurrency: int = 8  # parallel transcription API calls
    scoring_concurrency: int = 8  # parallel LLM scoring batches
    render_concurrency: int = 4  # parallel full re-encodes (vertical clips; watch RAM on 4K)
    # Parallel stream-copy cuts (horizontal clips). These are mostly
    # single-threaded audio work, so many can run at once.
    copy_concurrency: int = 8
    render_preset: str = "ultrafast"  # fastest x264 encode (larger files)
    # Cap candidates per platform to bound scoring latency on long videos.
    max_candidates: int = 120
    # Face-detection samples per clip during reframe (fewer = faster).
    reframe_samples: int = 8

    # Path to the platform presets YAML (overridable for tests).
    platforms_config_path: str = str(REPO_ROOT / "app" / "config" / "platforms.yaml")

    def masked_openai_key(self) -> str:
        """Return a safe-to-log representation of the API key."""
        if not self.openai_api_key:
            return "<unset>"
        tail = self.openai_api_key[-4:]
        return f"sk-***{tail}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
