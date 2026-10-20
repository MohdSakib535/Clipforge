# ClipForge

Turn one long video (film, podcast, lecture, interview…) into ready-to-post
clips for YouTube Shorts, YouTube long-form, Facebook Reels and Facebook video.

ClipForge transcribes the video, cuts the whole of it into clips of the length
you choose (always on a sentence boundary, never mid-word), reframes 16:9 → 9:16
with face tracking, writes captions to a separate `.srt` file (never burned into
the video), normalises audio and renders platform-correct files, each titled
"Part N - <its first words>". No LLM is used. **Phase 1** (this repo) does everything up to export; direct
publishing to YouTube/Facebook is designed-for but not built (see
[Phase 2](#phase-2)).

---

## How it works

```
Upload ─▶ Rights ─▶ Platforms & settings ─▶ Processing ─▶ Review ─▶ Export
 or link    │              │                     │           │         │
  │       stored      config-driven         Celery       trim /     single
chunked    (user,     presets (YAML)        pipeline     approve /   file or
resumable  video,                           (below)      re-render   ZIP + .txt/.json
 upload    timestamp)
```

### Import from a link

Instead of uploading a file, paste a link (YouTube, Vimeo, Facebook, Instagram,
X, a direct `.mp4`, and the other sites [yt-dlp](https://github.com/yt-dlp/yt-dlp)
supports):

1. **Check link** lists the qualities the video offers (e.g. Best, 1080p, 720p…)
   with approximate sizes. "720p" means the frame's short side, so it works the
   same for vertical (Shorts) videos.
2. **Download** returns at once. The download runs on its own Celery queue
   (`downloads`) served by the `downloader` worker, so it never waits behind clip
   processing and never holds it up. Progress (%) shows on the Rights step, and
   you can confirm rights while it downloads.
3. When it finishes, the file is probed and marked ready, exactly like an upload.

At equal resolution H.264 + AAC is preferred, so horizontal clips can be cut
without re-encoding. Playlists and live streams are rejected; the upload size
limit also applies to downloads. Only import videos you own or are licensed to
use. The rights confirmation still applies.

YouTube needs a JavaScript runtime: the Docker image includes
[Deno](https://deno.com) and `yt-dlp[default]` pulls in the `yt-dlp-ejs` solver.
Sites change often: if a site stops working, bump `yt-dlp` in
`requirements.txt` and rebuild.

### Processing pipeline (Celery)

| Stage | What happens |
|---|---|
| **ingest** | `ffprobe` metadata (duration, resolution, fps, audio tracks, codec) — about a second, even for a 3-hour file |
| **transcribe** | Extract audio once → slice ~10-min chunks without re-encoding → transcribe chunks in parallel with word-level timestamps (retry/backoff) → shift & merge timestamps, de-duplicating the overlap. Progress updates per chunk. Transcript is cached per video. |
| **analyze** | Scene cuts from **keyframes only** (ffmpeg `-skip_frame nokey`; ~18s for a 104-min 1080p film), run in parallel with transcription and cached per video. Also, once per video and in parallel: the audio is loudness-normalised for horizontal clips (see below). |
| **plan clips** | Tile the whole video into contiguous ~N-second parts (N per platform), cut at sentence ends (snapped to nearby scene cuts), never mid-word; no gaps, no overlap |
| **titles** | Each clip: title `Part N - <first 9 words of its transcript>`, description = its transcript (Hindi in Devanagari is written in Roman letters), no hashtags; parts without speech are marked "No speech" |
| **render** | See below. Each clip is marked ready the moment it finishes, so you can start reviewing while the rest render. |
| **finalize** | Mark complete; progress is polled (or streamed over WebSocket) |

### How clips are rendered

| | Vertical (Shorts, Reels) | Horizontal (YouTube clip, Facebook video) |
|---|---|---|
| Frame | Face-tracked 9:16 crop | Full frame, original aspect ratio (no crop) |
| Captions | Separate `.srt` file (never burned in) | Separate `.srt` file (never burned in) |
| Video | Re-encoded (H.264) | **Cut without re-encoding** when the source is 8-bit H.264; otherwise re-encoded |
| Audio | First track, stereo, loudnorm, AAC | From the video's audio normalised **once** (cached), each clip's slice given one measured gain to -14 LUFS + a peak limiter |

Cutting without re-encoding takes ~2s per clip instead of a full encode
(82 clips from a 104-min film: ~12 min → about a minute). The trade-off: a
copied clip has to start on a keyframe, so it begins up to a couple of seconds
before the chosen start. The source's own subtitle tracks and chapters are
never copied into clips.

**Audio for horizontal clips.** Running `loudnorm` per clip was ~95% of a
copy cut's time (~17-28 s per 10-min part). Instead the video's first audio
track is loudness-normalised once (`videos/<id>/audio_norm.m4a`, made in the
background while transcription runs) and each clip takes its slice, measures
it (`ebur128`, ~0.5 s) and gets one gain to -14 LUFS plus a limiter at -3 dBFS.
Measured on a 104-min film cut into 11 x 10-min parts: rendering went from
114 s to ~10 s and, with the transcript and scene cuts cached, a whole re-run
from 133 s to ~27 s. Loudness -14.3..-15.8 LUFS with true peaks -1.3..-2.1 dBTP
(per-clip loudnorm gave -14.8..-16.3 LUFS, -0.6..-0.8 dBTP); A/V sync is
tested frame-accurate (`tests/test_fast_render.py`). Graded clips, vertical
clips and videos whose picture can't be copied still use per-clip loudnorm.

**Keep the Mac awake while a job runs.** Docker pauses when macOS sleeps (on
battery the default can be 1 minute idle), and the job stops until you wake
it. Plug in the charger and allow "Prevent automatic sleeping when the display
is off", or run `caffeinate -i` in a terminal during long jobs.

### Background music (review screen)

Upload an audio file (MP3, M4A, AAC, WAV, OGG, Opus, FLAC; up to 200 MB), or
paste a link (YouTube, SoundCloud, …) and *Get audio* to grab only its audio
(downloaded in the background on the `downloader` worker). Either way it joins
your music library, converted to stereo AAC at the same loudness as the clips'
speech (-14 LUFS), so **volume** means the same for every track: 100% = as
loud as the speech, 40% (default) ≈ 8 dB below it.

- **All clips at once:** pick a track and volume, then *Add to all clips*
  (or *Remove from all*). Clips still rendering are skipped.
- **One by one:** each clip card has a *Background music* checkbox (tick to
  add, untick to remove) with its own track and volume (*Apply* appears when
  you change them).
- **Mute original audio (music only):** tick this (in the panel for all clips,
  or on a clip) to drop the clip's own sound so only the music plays — for
  b-roll or montages. Leave it off to mix music *under* the speech. Turn the
  volume up for music-only clips, since there's no speech for it to sit under.

The music loops if it is shorter than the clip, fades in/out, and a limiter
stops speech + music from clipping. Only the audio is redone (the video is
copied), so mixing takes a second or two per clip. It runs on its own `audio`
queue (`mixer` worker), so it never waits behind a long render. The clip as
rendered is kept next to it while music is on, so changing the volume or
removing the music never needs a re-render, and *Save & re-render* puts the
clip's music back automatically. Downloads and the ZIP include the music.
Preview/download URLs carry the file's version (`file_version`, its mtime), so
the browser always plays the current file, never a cached copy.

### Colour adjustment (review screen)

Three gentle sliders — **Contrast**, **Brightness**, **Saturation** (each
0 = no change, range -50..50, mapped to a subtle ffmpeg `eq` grade), the same
way background music works:

- **All clips at once:** the *Colour adjustment — all clips* panel at the top;
  set the sliders and click *Apply to all clips* (or *Reset all*).
- **One by one:** each clip card has its own *Colour adjustment* panel.

A colour change edits pixels, so unlike music it **re-renders** each clip.
Horizontal clips are still cut on the same keyframe and only that cut is
re-encoded, so a graded clip keeps the frame size, start and length it had
without a grade (no jump to 1080p, nothing trimmed). The clip's background
music is re-applied automatically after the re-render, so colour and music
stack, and an approved clip stays approved. Defaults are 0, so new clips render
with no grade and ungraded clips are still cut without re-encoding.

Only *Apply colour* / *Save & re-render* change the colour; *Save* keeps text
and trim edits. Each clip records the grade its file was rendered with
(`color_rendered`), so sliders that were changed but not applied show
*not applied* (also after a reload), and *Apply to all clips* re-renders every
clip whose file doesn't have that grade yet.

Re-renders (colour, trim) run on their own `renders` queue (`renderer` worker,
two at a time), so a colour change on dozens of clips never blocks new jobs.

### How clips are chosen

For each platform you pick a target length (e.g. 40s); the whole video is tiled
end-to-end into contiguous ~40s clips (snapped to sentence/scene boundaries, no
gaps or overlap), and the clip count is derived automatically and shown in the
UI. Every part becomes a clip, in order. Titles and descriptions come from each
part's transcript and can be edited on the review screen.

---

## Tech stack

- **Backend:** Python 3.11, FastAPI, Pydantic v2
- **Jobs:** Celery + Redis (video work never blocks the API)
- **DB:** PostgreSQL + SQLAlchemy 2.0 + Alembic
- **Storage:** local disk in dev, behind a `StorageService` interface (swap in S3)
- **Transcription:** OpenAI `whisper-1` (default) or local `faster-whisper` — behind a common `Transcriber` interface, chosen with `TRANSCRIBER=openai|local`
- **Analysis:** FFmpeg scene scores on keyframes
- **Video:** FFmpeg; MediaPipe for face-tracked reframing; `.srt` sidecar captions
- **Import from a link:** yt-dlp (+ Deno for YouTube), on its own Celery queue
- **Background music:** FFmpeg audio-only mix (video stream-copied), on its own Celery queue
- **Colour adjustment:** per-clip FFmpeg `eq` grade, applied on re-render
- **UI:** single-page dashboard served by the API (Tailwind + Alpine via CDN — no build step)
- **Containers:** Docker Compose (api, worker, downloader, mixer, renderer, flower, redis, postgres)

---

## Quick start (Docker)

```bash
cp .env.example .env            # add your OPENAI_API_KEY
docker compose up --build
```

- **Dashboard:** http://localhost:8080/
- **API docs:** http://localhost:8080/docs
- **Celery monitor (Flower):** http://localhost:5555/

Services and host ports (chosen to avoid clashing with local installs):

| Service | Host | Container |
|---|---|---|
| API + UI | 8080 | 8000 |
| Flower (Celery UI) | 5555 | 5555 |
| Postgres | 5433 | 5432 |
| Redis | 6380 | 6379 |

The `api` container runs `alembic upgrade head` on start, then serves the API
and the dashboard. The `worker` container runs one Celery worker at
concurrency 1 (laptop mode) and **auto-restarts on code changes** (via
`watchmedo`), so edits to `app/` take effect without a manual restart. The
`downloader` container runs a second worker that only serves the `downloads`
queue (link imports, two at a time), `mixer` serves the `audio` queue
(background music), and `renderer` serves the `renders` queue (re-rendering
edited clips, two at a time), so downloads, music, re-renders and clip jobs
never wait for each other. `flower` provides a live view of tasks, workers and queues.

### Try it with a sample video

```bash
# Generate a 5-minute sample inside the api container (it has ffmpeg):
docker compose exec api scripts/make_sample.sh /data/storage/sample.mp4 300
```

Then open the dashboard and upload `storage/sample.mp4` (the `storage` volume is
shared), **or** drive the API directly:

```bash
# single-shot upload
curl -F "file=@storage/sample.mp4;type=video/mp4" http://localhost:8080/videos/upload
```

---

## Laptop mode (Postgres + Redis in Docker, Python native)

Recommended for fast iteration on a 16 GB laptop with no GPU.

```bash
# 1) Only the infra in Docker
docker compose up -d postgres redis

# 2) Native Python (3.11 recommended)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # full stack incl. ML libs
#   (or: uv venv --python 3.11 && uv pip install -r requirements-dev.txt)

# 3) Point at the Docker infra via mapped ports (defaults already do this)
export DATABASE_URL="postgresql+psycopg://clipforge:clipforge@localhost:5433/clipforge"
export REDIS_URL="redis://localhost:6380/0"
export OPENAI_API_KEY=sk-...

# 4) Migrate, then run API + worker + downloader + mixer + renderer in five terminals
alembic upgrade head
uvicorn app.main:app --reload --port 8000
celery -A app.tasks.celery_app:celery_app worker --loglevel=info --concurrency=1
celery -A app.tasks.celery_app:celery_app worker -Q downloads -n downloader@%h --concurrency=2
celery -A app.tasks.celery_app:celery_app worker -Q audio -n mixer@%h --concurrency=1
celery -A app.tasks.celery_app:celery_app worker -Q renders -n renderer@%h --concurrency=2
```

Imports from YouTube also need Deno natively (`brew install deno`).

You need **ffmpeg** installed natively (`brew install ffmpeg`). MediaPipe wheels
require Python ≤ 3.12. To avoid OpenAI usage while iterating, set
`TRANSCRIBER=local` - see below.

---

## Transcription: OpenAI or local

Transcription (speech -> text with word timings) is the only model ClipForge
uses; there is no LLM. Choose it in `.env`:

| `TRANSCRIBER=` | Model | Where it runs | 104-min film | Cost |
|---|---|---|---|---|
| `openai` (default) | `whisper-1` | OpenAI | a few minutes | ~$0.006 per audio minute (~$0.62) |
| `local` | `faster-whisper` `large-v3-turbo` (~1.6 GB) | worker container, CPU, int8 | ~48 min | free, no API key needed |

The local model ([faster-whisper](https://github.com/SYSTRAN/faster-whisper),
`mobiuslabsgmbh/faster-whisper-large-v3-turbo`) downloads from Hugging Face on the
first local job and is cached in the storage volume (`/data/storage/.hf-cache`).
Other sizes also work with `LOCAL_WHISPER_MODEL` (e.g. `small`, `medium`,
`large-v3`): smaller is faster but makes more mistakes on Hindi. Optional:
`LOCAL_WHISPER_LANGUAGE=hi` skips language detection.

Everything else is always local: FFmpeg (cutting, encoding, scene detection,
loudness) and MediaPipe face detection (vertical reframing).

After changing `.env`: `docker compose up -d` (not `restart` - the containers read
`.env` only when they are created), then check `curl localhost:8080/health` - it
shows `"transcriber"` (`openai` or `local`). A bad value, or `openai` without
`OPENAI_API_KEY`, is refused with a clear message before a job starts.

**Never paid twice:** the same file uploaded again reuses its transcript (matched by a
fast fingerprint: size + three 4 MB samples). OpenAI transcription saves each
10-minute piece as it finishes, so a job interrupted by a worker restart only pays
for the pieces still missing.

---

## Environment variables

See [`.env.example`](.env.example). Key ones:

| Var | Purpose |
|---|---|
| `TRANSCRIBER` | `openai` (default) or `local` |
| `OPENAI_API_KEY` | OpenAI key, only for `TRANSCRIBER=openai` (never logged) |
| `OPENAI_TRANSCRIBE_MODEL` | OpenAI transcription model (`whisper-1`) |
| `LOCAL_WHISPER_MODEL`, `LOCAL_WHISPER_LANGUAGE`, `LOCAL_WHISPER_BEAM_SIZE` | local transcription: model (`large-v3-turbo`), fixed language (empty = detect), accuracy vs speed |
| `MAX_JOB_COST_USD` | hard per-job cost cap; the job stops with a clear message if exceeded |
| `DATABASE_URL`, `REDIS_URL`, `STORAGE_PATH` | infrastructure |
| `MAX_UPLOAD_BYTES` | upload limit |
| `RENDER_CONCURRENCY`, `COPY_CONCURRENCY`, `RENDER_PRESET` | render speed tuning (see `.env.example`) |
| `MAX_AUDIO_UPLOAD_BYTES` | background-music upload limit (default 200 MB) |
| `DOWNLOAD_TIMEOUT_S`, `URL_PROBE_TIMEOUT_S` | import from a link: max download time, max "check link" wait |
| `YTDLP_COOKIES_FILE` | optional cookies.txt for videos that need a login (or YouTube's "not a bot" check) |

**Cost tracking:** every job records audio-minutes transcribed and the model
used, and shows an estimated cost on the review screen.

---

## Platform presets

Presets live in [`app/config/platforms.yaml`](app/config/platforms.yaml) so
limits can change without code edits. The seconds per clip you choose must lie
in the preset's allowed range (default: 40 s vertical, 3 min horizontal).

| Preset | Aspect | Resolution | Allowed length |
|---|---|---|---|
| `youtube_shorts` | 9:16 | 1080×1920 | 5–60 s |
| `youtube_clip` | 16:9 | 1920×1080 | 1–10 min |
| `facebook_reel` | 9:16 | 1080×1920 | 5–90 s |
| `facebook_video` | 16:9 | 1920×1080 | 1–15 min |

---

## API (OpenAPI at `/docs`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/videos/upload` | single-shot upload |
| POST | `/videos/upload/init` → PUT `/videos/{id}/upload/chunk` → POST `/videos/{id}/upload/complete` | resumable chunked upload |
| POST | `/videos/from-url/qualities` | check a link: title, duration, available qualities |
| POST | `/videos/from-url` | import a link at a quality (`best`, `1080`, `720`…); returns 202, downloads in the background (poll `GET /videos/{id}` for `download_progress`) |
| POST | `/videos/{id}/confirm-rights` | mandatory rights confirmation |
| POST | `/videos/{id}/jobs` | start a job (`platforms`, `coverage_lengths` = seconds per clip per platform, `captions`) |
| GET | `/jobs/{id}` | status, stage, progress %, cost |
| GET | `/jobs/{id}/clips` | clips with metadata |
| POST | `/audio/upload` | upload a background-music track (multipart `file`) |
| POST | `/audio/from-url` | grab only the audio of a link as a track; returns 202, `status: processing` → `ready` (poll `GET /audio`) |
| GET | `/audio` / `/audio/{id}/file` | your music library / play a track |
| DELETE | `/audio/{id}` | delete a track (409 while a clip still uses it) |
| POST | `/jobs/{id}/color` | apply one colour grade (`contrast`/`brightness`/`saturation`, -50..50) to some/all clips; returns at once, clips re-render in the background |
| POST | `/jobs/{id}/background-audio` | `{audio_id, volume, clip_ids?}`: add (or with `audio_id: null` remove) music on some or all ready clips; returns at once, clips show `rendering` until mixed |
| PATCH | `/clips/{id}` | trim, edit title/description, caption style, colour grade (`color_contrast`/`color_brightness`/`color_saturation`, -50..50), approve/delete |
| POST | `/clips/{id}/render` | re-render after edits |
| GET | `/clips/{id}/download` / `/preview` / `/thumbnail` / `/captions.srt` | clip file / inline player / thumbnail / sidecar captions |
| GET | `/jobs/{id}/export.zip` | all approved/ready clips (`?only=approved`: approved only) + per-clip `.srt` and `.txt`/`.json` metadata |
| WS | `/ws/jobs/{id}` | live progress stream |

---

## Testing & quality

Everything runs inside the containers (the image includes pytest/ruff/black
when built with the default `INSTALL_DEV=true`):

```bash
# tests/ is not mounted into the api container, so mount the current copy:
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests" api pytest -q
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests" api ruff check app tests
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests" api black --check app tests
```

Tests cover segment building/snapping, preset loading, transcription chunking
and resume, transcript reuse, Hinglish transliteration, cost capping, caption generation, FFmpeg command generation, and the API
endpoints (with mocked Celery tasks and an in-memory DB). Logs are structured
JSON with `job_id` on every line.

---

## Project structure

```
app/
  api/        FastAPI routers (health, config, videos, jobs, clips) + errors
  config/     settings + platforms.yaml loader
  core/       JSON logging
  db/         SQLAlchemy base + session
  models/     User, Video, Job, Transcript, Clip
  schemas/    Pydantic request/response models
  services/   storage, ffmpeg, transcriber, cost, analysis, segments, romanize,
              filehash, naming, reframe, captions, render, downloader, bgaudio
  tasks/      Celery app, pipeline tasks, link downloads, music mixing, dispatch
  web/static/ single-page dashboard (index.html + app.js)
alembic/      migrations
tests/        pytest suite
scripts/      make_sample.sh
```

---

## Rules & non-goals

- **Rights confirmation is mandatory** — processing cannot start until the user
  confirms they own or are licensed to edit and publish the video (stored with
  user, video and timestamp).
- ClipForge does **not** include anything designed to evade copyright or
  content-matching systems, and does not upload to external platforms in Phase 1.
- Secrets stay in `.env`; the API key is never logged.

---

## Phase 2

Designed-for, not built: YouTube Data API v3 + Meta Graph API publishing via
OAuth, a scheduling queue, analytics pull-back, and multi-language subtitles.
The `StorageService` and `Transcriber` interfaces and the
platform-preset config are the seams intended to make these additions clean.
