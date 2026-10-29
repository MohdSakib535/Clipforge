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

    # --- Reel maker ("Create a reel") ---
    # The script comes from Ollama and the pictures from imagegen/server.py; both
    # run natively on the Mac (Docker can't use its GPU), the voice (Kokoro) in
    # the reelmaker worker.
    ollama_url: str = "http://host.docker.internal:11434"
    reel_script_model: str = "gemma3:4b"
    imagegen_url: str = "http://host.docker.internal:8189"
    # Pictures are made at 9:16 below full size (faster on a 16 GB Mac) and
    # scaled up to 1080x1920; the slow zoom on each one hides the softness.
    reel_image_width: int = 576
    reel_image_height: int = 1024
    reel_image_steps: int = 4
    # Kokoro voice model, downloaded on first use to <storage>/.kokoro.
    kokoro_model_url: str = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/kokoro-v1.0.int8.onnx"
    )
    kokoro_voices_url: str = (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin"
    )

    # --- "Upload to bucket" (Create a reel): AWS S3 or any S3-compatible storage
    # (Cloudflare R2, MinIO, DigitalOcean Spaces, Backblaze B2, Google Cloud
    # Storage). Set in .env (BUCKET_*). Empty BUCKET_NAME = not set up; the
    # upload buttons say so.
    bucket_name: str = ""
    # Empty for AWS S3. Otherwise the provider's S3 address, e.g.
    # https://<account-id>.r2.cloudflarestorage.com, or
    # http://host.docker.internal:9000 for MinIO on this Mac.
    bucket_endpoint_url: str = ""
    bucket_region: str = ""  # e.g. ap-south-1 (AWS); "auto" for R2
    # Empty = boto3's usual places (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, ~/.aws).
    bucket_access_key_id: str = Field(default="", repr=False)
    bucket_secret_access_key: str = Field(default="", repr=False)
    bucket_prefix: str = "clipforge"  # folder in the bucket the reels go under
    # The bucket's public address (public bucket or CDN), used for "Open"
    # links. Empty = signed links that work for 7 days.
    bucket_public_url: str = ""

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
