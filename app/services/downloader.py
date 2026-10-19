"""Import a video from a link (YouTube, Vimeo, Facebook, Instagram, X, a direct
.mp4 …) with yt-dlp.

``probe`` lists the qualities a link offers (a few seconds, no download);
``download`` fetches the chosen quality into storage. Both are blocking network
calls: the API runs ``probe`` in a thread and ``download`` only ever runs on the
Celery ``downloads`` queue.

YouTube needs a JavaScript runtime (Deno, installed in the Docker image) plus the
``yt-dlp-ejs`` package (pulled in by ``yt-dlp[default]``).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config.settings import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DownloadError(Exception):
    """A user-facing reason the link can't be imported."""


@dataclass
class QualityOption:
    id: str  # "best" or the short side in pixels, e.g. "720"
    label: str
    height: int | None
    approx_size_bytes: int | None


@dataclass
class ProbeResult:
    url: str
    title: str
    duration_s: float | None
    thumbnail: str | None
    site: str | None
    qualities: list[QualityOption]


@dataclass
class DownloadResult:
    path: Path
    title: str


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------
def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DownloadError("Paste a full link starting with http:// or https://")
    return url


def parse_quality(quality: str) -> int | None:
    """ "best" -> None (no limit); "720" / "720p" -> 720."""
    q = (quality or "best").strip().lower().removesuffix("p")
    if q == "best":
        return None
    if not q.isdigit() or not 100 <= int(q) <= 4320:
        raise DownloadError(f"Unknown quality '{quality}'")
    return int(q)


def format_sort(max_res: int | None) -> list[str]:
    """yt-dlp sort order. ``res`` is the frame's short side, so "720" means 720p
    for both landscape and vertical (Shorts) videos. At equal resolution prefer
    H.264 + AAC: such sources can be cut without re-encoding, which makes
    horizontal clips ~10x faster to render."""
    res = f"res:{max_res}" if max_res else "res"
    return [res, "fps", "vcodec:h264", "acodec:m4a"]


def _short_side(fmt: dict[str, Any]) -> int | None:
    sides = [v for v in (fmt.get("width"), fmt.get("height")) if v]
    return min(sides) if sides else None


def _has_video(fmt: dict[str, Any]) -> bool:
    return fmt.get("vcodec") != "none"


def _size(fmt: dict[str, Any]) -> int | None:
    return fmt.get("filesize") or fmt.get("filesize_approx")


def quality_options(info: dict[str, Any]) -> list[QualityOption]:
    """One option per distinct resolution the link offers, best first."""
    formats = info.get("formats") or [info]
    audio_sizes = [
        _size(f) for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"
    ]
    audio_size = max((s for s in audio_sizes if s), default=0)

    by_res: dict[int, int | None] = {}
    for f in formats:
        res = _short_side(f)
        if not res or not _has_video(f):
            continue
        size = _size(f)
        if size and f.get("acodec") == "none":
            size += audio_size  # video-only stream: audio is merged in
        prev = by_res.get(res)
        by_res[res] = max(size, prev or 0) if size else prev

    options = [
        QualityOption(id=str(res), label=f"{res}p", height=res, approx_size_bytes=by_res[res])
        for res in sorted(by_res, reverse=True)
    ]
    best_label = f"Best available ({options[0].label})" if options else "Best available"
    best = QualityOption(
        id="best",
        label=best_label,
        height=options[0].height if options else None,
        approx_size_bytes=options[0].approx_size_bytes if options else None,
    )
    return [best, *options]


