from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from app.config.settings import get_settings


class PlatformPreset(BaseModel):
    """A single platform/format preset, loaded from platforms.yaml."""

    key: str
    label: str
    aspect: str  # "9:16" | "16:9"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    length_min_s: int = Field(gt=0)
    length_max_s: int = Field(gt=0)
    length_default_min_s: int = Field(gt=0)
    length_default_max_s: int = Field(gt=0)
    reframe: bool = False
    captions_recommended: bool = False

    @model_validator(mode="after")
    def _check_ranges(self) -> PlatformPreset:
        if self.length_max_s < self.length_min_s:
            raise ValueError(f"{self.key}: length_max_s < length_min_s")
        if not (self.length_min_s <= self.length_default_min_s <= self.length_max_s):
            raise ValueError(f"{self.key}: length_default_min_s out of range")
        if not (self.length_min_s <= self.length_default_max_s <= self.length_max_s):
            raise ValueError(f"{self.key}: length_default_max_s out of range")
        if self.length_default_max_s < self.length_default_min_s:
            raise ValueError(f"{self.key}: default max < default min")
        return self

    @property
    def is_vertical(self) -> bool:
        return self.aspect == "9:16"


def load_presets(path: str | Path | None = None) -> dict[str, PlatformPreset]:
    """Load and validate all presets from YAML. Raises on malformed config."""
    cfg_path = Path(path) if path else Path(get_settings().platforms_config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Platform config not found: {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text())
    presets_raw = (raw or {}).get("presets")
    if not presets_raw:
        raise ValueError(f"No 'presets' section in {cfg_path}")

    presets: dict[str, PlatformPreset] = {}
    for key, body in presets_raw.items():
        presets[key] = PlatformPreset(key=key, **body)
    return presets


@lru_cache
def _cached_presets() -> dict[str, PlatformPreset]:
    return load_presets()


def get_preset(key: str) -> PlatformPreset:
    """Return a preset by key, or raise KeyError with the valid options."""
    presets = _cached_presets()
    if key not in presets:
        raise KeyError(f"Unknown platform '{key}'. Valid: {sorted(presets)}")
    return presets[key]
