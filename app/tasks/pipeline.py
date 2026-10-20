from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy.orm import Session

from app.config.platforms import PlatformPreset, get_preset
from app.config.settings import Settings, get_settings
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
from app.services import analysis, bgaudio, ffmpeg
from app.services.cost import CostCapExceeded, CostTracker
from app.services.llm import get_llm_client
from app.services.render import RenderSpec, render_clip
from app.services.scoring import HighlightScore, refine_metadata, score_candidates
from app.services.segments import (
    build_candidates,
    build_coverage_segments,
    select_non_overlapping,
)
from app.services.storage import get_storage
from app.services.transcriber import get_transcriber
from app.tasks.celery_app import celery_app

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _set_stage(db: Session, job: Job, stage: JobStage, progress: int) -> None:
    job.stage = stage
    job.progress = max(job.progress, progress)
    db.commit()
    log.info("job_stage", extra={"stage": stage.value, "progress": job.progress})


def _save_cost(db: Session, job: Job, cost: CostTracker) -> None:
    job.audio_minutes = round(cost.audio_minutes, 3)
    job.llm_tokens_in = cost.tokens_in
    job.llm_tokens_out = cost.tokens_out
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


def _length_window(job: Job, preset: PlatformPreset) -> tuple[float, float]:
    overrides = (job.settings or {}).get("length_overrides", {})
    ov = overrides.get(preset.key)
    if ov:
        lo = max(preset.length_min_s, int(ov["min_s"]))
        hi = min(preset.length_max_s, int(ov["max_s"]))
    else:
        lo, hi = preset.length_default_min_s, preset.length_default_max_s
    return float(lo), float(hi)


def _coverage_score(cand, rank: int) -> HighlightScore:
    """Fallback metadata for a coverage segment when the LLM gave none.
    Title = first few words of the segment; description = the segment text."""
    text = (cand.text or "").strip()
    snippet = " ".join(text.split()[:9])
    title = f"Part {rank}" + (f" - {snippet}" if snippet else "")
    return HighlightScore(
        index=rank,
        score=0,
        hook_strength=0,
        standalone=True,
        reason=f"Sequential coverage segment {rank}",
        suggested_title=title[:120],
        description=text[:800],
        hashtags=[],
    )


def _with_fallback(scored: HighlightScore, fallback: HighlightScore) -> HighlightScore:
    """Keep the LLM's score, but fill any text it left empty from the fallback."""
    return scored.model_copy(
        update={
            "suggested_title": scored.suggested_title or fallback.suggested_title,
            "description": scored.description or fallback.description,
            "reason": scored.reason or fallback.reason,
        }
    )


def _clip_count_for(job: Job, platform: str) -> int:
    """Per-platform clip count. Falls back to the job-wide ``clip_count``."""
    settings = job.settings or {}
    per_platform = settings.get("clip_counts", {}) or {}
    if platform in per_platform:
        return max(1, int(per_platform[platform]))
    return max(1, int(settings.get("clip_count", 5)))


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
def _transcribe(db: Session, video: Video, cost: CostTracker, on_progress=None) -> Transcript:
    existing = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
    if existing and existing.words:
        log.info("transcript_cache_hit", extra={"video_id": video.id})
        return existing

    audio_path = str(get_storage().path_for(f"videos/{video.id}/audio.mp3"))
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(audio_path):
        ffmpeg.run(
            ffmpeg.build_audio_chunk_cmd(
                _source_path(video), audio_path, 0.0, video.duration_s or 1e9
            )
        )

    settings = get_settings()
    transcriber = get_transcriber(settings)
    result = transcriber.transcribe(audio_path, language=None, on_progress=on_progress)
    cost.add_transcription(settings.openai_transcribe_model, result.audio_minutes)

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
    return row


