"""Crossref REST API client: the only code that talks to api.crossref.org.

Docs: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol
from urllib.parse import quote

import httpx2
from pydantic import BaseModel

log = logging.getLogger(__name__)


BASE_URL = "https://api.crossref.org"
DEFAULT_ROWS = 25
DEFAULT_TIMEOUT_S = 8.0

# Transient failures (429, 5xx, timeouts, connection errors) are retried ONCE, after a short pause.
# Crossref asks clients to back off on 429; waiting for its Retry-After (capped) and trying once
# more is polite and bounded. A second failure is reported, not retried again.
MAX_ATTEMPTS = 2
RETRY_DEFAULT_DELAY_S = 1.0
RETRY_MAX_DELAY_S = 2.0

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

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
        rate_limit: "RateLimitInfo | None" = None,
        retries: int = 0,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.rate_limit = rate_limit  # Crossref's rate-limit headers, when it answered
        self.retries = retries  # how many retries were used before giving up


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
    retries: int = 0  # 0 or 1: whether a transient failure was retried to get this result


class PaperSearch(Protocol):
    """The boundary the rest of the app depends on; tests substitute a fake."""

    async def search_works(
        self,
        query: str,
        rows: int = DEFAULT_ROWS,
        *,
        from_year: int | None = None,
        until_year: int | None = None,
        types: Sequence[str] = (),
    ) -> SearchResult: ...

    async def get_work(self, doi: str) -> dict[str, Any] | None:
        """The raw Crossref record for one DOI, or None if Crossref has no such DOI."""
        ...

    async def aclose(self) -> None: ...


# The optional, explicitly configured CROSSREF_MAILTO contact address is sent ONLY in the
# User-Agent header, never in the URL. Crossref documents the User-Agent as one of two ways to
# identify yourself for its polite pool (the other is a `mailto` query parameter), and keeping the
# address out of URLs keeps it out of request logs, exception text and echoed URLs. When it is
# unset, no address is sent at all and requests use the public pool (lower rate limits).


def build_filter(
    from_year: int | None = None, until_year: int | None = None, types: Sequence[str] = ()
) -> str | None:
    """Crossref `filter=` value, or None when there are no constraints.

    Different filters are ANDed. Repeating the same filter name ORs the values, so several
    `type:` entries mean "any of these types".
    """
    parts = []
    if from_year is not None:
        parts.append(f"from-pub-date:{from_year:04d}-01-01")
    if until_year is not None:
        parts.append(f"until-pub-date:{until_year:04d}-12-31")
    parts.extend(f"type:{work_type}" for work_type in types)
    return ",".join(parts) or None


def build_search_params(
    query: str,
    rows: int,
    *,
    from_year: int | None = None,
    until_year: int | None = None,
    types: Sequence[str] = (),
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "query.bibliographic": query,
        "rows": rows,
        "sort": "relevance",
        "select": ",".join(SELECT_FIELDS),
    }
    if filter_value := build_filter(from_year, until_year, types):
        params["filter"] = filter_value
    return params


def user_agent(mailto: str | None) -> str:
    return f"paper-finder/0.1 (mailto:{mailto})" if mailto else "paper-finder/0.1"


class CrossrefClient:
    def __init__(
        self,
        mailto: str | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        # `mailto` comes only from Settings.crossref_mailto (create_app) and goes only into the
        # User-Agent; None sends no address.
        self._mailto = mailto
        self._sleep = sleep  # injectable so tests never really wait
        self._http = httpx2.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": user_agent(mailto)},
            timeout=timeout,
            transport=transport,
        )

    async def search_works(
        self,
        query: str,
        rows: int = DEFAULT_ROWS,
        *,
        from_year: int | None = None,
        until_year: int | None = None,
        types: Sequence[str] = (),
    ) -> SearchResult:
        params = build_search_params(
            query, rows, from_year=from_year, until_year=until_year, types=types
        )
        response, retries = await self._get("/works", params)
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
            retries=retries,
        )

    async def get_work(self, doi: str) -> dict[str, Any] | None:
        """One record via `GET /works/{doi}`. Unknown DOIs answer 404 (plain text), meaning None."""
        response, _ = await self._get(
            f"/works/{quote(doi, safe='/:;()-._')}", {}, not_found_ok=True
        )
        if response.status_code == 404:
            return None
        message = _message(response)
        if not isinstance(message.get("DOI"), str):
            raise CrossrefError(
                "upstream_invalid_response",
                "Crossref returned an unexpected response shape.",
                retryable=False,
                status=response.status_code,
            )
        return message

    async def aclose(self) -> None:
        await self._http.aclose()

    def _redact(self, text: str) -> str:
        """Strip the configured contact address from text about to be logged. Redacting before
        truncating means an address cut off at the truncation boundary can't leave a partial copy.
        The address is never in a URL now; this only covers Crossref echoing the User-Agent."""
        return text.replace(self._mailto, "[redacted]") if self._mailto else text

    async def _get(
        self, path: str, params: dict[str, str | int], *, not_found_ok: bool = False
    ) -> tuple[httpx2.Response, int]:
        """GET `path`. Returns (response, retries used). Retries transient failures once."""
        for attempt in range(MAX_ATTEMPTS):
            last_attempt = attempt == MAX_ATTEMPTS - 1
            try:
                response = await self._http.get(path, params=params)
            except httpx2.TimeoutException as exc:
                if not last_attempt:
                    await self._sleep(RETRY_DEFAULT_DELAY_S)
                    continue
                raise CrossrefError(
                    "upstream_unavailable",
                    "Crossref did not respond in time.",
                    retryable=True,
                    retries=attempt,
                ) from exc
            except httpx2.HTTPError as exc:
                if not last_attempt:
                    await self._sleep(RETRY_DEFAULT_DELAY_S)
                    continue
                raise CrossrefError(
                    "upstream_unavailable",
                    "Could not reach Crossref.",
                    retryable=True,
                    retries=attempt,
                ) from exc

            status = response.status_code
            if status == 200 or (status == 404 and not_found_ok):
                return response, attempt
            if (status == 429 or status >= 500) and not last_attempt:
                await self._sleep(_retry_delay(response))
                continue
            raise self._error_for(response, path, retries=attempt)
        raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover

    def _error_for(self, response: httpx2.Response, path: str, *, retries: int) -> CrossrefError:
        """The CrossrefError for a non-success response."""
        status = response.status_code
        info = _rate_limit_info(response.headers)  # Crossref's own limits, as reported
        if status == 429:
            return CrossrefError(
                "upstream_rate_limited",
                "Crossref is rate limiting requests. Try again shortly.",
                retryable=True,
                status=status,
                rate_limit=info,
                retries=retries,
            )
        if status >= 500:
            return CrossrefError(
                "upstream_unavailable",
                "Crossref is temporarily unavailable.",
                retryable=True,
                status=status,
                rate_limit=info,
                retries=retries,
            )
        if status == 400:
            # Our request was invalid (e.g. a bad filter): a bug on our side, so log the detail.
            log.error("Crossref rejected request %s: %s", path, self._redact(response.text)[:500])
            return CrossrefError(
                "upstream_rejected",
                "Crossref rejected the search request.",
                retryable=False,
                status=status,
                rate_limit=info,
            )
        return CrossrefError(
            "upstream_error",
            f"Unexpected Crossref response (HTTP {status}).",
            retryable=False,
            status=status,
            rate_limit=info,
        )


def _retry_delay(response: httpx2.Response) -> float:
    """Seconds to wait before the retry: Crossref's Retry-After if it is a number, else a default,
    never more than RETRY_MAX_DELAY_S."""
    try:
        wanted = float(response.headers.get("retry-after", ""))
    except ValueError:
        wanted = RETRY_DEFAULT_DELAY_S
    return min(max(wanted, 0.0), RETRY_MAX_DELAY_S)


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
