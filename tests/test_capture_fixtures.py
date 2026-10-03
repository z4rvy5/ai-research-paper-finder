"""The fixture-capture script, run against a mocked transport (no live Crossref calls)."""

import json

import httpx2
import pytest

from app.crossref.client import BASE_URL, user_agent
from scripts.capture_fixtures import capture, resolve_mailto
from tests.conftest import assert_address_absent, load_crossref_fixture

CONFIGURED = "project-contact@example.org"
SEARCHES = {"sample": ("software testing", 2)}


def mock_http(mailto: str | None, status: int = 200) -> tuple[httpx2.Client, list[httpx2.Request]]:
    """A client built the way main() builds it, with requests recorded instead of sent."""
    fixture = load_crossref_fixture("works_llm_software_testing")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if status != 200:
            return httpx2.Response(status, text="error")
        return httpx2.Response(200, json=fixture["body"], headers=fixture["headers"])

    http = httpx2.Client(
        base_url=BASE_URL,
        headers={"User-Agent": user_agent(mailto)},
        transport=httpx2.MockTransport(handler),
    )
    return http, seen


@pytest.mark.parametrize("environ", [{}, {"CROSSREF_MAILTO": ""}, {"CROSSREF_MAILTO": "   "}])
def test_mailto_is_none_when_crossref_mailto_is_unset_or_blank(environ):
    assert resolve_mailto(environ) is None


def test_mailto_comes_only_from_crossref_mailto():
    environ = {"CROSSREF_MAILTO": f" {CONFIGURED} ", "EMAIL": "other@example.org"}

    assert resolve_mailto(environ) == CONFIGURED


def test_capture_without_mailto_sends_no_email_address(tmp_path):
    http, seen = mock_http(mailto=None)

    written = capture(http, searches=SEARCHES, fixture_dir=tmp_path)

    request = seen[0]
    assert "mailto" not in request.url.params
    assert "@" not in str(request.url)
    assert all("@" not in value for value in request.headers.values())
    assert request.headers["user-agent"] == "paper-finder/0.1"
    assert written == [(tmp_path / "sample.json", 2)]


def test_capture_with_configured_mailto_uses_user_agent_only_and_never_writes_it(tmp_path):
    http, seen = mock_http(mailto=CONFIGURED)

    capture(http, searches=SEARCHES, fixture_dir=tmp_path)

    request = seen[0]
    assert request.headers["user-agent"] == f"paper-finder/0.1 (mailto:{CONFIGURED})"
    assert "mailto" not in request.url.params
    assert_address_absent(str(request.url), CONFIGURED)
    assert_address_absent((tmp_path / "sample.json").read_text(encoding="utf-8"), CONFIGURED)


def test_capture_writes_trimmed_envelope_with_rate_limit_headers(tmp_path):
    http, _ = mock_http(mailto=None)

    capture(http, searches=SEARCHES, fixture_dir=tmp_path)

    fixture = json.loads((tmp_path / "sample.json").read_text(encoding="utf-8"))
    assert fixture["query"] == "software testing"
    assert len(fixture["body"]["message"]["items"]) == 2
    assert set(fixture["headers"]) == {
        "x-api-pool",
        "x-rate-limit-limit",
        "x-rate-limit-interval",
        "x-concurrency-limit",
    }


@pytest.mark.parametrize("status", [400, 429, 500])
def test_capture_failure_message_does_not_contain_the_configured_address(tmp_path, status):
    # raise_for_status() text includes the full request URL; the address is only in a header.
    http, _ = mock_http(mailto=CONFIGURED, status=status)

    with pytest.raises(httpx2.HTTPStatusError) as exc_info:
        capture(http, searches=SEARCHES, fixture_dir=tmp_path)

    assert str(status) in str(exc_info.value)
    assert_address_absent(str(exc_info.value), CONFIGURED)
    assert not list(tmp_path.iterdir())  # nothing written on failure