# ---------------------------------------------------------------------------
# Full job (transcribe -> analyze -> segments -> score -> select -> render)
# ---------------------------------------------------------------------------
@celery_app.task(name="run_job")
def run_job(job_id: str) -> None:
    bind_job_id(job_id)
    settings = get_settings()
    storage = get_storage()

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            log.warning("run_job_missing", extra={"job_id": job_id})
            return
        if job.status == JobStatus.completed:
            return
        video = db.get(Video, job.video_id)

        cost = CostTracker(
            cap_usd=settings.max_job_cost_usd,
            audio_minutes=job.audio_minutes,
            tokens_in=job.llm_tokens_in,
            tokens_out=job.llm_tokens_out,
        )

        try:
            job.status = JobStatus.running
            job.error = None
            db.commit()

            # 1+2) Transcribe and detect scenes CONCURRENTLY: transcription waits
            # on the network, scene detection runs in an ffmpeg subprocess, so
            # they overlap almost for free.
            _set_stage(db, job, JobStage.transcribing, 5)
            audio_path = str(storage.path_for(f"videos/{video.id}/audio.mp3"))

            def _tx_progress(fraction: float) -> None:
                # Runs on this thread (the transcriber collects results here).
                job.progress = max(job.progress, 5 + int(20 * fraction))
                db.commit()

            with ThreadPoolExecutor(max_workers=1) as bg:
                scene_future = bg.submit(analysis.detect_scenes, _source_path(video))
                transcript = _transcribe(db, video, cost, on_progress=_tx_progress)
                _save_cost(db, job, cost)
                _set_stage(db, job, JobStage.analyzing, 25)
                scene_cuts = scene_future.result()

            energy_peaks = analysis.audio_energy_peaks(audio_path)
            result = analysis.AnalysisResult(scene_cuts=scene_cuts, energy_peaks=energy_peaks)

            sentences = transcript.segments or []
            words = transcript.words or []
            llm = get_llm_client(settings)

            platforms: list[str] = job.platforms or []
            caption_cfg = (job.settings or {}).get("captions", {})
            coverage_lengths = (job.settings or {}).get("coverage_lengths", {}) or {}

            total_platforms = max(1, len(platforms))
            for p_idx, platform in enumerate(platforms):
                preset = get_preset(platform)
                base = 35 + int(55 * p_idx / total_platforms)
                span = int(55 / total_platforms)
                render_range = (base + max(1, span // 2), base + span)

                _set_stage(db, job, JobStage.finding_highlights, base)

                if platform in coverage_lengths:
                    # --- Cover-whole-video mode: tile the entire video. ---
                    target = max(
                        preset.length_min_s,
                        min(preset.length_max_s, int(coverage_lengths[platform])),
                    )
                    covers = build_coverage_segments(
                        sentences,
                        result.scene_cuts,
                        target,
                        words=words,
                        energy_peaks=result.energy_peaks,
                    )
                    log.info(
                        "coverage_segments",
                        extra={"platform": platform, "count": len(covers), "target_s": target},
                    )
                    if not covers:
                        continue
                    fallback = {
                        id(c): _coverage_score(c, rank) for rank, c in enumerate(covers, start=1)
                    }
                    # Every segment becomes a clip, so scoring doesn't select
                    # anything — it tells the user which parts are strongest and
                    # writes titles/hashtags, using the cheap model in batches.
                    # Clips are still produced if scoring fails.
                    try:
                        pairs = score_candidates(
                            llm,
                            cost,
                            covers,
                            preset,
                            settings.openai_scoring_model,
                            concurrency=settings.scoring_concurrency,
                        )
                        score_by_id = {
                            id(c): _with_fallback(hs, fallback[id(c)]) for c, hs in pairs
                        }
                    except CostCapExceeded:
                        raise
                    except Exception:
                        log.exception("coverage_scoring_failed", extra={"platform": platform})
                        score_by_id = fallback
                    _save_cost(db, job, cost)
                    _set_stage(db, job, JobStage.rendering, render_range[0])
                    # No extra per-clip metadata call (gpt-4o) in coverage mode —
                    # there can be dozens of clips; the scoring call already
                    # wrote titles/descriptions/hashtags.
                    _render_selected(
                        db,
                        job,
                        video,
                        preset,
                        covers,
                        score_by_id,
                        words,
                        caption_cfg,
                        llm,
                        cost,
                        settings,
                        progress=render_range,
                        refine=False,
                    )
                    _save_cost(db, job, cost)
                    continue

                # --- Top-highlights mode: score candidates, pick top N. ---
                clip_count = _clip_count_for(job, platform)
                lo, hi = _length_window(job, preset)
                candidates = build_candidates(
                    sentences,
                    result.scene_cuts,
                    lo,
                    hi,
                    words=words,
                    energy_peaks=result.energy_peaks,
                    max_candidates=settings.max_candidates,
                )
                log.info("candidates", extra={"platform": platform, "count": len(candidates)})
                if not candidates:
                    continue

                pairs = score_candidates(
                    llm,
                    cost,
                    candidates,
                    preset,
                    settings.openai_scoring_model,
                    concurrency=settings.scoring_concurrency,
                )
                _save_cost(db, job, cost)
                scored = [(hs.score, cand) for cand, hs in pairs]
                score_by_id = {id(cand): hs for cand, hs in pairs}

                selected = select_non_overlapping(scored, clip_count)

                _set_stage(db, job, JobStage.rendering, render_range[0])
                _render_selected(
                    db,
                    job,
                    video,
                    preset,
                    selected,
                    score_by_id,
                    words,
                    caption_cfg,
                    llm,
                    cost,
                    settings,
                    progress=render_range,
                )
                _save_cost(db, job, cost)

            # 7) finalize
            _set_stage(db, job, JobStage.finalizing, 98)
            job.status = JobStatus.completed
            job.stage = JobStage.done
            job.progress = 100
            db.commit()
            log.info("job_complete", extra={"clips": len(job.clips)})

        except CostCapExceeded as exc:
            _save_cost(db, job, cost)
            job.status = JobStatus.failed
            job.error = str(exc)
            db.commit()
            log.warning("job_cost_capped", extra={"error": str(exc)})
        except Exception as exc:
            job.status = JobStatus.failed
            job.error = str(exc)[:2000]
            db.commit()
            log.exception("job_failed", extra={"job_id": job_id})
            raise
        finally:
            bind_job_id(None)


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
    )


def _render_selected(
    db,
    job,
    video,
    preset,
    selected,
    score_by_id,
    words,
    caption_cfg,
    llm,
    cost,
    settings,
    *,
    progress: tuple[int, int],
    refine: bool = True,
) -> None:
    """Create clip rows + metadata (sequential, needs the DB/LLM), then render
    clips in parallel and mark EACH one ready the moment it finishes, so the
    dashboard can show clips while the rest are still rendering.

    Rendering threads only run ffmpeg; every DB write happens on this thread,
    which keeps the SQLAlchemy session single-threaded. ``refine=False`` skips
    the per-clip LLM metadata call (coverage mode may create dozens of clips).
    ``progress`` is the (start, end) job-progress range for this batch."""
    copy_ok = not preset.is_vertical and _stream_copy_ok(db, video)

    specs: list[tuple[str, RenderSpec]] = []  # (clip_id, spec)
    for rank, cand in enumerate(selected, start=1):
        hs = score_by_id[id(cand)]
        clip = Clip(
            job_id=job.id,
            platform=preset.key,
            start_s=cand.start,
            end_s=cand.end,
            score=hs.score,
            hook_strength=hs.hook_strength,
            standalone=hs.standalone,
            reason=hs.reason,
            title=hs.suggested_title,
            description=hs.description,
            hashtags=hs.hashtags,
            captions_enabled=bool(caption_cfg.get("enabled", True)),
            language=caption_cfg.get("language"),
            rank=rank,
            status=ClipStatus.rendering,
            duration_s=round(cand.end - cand.start, 3),
        )
        db.add(clip)
        db.commit()

        # Optional stronger metadata for the final selected clip.
        if refine:
            try:
                meta = refine_metadata(llm, cost, cand.text, preset, settings.openai_metadata_model)
                if meta.title:
                    clip.title = meta.title
                if meta.description:
                    clip.description = meta.description
                if meta.hashtags:
                    clip.hashtags = meta.hashtags
                db.commit()
            except Exception:
                log.warning("metadata_refine_failed", extra={"clip_id": clip.id})

        spec = _render_spec(clip, video, preset, words, settings, stream_copy_ok=copy_ok)
        specs.append((clip.id, spec))

    # Copy cuts are mostly single-threaded audio work, so many can run at once;
    # full encodes already use every core, so more parallelism barely helps.
    limit = settings.copy_concurrency if copy_ok else settings.render_concurrency
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
                clip.status = ClipStatus.ready
            except Exception as exc:
                log.exception("clip_render_failed", extra={"clip_id": clip_id})
                clip.status = ClipStatus.failed
                clip.error = str(exc)[:2000]
            done += 1
            job.progress = max(job.progress, lo + int((hi - lo) * done / len(specs)))
            db.commit()


# ---------------------------------------------------------------------------
# Re-render a single clip after edits
# ---------------------------------------------------------------------------
@celery_app.task(name="render_single_clip")
def render_single_clip(clip_id: str, keep_approved: bool = False) -> None:
    with SessionLocal() as db:
        clip = db.get(Clip, clip_id)
        if clip is None:
            return
        bind_job_id(clip.job_id)
        job = db.get(Job, clip.job_id)
        video = db.get(Video, job.video_id)
        transcript = db.query(Transcript).filter(Transcript.video_id == video.id).one_or_none()
        words = transcript.words if transcript else []
        preset = get_preset(clip.platform)
        settings = get_settings()

        clip.status = ClipStatus.rendering
        clip.error = None
        db.commit()

        copy_ok = not preset.is_vertical and _stream_copy_ok(db, video)
        spec = _render_spec(clip, video, preset, words, settings, stream_copy_ok=copy_ok)
        track = db.get(AudioTrack, clip.bg_audio_id) if clip.bg_audio_id else None
        try:
            # Locked so a music change can't touch the files mid-render; the
            # clip's background music (if any) is mixed in again afterwards.
            with bgaudio.clip_lock(spec.out_path):
                render_clip(spec)
                bgaudio.after_render(
                    spec.out_path,
                    track.path if track else None,
                    clip.bg_audio_volume,
                    mute_original=clip.bg_audio_mute_original,
                )
            clip.file_path = spec.out_path
            clip.thumbnail_path = spec.thumb_path
            clip.duration_s = round(clip.end_s - clip.start_s, 3)
            clip.status = ClipStatus.approved if keep_approved else ClipStatus.ready
        except Exception as exc:
            clip.status = ClipStatus.failed
            clip.error = str(exc)[:2000]
            log.exception("clip_rerender_failed", extra={"clip_id": clip.id})
        db.commit()
        bind_job_id(None)
