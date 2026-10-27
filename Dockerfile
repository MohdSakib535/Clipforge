# Shared image for the API and the Celery worker.
# Deno: the JavaScript runtime yt-dlp needs to download from YouTube.
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps:
#   ffmpeg          - cutting / reframing / captions / encoding
#   libgl1, libglib - OpenCV / MediaPipe runtime
#   build-essential - some wheels need a compiler at install time
#   fonts-noto-core - reel captions (Devanagari and Latin) burned into the video
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        build-essential \
        curl \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

# INSTALL_DEV=true (default) also installs test/lint tools (pytest, ruff, black)
# so the suite runs inside the container. Set to "false" for a lean prod image.
ARG INSTALL_DEV=true

COPY requirements.txt requirements-dev.txt requirements-tts.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt && \
    pip install --no-deps -r requirements-tts.txt && \
    if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

COPY . .

# Non-root user; owns the storage mount.
RUN useradd -m appuser && mkdir -p /data/storage && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# Overridden per-service in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
