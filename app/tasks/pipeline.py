from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.config.platforms import PlatformPreset, get_preset
from app.config.settings import ConfigError, Settings, get_settings
from app.core.logging import bind_job_id, get_logger
from app.db.session import SessionLocal
from app.models import (
    AudioTrack,
    Clip,
    ClipStatus,
    Job,
    JobStage,
    JobStatus,
    Transcript,
    Video,
    VideoStatus,
)
from app.services import analysis, bgaudio, dub, ffmpeg
from app.services.cost import CostCapExceeded, CostTracker
from app.services.discard import discard_job
from app.services.filehash import file_fingerprint
from app.services.render import RenderSpec, render_clip
from app.services.romanize import has_words, is_mostly_devanagari, romanize
from app.services.segments import Candidate, build_coverage_segments
from app.services.storage import get_storage
from app.services.transcriber import get_transcriber
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
class JobCancelled(Exception):
    """The user stopped the job (Start over); it is deleted, not failed."""


def _cancel_requested(db: Session, job_id: str) -> bool:
    """Fresh read: the API marks a running job ``cancelled`` (or deletes a
    queued one) from another process."""
    status = db.execute(select(Job.status).where(Job.id == job_id)).scalar_one_or_none()
    return status is None or status == JobStatus.cancelled


def _check_cancel(db: Session, job: Job) -> None:
    if _cancel_requested(db, job.id):
        raise JobCancelled


def _set_stage(db: Session, job: Job, stage: JobStage, progress: int) -> None:
    job.stage = stage
    job.progress = max(job.progress, progress)
    db.commit()
    log.info("job_stage", extra={"stage": stage.value, "progress": job.progress})


def _save_cost(db: Session, job: Job, cost: CostTracker) -> None:
    job.audio_minutes = round(cost.audio_minutes, 3)
    job.models_used = {k: round(v, 4) for k, v in cost.models_used.items()}
    job.estimated_cost_usd = cost.total_usd
    db.commit()


def _source_path(video: Video) -> str:
    return video.path or str(get_storage().path_for(f"videos/{video.id}/source"))


def _stream_copy_ok(db: Session, video: Video) -> bool:
    """Can this video be cut without re-encoding? Codec info is recorded at
    ingest; videos ingested before that are probed once and cached."""
    meta = dict(video.ff_metadata or {})
    if "video_codec" not in meta:
        info = ffmpeg.ffprobe_metadata(_source_path(video))
        meta["video_codec"], meta["pix_fmt"] = info.video_codec, info.pix_fmt
        video.ff_metadata = meta
        db.commit()
    return ffmpeg.can_stream_copy(meta.get("video_codec"), meta.get("pix_fmt"))


def _normalized_audio_path(video: Video) -> str:
    return str(get_storage().path_for(f"videos/{video.id}/audio_norm.m4a"))


