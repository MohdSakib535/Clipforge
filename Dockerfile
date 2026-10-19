# Shared image for the API and the Celery worker.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps:
#   ffmpeg          - cutting / reframing / captions / encoding
#   libgl1, libglib - OpenCV / MediaPipe runtime
#   libsndfile1     - librosa audio IO
#   build-essential - some wheels need a compiler at install time
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# INSTALL_DEV=true (default) also installs test/lint tools (pytest, ruff, black)
# so the suite runs inside the container. Set to "false" for a lean prod image.
ARG INSTALL_DEV=true

COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt && \
    if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

COPY . .

# Non-root user; owns the storage mount.
RUN useradd -m appuser && mkdir -p /data/storage && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# Overridden per-service in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
