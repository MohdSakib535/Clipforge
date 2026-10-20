from __future__ import annotations

from app.config.platforms import get_preset
from app.models import Job
from app.services.segments import Candidate
from app.tasks.pipeline import _clip_length, _part_text


def test_title_is_part_number_and_first_words():
    title, description = _part_text(
        Candidate(0, 60, "Hello there everyone welcome back to the show today we talk"), rank=3
    )
    assert title == "Part 3 - Hello there everyone welcome back to the show today"
    assert description == "Hello there everyone welcome back to the show today we talk"


def test_part_without_speech_is_just_the_number():
    assert _part_text(Candidate(0, 60, ""), rank=7) == ("Part 7", "")


def test_hindi_title_is_written_in_roman_letters():
    title, description = _part_text(Candidate(0, 60, "तुम कहाँ जा रहे हो"), rank=1)
    assert title.startswith("Part 1 - ") and title.isascii()
    assert description.isascii()


def test_clip_length_uses_the_job_choice_within_the_preset_range():
    job = Job(platforms=["youtube_shorts"], settings={"coverage_lengths": {"youtube_shorts": 45}})
    assert _clip_length(job, get_preset("youtube_shorts")) == 45
    too_long = Job(settings={"coverage_lengths": {"youtube_shorts": 500}})
    assert _clip_length(too_long, get_preset("youtube_shorts")) == 60  # preset max


def test_clip_length_default_is_40s_vertical_and_3min_horizontal():
    job = Job(platforms=["youtube_shorts", "facebook_video"], settings={})
    assert _clip_length(job, get_preset("youtube_shorts")) == 40
    assert _clip_length(job, get_preset("facebook_video")) == 180


def test_rerenders_run_on_their_own_queue():
    # Colour on dozens of clips must not block new jobs on the main worker.
    from app.tasks.celery_app import celery_app

    assert celery_app.conf.task_routes["render_single_clip"] == {"queue": "renders"}
