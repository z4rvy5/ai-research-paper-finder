"""FastAPI application factory.

Run locally with:  uv run uvicorn app.main:create_app --factory --reload
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.schemas import HealthResponse

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass explicit settings (and, in later milestones, fake clients)."""
    settings = settings or Settings()

    app = FastAPI(title="AI Research Paper Finder", version="0.1.0")
    app.state.settings = settings

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            db="not_checked",
            model_configured=settings.anthropic_api_key is not None,
            crossref_mailto_configured=bool(settings.crossref_mailto),
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
