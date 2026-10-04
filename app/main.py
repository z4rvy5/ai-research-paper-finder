"""FastAPI application factory.

Run locally with:  uv run uvicorn app.main:create_app --factory --reload
"""

import logging
import math
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent.budget import BudgetedModelClient
from app.agent.llm import AnthropicModelClient, ModelClient
from app.agent.orchestrator import Orchestrator, SearchFailed
from app.config import Settings
from app.crossref.client import CrossrefClient, CrossrefError, PaperSearch
from app.crossref.normalize import normalize_work
from app.errors import ApiError
from app.limits import DailyCallBudget, SlidingWindowLimiter
from app.schemas import (
    AskRequest,
    AskResponse,
    ErrorResponse,
    HealthResponse,
    ReadingListResponse,
    SavedPaper,
    SavePaperRequest,
    Trace,
    normalize_doi,
)
from app.storage.paper_cache import PaperCache
from app.storage.reading_list import ReadingListRepo, StorageError, make_engine

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

READING_LIST_ERRORS = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


# Applied to every response. The page has no inline scripts or styles, so a strict policy works.
# (The interactive API docs at /docs load scripts from a CDN, so they are exempt from the CSP.)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def error_response(
    status: int,
    code: str,
    message: str,
    retryable: bool,
    trace: Trace | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """The error envelope. `trace` is added only when there is one; otherwise the body is
    exactly `{"error": {...}}`, as before."""
    body: dict[str, object] = {"error": {"code": code, "message": message, "retryable": retryable}}
    if trace is not None:
        body["trace"] = trace.model_dump(mode="json")
    return JSONResponse(status_code=status, content=body, headers=headers)


