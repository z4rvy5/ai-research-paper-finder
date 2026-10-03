"""Crossref REST API client: the only code that talks to api.crossref.org.

Docs: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
"""

import logging
from typing import Any, Protocol

import httpx2
from pydantic import BaseModel

log = logging.getLogger(__name__)


BASE_URL = "https://api.crossref.org"
DEFAULT_ROWS = 25
DEFAULT_TIMEOUT_S = 8.0

# Only the fields we use; `select` keeps responses small (Crossref "tips" guidance).
SELECT_FIELDS = (
    "DOI",
    "title",
    "author",
    "issued",
    "published",
    "type",
    "abstract",
    "URL",
    "container-title",
    "publisher",
    "is-referenced-by-count",
    "score",
)


class CrossrefError(Exception):
    """A Crossref call failed. `code` is the stable identifier used in API error responses."""

    def __init__(self, code: str, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


class RateLimitInfo(BaseModel):
    """Crossref's rate-limit response headers, recorded as reported (headers are authoritative)."""

    pool: str | None = None  # x-api-pool, e.g. "polite-array" (list) or "polite-single" (record)
    limit: int | None = None  # x-rate-limit-limit
    interval: str | None = None  # x-rate-limit-interval, e.g. "1s"
    concurrency: int | None = None  # x-concurrency-limit


class SearchResult(BaseModel):
    url: str  # request URL; never contains the contact address (it is only in the User-Agent)
    http_status: int
    total_results: int
    items: list[dict[str, Any]]  # raw Crossref work records, in Crossref relevance order
    rate_limit: RateLimitInfo


class PaperSearch(Protocol):
    """The boundary the rest of the app depends on; tests substitute a fake."""

    async def search_works(self, query: str, rows: int = DEFAULT_ROWS) -> SearchResult: ...

    async def aclose(self) -> None: ...


# The optional, explicitly configured CROSSREF_MAILTO contact address is sent ONLY in the
# User-Agent header, never in the URL. Crossref documents the User-Agent as one of two ways to
# identify yourself for its polite pool (the other is a `mailto` query parameter), and keeping the
# address out of URLs keeps it out of request logs, exception text and echoed URLs. When it is
# unset, no address is sent at all and requests use the public pool (lower rate limits).


def build_search_params(query: str, rows: int) -> dict[str, str | int]:
    return {
        "query.bibliographic": query,
        "rows": rows,
        "sort": "relevance",
        "select": ",".join(SELECT_FIELDS),
    }


def user_agent(mailto: str | None) -> str:
    return f"paper-finder/0.1 (mailto:{mailto})" if mailto else "paper-finder/0.1"


class CrossrefClient:
    def __init__(
        self,
        mailto: str | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        # `mailto` comes only from Settings.crossref_mailto (create_app) and goes only into the
        # User-Agent; None sends no address.
        self._mailto = mailto
        self._http = httpx2.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": user_agent(mailto)},
            timeout=timeout,
            transport=transport,
        )

    async def search_works(self, query: str, rows: int = DEFAULT_ROWS) -> SearchResult:
        params = build_search_params(query, rows)
        response = await self._get("/works", params)
        message = _message(response)
        items = message.get("items")
        total = message.get("total-results")
        if not isinstance(items, list) or not isinstance(total, int):
            raise CrossrefError(
                "upstream_invalid_response",
                "Crossref returned an unexpected response shape.",
                retryable=False,
                status=response.status_code,
            )
        return SearchResult(
            url=str(response.request.url),
            http_status=response.status_code,
            total_results=total,
            items=[item for item in items if isinstance(item, dict)],
            rate_limit=_rate_limit_info(response.headers),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _redact(self, text: str) -> str:
        """Strip the configured contact address from text about to be logged. Redacting before
        truncating means an address cut off at the truncation boundary can't leave a partial copy.
        The address is never in a URL now; this only covers Crossref echoing the User-Agent."""
        return text.replace(self._mailto, "[redacted]") if self._mailto else text

    async def _get(self, path: str, params: dict[str, str | int]) -> httpx2.Response:
        try:
            response = await self._http.get(path, params=params)
        except httpx2.TimeoutException as exc:
            raise CrossrefError(
                "upstream_unavailable", "Crossref did not respond in time.", retryable=True
            ) from exc
        except httpx2.HTTPError as exc:
            raise CrossrefError(
                "upstream_unavailable", "Could not reach Crossref.", retryable=True
            ) from exc

        status = response.status_code
        if status == 200:
            return response
        if status == 429:
            raise CrossrefError(
                "upstream_rate_limited",
                "Crossref is rate limiting requests. Try again shortly.",
                retryable=True,
                status=status,
            )
        if status >= 500:
            raise CrossrefError(
                "upstream_unavailable",
                "Crossref is temporarily unavailable.",
                retryable=True,
                status=status,
            )
        if status == 400:
            # Our request was invalid (e.g. a bad filter): a bug on our side, so log the detail.
            log.error("Crossref rejected request %s: %s", path, self._redact(response.text)[:500])
            raise CrossrefError(
                "upstream_rejected",
                "Crossref rejected the search request.",
                retryable=False,
                status=status,
            )
        raise CrossrefError(
            "upstream_error",
            f"Unexpected Crossref response (HTTP {status}).",
            retryable=False,
            status=status,
        )


def _message(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise CrossrefError(
            "upstream_invalid_response",
            "Crossref returned invalid JSON.",
            retryable=False,
            status=response.status_code,
        ) from exc
    if not isinstance(body, dict):
        body = {}
    message = body.get("message")
    if body.get("status") != "ok" or not isinstance(message, dict):
        raise CrossrefError(
            "upstream_invalid_response",
            "Crossref returned an unexpected response shape.",
            retryable=False,
            status=response.status_code,
        )
    return message


def _rate_limit_info(headers: httpx2.Headers) -> RateLimitInfo:
    def as_int(name: str) -> int | None:
        value = headers.get(name)
        return int(value) if value is not None and value.isdigit() else None

    return RateLimitInfo(
        pool=headers.get("x-api-pool"),
        limit=as_int("x-rate-limit-limit"),
        interval=headers.get("x-rate-limit-interval"),
        concurrency=as_int("x-concurrency-limit"),
    )
