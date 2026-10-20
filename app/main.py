from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import audio, clips, config_routes, health, jobs, videos
from app.api.errors import register_exception_handlers
from app.core.logging import configure_logging

_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class _RevalidatingStaticFiles(StaticFiles):
    """Serve the dashboard with ``Cache-Control: no-cache`` so the browser
    re-checks (cheap 304 via ETag) on every load. Without it browsers keep an
    old index.html/app.js and keep showing the previous UI after an update."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="ClipForge API",
        version="0.1.0",
        description="Turn long videos into platform-ready clips.",
    )

    # Dev-friendly CORS (frontend runs on a different port).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(config_routes.router)
    app.include_router(videos.router)
    app.include_router(jobs.router)
    app.include_router(clips.router)
    app.include_router(audio.router)

    # Serve the single-page dashboard from the API itself (no separate build).
    if _STATIC_DIR.exists():
        app.mount("/ui", _RevalidatingStaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    return app


app = create_app()
