"""Frame: a clip made into a 1080x1920 (9:16) video - the picture placed and
sized as the user likes on a background (black, a colour, or a blurred copy of
the picture), with any number of text layers anywhere on it, like a video
editing app.

The text is drawn by libass (an ASS subtitle file burned in), so Hindi and
English can share a line: fontconfig picks Noto Sans Devanagari for the Hindi
letters (Noto Sans has none) and libass shapes them.

The dashboard's mobile preview (app.js ``frameLayout`` / ``frameTextStyle``)
lays a frame out the same way, so keep the numbers here and there in step.
"""

from __future__ import annotations

import re
from typing import Any

CANVAS_W, CANVAS_H = 1080, 1920
# A text line wraps when wider than this (px on the canvas).
TEXT_WRAP_W = 960
FONT = "Noto Sans"
# libass sizes a font by its whole line height (ascent + descent: 1.362 em for
# Noto Sans); the dashboard's size is a CSS font size (1 em), so scale it up.
ASS_SIZE_PER_PX = 1.362
# Blurred background: the picture shrunk, blurred, scaled back up and darkened.
_BLUR_W, _BLUR_H = 270, 480
# Text anchored at its (x, y): ASS \an for the middle-left / centre / right.
_ANCHOR = {"left": 4, "center": 5, "right": 6}


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def picture_rect(
    src_w: int, src_h: int, video_y: float = 50, video_x: float = 50, video_scale: float = 100
) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the picture on the canvas. At scale 100 it is as big as
    fits (full width for a landscape video); its middle sits at ``video_x`` /
    ``video_y`` % of the canvas, kept fully on it."""
    fit = min(CANVAS_W / src_w, CANVAS_H / src_h) * video_scale / 100
    w, h = min(_even(src_w * fit), CANVAS_W), min(_even(src_h * fit), CANVAS_H)
    x = round(CANVAS_W * video_x / 100 - w / 2)
    y = round(CANVAS_H * video_y / 100 - h / 2)
    x = min(max(x, 0), CANVAS_W - w) // 2 * 2
    y = min(max(y, 0), CANVAS_H - h) // 2 * 2
    return x, y, w, h


def frame_picture_rect(frame: dict, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    return picture_rect(
        src_w,
        src_h,
        frame.get("video_y", 50),
        frame.get("video_x", 50),
        frame.get("video_scale", 100),
    )


def fill(text: str, values: dict[str, Any]) -> str:
    """``{part}`` -> the part number, ``{title}`` -> the clip title."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", "" if value is None else str(value))
    return text


def _ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR (ASS keeps blue first; alpha 0 = solid)."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


# Emoji: libass can't draw colour emoji (they come out as empty boxes), so
# they are left out; the dashboard preview drops the same ones.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]")


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text)


def _ass_text(text: str) -> str:
    # ASS reads {...} as override tags and \ as an escape; the user's text is plain.
    text = strip_emoji(text).replace("\\", "/").replace("{", "(").replace("}", ")")
    return "\\N".join(line.strip() for line in text.replace("\r", "").split("\n"))


def _style(name: str, t: dict) -> str:
    px = t.get("size") or 60
    size = round(px * ASS_SIZE_PER_PX)
    colour = _ass_colour(t.get("color") or "#ffffff")
    if t.get("box"):
        # BorderStyle 3: a solid box behind each line, ``Outline`` px of padding.
        box = _ass_colour(t.get("box_color") or "#000000")
        border, edge, back = 3, max(6, round(px * 0.25)), box
    else:
        # Outline: a dark edge keeps the text readable on the picture.
        edge = max(2, round(px * 0.06)) if t.get("outline", True) else 0
        box, border, back = "&H40000000", 1, "&H00000000"
    bold = -1 if t.get("bold") else 0
    italic = -1 if t.get("italic") else 0
    # Margins (left/right) set the wrap width: PlayResX - 2 * margin.
    margin = (CANVAS_W - TEXT_WRAP_W) // 2
    return (
        f"Style: {name},{FONT},{size},{colour},{colour},{box},{back},{bold},{italic},0,0,"
        f"100,100,0,0,{border},{edge},0,5,{margin},{margin},0,1"
    )


def build_ass(frame: dict, values: dict[str, Any]) -> str:
    """The subtitle file with every text layer, shown for the whole clip, in
    list order (later ones on top). Each is anchored at its (x, y) on the
    canvas: its middle, or the middle of its left / right side."""
    styles, events = [], []
    for n, t in enumerate(frame.get("texts") or []):
        text = strip_emoji(fill(t.get("text") or "", values)).strip()
        if not text:
            continue
        name = f"t{n}"
        an = _ANCHOR.get(t.get("align") or "center", 5)
        x = round(min(max(t.get("x", CANVAS_W / 2), 0), CANVAS_W))
        y = round(min(max(t.get("y", CANVAS_H / 2), 0), CANVAS_H))
        styles.append(_style(name, t))
        events.append(
            f"Dialogue: {n},0:00:00.00,9:59:59.00,{name},,0,0,0,,"
            f"{{\\an{an}\\pos({x},{y})}}{_ass_text(text)}"
        )
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {CANVAS_W}",
            f"PlayResY: {CANVAS_H}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


def frame_filter(frame: dict, src_w: int, src_h: int, ass_path: str, pre: str | None = None) -> str:
    """The ffmpeg -vf graph: ``pre`` (e.g. colour grade) on the picture, the
    picture placed on the 1080x1920 canvas over its background, then the text.
    ``ass_path`` must not contain ``:`` or ``'`` (a temp file path)."""
    x, y, w, h = frame_picture_rect(frame, src_w, src_h)
    head = f"{pre}," if pre else ""
    if frame.get("background") == "blur":
        graph = (
            f"{head}split=2[bg][fg];"
            f"[bg]scale={_BLUR_W}:{_BLUR_H}:force_original_aspect_ratio=increase,"
            f"crop={_BLUR_W}:{_BLUR_H},boxblur=12:2,scale={CANVAS_W}:{CANVAS_H},"
            f"eq=brightness=-0.12[bgb];"
            f"[fg]scale={w}:{h}[pic];"
            f"[bgb][pic]overlay={x}:{y}"
        )
    else:
        colour = frame.get("bg_color") if frame.get("background") == "color" else "#000000"
        graph = f"{head}scale={w}:{h},pad={CANVAS_W}:{CANVAS_H}:{x}:{y}:color=0x{colour[1:]}"
    return f"{graph},setsar=1,ass=filename={ass_path}"
