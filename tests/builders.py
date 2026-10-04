"""Builders for Crossref-shaped test data."""

from typing import Any

from app.crossref.client import RateLimitInfo, SearchResult
from tests.conftest import load_crossref_fixture


def work(
    doi: str = "10.1000/example",
    title: str | None = "A paper about software testing with language models",
    year: int | None = 2024,
    authors: tuple[str, ...] = ("Ada Lovelace",),
    abstract: str | None = None,
    work_type: str | None = "journal-article",
    venue: str | None = "Journal of Testing",
    **extra: Any,
) -> dict[str, Any]:
    """A Crossref work record. `abstract` is plain text, wrapped as Crossref's JATS XML."""
    item: dict[str, Any] = {"DOI": doi}
    if title is not None:
        item["title"] = [title]
    if year is not None:
        item["issued"] = {"date-parts": [[year]]}
    if authors:
        item["author"] = [
            {"given": name.split(" ")[0], "family": name.split(" ", 1)[-1], "sequence": "first"}
            for name in authors
        ]
    if abstract is not None:
        item["abstract"] = f"<jats:p>{abstract}</jats:p>"
    if work_type is not None:
        item["type"] = work_type
    if venue is not None:
        item["container-title"] = [venue]
    item.update(extra)
    return item


def search_result(items: list[dict[str, Any]], total: int | None = None) -> SearchResult:
    return SearchResult(
        url="https://api.crossref.org/works?query.bibliographic=test",
        http_status=200,
        total_results=len(items) if total is None else total,
        items=items,
        rate_limit=RateLimitInfo(pool="polite-array", limit=3, interval="1s", concurrency=3),
    )


def fixture_result(name: str = "works_llm_software_testing") -> SearchResult:
    """The captured real Crossref response as a SearchResult."""
    message = load_crossref_fixture(name)["body"]["message"]
    return search_result(message["items"], total=message["total-results"])


def many_works(count: int, **overrides: Any) -> list[dict[str, Any]]:
    """`count` distinct, well-formed records that match the default topic terms."""
    return [
        work(
            doi=f"10.1000/paper{i}",
            title=f"Software testing with language models, study {chr(64 + i)}",
            abstract=f"We study software testing with language models, variant {chr(64 + i)}.",
            **overrides,
        )
        for i in range(1, count + 1)
    ]


def mock_crossref_client(mailto: str | None = None, respond=None):
    """The REAL CrossrefClient with Crossref replaced by a recorded mock transport.

    Returns (client, requests_seen). `respond(request)` overrides the default canned fixture 200.
    """
    import httpx2

    from app.crossref.client import CrossrefClient

    fixture = load_crossref_fixture("works_llm_software_testing")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if respond:
            return respond(request)
        return httpx2.Response(200, json=fixture["body"], headers=fixture["headers"])

    return CrossrefClient(mailto, transport=httpx2.MockTransport(handler)), seen
