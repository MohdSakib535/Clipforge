# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "mflux==0.23.0",
#     "pillow<13",  # Pillow 13.0.0 breaks mflux 0.23 at import (PIL._typing)
# ]
# ///
"""ClipForge image generator: FLUX.2 [klein] 4B on the Mac's GPU (Apple MLX).

Docker on a Mac can't use the GPU, so this runs natively on the Mac, next to
Ollama, and the reel maker in Docker calls it at
http://host.docker.internal:8189. Start it in a terminal (Ctrl+C stops it):

    uv run imagegen/server.py

The model is read from IMAGEGEN_MODEL_PATH (a 4-bit copy, ~4.3 GB). If it is
not there yet, the first image downloads FLUX.2-klein-4B from Hugging Face
(~16 GB, once) and saves the 4-bit copy; the download in
~/.cache/huggingface can be deleted afterwards.

The model stays loaded between the pictures of a reel (~5 GB of RAM). The reel
maker unloads it when its pictures are done, and it is also unloaded after
IMAGEGEN_IDLE_UNLOAD_S seconds without work, so the Mac gets the RAM back. On a
16 GB Mac running Docker, holding it longer pushed the Mac into heavy swapping.

API:
    GET  /health    -> {"ok": true, "loaded": bool, "model_path": str}
    POST /generate  {"prompt", "width", "height", "seed", "steps"} -> image/png
    POST /unload    -> {"ok": true, "loaded": false}
"""

from __future__ import annotations

import gc
import io
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("IMAGEGEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMAGEGEN_PORT", "8189"))
MODEL_PATH = Path(
    os.environ.get("IMAGEGEN_MODEL_PATH", "~/.cache/clipforge/flux2-klein-4b-q4")
).expanduser()
IDLE_UNLOAD_S = int(os.environ.get("IMAGEGEN_IDLE_UNLOAD_S", "120"))
# MLX keeps freed GPU buffers for reuse; cap that cache (what mflux's
# --low-ram does) so it can't grow on top of the model.
CACHE_LIMIT_GB = float(os.environ.get("IMAGEGEN_CACHE_LIMIT_GB", "1"))

# FLUX works on 16-pixel blocks; keep requests to sizes a 16 GB Mac can do.
_MIN_SIDE, _MAX_SIDE = 256, 1536

log = logging.getLogger("imagegen")


class Generator:
    """One FLUX.2 model, one image at a time (the GPU is shared)."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._last_used = time.monotonic()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @staticmethod
    def _load():
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.flux2.variants import Flux2Klein

        config = ModelConfig.from_name("flux2-klein-4b")
        if MODEL_PATH.is_dir() and any(MODEL_PATH.iterdir()):
            return Flux2Klein(model_config=config, model_path=str(MODEL_PATH))
        log.info("no model at %s: downloading FLUX.2-klein-4B, saving a 4-bit copy", MODEL_PATH)
        model = Flux2Klein(model_config=config, quantize=4)
        model.save_model(str(MODEL_PATH))
        return model

    def generate(self, prompt: str, width: int, height: int, seed: int, steps: int) -> bytes:
        import mlx.core as mx

        with self._lock:
            # On battery the Mac sleeps after a minute idle and Docker pauses
            # mid-reel; 90 s from each picture also covers putting the video
            # together after the last one.
            subprocess.Popen(["caffeinate", "-i", "-t", "90"])
            if self._model is None:
                self._model = self._load()
            started = time.perf_counter()
            image = self._model.generate_image(
                seed=seed,
                prompt=prompt,
                width=width,
                height=height,
                guidance=1.0,
                num_inference_steps=steps,
            ).image
            mx.clear_cache()
            self._last_used = time.monotonic()
        log.info("%dx%d image in %.1fs", width, height, time.perf_counter() - started)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def unload(self, *, only_if_idle: bool = False) -> None:
        with self._lock:
            if self._model is None:
                return
            if only_if_idle and time.monotonic() - self._last_used < IDLE_UNLOAD_S:
                return
            self._model = None
            gc.collect()
            import mlx.core as mx

            mx.clear_cache()
        log.info("model unloaded%s", " (idle)" if only_if_idle else "")


GENERATOR = Generator()


def _side(value: object) -> int:
    """A width/height rounded down to a multiple of 16, within limits."""
    return max(_MIN_SIDE, min(_MAX_SIDE, int(value) // 16 * 16))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"ok": True, "loaded": GENERATOR.loaded, "model_path": str(MODEL_PATH)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/unload":
            GENERATOR.unload()
            self._json(200, {"ok": True, "loaded": False})
            return
        if self.path != "/generate":
            self._json(404, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            prompt = str(body["prompt"]).strip()
            if not prompt:
                raise ValueError("empty prompt")
            width = _side(body.get("width", 384))
            height = _side(body.get("height", 672))
            seed = int(body.get("seed", 0))
            steps = max(1, min(int(body.get("steps", 4)), 50))
        except (KeyError, TypeError, ValueError) as exc:  # JSONDecodeError is a ValueError
            self._json(400, {"error": f"bad request: {exc}"})
            return
        try:
            png = GENERATOR.generate(prompt, width, height, seed, steps)
        except Exception as exc:
            log.exception("generation failed")
            self._json(500, {"error": f"generation failed: {exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.end_headers()
        self.wfile.write(png)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def _unload_when_idle() -> None:
    while True:
        time.sleep(30)
        GENERATOR.unload(only_if_idle=True)


def main() -> None:
    import mlx.core as mx

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mx.set_cache_limit(int(CACHE_LIMIT_GB * 1024**3))
    threading.Thread(target=_unload_when_idle, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("serving on http://%s:%d (model %s)", HOST, PORT, MODEL_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
