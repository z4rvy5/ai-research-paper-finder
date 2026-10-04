"""CrossrefClient against a mocked HTTP transport: no live Crossref calls."""

import logging
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from app.crossref.client import SELECT_FIELDS, CrossrefClient, CrossrefError
from tests.conftest import assert_address_absent, load_crossref_fixture

pytestmark = pytest.mark.anyio

MAILTO = "dev@example.org"


def mock_client(handler, mailto: str | None = MAILTO) -> tuple[CrossrefClient, list]:
    """A CrossrefClient whose HTTP calls go to `handler`; returns it and the requests seen."""
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    return CrossrefClient(mailto, transport=httpx2.MockTransport(record)), seen


def fixture_response(name: str = "works_llm_software_testing"):
    fixture = load_crossref_fixture(name)
    return lambda request: httpx2.Response(200, json=fixture["body"], headers=fixture["headers"])


async def test_search_sends_question_as_bibliographic_query_with_contact_only_in_user_agent():
    client, seen = mock_client(fixture_response())

    await client.search_works("LLMs for software testing")

    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == "api.crossref.org"
    assert request.url.path == "/works"
    params = parse_qs(urlsplit(str(request.url)).query)
    assert params == {
        "query.bibliographic": ["LLMs for software testing"],
        "rows": ["25"],
        "sort": ["relevance"],
        "select": [",".join(SELECT_FIELDS)],
    }  # no mailto parameter
    assert request.headers["user-agent"] == f"paper-finder/0.1 (mailto:{MAILTO})"
    assert_address_absent(str(request.url), MAILTO)
    # The address appears in exactly one place in the request: the User-Agent header.
    assert [name for name, value in request.headers.items() if MAILTO in value] == ["user-agent"]


async def test_search_without_mailto_sends_no_address_at_all():
    client, seen = mock_client(fixture_response(), mailto=None)

    await client.search_works("graph neural networks")

    request = seen[0]
    assert "mailto" not in str(request.url)
    assert "@" not in str(request.url)
    assert all("@" not in value and "mailto" not in value for value in request.headers.values())
    assert request.headers["user-agent"] == "paper-finder/0.1"


async def test_search_parses_fixture_items_in_crossref_order():
    fixture = load_crossref_fixture("works_llm_software_testing")
    client, _ = mock_client(fixture_response())

    result = await client.search_works(fixture["query"])

    expected_items = fixture["body"]["message"]["items"]
    assert result.http_status == 200
    assert result.total_results == fixture["body"]["message"]["total-results"]
    assert [item["DOI"] for item in result.items] == [item["DOI"] for item in expected_items]


async def test_search_records_rate_limit_headers_and_url_has_no_contact_address():
    client, _ = mock_client(fixture_response())

    result = await client.search_works("software testing")

    assert result.rate_limit.pool == "polite-array"
    assert result.rate_limit.limit == 3
    assert result.rate_limit.interval == "1s"
    assert result.rate_limit.concurrency == 3
    assert "mailto" not in result.url
    assert_address_absent(result.url, MAILTO)
    assert "query.bibliographic=software+testing" in result.url


async def test_missing_rate_limit_headers_are_recorded_as_unknown():
    body = {"status": "ok", "message": {"total-results": 0, "items": []}}
    client, _ = mock_client(lambda request: httpx2.Response(200, json=body))

    result = await client.search_works("anything")

    assert result.rate_limit.model_dump() == {
        "pool": None,
        "limit": None,
        "interval": None,
        "concurrency": None,
    }
    assert result.items == []


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (429, "upstream_rate_limited", True),
        (500, "upstream_unavailable", True),
        (503, "upstream_unavailable", True),
        (400, "upstream_rejected", False),
        (404, "upstream_error", False),
    ],
)
async def test_http_errors_map_to_crossref_error_codes(status, code, retryable):
    client, _ = mock_client(lambda request: httpx2.Response(status, text="error"))

    with pytest.raises(CrossrefError) as exc_info:
        await client.search_works("software testing")

    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    assert exc_info.value.status == status


async def test_crossref_400_validation_failure_is_reported_as_rejected():
    body = {
        "status": "failed",
        "message-type": "validation-failure",
        "message": [{"type": "filter-not-available", "value": "bogus", "message": "..."}],
    }
    client, _ = mock_client(lambda request: httpx2.Response(400, json=body))

    with pytest.raises(CrossrefError) as exc_info:
        await client.search_works("software testing")

    assert exc_info.value.code == "upstream_rejected"


@pytest.mark.parametrize(
    "exception", [httpx2.ReadTimeout, httpx2.ConnectTimeout, httpx2.ConnectError]
)
async def test_timeouts_and_network_errors_are_unavailable(exception):
    def fail(request):
        raise exception("simulated failure", request=request)

    client, _ = mock_client(fail)

    with pytest.raises(CrossrefError) as exc_info:
        await client.search_works("software testing")

    assert exc_info.value.code == "upstream_unavailable"
    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="<html>not json</html>"),
        httpx2.Response(200, json=["not", "an", "object"]),
        httpx2.Response(200, json={"status": "failed", "message": {}}),
        httpx2.Response(200, json={"status": "ok", "message": {"items": "nope"}}),
    ],
)
async def test_malformed_success_responses_are_invalid(response):
    client, _ = mock_client(lambda request: response)

    with pytest.raises(CrossrefError) as exc_info:
        await client.search_works("software testing")

    assert exc_info.value.code == "upstream_invalid_response"


# --- The configured contact address must not leak through logs or exceptions -----------------
# The address is only in the User-Agent header, never in the URL. httpx2 logs every request URL
# at INFO (not headers), so the library's own request log has nothing to leak.


