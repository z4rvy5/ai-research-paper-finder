"""FastAPI application factory.

Run locally with:  uv run uvicorn app.main:create_app --factory --reload
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent.llm import AnthropicModelClient, ModelClient
from app.agent.orchestrator import Orchestrator, SearchFailed
from app.config import Settings
from app.crossref.client import CrossrefClient, CrossrefError, PaperSearch
from app.schemas import AskRequest, AskResponse, ErrorResponse, HealthResponse, Trace

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Crossref failure code -> HTTP status returned to the browser.
CROSSREF_ERROR_STATUS = {
    "upstream_rate_limited": 503,
    "upstream_unavailable": 503,
    "upstream_rejected": 502,
    "upstream_invalid_response": 502,
    "upstream_error": 502,
}


def error_response(
    status: int, code: str, message: str, retryable: bool, trace: Trace | None = None
) -> JSONResponse:
    """The error envelope. `trace` is added only when there is one; otherwise the body is
    exactly `{"error": {...}}`, as before."""
    body: dict[str, object] = {"error": {"code": code, "message": message, "retryable": retryable}}
    if trace is not None:
        body["trace"] = trace.model_dump(mode="json")
    return JSONResponse(status_code=status, content=body)


def create_app(
    settings: Settings | None = None,
    crossref: PaperSearch | None = None,
    model: ModelClient | None = None,
    clock: Callable[[], date] = date.today,
) -> FastAPI:
    """Build the app. Tests pass explicit settings and fake Crossref and model clients."""
    settings = settings or Settings()
    crossref = crossref or CrossrefClient(settings.crossref_mailto)
    model = model or AnthropicModelClient(settings)
    orchestrator = Orchestrator(
        model=model, crossref=crossref, model_name=settings.anthropic_model, clock=clock
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await crossref.aclose()
        await model.aclose()

    app = FastAPI(title="AI Research Paper Finder", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        return error_response(422, "invalid_input", message, retryable=False)

    @app.exception_handler(SearchFailed)
    async def on_search_failed(request: Request, exc: SearchFailed) -> JSONResponse:
        """Same status and error body as a plain Crossref failure, plus the trace."""
        status = CROSSREF_ERROR_STATUS.get(exc.error.code, 502)
        error = exc.error
        return error_response(status, error.code, error.message, error.retryable, exc.trace)

    @app.exception_handler(CrossrefError)
    async def on_crossref_error(request: Request, exc: CrossrefError) -> JSONResponse:
        status = CROSSREF_ERROR_STATUS.get(exc.code, 502)
        return error_response(status, exc.code, exc.message, exc.retryable)

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error while handling %s", request.url.path)
        return error_response(500, "internal_error", "Something went wrong on the server.", False)

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            db="not_checked",
            model_configured=settings.model_api_key is not None,
            crossref_mailto_configured=bool(settings.crossref_mailto),
        )

    @app.post(
        "/api/ask",
        response_model=AskResponse,
        responses={
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def ask(body: AskRequest) -> AskResponse:
        return await orchestrator.ask(body.question)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
