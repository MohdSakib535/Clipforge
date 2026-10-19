from __future__ import annotations

from app.services.naming import clip_basename


def test_clip_basename_has_video_name_part_and_platform():
    name = clip_basename("The.Popes.Exorcist.2023.1080p.mkv", "facebook_video", 41)
    assert name == "The_Popes_Exorcist_2023_1080p_part-041_facebook_video"


def test_clip_basename_is_filesystem_safe_and_sorts():
    name = clip_basename('My: "Movie" <final>?.mp4', "youtube_shorts", 3)
    assert name == "My_Movie_final_part-003_youtube_shorts"
    assert clip_basename("a.mp4", "x", 9) < clip_basename("a.mp4", "x", 10)  # zero-padded


def test_clip_basename_keeps_hindi_and_handles_empty():
    assert clip_basename("पोप.mp4", "facebook_reel", 1).startswith("पोप_part-001")
    assert clip_basename("", "facebook_reel", None) == "video_part-000_facebook_reel"


def test_clip_basename_cannot_escape_with_a_path():
    assert clip_basename("../../etc/passwd.mp4", "x", 1) == "passwd_part-001_x"