async def failing_search(client: CrossrefClient) -> CrossrefError:
    with pytest.raises(CrossrefError) as exc_info:
        await client.search_works("software testing")
    return exc_info.value


async def test_http_library_request_logs_do_not_contain_the_configured_address(caplog):
    address = "log-check-ok@example.org"
    client, seen = mock_client(fixture_response(), mailto=address)

    with caplog.at_level(logging.DEBUG):
        await client.search_works("software testing")

    # Guard against a vacuous pass: the address was configured and sent, and the library
    # really did log the request line.
    assert address in seen[0].headers["user-agent"]
    assert "HTTP Request: GET https://api.crossref.org/works" in caplog.text
    assert_address_absent(caplog.text, address)
    assert_address_absent(" ".join(r.getMessage() for r in caplog.records), address)


async def test_crossref_400_body_echoing_the_address_is_redacted_from_logs_and_errors(caplog):
    address = "log-check-400@example.org"
    # Hypothetical: Crossref echoing the User-Agent contact address in an error body.
    echo = {"status": "failed", "message": [{"type": "x", "message": f"bad agent {address}"}]}
    client, _ = mock_client(lambda request: httpx2.Response(400, json=echo), mailto=address)

    with caplog.at_level(logging.DEBUG):
        error = await failing_search(client)

    assert "Crossref rejected request /works" in caplog.text  # the line was logged
    assert "[redacted]" in caplog.text
    assert_address_absent(caplog.text, address)
    assert_address_absent(f"{error} {error.message} {error.code}", address)


async def test_address_cut_off_at_the_log_truncation_boundary_is_still_redacted(caplog):
    address = "log-check-edge@example.org"
    # Pad so the address straddles character 500 of the body, where the log line is truncated.
    padding = "x" * (500 - len(address) // 2)
    client, _ = mock_client(
        lambda request: httpx2.Response(400, text=padding + address), mailto=address
    )

    with caplog.at_level(logging.DEBUG):
        await failing_search(client)

    assert "Crossref rejected request /works" in caplog.text
    assert address[: len(address) // 2] not in caplog.text  # not even a partial address


@pytest.mark.parametrize("exception", [httpx2.ReadTimeout, httpx2.ConnectError])
async def test_error_text_does_not_contain_address_even_if_the_library_exception_does(exception):
    address = "log-check-exc@example.org"

    def fail(request):
        # Worst case: a library exception whose text embeds the request URL and the User-Agent.
        agent = request.headers["user-agent"]
        raise exception(f"failed for {request.url} (User-Agent: {agent})", request=request)

    client, _ = mock_client(fail, mailto=address)

    error = await failing_search(client)

    assert_address_absent(f"{error} {error.message} {error.code}", address)


# --- get_work: one record by DOI (used when saving a paper that was not just recommended) --------


def work_response(item):
    body = {"status": "ok", "message-type": "work", "message-version": "1.0.0", "message": item}
    return lambda request: httpx2.Response(
        200, json=body, headers={"x-api-pool": "polite-single", "x-rate-limit-limit": "10"}
    )


async def test_get_work_returns_the_raw_record_from_the_works_doi_route():
    record = {"DOI": "10.1000/abc", "title": ["T"], "type": "journal-article"}
    client, seen = mock_client(work_response(record))

    result = await client.get_work("10.1000/abc")

    assert result == record
    assert seen[0].method == "GET" and seen[0].url.path == "/works/10.1000/abc"
    assert seen[0].url.query == b""  # no parameters; in particular no contact address
    assert seen[0].headers["user-agent"] == f"paper-finder/0.1 (mailto:{MAILTO})"


async def test_get_work_treats_crossrefs_plain_text_404_as_not_found():
    client, _ = mock_client(lambda request: httpx2.Response(404, text="Resource not found."))

    assert await client.get_work("10.9999/does-not-exist") is None


async def test_get_work_path_encodes_characters_that_would_change_the_url():
    client, seen = mock_client(work_response({"DOI": "10.1000/a#b?c d"}))

    await client.get_work("10.1000/a#b?c d")

    assert seen[0].url.raw_path == b"/works/10.1000/a%23b%3Fc%20d"
    assert seen[0].url.fragment == ""  # the '#' did not become a URL fragment


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "upstream_rate_limited"), (500, "upstream_unavailable"), (400, "upstream_rejected")],
)
async def test_get_work_errors_map_like_search_errors(status, code):
    client, _ = mock_client(lambda request: httpx2.Response(status, text="error"))

    with pytest.raises(CrossrefError) as exc_info:
        await client.get_work("10.1000/abc")

    assert exc_info.value.code == code and exc_info.value.status == status


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"status": "ok", "message": {"no": "doi"}}),
        httpx2.Response(200, json={"status": "ok", "message": {"DOI": 5}}),
        httpx2.Response(200, json={"status": "failed", "message": {"DOI": "10.1/x"}}),
    ],
)
async def test_get_work_rejects_malformed_responses(response):
    client, _ = mock_client(lambda request: response)

    with pytest.raises(CrossrefError) as exc_info:
        await client.get_work("10.1000/abc")

    assert exc_info.value.code == "upstream_invalid_response"


async def test_get_work_network_failure_is_unavailable():
    def fail(request):
        raise httpx2.ConnectError("no route", request=request)

    client, _ = mock_client(fail)

    with pytest.raises(CrossrefError) as exc_info:
        await client.get_work("10.1000/abc")

    assert exc_info.value.code == "upstream_unavailable" and exc_info.value.retryable