def client_address(request: Request) -> str:
    """The address used to rate-limit a client.

    Behind a proxy (Render) the TCP peer is the proxy, so the forwarding header is used, taking its
    LAST entry: that is the one our own proxy appended, which a client cannot forge (anything a
    client puts in the header comes earlier in the list). If more than one proxy sits in front,
    clients may share an address and so share a limit, which errs on the side of limiting.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


def client_id_header(x_client_id: str | None = Header(default=None)) -> str:
    """The browser's anonymous id from `X-Client-Id`.

    This separates one browser's reading list from another's. It is NOT authentication: it is a
    random value the browser generated, and anyone who has it can use that list.
    """
    if not x_client_id:
        raise ApiError(400, "missing_client_id", "The X-Client-Id header is required.")
    try:
        return str(uuid.UUID(x_client_id.strip()))  # canonical lowercase form
    except ValueError:
        raise ApiError(400, "invalid_client_id", "X-Client-Id must be a UUID.") from None


def create_app(
    settings: Settings | None = None,
    crossref: PaperSearch | None = None,
    model: ModelClient | None = None,
    clock: Callable[[], date] = date.today,
    repo: ReadingListRepo | None = None,
) -> FastAPI:
    """Build the app. Tests pass explicit settings and fake Crossref/model clients and a repo."""
    settings = settings or Settings()
    crossref = crossref or CrossrefClient(settings.crossref_mailto)
    # Every model call spends from a daily budget; once it is gone the app uses its fallbacks.
    model = BudgetedModelClient(
        model or AnthropicModelClient(settings), DailyCallBudget(settings.max_daily_model_calls)
    )
    ask_limiter = SlidingWindowLimiter(settings.ask_rate_limit_per_minute, 60.0)
    repo = repo or ReadingListRepo(make_engine(settings.database_url))
    orchestrator = Orchestrator(
        model=model, crossref=crossref, model_name=settings.anthropic_model, clock=clock
    )
    recent_papers = PaperCache()  # papers we just recommended, so saving needs no second lookup

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if os.environ.get("RENDER") and repo.backend == "sqlite":
            # Render's disk is ephemeral: a SQLite file would silently lose every reading list.
            log.warning(
                "DATABASE_URL is not a Postgres database on Render: reading lists will NOT "
                "survive a restart or redeploy. Set DATABASE_URL to the Neon connection string."
            )
        try:
            await run_in_threadpool(repo.ensure_schema)
        except StorageError:
            # The app still starts: /api/ask works and the reading list retries on first use.
            log.warning("Reading-list database not reachable at startup; will retry on use")
        yield
        await crossref.aclose()
        await model.aclose()
        await run_in_threadpool(repo.dispose)

    app = FastAPI(title="AI Research Paper Finder", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response

    async def limit_asks(request: Request) -> None:
        wait = ask_limiter.check(client_address(request))
        if wait is not None:
            seconds = max(1, math.ceil(wait))
            raise ApiError(
                429,
                "rate_limited",
                f"Too many questions in a short time. Please wait about {seconds} seconds.",
                retryable=True,
                headers={"Retry-After": str(seconds)},
            )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Unknown routes and methods use the same error envelope as everything else."""
        known = {
            404: ("not_found", "Not found."),
            405: ("method_not_allowed", "That method is not allowed here."),
        }
        code, message = known.get(
            exc.status_code, ("http_error", "The request could not be handled.")
        )
        headers = dict(exc.headers) if exc.headers else None
        return error_response(exc.status_code, code, message, False, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        return error_response(422, "invalid_input", message, retryable=False)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.retryable, headers=exc.headers)

    @app.exception_handler(StorageError)
    async def on_storage_error(request: Request, exc: StorageError) -> JSONResponse:
        return error_response(503, exc.code, exc.message, exc.retryable)

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
    async def health() -> HealthResponse:
        database_ok = await run_in_threadpool(repo.ping)
        return HealthResponse(
            status="ok",  # the process is up; `db` says whether the database answered
            db="ok" if database_ok else "unavailable",
            db_backend="postgresql" if repo.backend.startswith("postgres") else "sqlite",
            model_configured=settings.model_api_key is not None,
            crossref_mailto_configured=bool(settings.crossref_mailto),
        )

    @app.post(
        "/api/ask",
        response_model=AskResponse,
        dependencies=[Depends(limit_asks)],
        responses={
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def ask(body: AskRequest) -> AskResponse:
        result = await orchestrator.ask(body.question)
        for paper in result.papers:
            recent_papers.put(paper)
        return result

    # ---- reading list (scoped by X-Client-Id; see client_id_header) ---------------------------

    @app.get("/api/reading-list", response_model=ReadingListResponse, responses=READING_LIST_ERRORS)
    async def list_reading_list(client_id: str = Depends(client_id_header)) -> ReadingListResponse:
        items = await run_in_threadpool(repo.list_papers, client_id)
        return ReadingListResponse(items=items)

    @app.post(
        "/api/reading-list",
        response_model=SavedPaper,
        status_code=201,
        responses={200: {"model": SavedPaper}, **READING_LIST_ERRORS},
    )
    async def save_paper(
        body: SavePaperRequest, response: Response, client_id: str = Depends(client_id_header)
    ) -> SavedPaper:
        """201 when newly saved, 200 when it was already in the list (saving twice is fine)."""
        existing = await run_in_threadpool(repo.get, client_id, body.doi)
        if existing is not None:
            response.status_code = 200
            return existing

        # The metadata is always Crossref's: from a paper we just recommended, or looked up now.
        paper = recent_papers.get(body.doi)
        if paper is None:
            record = await crossref.get_work(body.doi)  # CrossrefError -> 502/503 envelope
            paper = normalize_work(record, 0) if record else None
            if paper is None or paper.doi != body.doi:
                raise ApiError(404, "doi_not_found", "Crossref has no record for that DOI.")
        saved, created = await run_in_threadpool(repo.add, client_id, paper)
        response.status_code = 201 if created else 200
        return saved

    @app.delete(
        "/api/reading-list/{doi:path}",
        status_code=204,
        response_class=Response,
        responses=READING_LIST_ERRORS,
    )
    async def remove_paper(doi: str, client_id: str = Depends(client_id_header)) -> Response:
        try:
            doi = normalize_doi(doi)
        except ValueError as exc:
            raise ApiError(422, "invalid_input", str(exc)) from None
        removed = await run_in_threadpool(repo.remove, client_id, doi)
        if not removed:
            raise ApiError(404, "not_saved", "That paper is not in your reading list.")
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
