from __future__ import annotations

import pytest

from app.models import VideoStatus
from app.services import downloader
from app.services.downloader import (
    DownloadError,
    ProbeResult,
    ProgressTracker,
    QualityOption,
    check_single_video,
    clean_error,
    format_sort,
    parse_quality,
    quality_options,
    validate_url,
)

# A YouTube-like format list: separate video-only and audio-only streams.
_YT_FORMATS = [
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 5_000_000},
    {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize": 4_000_000},
    {"format_id": "137", "vcodec": "avc1", "acodec": "none", "width": 1920, "height": 1080,
     "filesize": 80_000_000},
    {"format_id": "248", "vcodec": "vp9", "acodec": "none", "width": 1920, "height": 1080,
     "filesize": 60_000_000},
    {"format_id": "136", "vcodec": "avc1", "acodec": "none", "width": 1280, "height": 720,
     "filesize_approx": 40_000_000},
    {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "width": 640, "height": 360,
     "filesize": 15_000_000},
    {"format_id": "sb0", "vcodec": "none", "acodec": "none", "format_note": "storyboard"},
]  # fmt: skip


def test_validate_url():
    assert validate_url("  https://youtu.be/abc  ") == "https://youtu.be/abc"
    for bad in ("", "youtube.com/watch?v=x", "ftp://x.com/a.mp4", "file:///etc/passwd", "https://"):
        with pytest.raises(DownloadError):
            validate_url(bad)


def test_parse_quality():
    assert parse_quality("best") is None
    assert parse_quality("720") == 720
    assert parse_quality("1080p") == 1080
    for bad in ("hd", "50", "99999"):
        with pytest.raises(DownloadError):
            parse_quality(bad)


def test_format_sort_limits_resolution_and_prefers_h264():
    assert format_sort(720) == ["res:720", "fps", "vcodec:h264", "acodec:m4a"]
    assert format_sort(None)[0] == "res"


def test_quality_options_lists_each_resolution_best_first():
    opts = quality_options({"formats": _YT_FORMATS})
    assert [o.id for o in opts] == ["best", "1080", "720", "360"]
    assert opts[0].label == "Best available (1080p)"
    by_id = {o.id: o for o in opts}
    # video-only stream + best audio stream; largest 1080p stream wins
    assert by_id["1080"].approx_size_bytes == 85_000_000
    assert by_id["720"].approx_size_bytes == 45_000_000
    # format 18 already contains audio
    assert by_id["360"].approx_size_bytes == 15_000_000


def test_quality_options_vertical_video_uses_short_side():
    info = {"formats": [{"vcodec": "avc1", "acodec": "mp4a", "width": 1080, "height": 1920}]}
    assert [o.id for o in quality_options(info)] == ["best", "1080"]


def test_quality_options_unknown_resolution_offers_best_only():
    opts = quality_options({"url": "https://x.com/a.mp4", "ext": "mp4"})
    assert [o.id for o in opts] == ["best"]
    assert opts[0].label == "Best available"


def test_check_single_video_rejects_playlist_live_and_audio_only():
    with pytest.raises(DownloadError, match="playlist"):
        check_single_video({"_type": "playlist", "entries": []})
    with pytest.raises(DownloadError, match="Live"):
        check_single_video({"is_live": True, "formats": _YT_FORMATS})
    with pytest.raises(DownloadError, match="no video"):
        check_single_video({"formats": [{"vcodec": "none", "acodec": "mp3"}]})
    check_single_video({"formats": _YT_FORMATS})  # ok


def test_clean_error_strips_noise():
    raw = "\x1b[0;31mERROR:\x1b[0m [youtube] abc: Video unavailable\nmore detail"
    assert clean_error(Exception(raw)) == "[youtube] abc: Video unavailable"
    raw = "ERROR: Unsupported URL: x; please report this issue on https://github.com/..."
    assert clean_error(Exception(raw)) == "Unsupported URL: x"


def test_progress_covers_video_and_audio_streams_and_never_goes_back():
    seen: list[int] = []
    hook = ProgressTracker(seen.append, min_interval_s=0)
    info = {"requested_formats": [{}, {}]}  # video + audio, downloaded in turn

    def ev(name, status, done=0, total=100):
        hook({"filename": name, "status": status, "downloaded_bytes": done,
              "total_bytes": total, "info_dict": info})  # fmt: skip

    ev("v", "downloading", 50)
    ev("v", "finished", 100)
    ev("a", "downloading", 0)  # second stream starts at 0 -> must not drop back
    ev("a", "downloading", 50)
    ev("a", "finished", 100)
    assert seen == [25, 50, 75, 99]


def test_progress_is_throttled():
    seen: list[int] = []
    hook = ProgressTracker(seen.append, min_interval_s=3600)
    for done in (10, 20, 30):
        hook({"filename": "v", "status": "downloading", "downloaded_bytes": done,
              "total_bytes": 100, "info_dict": {}})  # fmt: skip
    assert seen == [10]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_probe_endpoint_returns_qualities(client, monkeypatch):
    def fake_probe(url):
        return ProbeResult(
            url=url,
            title="My talk",
            duration_s=120.0,
            thumbnail=None,
            site="Youtube",
            qualities=[
                QualityOption("best", "Best available (720p)", 720, 1000),
                QualityOption("720", "720p", 720, 1000),
            ],
        )

    monkeypatch.setattr(downloader, "probe", fake_probe)
    resp = client.post("/videos/from-url/qualities", json={"url": "https://youtu.be/abc"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "My talk"
    assert [q["id"] for q in body["qualities"]] == ["best", "720"]


def test_probe_endpoint_reports_bad_link(client, monkeypatch):
    def fake_probe(url):
        raise DownloadError("Unsupported URL: https://example.com")

    monkeypatch.setattr(downloader, "probe", fake_probe)
    resp = client.post("/videos/from-url/qualities", json={"url": "https://example.com"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_link"


def test_import_from_url_returns_at_once_and_queues_download(client):
    resp = client.post(
        "/videos/from-url", json={"url": "https://www.youtube.com/watch?v=abc", "quality": "720"}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == VideoStatus.downloading.value
    assert body["download_progress"] == 0
    assert body["source_url"] == "https://www.youtube.com/watch?v=abc"
    assert client.enqueued["download"] == [(body["id"], "720")]

    # Rights can be confirmed while it downloads; a job can't start until ready.
    rights = client.post(f"/videos/{body['id']}/confirm-rights", json={"confirmed": True})
    assert rights.status_code == 200
    job = client.post(f"/videos/{body['id']}/jobs", json={"platforms": ["youtube_shorts"]})
    assert job.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://youtu.be/abc", "quality": "ultra"},
        {"url": "not a link at all"},
    ],
)
def test_import_from_url_rejects_bad_input(client, payload):
    resp = client.post("/videos/from-url", json=payload)
    assert resp.status_code in (400, 422)
    assert client.enqueued["download"] == []