def _ensure_normalized_audio(norm_path: str, source_path: str) -> str | None:
    """Loudness-normalise the source's audio once and keep it for every job and
    re-render of this video (copy cuts then skip their per-clip loudnorm).
    Runs in a background thread while transcription runs, so it must not touch
    the database. Written to a temp name first: a crash never leaves a
    half-written track behind. On failure clips fall back to per-clip loudnorm."""
    if os.path.exists(norm_path):
        return norm_path
    Path(norm_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{norm_path[: -len('.m4a')]}.part-{uuid.uuid4().hex}.m4a"
    try:
        ffmpeg.run(ffmpeg.build_normalized_audio_cmd(source_path, tmp))
        os.replace(tmp, norm_path)
        return norm_path
    except Exception:
        log.warning("normalized_audio_failed", exc_info=True)
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def _cached_scene_cuts(video: Video) -> list[float] | None:
    """Scene cuts found by an earlier job of this video (same threshold)."""
    cached = (video.ff_metadata or {}).get("scene_cuts")
    if isinstance(cached, dict) and cached.get("threshold") == analysis.SCENE_THRESHOLD:
        return cached.get("cuts")
    return None


def _save_scene_cuts(db: Session, video: Video, cuts: list[float]) -> None:
    video.ff_metadata = {
        **(video.ff_metadata or {}),
        "scene_cuts": {"threshold": analysis.SCENE_THRESHOLD, "cuts": cuts},
    }
    db.commit()


def _clip_length(job: Job, preset: PlatformPreset) -> int:
    """Target seconds per clip for this platform: the job's choice, else 40 s
    for vertical formats and 3 min for horizontal ones, kept inside the
    preset's allowed range."""
    chosen = ((job.settings or {}).get("coverage_lengths") or {}).get(preset.key)
    want = int(chosen) if chosen else (40 if preset.is_vertical else 180)
    return max(preset.length_min_s, min(preset.length_max_s, want))


def _part_text(part: Candidate, rank: int, label: str = "Part") -> tuple[str, str]:
    """(title, description) for a clip, taken from its own transcript:
    "Part 12 - <first 9 words>" and the part's text. Hindi (Devanagari) is
    written in Roman letters, the way it is typed on social media."""
    text = (part.text or "").strip()
    if is_mostly_devanagari(text):
        text = romanize(text)
    snippet = " ".join(text.split()[:9])
    title = f"{label} {rank}" + (f" - {snippet}" if snippet else "")
    return title[:120], text[:800]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
@celery_app.task(name="ingest_video")
def ingest_video(video_id: str) -> None:
    """Probe metadata (~1s even for a 3-hour file) and mark the video ready.

    There is no preview proxy: scene and face analysis read keyframes straight
    from the source, which is faster than building a low-res copy first."""
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        if video is None:
            log.warning("ingest_missing_video", extra={"video_id": video_id})
            return
        if video.status == VideoStatus.ready:
            return  # idempotent

        video.status = VideoStatus.ingesting
        video.error = None
        db.commit()
        try:
            info = ffmpeg.ffprobe_metadata(_source_path(video))
            video.duration_s = info.duration_s
            video.width = info.width
            video.height = info.height
            video.fps = info.fps
            video.audio_tracks = info.audio_tracks
            video.ff_metadata = {
                "format": info.raw.get("format", {}),
                "video_codec": info.video_codec,
                "pix_fmt": info.pix_fmt,
            }
            # ~12 MB read, a fraction of a second even for a 5 GB film.
            video.content_hash = file_fingerprint(_source_path(video))
            video.status = VideoStatus.ready
            db.commit()
            log.info("ingest_ready", extra={"video_id": video_id, "duration": info.duration_s})
        except Exception as exc:
            video.status = VideoStatus.failed
            video.error = str(exc)[:2000]
            db.commit()
            log.exception("ingest_failed", extra={"video_id": video_id})
            raise


# ---------------------------------------------------------------------------
# Stage: transcribe (with caching)
# ---------------------------------------------------------------------------
def _audio_path(video: Video) -> str:
    return str(get_storage().path_for(f"videos/{video.id}/audio.mp3"))


def _ensure_audio(video: Video, donor: Video | None = None) -> str:
    """The video's mono MP3 (transcription input and energy analysis). Taken
    from ``donor`` — the same file uploaded before — when it has one."""
    audio_path = _audio_path(video)
    if os.path.exists(audio_path):
        return audio_path
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    donor_audio = _audio_path(donor) if donor else None
    if donor_audio and os.path.exists(donor_audio):
        shutil.copyfile(donor_audio, audio_path)
        return audio_path
    ffmpeg.run(
        ffmpeg.build_audio_chunk_cmd(_source_path(video), audio_path, 0.0, video.duration_s or 1e9)
    )
    return audio_path


def _fingerprint(db: Session, video: Video) -> str | None:
    """The video's content fingerprint, computed once and stored."""
    if video.content_hash is None:
        try:
            video.content_hash = file_fingerprint(_source_path(video))
        except OSError:
            return None  # source file gone
        db.commit()
    return video.content_hash


def _same_file_transcript(db: Session, video: Video) -> Transcript | None:
    """A finished transcript of the very same file uploaded earlier by this
    user, so re-uploading a film never pays for its transcription twice.

    Older videos have no stored fingerprint; it is worked out for them here,
    but only for those whose file size matches (the cheap first check)."""
    fp = _fingerprint(db, video)
    if fp is None:
        return None
    size = int(fp.split("-", 1)[0])
    rows = (
        db.query(Video, Transcript)
        .join(Transcript, Transcript.video_id == Video.id)
        .filter(Video.user_id == video.user_id, Video.id != video.id)
        .order_by(Transcript.created_at.desc())
        .all()
    )
    for other, transcript in rows:
        if not transcript.words:
            continue
        if other.content_hash is None:
            try:
                if os.path.getsize(_source_path(other)) != size:
                    continue
            except OSError:
                continue
        if _fingerprint(db, other) == fp:
            return transcript
    return None


def _transcribe(db: Session, video: Video, cost: CostTracker, on_progress=None) -> Transcript:
    existing = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
    if existing and existing.words:
        log.info("transcript_cache_hit", extra={"video_id": video.id})
        _ensure_audio(video)
        return existing

    donor = _same_file_transcript(db, video)
    if donor is not None:
        log.info(
            "transcript_reused_same_file",
            extra={"video_id": video.id, "from_video_id": donor.video_id},
        )
        _ensure_audio(video, donor=db.get(Video, donor.video_id))
        row = existing or Transcript(video_id=video.id)
        for field in ("language", "provider", "duration_s", "text", "words", "segments"):
            setattr(row, field, getattr(donor, field))
        if existing is None:
            db.add(row)
        db.commit()
        return row

    audio_path = _ensure_audio(video)
    settings = get_settings()
    transcriber = get_transcriber(settings)
    # Finished pieces are kept here until the transcript is saved: if the
    # worker restarts mid-way, the retry pays only for what is left.
    chunk_dir = get_storage().path_for(f"videos/{video.id}/transcript_chunks")
    result = transcriber.transcribe(
        audio_path, language=None, on_progress=on_progress, cache_dir=chunk_dir
    )
    # The whole duration is counted even when pieces came from an interrupted
    # run: that run stopped before it could record what it spent.
    cost.add_transcription(
        settings.transcribe_model, result.audio_minutes, free=settings.transcribe_is_local
    )

    row = existing or Transcript(video_id=video.id)
    row.language = result.language
    row.provider = result.provider
    row.duration_s = result.duration_s
    row.text = result.text
    row.words = [w.as_dict() for w in result.words]
    row.segments = result.segments
    if existing is None:
        db.add(row)
    db.commit()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    return row


# ---------------------------------------------------------------------------
# Full job (transcribe -> analyze -> cut the whole video into parts -> render)
# ---------------------------------------------------------------------------
@celery_app.task(name="run_job")
def run_job(job_id: str) -> None:
    bind_job_id(job_id)
    settings = get_settings()

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            log.warning("run_job_missing", extra={"job_id": job_id})
            return
        if job.status == JobStatus.completed:
            return
        if job.status == JobStatus.cancelled:  # stopped before it started
            discard_job(db, job_id)
            return
        video = db.get(Video, job.video_id)

        cost = CostTracker(cap_usd=settings.max_job_cost_usd, audio_minutes=job.audio_minutes)

        try:
            job.status = JobStatus.running
            job.error = None
            db.commit()
        except StaleDataError:  # deleted by a cancel just as it was picked up
            log.info("run_job_cancelled_before_start", extra={"job_id": job_id})
            return

        try:
            if (job.settings or {}).get("trims") is not None:
                _make_trim_clips(db, job, video, cost, settings)
            else:
                _make_coverage_clips(db, job, video, cost, settings)

            _check_cancel(db, job)
            _set_stage(db, job, JobStage.finalizing, 98)
            job.status = JobStatus.completed
            job.stage = JobStage.done
            job.progress = 100
            db.commit()
            log.info("job_complete", extra={"clips": len(job.clips)})

        except JobCancelled:
            discard_job(db, job_id)
        except ConfigError as exc:
            _save_cost(db, job, cost)
            job.status = JobStatus.failed
            job.error = str(exc)[:2000]
            db.commit()
            log.warning("job_config_error", extra={"error": str(exc)})
        except CostCapExceeded as exc:
            _save_cost(db, job, cost)
            job.status = JobStatus.failed
            job.error = str(exc)
            db.commit()
            log.warning("job_cost_capped", extra={"error": str(exc)})
        except Exception as exc:
            db.rollback()
            if _cancel_requested(db, job_id):  # stopped while something broke
                discard_job(db, job_id)
                return
            job.status = JobStatus.failed
            job.error = str(exc)[:2000]
            db.commit()
            log.exception("job_failed", extra={"job_id": job_id})
            raise
        finally:
            bind_job_id(None)


def _make_coverage_clips(
    db: Session, job: Job, video: Video, cost: CostTracker, settings: Settings
) -> None:
    """Transcribe, find scene cuts, cut the whole video (or the stretch picked
    in the Trim step) into ~N-second parts per platform and render them."""
    # Transcription can be switched off per job (Settings step): then
    # parts are cut on time at scene changes, with no titles from speech.
    transcribe = bool((job.settings or {}).get("transcribe", True))
    picked = (job.settings or {}).get("source_range")
    start_s, end_s = (picked["start_s"], picked["end_s"]) if picked else (0.0, video.duration_s)
    if transcribe:
        # Fail at once, not after minutes of work, if transcription can't
        # run (unknown TRANSCRIBER, or OpenAI chosen without a key).
        settings.check_transcriber_config()

    # 1+2) Transcribe and detect scenes CONCURRENTLY: transcription waits
    # on the network, scene detection runs in an ffmpeg subprocess, so
    # they overlap almost for free.
    _set_stage(db, job, JobStage.transcribing if transcribe else JobStage.analyzing, 5)

    def _tx_progress(fraction: float) -> None:
        # Runs on this thread (the transcriber collects results here),
        # so a Start over stops transcription within a chunk/segment.
        _check_cancel(db, job)
        job.progress = max(job.progress, 5 + int(20 * fraction))
        db.commit()

    # Also, once per video: loudness-normalise the audio for copy cuts
    # (horizontal clips) so they don't run loudnorm one by one. Scene
    # cuts are cached too; a re-run of a video skips both. A trimmed video
    # doesn't normalise all of it: its few clips run their own loudnorm.
    source = _source_path(video)
    needs_norm = (
        picked is None
        and (video.audio_tracks or 0) > 0
        and any(not get_preset(p).is_vertical for p in (job.platforms or []))
        and _stream_copy_ok(db, video)
    )
    scene_cuts = _cached_scene_cuts(video)
    bg = ThreadPoolExecutor(max_workers=2)
    try:
        scene_future = bg.submit(analysis.detect_scenes, source) if scene_cuts is None else None
        norm_future = (
            bg.submit(_ensure_normalized_audio, _normalized_audio_path(video), source)
            if needs_norm
            else None
        )
        if transcribe and picked:
            sentences, words = _range_transcript(
                db, video, start_s, end_s, cost, settings, on_progress=_tx_progress
            )
        elif transcribe:
            transcript = _transcribe(db, video, cost, on_progress=_tx_progress)
            sentences, words = transcript.segments or [], transcript.words or []
        else:
            sentences, words = [], []
        _save_cost(db, job, cost)
        _check_cancel(db, job)
        _set_stage(db, job, JobStage.analyzing, 25)
        if scene_future is not None:
            scene_cuts = scene_future.result()
            _save_scene_cuts(db, video, scene_cuts)
        if norm_future is not None:
            norm_future.result()  # copy cuts use it
    finally:
        # A cancel or failure doesn't wait for these ffmpeg runs: scene
        # detection ends on its own, and the normalised audio is a
        # cache for this video, which is kept.
        bg.shutdown(wait=False, cancel_futures=True)

    platforms: list[str] = job.platforms or []
    caption_cfg = (job.settings or {}).get("captions", {})

    total_platforms = max(1, len(platforms))
    for p_idx, platform in enumerate(platforms):
        preset = get_preset(platform)
        base = 35 + int(55 * p_idx / total_platforms)
        span = int(55 / total_platforms)
        render_range = (base + max(1, span // 2), base + span)

        _check_cancel(db, job)
        _set_stage(db, job, JobStage.planning_clips, base)

        # Tile the entire video (or the picked stretch) into ~target-second
        # parts; each part is a clip.
        target = _clip_length(job, preset)
        parts = build_coverage_segments(
            sentences, scene_cuts, target, start_s=start_s, duration_s=end_s, words=words
        )
        log.info(
            "coverage_segments",
            extra={"platform": platform, "count": len(parts), "target_s": target},
        )
        if not parts:
            continue
        _set_stage(db, job, JobStage.rendering, render_range[0])
        _render_parts(
            db, job, video, preset, parts, words, caption_cfg, settings,
            progress=render_range, transcribed=transcribe,
        )  # fmt: skip


# ---------------------------------------------------------------------------
# Part of a video only: trims picked by hand ("Reel from a video") and the
# stretch picked in "Clip a video"'s Trim step
# ---------------------------------------------------------------------------
_TRIM_CONTEXT_S = 1.5  # heard on each side of a trim (see _transcribe_seconds)


def _range_transcript(
    db: Session,
    video: Video,
    start: float,
    end: float,
    cost: CostTracker,
    settings: Settings,
    on_progress=None,
) -> tuple[list[dict], list[dict]]:
    """(sentences, words) of the stretch picked in the Trim step: from the
    whole-video transcript when there is one (this video's or the same
    file's), else by transcribing only these seconds. Not saved as the
    video's transcript: it covers only part of it."""
    own = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
    full = own if own and own.words else _same_file_transcript(db, video)
    if full is not None:
        log.info("transcript_cache_hit", extra={"video_id": video.id})
        return _inside(full.segments or [], start, end), _inside(full.words, start, end)
    return _transcribe_seconds(video, start, end, cost, settings, on_progress)


def _inside(items: list[dict], start: float, end: float) -> list[dict]:
    """Timed words/sentences that overlap ``start``-``end``."""
    return [x for x in items if x["end"] > start and x["start"] < end]


def _transcribe_seconds(
    video: Video,
    start: float,
    end: float,
    cost: CostTracker,
    settings: Settings,
    on_progress=None,
) -> tuple[list[dict], list[dict]]:
    """(sentences, words) said between ``start`` and ``end`` (seconds in the
    video), by transcribing just these seconds, which takes moments even in a
    3-hour film."""
    if video.audio_tracks == 0:
        return [], []  # a silent video: nothing to transcribe
    # A little before and after is heard too: a word cut at the edge is
    # recognised from its context instead of guessed from a fragment.
    lo, hi = max(0.0, start - _TRIM_CONTEXT_S), end + _TRIM_CONTEXT_S
    with tempfile.TemporaryDirectory() as tmp:
        audio = str(Path(tmp) / "part.mp3")
        ffmpeg.run(ffmpeg.build_audio_chunk_cmd(_source_path(video), audio, lo, hi - lo))
        result = get_transcriber(settings).transcribe(audio, on_progress=on_progress)
    cost.add_transcription(
        settings.transcribe_model, result.audio_minutes, free=settings.transcribe_is_local
    )
    words = [w.shifted(lo).as_dict() for w in result.words]
    sentences = [
        {**s, "start": round(s["start"] + lo, 3), "end": round(s["end"] + lo, 3)}
        for s in result.segments
    ]
    return _inside(sentences, start, end), _inside(words, start, end)


def _full_transcript_words(db: Session, video: Video) -> list[dict] | None:
    """Timed words of the whole video if it (or the same file uploaded
    before) was already transcribed, else None."""
    own = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
    if own and own.words:
        return own.words
    donor = _same_file_transcript(db, video)
    return donor.words if donor else None


def _trim_words(
    video: Video,
    start: float,
    end: float,
    full: list[dict] | None,
    cost: CostTracker,
    settings: Settings,
) -> list[dict]:
    """Timed words (seconds in the video) said between ``start`` and ``end``:
    from the whole-video transcript when there is one, else by transcribing
    just these seconds."""
    if full is not None:
        return _inside(full, start, end)
    return _transcribe_seconds(video, start, end, cost, settings)[1]


def _make_trim_clips(
    db: Session, job: Job, video: Video, cost: CostTracker, settings: Settings
) -> None:
    """One clip per trim and platform, titled "Reel N". No scene detection
    and no whole-video transcript: only the trims' own seconds are
    transcribed, for trims that asked for it. Trims added later (POST
    /jobs/{id}/trims) run the job again; trims that have clips are skipped."""
    trims: list[dict] = job.settings["trims"]
    made = {(extra or {}).get("trim") for (extra,) in db.query(Clip.extra).filter_by(job_id=job.id)}
    todo = [(n, trim) for n, trim in enumerate(trims) if n not in made]
    transcribe = any(trim["transcribe"] for _, trim in todo)
    if transcribe:
        settings.check_transcriber_config()

    # 1) What is said in each trim.
    _set_stage(db, job, JobStage.transcribing if transcribe else JobStage.rendering, 5)
    full = _full_transcript_words(db, video) if transcribe else None
    said: dict[int, list[dict]] = {}
    for i, (n, trim) in enumerate(todo, start=1):
        if not trim["transcribe"]:
            continue
        _check_cancel(db, job)
        said[n] = _trim_words(video, trim["start_s"], trim["end_s"], full, cost, settings)
        _save_cost(db, job, cost)
        job.progress = max(job.progress, 5 + int(25 * i / len(todo)))
        db.commit()

    # 2) Create the clips, then render them all together.
    _check_cancel(db, job)
    _set_stage(db, job, JobStage.rendering, 30)
    language = ((job.settings or {}).get("captions") or {}).get("language")
    specs: list[tuple[str, RenderSpec]] = []
    for n, trim in todo:
        start, end = trim["start_s"], trim["end_s"]
        words = said.get(n)  # None: not transcribed
        text = "".join(w["word"] for w in words or []).strip()
        title, description = _part_text(Candidate(start, end, text), n + 1, label="Reel")
        extra: dict = {"trim": n, "has_speech": None}
        if words is not None:
            extra.update(has_speech=has_words(text), words=words, transcript=text)
        for platform in trim["platforms"]:
            preset = get_preset(platform)
            clip = Clip(
                job_id=job.id,
                platform=platform,
                start_s=start,
                end_s=end,
                extra=dict(extra),
                title=title,
                description=description,
                hashtags=[],
                captions_enabled=words is not None,  # captions come from the transcript
                language=language,
                rank=n + 1,
                status=ClipStatus.rendering,
                duration_s=round(end - start, 3),
            )
            db.add(clip)
            db.commit()
            copy_ok = not preset.is_vertical and _stream_copy_ok(db, video)
            spec = _render_spec(clip, video, preset, words or [], settings, stream_copy_ok=copy_ok)
            specs.append((clip.id, spec))
    _render_specs(db, job, specs, settings.render_concurrency, (30, 95))


def _render_spec(
    clip: Clip,
    video: Video,
    preset: PlatformPreset,
    words: list[dict],
    settings: Settings,
    *,
    stream_copy_ok: bool,
) -> RenderSpec:
    storage = get_storage()
    norm = _normalized_audio_path(video)
    return RenderSpec(
        source_path=_source_path(video),
        out_path=str(storage.path_for(f"clips/{clip.id}/clip.mp4")),
        thumb_path=str(storage.path_for(f"clips/{clip.id}/thumb.jpg")),
        start_s=clip.start_s,
        end_s=clip.end_s,
        preset=preset,
        src_width=video.width or preset.width,
        src_height=video.height or preset.height,
        words=words,
        captions_enabled=clip.captions_enabled,
        encode_preset=settings.render_preset,
        reframe_samples=settings.reframe_samples,
        stream_copy_ok=stream_copy_ok,
        color_contrast=clip.color_contrast,
        color_brightness=clip.color_brightness,
        color_saturation=clip.color_saturation,
        normalized_audio=norm if os.path.exists(norm) else None,
    )


def _spec_grade(spec: RenderSpec) -> dict[str, int]:
    """The colour grade a finished render gave the clip file (``Clip.color_rendered``)."""
    return {
        "contrast": spec.color_contrast,
        "brightness": spec.color_brightness,
        "saturation": spec.color_saturation,
    }


def _render_parts(
    db: Session,
    job: Job,
    video: Video,
    preset: PlatformPreset,
    parts: list[Candidate],
    words: list[dict],
    caption_cfg: dict,
    settings: Settings,
    *,
    progress: tuple[int, int],
    transcribed: bool = True,
) -> None:
    """Create a clip row per part (sequential, needs the DB), then render clips
    in parallel and mark EACH one ready the moment it finishes, so the
    dashboard can show clips while the rest are still rendering.

    Rendering threads only run ffmpeg; every DB write happens on this thread,
    which keeps the SQLAlchemy session single-threaded. ``progress`` is the
    (start, end) job-progress range for this batch."""
    copy_ok = not preset.is_vertical and _stream_copy_ok(db, video)
    _check_cancel(db, job)

    specs: list[tuple[str, RenderSpec]] = []  # (clip_id, spec)
    for rank, part in enumerate(parts, start=1):
        title, description = _part_text(part, rank)
        clip = Clip(
            job_id=job.id,
            platform=preset.key,
            start_s=part.start,
            end_s=part.end,
            # Without a transcript we can't tell whether a part has speech.
            extra={"has_speech": has_words(part.text or "") if transcribed else None},
            title=title,
            description=description,
            hashtags=[],
            captions_enabled=bool(caption_cfg.get("enabled", True)),
            language=caption_cfg.get("language"),
            rank=rank,
            status=ClipStatus.rendering,
            duration_s=round(part.end - part.start, 3),
        )
        db.add(clip)
        db.commit()

        spec = _render_spec(clip, video, preset, words, settings, stream_copy_ok=copy_ok)
        specs.append((clip.id, spec))

    # Copy cuts are mostly single-threaded audio work, so many can run at once;
    # full encodes already use every core, so more parallelism barely helps.
    limit = settings.copy_concurrency if copy_ok else settings.render_concurrency
    _render_specs(db, job, specs, limit, progress)


def _render_specs(
    db: Session,
    job: Job,
    specs: list[tuple[str, RenderSpec]],
    limit: int,
    progress: tuple[int, int],
) -> None:
    """Render ``specs`` (clip_id, spec) with up to ``limit`` at once and mark
    each clip ready (or failed) the moment it finishes."""
    workers = max(1, min(limit, len(specs)))
    lo, hi = progress
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(render_clip, spec): (cid, spec) for cid, spec in specs}
        for future in as_completed(futures):
            clip_id, spec = futures[future]
            clip = db.get(Clip, clip_id)
            try:
                future.result()
                clip.file_path = spec.out_path
                clip.thumbnail_path = spec.thumb_path
                clip.extra = {**(clip.extra or {}), "color_rendered": _spec_grade(spec)}
                clip.status = ClipStatus.ready
            except Exception as exc:
                log.exception("clip_render_failed", extra={"clip_id": clip_id})
                clip.status = ClipStatus.failed
                clip.error = str(exc)[:2000]
            done += 1
            job.progress = max(job.progress, lo + int((hi - lo) * done / len(specs)))
            db.commit()
            if _cancel_requested(db, job.id):
                # Drop the clips not started yet; let the running ones finish
                # (seconds) so no ffmpeg keeps writing into deleted folders.
                pool.shutdown(wait=True, cancel_futures=True)
                raise JobCancelled


# ---------------------------------------------------------------------------
# Re-render a single clip after edits
# ---------------------------------------------------------------------------
@celery_app.task(name="render_single_clip")
def render_single_clip(clip_id: str, keep_approved: bool = False) -> None:
    with SessionLocal() as db:
        clip = db.get(Clip, clip_id)
        if clip is None or clip.status == ClipStatus.deleted:
            return
        bind_job_id(clip.job_id)
        job = db.get(Job, clip.job_id)
        video = db.get(Video, job.video_id)
        # A clip made from a trim keeps the words of its own seconds; the
        # others use the video's transcript. A dubbed copy brings its own
        # voice and captions instead (services/dub.py).
        dubbed = clip.dub
        words = [] if dubbed else (clip.extra or {}).get("words")
        if words is None:
            transcript = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
            words = transcript.words if transcript else None
        preset = get_preset(clip.platform)
        settings = get_settings()

        clip.status = ClipStatus.rendering
        clip.error = None
        db.commit()

        copy_ok = not preset.is_vertical and _stream_copy_ok(db, video)
        spec = _render_spec(clip, video, preset, words or [], settings, stream_copy_ok=copy_ok)
        track = db.get(AudioTrack, clip.bg_audio_id) if clip.bg_audio_id else None
        try:
            # Locked so a music change can't touch the files mid-render; the
            # clip's background music (if any) is mixed in again afterwards.
            with bgaudio.clip_lock(spec.out_path):
                render_clip(spec)
                if dubbed:
                    dub.apply(dubbed, spec.out_path, clip.start_s, clip.end_s - clip.start_s)
                bgaudio.after_render(
                    spec.out_path,
                    track.path if track else None,
                    clip.bg_audio_volume,
                    mute_original=clip.bg_audio_mute_original,
                )
            clip.file_path = spec.out_path
            clip.thumbnail_path = spec.thumb_path
            clip.duration_s = round(clip.end_s - clip.start_s, 3)
            # Start/end may have been edited: does the clip still have speech?
            # Never transcribed: can't tell.
            spoken = " ".join(
                w["word"]
                for w in words or []
                if w["start"] < clip.end_s and w["end"] > clip.start_s
            )
            clip.extra = {
                **(clip.extra or {}),
                "has_speech": (clip.has_speech if dubbed or words is None else has_words(spoken)),
                # What this render used: the sliders may have moved meanwhile.
                "color_rendered": _spec_grade(spec),
            }
            clip.status = ClipStatus.approved if keep_approved else ClipStatus.ready
        except Exception as exc:
            clip.status = ClipStatus.failed
            clip.error = str(exc)[:2000]
            log.exception("clip_rerender_failed", extra={"clip_id": clip.id})
        db.commit()
        bind_job_id(None)
