from __future__ import annotations

from app.services import ffmpeg


def test_audio_chunk_cmd_has_seek_and_duration():
    cmd = ffmpeg.build_audio_chunk_cmd("in.mp4", "c.mp3", start_s=600.0, duration_s=600.0)
    assert "-ss" in cmd and "600.000" in cmd
    assert "-t" in cmd
    assert "libmp3lame" in cmd


def test_audio_copy_chunk_does_not_reencode():
    cmd = ffmpeg.build_audio_copy_chunk_cmd("a.mp3", "c.mp3", 595.0, 600.0)
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libmp3lame" not in cmd


def test_reframe_filter_16x9_to_9x16_centre_crop():
    f = ffmpeg.reframe_filter(1920, 1080, 1080, 1920, focus_x=None)
    assert f.startswith("crop=")
    assert "(in_w-out_w)/2" in f
    assert f.endswith("scale=1080:1920")


def test_reframe_filter_focus_clamped():
    f = ffmpeg.reframe_filter(1920, 1080, 1080, 1920, focus_x=0.0)
    # focus at x=0 clamps so crop x never goes negative.
    x = f.split(":")[2]
    assert int(x) == 0


def test_reframe_filter_same_aspect_is_scale_only():
    f = ffmpeg.reframe_filter(1920, 1080, 1920, 1080, focus_x=None)
    assert f == "scale=1920:1080"


def test_color_eq_filter_zero_is_none():
    # All-zero means no grade, so a clip can still be stream-copied.
    assert ffmpeg.color_eq_filter(0, 0, 0) is None


def test_color_eq_filter_maps_sliders():
    f = ffmpeg.color_eq_filter(20, 0, -10)
    assert f == "eq=contrast=1.2:brightness=0.0:saturation=0.9"
    # brightness is softened (strong in ffmpeg): +50 -> 0.2, not 0.5
    assert ffmpeg.color_eq_filter(0, 50, 0) == "eq=contrast=1.0:brightness=0.2:saturation=1.0"


def test_fit_filter_keeps_widescreen_frame():
    # 2.39:1 movie into a 16:9 box: full width, letterbox height, no crop.
    assert ffmpeg.fit_filter(1920, 802, 1920, 1080) == "scale=1920:802"
    # 4:3 into 16:9: full height.
    assert ffmpeg.fit_filter(1440, 1080, 1920, 1080) == "scale=1440:1080"


def test_render_cmd_maps_streams_and_normalises_audio():
    cmd = ffmpeg.build_render_cmd(
        "in.mkv", "out.mp4", 10.0, 25.0, "crop=600:1080:0:0", loudnorm=True
    )
    joined = " ".join(cmd)
    # One video + first audio only; source subtitles/chapters dropped.
    assert "-map 0:v:0" in joined and "-map 0:a:0?" in joined
    assert "-sn" in cmd and "-map_chapters" in cmd
    # Stereo downmix happens before loudnorm.
    af = cmd[cmd.index("-af") + 1]
    assert af.index("channel_layouts=stereo") < af.index("loudnorm")
    assert "libx264" in joined and "aac" in joined
    assert "15.000" in cmd  # duration = end - start


def test_copy_remux_copies_both_streams_and_keeps_timestamps():
    cmd = ffmpeg.build_copy_remux_cmd("in.mkv", "cut.mkv", 600.0, 660.0)
    # Both streams copied in step 1 — re-encoding audio here desyncs it from
    # the keyframe-aligned video and leaves a silent tail.
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "-af" not in cmd and "libx264" not in cmd
    assert "-copyts" in cmd  # so media_start_time() can report the real start
    assert "60.000" in cmd
    assert "-map_chapters" in cmd


def test_audio_normalize_keeps_video_and_reencodes_audio():
    cmd = ffmpeg.build_audio_normalize_cmd("cut.mkv", "out.mp4")
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "loudnorm" in cmd[cmd.index("-af") + 1]
    assert "-ss" not in cmd  # no seeking in step 2
    assert cmd[-1] == "out.mp4"


def test_can_stream_copy_only_web_safe_h264():
    assert ffmpeg.can_stream_copy("h264", "yuv420p") is True
    assert ffmpeg.can_stream_copy("h264", "yuv420p10le") is False  # Hi10P won't play in browsers
    assert ffmpeg.can_stream_copy("hevc", "yuv420p") is False
    assert ffmpeg.can_stream_copy(None, None) is False


def test_scene_detect_decodes_keyframes_only():
    cmd = ffmpeg.build_scene_detect_cmd("in.mkv", "/tmp/s.txt")
    assert cmd[cmd.index("-skip_frame") + 1] == "nokey"
    assert cmd.index("-skip_frame") < cmd.index("-i")
    assert "metadata=print:file=/tmp/s.txt" in cmd[cmd.index("-vf") + 1]


def test_parse_scene_scores():
    text = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.scene_score=0.000000\n"
        "frame:1    pts:2335    pts_time:2.335\n"
        "lavfi.scene_score=0.412000\n"
    )
    assert ffmpeg.parse_scene_scores(text) == [(0.0, 0.0), (2.335, 0.412)]


def test_parse_probe_extracts_fields():
    raw = {
        "format": {"duration": "123.4"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
            },
            {"codec_type": "audio"},
            {"codec_type": "audio"},
        ],
    }
    info = ffmpeg._parse_probe(raw)
    assert info.duration_s == 123.4
    assert info.width == 1920 and info.height == 1080
    assert info.audio_tracks == 2
    assert round(info.fps, 2) == 29.97
    assert info.video_codec == "h264" and info.pix_fmt == "yuv420p"
