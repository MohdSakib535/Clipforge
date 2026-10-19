#!/usr/bin/env bash
# Generate a short sample video for fast iteration.
# Usage: scripts/make_sample.sh [out.mp4] [seconds]
# Requires ffmpeg. If you don't have it locally, run inside the api container:
#   docker compose exec api scripts/make_sample.sh /data/storage/sample.mp4 300
set -euo pipefail

OUT="${1:-sample.mp4}"
SECONDS_LEN="${2:-300}"   # default 5 minutes

ffmpeg -y \
  -f lavfi -i "testsrc=duration=${SECONDS_LEN}:size=1280x720:rate=30" \
  -f lavfi -i "sine=frequency=440:duration=${SECONDS_LEN}" \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p \
  -c:a aac -shortest \
  "$OUT"

echo "Wrote ${OUT} (${SECONDS_LEN}s)"
