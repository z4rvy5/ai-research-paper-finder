"""POST /api/ask through the app factory: with a fake Crossref client, and (for the contact
email tests) with the real CrossrefClient over a mocked transport."""

import logging

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.crossref.client import CrossrefClient, CrossrefError, RateLimitInfo, SearchResult
from app.main import create_app
from tests.conftest import assert_address_absent, load_crossref_fixture, make_settings
from tests.fakes import FakeCrossrefClient


def fixture_result(name: str = "works_llm_software_testing") -> SearchResult:
    fixture = load_crossref_fixture(name)
    message = fixture["body"]["message"]
    return SearchResult(
        url="https://api.crossref.org/works?query.bibliographic=...",
        http_status=200,
        total_results=message["total-results"],
        items=message["items"],
        rate_limit=RateLimitInfo(pool="polite-array", limit=3, interval="1s", concurrency=3),
    )


def make_client(fake: FakeCrossrefClient) -> TestClient:
    return TestClient(create_app(make_settings(), crossref=fake))


def test_ask_returns_only_papers_from_crossref_records():
    fixture = load_crossref_fixture("works_llm_software_testing")
    fake = FakeCrossrefClient(result=fixture_result())

    res = make_client(fake).post("/api/ask", json={"question": fixture["query"]})

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    items = fixture["body"]["message"]["items"]
    assert [p["doi"] for p in body["papers"]] == [i["DOI"].lower() for i in items]
    assert [p["title"] for p in body["papers"]] == [i["title"][0] for i in items]
    assert body["search"]["total_results"] == fixture["body"]["message"]["total-results"]
    assert body["search"]["returned"] == len(items)
    assert body["search"]["rate_limit"]["pool"] == "polite-array"


def test_ask_sends_cleaned_question_to_crossref():
    fake = FakeCrossrefClient(result=fixture_result())

    make_client(fake).post("/api/ask", json={"question": "  LLMs for\n\n software   testing "})

    assert fake.queries == ["LLMs for software testing"]


def test_ask_with_no_crossref_items_reports_no_results():
    empty = SearchResult(
        url="u", http_status=200, total_results=0, items=[], rate_limit=RateLimitInfo()
    )

    res = make_client(FakeCrossrefClient(result=empty)).post(
        "/api/ask", json={"question": "zzqx nonexistent topic"}
    )

    assert res.status_code == 200
    assert res.json()["status"] == "no_results"
    assert res.json()["papers"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "  a "},
        {"question": "x" * 501},
        {"question": "bad \x00 byte"},
        {"question": 42},
        {},
        {"question": "valid question", "extra": "field"},
    ],
)
def test_invalid_questions_get_422_error_envelope_without_calling_crossref(payload):
    fake = FakeCrossrefClient(result=fixture_result())

    res = make_client(fake).post("/api/ask", json=payload)

    assert res.status_code == 422
    error = res.json()["error"]
    assert error["code"] == "invalid_input"
    assert error["retryable"] is False
    assert error["message"]
    assert fake.queries == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (CrossrefError("upstream_rate_limited", "Rate limited.", retryable=True), 503),
        (CrossrefError("upstream_unavailable", "Down.", retryable=True), 503),
        (CrossrefError("upstream_rejected", "Rejected.", retryable=False), 502),
        (CrossrefError("upstream_invalid_response", "Bad JSON.", retryable=False), 502),
    ],
)
def test_crossref_failures_return_error_envelope(error, status):
    res = make_client(FakeCrossrefClient(error=error)).post(
        "/api/ask", json={"question": "software testing"}
    )

    assert res.status_code == status
    assert res.json() == {
        "error": {"code": error.code, "message": error.message, "retryable": error.retryable}
    }


def test_app_shutdown_closes_crossref_client():
    fake = FakeCrossrefClient(result=fixture_result())

    with make_client(fake):
        pass

    assert fake.closed is True


def real_client_over_mock_transport(mailto: str | None, respond=None) -> tuple[TestClient, list]:
    """The real CrossrefClient behind the API, with Crossref replaced by a recorded mock.

    `respond(request)` overrides the default canned 200 response.
    """
    fixture = load_crossref_fixture("works_llm_software_testing")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if respond:
            return respond(request)
        return httpx2.Response(200, json=fixture["body"], headers=fixture["headers"])

    settings = make_settings(crossref_mailto=mailto)
    crossref = CrossrefClient(settings.crossref_mailto, transport=httpx2.MockTransport(handler))
    return TestClient(create_app(settings, crossref=crossref)), seen


def test_unset_crossref_mailto_sends_no_email_address_to_crossref():
    client, seen = real_client_over_mock_transport(mailto=None)

    res = client.post("/api/ask", json={"question": "software testing"})

    assert res.status_code == 200
    assert "mailto" not in seen[0].url.params
    assert "@" not in str(seen[0].url)
    assert all("@" not in value for value in seen[0].headers.values())
    assert seen[0].headers["user-agent"] == "paper-finder/0.1"
    assert "@" not in res.text


def test_configured_crossref_mailto_is_sent_only_in_user_agent_and_never_returned(caplog):
    address = "project-contact@example.org"
    client, seen = real_client_over_mock_transport(mailto=address)

    with caplog.at_level(logging.DEBUG):
        res = client.post("/api/ask", json={"question": "software testing"})

    request = seen[0]
    assert request.headers["user-agent"] == f"paper-finder/0.1 (mailto:{address})"
    assert "mailto" not in request.url.params
    assert_address_absent(str(request.url), address)
    assert [name for name, value in request.headers.items() if address in value] == ["user-agent"]
    assert res.status_code == 200
    assert_address_absent(res.text, address)
    assert "mailto" not in res.json()["search"]["url"]
    assert_address_absent(caplog.text, address)


def raise_timeout_with_url_and_agent(request: httpx2.Request):
    # Worst case: a library exception whose text embeds the request URL and the User-Agent.
    agent = request.headers["user-agent"]
    raise httpx2.ReadTimeout(f"timed out for {request.url} ({agent})", request=request)


@pytest.mark.parametrize(
    "respond",
    [
        # Crossref echoing the contact address back in an error body (hypothetical).
        lambda request: httpx2.Response(400, text='{"message": "bad agent leak-api@example.org"}'),
        raise_timeout_with_url_and_agent,
        httpx2.Response(500, text="oops leak-api@example.org"),
    ],
    ids=["400-body-echo", "exception-embeds-url-and-agent", "500-body-echo"],
)
def test_crossref_errors_never_expose_the_configured_address_to_the_browser(respond, caplog):
    address = "leak-api@example.org"
    handler = respond if callable(respond) else (lambda request: respond)
    client, _ = real_client_over_mock_transport(mailto=address, respond=handler)

    with caplog.at_level(logging.DEBUG):
        res = client.post("/api/ask", json={"question": "software testing"})

    assert res.status_code in (502, 503)
    assert_address_absent(res.text, address)
    assert_address_absent(caplog.text, address)
