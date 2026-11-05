from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ClipStatus
from app.schemas.bucket import BucketCopy

__all__ = [
    "ClipOut",
    "ClipUpdate",
    "ColorGrade",
    "ColorGradeApply",
    "ColorGradeResult",
    "DubLine",
    "DubRequest",
    "FrameSettings",
    "FrameText",
    "TranslateRequest",
    "TranslationOut",
]


class ColorGrade(BaseModel):
    contrast: int = 0
    brightness: int = 0
    saturation: int = 0


_HEX = r"^#[0-9a-fA-F]{6}$"


class FrameText(BaseModel):
    """One text layer, anywhere on the 1080x1920 frame (also on the picture).
    ``{part}`` / ``{title}`` are filled per clip."""

    text: str = Field(default="", max_length=400)
    # Anchor point on the canvas: the text's middle (align "center") or the
    # middle of its left / right side.
    x: float = Field(default=540, ge=0, le=1080)
    y: float = Field(default=960, ge=0, le=1920)
    align: Literal["left", "center", "right"] = "center"
    size: int = Field(default=60, ge=16, le=200)  # px on the 1080-wide video
    color: str = Field(default="#ffffff", pattern=_HEX)
    bold: bool = True
    italic: bool = False
    outline: bool = True  # a dark edge, readable on the picture
    box: bool = False  # a solid box behind the text (instead of the edge)
    box_color: str = Field(default="#000000", pattern=_HEX)


class FrameSettings(BaseModel):
    """A 1080x1920 (9:16) clip: the picture placed and sized as set, any text
    layers on it (services/frame.py)."""

    background: Literal["black", "blur", "color"] = "blur"
    bg_color: str = Field(default="#000000", pattern=_HEX)
    # Middle of the picture, % of the canvas; size, % of the largest that fits.
    video_x: float = Field(default=50, ge=0, le=100)
    video_y: float = Field(default=50, ge=0, le=100)
    video_scale: int = Field(default=100, ge=30, le=100)
    texts: list[FrameText] = Field(default_factory=list, max_length=12)


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    platform: str
    start_s: float
    end_s: float
    duration_s: float | None = None
    title: str | None = None
    description: str | None = None
    hashtags: list[str] = []
    captions_enabled: bool
    color_contrast: int = 0
    color_brightness: int = 0
    color_saturation: int = 0
    # The grade the clip file actually has (the three above may be saved but
    # not applied yet). None: rendered before this was recorded.
    color_rendered: ColorGrade | None = None
    frame: FrameSettings | None = None  # saved; applied on re-render
    # The frame the file has, plus "rect" (x, y, w, h of the picture); None: no frame.
    frame_rendered: dict | None = None
    caption_style: str | None = None
    language: str | None = None
    status: ClipStatus
    rank: int | None = None
    error: str | None = None
    has_file: bool = False
    file_version: int | None = None
    has_thumbnail: bool = False
    has_srt: bool = False
    has_speech: bool | None = None
    transcript: str | None = None  # clips made from a trim with transcription on
    # Its sentences translated with POST /clips/{id}/translate:
    # {"language", "lines": [{"start_s", "end_s", "text", "translation"}]}.
    translation: dict | None = None
    dub_language: str | None = None  # a dubbed copy: the language of its voice
    bucket: BucketCopy | None = None  # its copy in the bucket, once uploaded
    bg_audio_id: str | None = None
    bg_audio_volume: int = 40
    bg_audio_mute_original: bool = False


class ClipUpdate(BaseModel):
    """Editable fields from the review dashboard. All optional (PATCH)."""

    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    title: str | None = Field(default=None, max_length=512)
    description: str | None = None
    hashtags: list[str] | None = None
    captions_enabled: bool | None = None
    color_contrast: int | None = Field(default=None, ge=-50, le=50)
    color_brightness: int | None = Field(default=None, ge=-50, le=50)
    color_saturation: int | None = Field(default=None, ge=-50, le=50)
    frame: FrameSettings | None = None  # null removes the frame
    status: ClipStatus | None = None

    @model_validator(mode="after")
    def _check_times(self):
        if self.start_s is not None and self.end_s is not None:
            if self.end_s <= self.start_s:
                raise ValueError("end_s must be > start_s")
        return self


class ColorGradeApply(BaseModel):
    """Apply one colour grade to some or all of a job's clips (each 0 = reset)."""

    contrast: int = Field(default=0, ge=-50, le=50)
    brightness: int = Field(default=0, ge=-50, le=50)
    saturation: int = Field(default=0, ge=-50, le=50)
    clip_ids: list[str] | None = Field(
        default=None, description="Clips to change; omit for every ready clip of the job."
    )


class ColorGradeResult(BaseModel):
    queued: int
    skipped: int  # still rendering / failed / unchanged
    clips: list[ClipOut]  # the queued clips, now "rendering"


class TranslateRequest(BaseModel):
    language: str = "hi"


class TranslationLine(BaseModel):
    start_s: float
    end_s: float
    text: str  # as said in the clip
    translation: str
    roman: str | None = None  # the translation in Roman letters (Hindi)


class VoiceOption(BaseModel):
    key: str
    label: str


class TranslationOut(BaseModel):
    language: str
    label: str
    voices: list[VoiceOption]  # for the dub
    lines: list[TranslationLine]


class DubLine(BaseModel):
    """One sentence of the dubbed voice: said from ``start_s`` (seconds in the
    video), as long as it fits before the next one."""

    model_config = ConfigDict(str_strip_whitespace=True)

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)


class DubRequest(BaseModel):
    language: str = "hi"
    voice: str | None = Field(default=None, description="Kokoro voice; omit for the first one.")
    lines: list[DubLine] = Field(min_length=1, max_length=80)
