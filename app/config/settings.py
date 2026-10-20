from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (app/config/settings.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """The .env configuration is invalid or incomplete."""


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

    # --- Transcription (speech -> text with word timings), env var TRANSCRIBER ---
    #   openai -> OpenAI whisper-1: fast, ~$0.006 per audio minute
    #   local  -> faster-whisper on this machine: free, slower (CPU)
    # The field name comes first so Settings(transcriber_choice=...) wins over
    # the env var (pydantic picks the first alias present).
    transcriber_choice: str = Field(
        default="openai", validation_alias=AliasChoices("transcriber_choice", "transcriber")
    )

    # OpenAI (only for TRANSCRIBER=openai)
    openai_api_key: str = Field(default="", repr=False)
    openai_transcribe_model: str = "whisper-1"

    # Local: faster-whisper (TRANSCRIBER=local).
    local_whisper_model: str = "large-v3-turbo"
    local_whisper_compute_type: str = "int8"
    local_whisper_device: str = "auto"
    local_whisper_beam_size: int = 1  # 1 = fastest; 5 = a little more accurate
    # Spoken language ("hi", "en", ...). Empty = detect it by listening to several
    # places in the audio. Set it if detection ever gets it wrong.
    local_whisper_language: str = ""
    # Skip silence/music with voice-activity detection (faster, and stops Whisper
    # inventing text for music).
    local_whisper_vad: bool = True

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
    render_concurrency: int = 4  # parallel full re-encodes (vertical clips; watch RAM on 4K)
    # Parallel stream-copy cuts (horizontal clips). These are mostly
    # single-threaded audio work, so many can run at once.
    copy_concurrency: int = 8
    render_preset: str = "ultrafast"  # fastest x264 encode (larger files)
    # Face-detection samples per clip during reframe (fewer = faster).
    reframe_samples: int = 8

    # Path to the platform presets YAML (overridable for tests).
    platforms_config_path: str = str(REPO_ROOT / "app" / "config" / "platforms.yaml")

    @property
    def transcriber(self) -> str:
        """ "openai" or "local": who transcribes. Raises ConfigError for anything
        else, so a typo fails loudly instead of silently using the paid one."""
        choice = (self.transcriber_choice or "").strip().lower()
        if choice not in ("openai", "local"):
            raise ConfigError(
                f"TRANSCRIBER={self.transcriber_choice!r} is not valid. Use openai or local."
            )
        return choice

    @property
    def transcribe_is_local(self) -> bool:
        return self.transcriber == "local"

    @property
    def transcribe_model(self) -> str:
        return (
            self.local_whisper_model if self.transcribe_is_local else self.openai_transcribe_model
        )

    def check_transcriber_config(self) -> None:
        """Raise ConfigError if transcription can't run: an unknown TRANSCRIBER,
        or TRANSCRIBER=openai without an OpenAI key."""
        if self.transcriber == "openai" and not self.openai_api_key.strip():
            raise ConfigError(
                "TRANSCRIBER=openai but OPENAI_API_KEY is empty in .env. Add the key, "
                "or set TRANSCRIBER=local to transcribe on this machine for free."
            )

    def masked_openai_key(self) -> str:
        """Return a safe-to-log representation of the API key."""
        if not self.openai_api_key:
            return "<unset>"
        tail = self.openai_api_key[-4:]
        return f"sk-***{tail}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
