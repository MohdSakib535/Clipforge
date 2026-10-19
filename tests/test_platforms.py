from __future__ import annotations

import textwrap

import pytest

from app.config.platforms import PlatformPreset, get_preset, load_presets


def test_load_presets_from_default_yaml():
    presets = load_presets()
    assert set(presets) == {
        "youtube_shorts",
        "youtube_clip",
        "facebook_reel",
        "facebook_video",
    }
    shorts = presets["youtube_shorts"]
    assert shorts.aspect == "9:16"
    assert shorts.is_vertical is True
    assert shorts.reframe is True
    assert shorts.width == 1080 and shorts.height == 1920


def test_youtube_clip_is_horizontal():
    clip = get_preset("youtube_clip")
    assert clip.aspect == "16:9"
    assert clip.is_vertical is False
    assert clip.reframe is False


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        get_preset("tiktok")


def test_preset_validates_length_ranges():
    with pytest.raises(ValueError):
        PlatformPreset(
            key="bad",
            label="Bad",
            aspect="9:16",
            width=1080,
            height=1920,
            length_min_s=60,
            length_max_s=30,  # max < min
            length_default_min_s=40,
            length_default_max_s=50,
        )


def test_load_presets_custom_file(tmp_path):
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            presets:
              demo:
                label: Demo
                aspect: "16:9"
                width: 1920
                height: 1080
                length_min_s: 10
                length_max_s: 120
                length_default_min_s: 20
                length_default_max_s: 60
            """
        )
    )
    presets = load_presets(cfg)
    assert "demo" in presets
    assert presets["demo"].length_default_max_s == 60
