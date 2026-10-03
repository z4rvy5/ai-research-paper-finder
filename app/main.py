"""FastAPI application factory.

Run locally with:  uv run uvicorn app.main:create_app --factory --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.crossref.client import CrossrefClient, CrossrefError, PaperSearch
from app.crossref.normalize import normalize_works
from app.schemas import AskRequest, AskResponse, ErrorResponse, HealthResponse, SearchInfo

STATIC_DIR = Path(__file__).parent / "static"

# Crossref failure code -> HTTP status returned to the browser.
CROSSREF_ERROR_STATUS = {
    "upstream_rate_limited": 503,
    "upstream_unavailable": 503,
    "upstream_rejected": 502,
    "upstream_invalid_response": 502,
    "upstream_error": 502,
}


def error_response(status: int, code: str, message: str, retryable: bool) -> JSONResponse:
    body = ErrorResponse(error={"code": code, "message": message, "retryable": retryable})
    return JSONResponse(status_code=status, content=body.model_dump())


def create_app(settings: Settings | None = None, crossref: PaperSearch | None = None) -> FastAPI:
    """Build the app. Tests pass explicit settings and a fake Crossref client."""
    settings = settings or Settings()
    crossref = crossref or CrossrefClient(settings.crossref_mailto)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await crossref.aclose()

    app = FastAPI(title="AI Research Paper Finder", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        return error_response(422, "invalid_input", message, retryable=False)

    @app.exception_handler(CrossrefError)
    async def on_crossref_error(request: Request, exc: CrossrefError) -> JSONResponse:
        status = CROSSREF_ERROR_STATUS.get(exc.code, 502)
        return error_response(status, exc.code, exc.message, exc.retryable)

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            db="not_checked",
            model_configured=settings.anthropic_api_key is not None,
            crossref_mailto_configured=bool(settings.crossref_mailto),
        )

    @app.post(
        "/api/ask",
        response_model=AskResponse,
        responses={
            422: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def ask(body: AskRequest) -> AskResponse:
        # Milestone 2: the cleaned question is sent as-is as one Crossref bibliographic query.
        result = await crossref.search_works(body.question)
        papers = normalize_works(result.items)
        return AskResponse(
            status="ok" if papers else "no_results",
            papers=papers,
            search=SearchInfo(
                query=body.question,
                url=result.url,
                http_status=result.http_status,
                total_results=result.total_results,
                returned=len(result.items),
                rate_limit=result.rate_limit,
            ),
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