def check_single_video(info: dict[str, Any]) -> None:
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        raise DownloadError("This is a playlist. Paste the link of a single video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError("Live streams can't be imported. Try again after it ends.")
    formats = info.get("formats") or [info]
    if not any(_has_video(f) for f in formats):
        raise DownloadError("This link has no video (audio only).")


def clean_error(exc: BaseException) -> str:
    """yt-dlp's message without colour codes, the "ERROR:" prefix or the
    "please report this issue" boilerplate."""
    msg = _ANSI.sub("", str(exc)).strip().removeprefix("ERROR:").strip()
    msg = msg.split("; please report this issue")[0].splitlines()[0] if msg else ""
    return msg[:300] or "Download failed"


class ProgressTracker:
    """yt-dlp progress hook -> one overall 0-99% (100 is set once the file is
    merged). The best qualities come as separate video and audio streams, each
    downloaded in turn, so the percentage covers all of them and never goes
    backwards. Reports at most once a second."""

    def __init__(self, report: Callable[[int], None], min_interval_s: float = 1.0):
        self._report = report
        self._min_interval = min_interval_s
        self._finished: set[str] = set()
        self._last_pct = -1
        self._last_t = float("-inf")
        self._lock = threading.Lock()  # concurrent fragment downloads use threads

    def __call__(self, d: dict[str, Any]) -> None:
        with self._lock:
            name = d.get("filename") or ""
            info = d.get("info_dict") or {}
            parts = max(1, len(info.get("requested_formats") or ()))

            frac = 0.0
            if d.get("status") == "finished":
                self._finished.add(name)
            elif d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    frac = (d.get("downloaded_bytes") or 0) / total
                elif d.get("fragment_count"):
                    frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            done = len(self._finished - {name}) + (1.0 if name in self._finished else frac)
            pct = min(99, int(100 * done / parts))

            now = time.monotonic()
            if pct <= self._last_pct:
                return
            if now - self._last_t < self._min_interval and d.get("status") != "finished":
                return
            self._last_pct, self._last_t = pct, now
        self._report(pct)


# ---------------------------------------------------------------------------
# yt-dlp calls (network)
# ---------------------------------------------------------------------------
class _YdlLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("ytdlp_warning", extra={"detail": _ANSI.sub("", msg)[:500]})

    def error(self, msg: str) -> None:
        log.warning("ytdlp_error", extra={"detail": _ANSI.sub("", msg)[:500]})


def _base_opts() -> dict[str, Any]:
    settings = get_settings()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YdlLogger(),
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
    }
    if settings.ytdlp_cookies_file:
        opts["cookiefile"] = settings.ytdlp_cookies_file
    return opts


def probe(url: str) -> ProbeResult:
    """Read a link's title and available qualities (no download)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError as YdlError

    url = validate_url(url)
    try:
        with YoutubeDL(_base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except YdlError as exc:
        raise DownloadError(clean_error(exc)) from None
    if not info:
        raise DownloadError("Could not read this link")
    check_single_video(info)
    return ProbeResult(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "video",
        duration_s=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        site=info.get("extractor_key"),
        qualities=quality_options(info),
    )


def download(
    url: str,
    dest_dir: Path,
    quality: str = "best",
    on_progress: Callable[[int], None] | None = None,
) -> DownloadResult:
    """Download ``url`` at ``quality`` into ``dest_dir/source.<ext>``.

    Partial ``.part`` files are resumed if the task is retried."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError as YdlError

    url = validate_url(url)
    max_res = parse_quality(quality)
    max_bytes = get_settings().max_upload_bytes
    dest_dir.mkdir(parents=True, exist_ok=True)

    opts = _base_opts() | {
        "format": "bv*+ba/b",
        "format_sort": format_sort(max_res),
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "max_filesize": max_bytes,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [ProgressTracker(on_progress)] if on_progress else [],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise DownloadError("Could not read this link")
            check_single_video(info)
            info = ydl.process_ie_result(info, download=True)
    except YdlError as exc:
        raise DownloadError(clean_error(exc)) from None

    path = _downloaded_path(info, dest_dir)
    if path is None:
        raise DownloadError(
            "Nothing was downloaded. The video may be larger than the upload limit."
        )
    if path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise DownloadError("The video is larger than the upload limit. Pick a lower quality.")
    return DownloadResult(path=path, title=info.get("title") or "video")


def _downloaded_path(info: dict[str, Any], dest_dir: Path) -> Path | None:
    for d in info.get("requested_downloads") or []:
        p = Path(d.get("filepath") or "")
        if d.get("filepath") and p.is_file():
            return p
    files = [
        p
        for p in dest_dir.glob("source.*")
        if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")
    ]
    return max(files, key=lambda p: p.stat().st_size, default=None)
