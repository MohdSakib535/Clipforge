from __future__ import annotations

from app.services.naming import clip_basename, video_stem


def test_clip_basename_is_just_the_part_number():
    assert clip_basename(41) == "part-041"
    assert clip_basename(3) == "part-003"
    assert clip_basename(None) == "part-000"


def test_clip_basename_sorts_in_video_order():
    assert clip_basename(9) < clip_basename(10) < clip_basename(100)


def test_video_stem_is_filesystem_safe():
    assert video_stem("The.Popes.Exorcist.2023.1080p.mkv") == "The_Popes_Exorcist_2023_1080p"
    assert video_stem('My: "Movie" <final>?.mp4') == "My_Movie_final"
    assert video_stem("../../etc/passwd.mp4") == "passwd"
    assert video_stem("") == "video"


def test_video_stem_keeps_hindi():
    assert video_stem("पोप.mp4") == "पोप"
