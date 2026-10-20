from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger
from app.services import ffmpeg

log = get_logger(__name__)

# OpenAI transcription API accepts files up to 25 MB. Keep chunks well under that.
CHUNK_LEN_S = 600.0  # 10 minutes
CHUNK_OVERLAP_S = 5.0
_DEDUP_TOL_S = 0.1


@dataclass
class Word:
    word: str
    start: float
    end: float

    def shifted(self, offset: float) -> Word:
        return Word(self.word, self.start + offset, self.end + offset)

    def as_dict(self) -> dict:
        return {"word": self.word, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass
class TranscriptResult:
    text: str
    words: list[Word]
    language: str | None
    duration_s: float
    provider: str
    segments: list[dict] = field(default_factory=list)

    @property
    def audio_minutes(self) -> float:
        return self.duration_s / 60.0


# Called with the fraction of audio transcribed so far (0.0-1.0).
ProgressFn = Callable[[float], None]


class Transcriber(ABC):
    @abstractmethod
    def transcribe(
        self,
        audio_path: str | Path,
        language: str | None = None,
        on_progress: ProgressFn | None = None,
        cache_dir: str | Path | None = None,
    ) -> TranscriptResult:
        """``cache_dir``: where finished pieces are kept, so a run that is
        interrupted (worker restart, crash) resumes instead of starting over."""


def _merge_chunks(chunks: list[tuple[float, list[Word]]]) -> list[Word]:
    """Shift each chunk's words by its source offset and de-duplicate the
    overlap region. ``chunks`` is a list of (offset_s, words)."""
    merged: list[Word] = []
    cursor = -1.0  # source time already covered
    for offset, words in chunks:
        for w in words:
            sw = w.shifted(offset)
            if sw.start < cursor - _DEDUP_TOL_S:
                continue  # duplicate from the overlap with the previous chunk
            merged.append(sw)
            cursor = max(cursor, sw.end)
    return merged


def _sentences_from_words(
    words: list[Word],
    *,
    max_gap_s: float = 0.6,
    max_dur_s: float = 8.0,
    max_words: int = 24,
) -> list[dict]:
    """Group words into sentence-like segments.

    OpenAI's word-level timestamps carry no punctuation, so splitting on
    terminal punctuation alone collapses everything into one giant segment.
    We therefore also break on a pause between words (``max_gap_s``), on a
    maximum segment duration, and on a maximum word count. Terminal
    punctuation, when present (e.g. local whisper), still forces a break.
    """

    def _flush(buf: list[Word]) -> dict:
        return {
            "text": "".join(x.word for x in buf).strip(),
            "start": round(buf[0].start, 3),
            "end": round(buf[-1].end, 3),
        }

    out: list[dict] = []
    buf: list[Word] = []
    for w in words:
        if buf:
            gap = w.start - buf[-1].end
            dur = buf[-1].end - buf[0].start
            if gap > max_gap_s or dur >= max_dur_s or len(buf) >= max_words:
                out.append(_flush(buf))
                buf = []
        buf.append(w)
        if w.word.strip().endswith((".", "!", "?", "…")):
            out.append(_flush(buf))
            buf = []
    if buf:
        out.append(_flush(buf))
    return out


class OpenAITranscriber(Transcriber):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.settings.openai_api_key)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _transcribe_chunk(self, client, chunk_path: str, language: str | None) -> list[Word]:
        with open(chunk_path, "rb") as fh:
            resp = client.audio.transcriptions.create(
                model=self.settings.openai_transcribe_model,
                file=fh,
                response_format="verbose_json",
                timestamp_granularities=["word"],
                language=language or None,
            )
        words_raw = getattr(resp, "words", None) or []
        return [
            Word(word=_leading_space(w), start=float(w["start"]), end=float(w["end"]))
            for w in (_as_dict(x) for x in words_raw)
        ]

    def _chunk_cache_file(self, cache_dir: Path, idx: int, offset: float) -> Path:
        # Model and chunk geometry are in the name: a cached piece is only
        # reused for exactly the same request.
        model = self.settings.openai_transcribe_model.replace("/", "_")
        return cache_dir / f"{model}-{CHUNK_LEN_S:.0f}s-{idx:03d}-{offset:.1f}.json"

    def transcribe(
        self,
        audio_path: str | Path,
        language: str | None = None,
        on_progress: ProgressFn | None = None,
        cache_dir: str | Path | None = None,
    ) -> TranscriptResult:
        audio_path = str(audio_path)
        info = ffmpeg.ffprobe_metadata(audio_path)
        duration = info.duration_s
        client = self._client()
        cache = Path(cache_dir) if cache_dir else None
        if cache:
            cache.mkdir(parents=True, exist_ok=True)

        step = CHUNK_LEN_S - CHUNK_OVERLAP_S
        results: dict[int, tuple[float, list[Word]]] = {}
        with tempfile.TemporaryDirectory() as tmp:
            # 1) Slice the chunks up front. The input is already a compact MP3,
            # so stream copy is enough — no re-encode per chunk. Chunks an
            # earlier, interrupted run already paid for are loaded instead.
            jobs: list[tuple[int, float, str]] = []
            offset = 0.0
            idx = 0
            while offset < duration:
                cached = _load_chunk(self._chunk_cache_file(cache, idx, offset)) if cache else None
                if cached is not None:
                    results[idx] = (offset, cached)
                else:
                    chunk_path = str(Path(tmp) / f"chunk_{idx}.mp3")
                    ffmpeg.run(
                        ffmpeg.build_audio_copy_chunk_cmd(
                            audio_path, chunk_path, offset, CHUNK_LEN_S
                        )
                    )
                    jobs.append((idx, offset, chunk_path))
                offset += step
                idx += 1
            total = idx
            if results:
                log.info("transcribe_resume", extra={"cached_chunks": len(results), "of": total})
                if on_progress:
                    on_progress(len(results) / total)

            # 2) Transcribe chunks concurrently (network I/O bound). Results are
            # collected on this thread, so on_progress may safely touch the DB.
            workers = max(1, min(self.settings.transcribe_concurrency, len(jobs) or 1))

            def _work(job: tuple[int, float, str]) -> tuple[int, float, list[Word]]:
                j_idx, j_off, j_path = job
                log.info("transcribe_chunk", extra={"idx": j_idx, "offset": round(j_off, 1)})
                return j_idx, j_off, self._transcribe_chunk(client, j_path, language)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_work, job) for job in jobs]
                for future in as_completed(futures):
                    j_idx, j_off, words = future.result()
                    results[j_idx] = (j_off, words)
                    if cache:  # saved at once: a restart now loses only chunks in flight
                        _save_chunk(self._chunk_cache_file(cache, j_idx, j_off), words)
                    if on_progress:
                        on_progress(len(results) / total)

            # Preserve chunk order for correct overlap de-duplication.
            chunks = [results[i] for i in sorted(results)]

        merged = _merge_chunks(chunks)
        text = "".join(w.word for w in merged).strip()
        return TranscriptResult(
            text=text,
            words=merged,
            language=language,
            duration_s=duration,
            provider=f"openai:{self.settings.openai_transcribe_model}",
            segments=_sentences_from_words(merged),
        )


