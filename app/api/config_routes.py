from __future__ import annotations

from fastapi import APIRouter

from app.config.platforms import load_presets

router = APIRouter(tags=["config"])


@router.get("/platforms")
def list_platforms() -> dict:
    """Expose platform presets so the UI is config-driven (no hardcoded limits)."""
    presets = load_presets()
    return {
        "platforms": [
            {
                "key": p.key,
                "label": p.label,
                "aspect": p.aspect,
                "width": p.width,
                "height": p.height,
                "length_min_s": p.length_min_s,
                "length_max_s": p.length_max_s,
                "length_default_min_s": p.length_default_min_s,
                "length_default_max_s": p.length_default_max_s,
                "captions_recommended": p.captions_recommended,
                "is_vertical": p.is_vertical,
            }
            for p in presets.values()
        ]
    }
