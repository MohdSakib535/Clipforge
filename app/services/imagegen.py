"""Client for the image generator on the Mac (imagegen/server.py, FLUX.2
[klein] 4B on the GPU). It runs outside Docker, which can't use the GPU."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config.settings import get_settings

START_HINT = "Start it in a terminal on the Mac with:  uv run imagegen/server.py"


class ImageGenError(RuntimeError):
    pass


@dataclass(frozen=True)
class Picture:
    png: bytes
    # {"faces", "hands", "people"} Apple's Vision saw in it on the Mac, and
    # "text" (pieces it could read) from a newer generator; None if it
    # couldn't look (an older generator, or Vision failed).
    counts: dict[str, int] | None = None


def check() -> None:
    """Fail fast, before any other work, when the generator isn't running."""
    try:
        httpx.get(f"{get_settings().imagegen_url}/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        raise ImageGenError(f"The image generator isn't running. {START_HINT}") from None


def unload() -> None:
    """Free the model's RAM (~5 GB) once a reel's pictures are done. Best
    effort: the generator also unloads by itself after a while."""
    try:
        httpx.post(f"{get_settings().imagegen_url}/unload", timeout=10)
    except httpx.HTTPError:
        pass


def generate(prompt: str, *, width: int, height: int, seed: int, steps: int) -> Picture:
    """One picture (~58 s at 720x1280 on an M4), with what is seen in it."""
    try:
        r = httpx.post(
            f"{get_settings().imagegen_url}/generate",
            json={"prompt": prompt, "width": width, "height": height, "seed": seed, "steps": steps},
            # The very first picture may load (or even download) the model.
            timeout=httpx.Timeout(10, read=900),
        )
    except httpx.ConnectError:
        raise ImageGenError(f"The image generator isn't running. {START_HINT}") from None
    except httpx.HTTPError as exc:
        raise ImageGenError(f"The image generator did not answer: {exc}") from None
    if r.status_code != 200:
        try:
            detail = r.json().get("error") or r.text
        except ValueError:
            detail = r.text
        raise ImageGenError(f"Picture failed: {detail[:300]}")
    try:
        counts = {k: int(r.headers[f"x-{k}"]) for k in ("faces", "hands", "people")}
        if "x-text" in r.headers:  # a generator from before has no text count
            counts["text"] = int(r.headers["x-text"])
    except (KeyError, ValueError):
        counts = None
    return Picture(r.content, counts)