class LocalWhisperTranscriber(Transcriber):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _detect_language(self, model, audio_path: str) -> str | None:
        """Pick the spoken language by listening to up to 5 places across the audio.

        faster-whisper's own detection uses only the first 30 seconds, then forces
        that language on the whole file. A film that opens with music, a logo
        jingle or one English word then gets transcribed in the wrong language
        throughout. Voting across the file avoids that."""
        import numpy as np

        duration = ffmpeg.ffprobe_metadata(audio_path).duration_s
        windows = max(1, min(5, int(duration // 120)))
        totals: dict[str, float] = {}
        for k in range(windows):
            centre = duration * (k + 0.5) / windows
            start = max(0.0, min(centre - 15, duration - 30))
            raw = ffmpeg.run_capture(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", "30", "-i", audio_path,
                    "-f", "f32le", "-ac", "1", "-ar", "16000", "-",
                ]
            )  # fmt: skip
            samples = np.frombuffer(raw, dtype=np.float32)
            if samples.size < 16000:  # under a second of audio
                continue
            _lang, _prob, all_probs = model.detect_language(audio=samples)
            for code, prob in all_probs:
                totals[code] = totals.get(code, 0.0) + float(prob)
        if not totals:
            return None
        best = max(totals, key=totals.get)
        log.info("whisper_language_vote", extra={"language": best, "windows": windows})
        return best

    def transcribe(
        self,
        audio_path: str | Path,
        language: str | None = None,
        on_progress: ProgressFn | None = None,
        cache_dir: str | Path | None = None,  # unused: local runs cost nothing to redo
    ) -> TranscriptResult:
        from faster_whisper import WhisperModel

        device = self.settings.local_whisper_device
        if device == "auto":
            device = "cpu"
        model = WhisperModel(
            self.settings.local_whisper_model,
            device=device,
            compute_type=self.settings.local_whisper_compute_type,
            cpu_threads=os.cpu_count() or 4,  # default is only 4 threads
        )
        language = (
            language
            or self.settings.local_whisper_language
            or self._detect_language(model, str(audio_path))
        )
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            beam_size=self.settings.local_whisper_beam_size,
            vad_filter=self.settings.local_whisper_vad,
        )
        total = float(getattr(info, "duration", 0.0)) or 0.0
        words: list[Word] = []
        segments: list[dict] = []
        for seg in segments_iter:
            seg_words = getattr(seg, "words", None) or []
            for w in seg_words:
                words.append(Word(word=w.word, start=float(w.start), end=float(w.end)))
            segments.append(
                {"text": seg.text.strip(), "start": round(seg.start, 3), "end": round(seg.end, 3)}
            )
            if on_progress and total:
                on_progress(min(1.0, seg.end / total))
        text = "".join(w.word for w in words).strip() or " ".join(s["text"] for s in segments)
        return TranscriptResult(
            text=text,
            words=words,
            language=getattr(info, "language", language),
            duration_s=float(getattr(info, "duration", 0.0)),
            provider=f"local:{self.settings.local_whisper_model}",
            # Same sentence grouping as the OpenAI path, so clip boundaries behave
            # alike on both backends (Whisper's own segments can run 20-30s).
            segments=_sentences_from_words(words) or segments,
        )


def _load_chunk(path: Path) -> list[Word] | None:
    try:
        data = json.loads(path.read_text())
        return [Word(w["word"], float(w["start"]), float(w["end"])) for w in data["words"]]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("transcribe_chunk_cache_unreadable", extra={"path": str(path)})
        return None


def _save_chunk(path: Path, words: list[Word]) -> None:
    # Write then rename, so a crash mid-write never leaves a half file that
    # would later be trusted.
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps({"words": [w.as_dict() for w in words]}))
    tmp.replace(path)


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    return {"word": obj.word, "start": obj.start, "end": obj.end}


def _leading_space(w: dict) -> str:
    """OpenAI word tokens have no leading space; add one so joins read naturally."""
    word = w["word"]
    return word if word.startswith(" ") else " " + word


def get_transcriber(settings: Settings | None = None) -> Transcriber:
    settings = settings or get_settings()
    if settings.transcribe_is_local:
        return LocalWhisperTranscriber(settings)
    return OpenAITranscriber(settings)
