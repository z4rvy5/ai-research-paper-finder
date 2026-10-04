"""POST /api/ask: the HTTP contract, input validation, error envelopes and privacy.

Crossref and the model are always faked (FakeCrossrefClient / FakeModelClient, or the real
CrossrefClient over a mock transport). The research workflow itself is in test_workflow.py.
"""

import logging
from datetime import date

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.agent.llm import ModelError
from app.crossref.client import CrossrefError
from app.main import create_app
from tests.builders import fixture_result, mock_crossref_client, search_result, work
from tests.conftest import assert_address_absent, load_crossref_fixture, make_settings
from tests.fakes import FakeCrossrefClient, FakeModelClient

TODAY = date(2026, 10, 3)


def make_client(crossref, model=None, **settings) -> TestClient:
    app = create_app(
        make_settings(**settings),
        crossref=crossref,
        model=model or FakeModelClient(),
        clock=lambda: TODAY,
    )
    return TestClient(app)


def test_response_keeps_the_m2_fields_and_adds_the_m3_ones():
    fixture = load_crossref_fixture("works_llm_software_testing")

    res = make_client(FakeCrossrefClient(result=fixture_result())).post(
        "/api/ask", json={"question": fixture["query"]}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    for paper in body["papers"]:  # M2 fields still present on every paper
        assert {"doi", "title", "url", "crossref_rank"} <= set(paper)
        assert paper["url"] == f"https://doi.org/{paper['doi']}"
    assert body["search"]["total_results"] == fixture["body"]["message"]["total-results"]  # M2
    assert body["search"]["returned"] == 5
    assert {"question", "summary", "limitations", "trace"} <= set(body)  # new in M3
    assert body["question"] == fixture["query"]


def test_the_cleaned_question_is_what_the_model_receives():
    model = FakeModelClient()

    make_client(FakeCrossrefClient(result=fixture_result()), model).post(
        "/api/ask", json={"question": "  LLMs for\n\n software   testing "}
    )

    assert model.interpret_questions == ["LLMs for software testing"]


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
def test_invalid_questions_get_422_and_neither_the_model_nor_crossref_is_called(payload):
    crossref, model = FakeCrossrefClient(result=fixture_result()), FakeModelClient()

    res = make_client(crossref, model).post("/api/ask", json=payload)

    assert res.status_code == 422
    error = res.json()["error"]
    assert error["code"] == "invalid_input" and error["retryable"] is False and error["message"]
    assert set(res.json()) == {"error"}  # no trace: the request was never understood
    assert crossref.calls == [] and model.interpret_questions == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (CrossrefError("upstream_rate_limited", "Rate limited.", retryable=True), 503),
        (CrossrefError("upstream_unavailable", "Down.", retryable=True), 503),
        (CrossrefError("upstream_rejected", "Rejected.", retryable=False), 502),
        (CrossrefError("upstream_invalid_response", "Bad JSON.", retryable=False), 502),
    ],
)
def test_crossref_failures_return_the_error_envelope(error, status):
    res = make_client(FakeCrossrefClient(error=error)).post(
        "/api/ask", json={"question": "software testing"}
    )

    assert res.status_code == status
    body = res.json()
    assert body["error"] == {  # the error contract itself is unchanged
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    assert set(body) == {"error", "trace"}  # the trace is the only addition
    assert body["trace"]["failure"]["code"] == error.code


def test_an_unexpected_server_bug_returns_the_error_envelope_not_a_crash(caplog):
    class Exploding(FakeCrossrefClient):
        async def search_works(self, *args, **kwargs):
            raise RuntimeError("secret internal detail")

    app = create_app(
        make_settings(), crossref=Exploding(), model=FakeModelClient(), clock=lambda: TODAY
    )

    with caplog.at_level(logging.CRITICAL):
        res = TestClient(app, raise_server_exceptions=False).post(
            "/api/ask", json={"question": "software testing"}
        )

    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "internal_error",
            "message": "Something went wrong on the server.",
            "retryable": False,
        }
    }


def test_app_shutdown_closes_both_clients():
    crossref, model = FakeCrossrefClient(result=fixture_result()), FakeModelClient()

    with make_client(crossref, model):
        pass

    assert crossref.closed and model.closed


def test_without_a_model_key_the_app_still_answers_using_deterministic_fallbacks():
    # No FakeModelClient here: the real AnthropicModelClient has no key and says so.
    app = create_app(
        make_settings(), crossref=FakeCrossrefClient(result=fixture_result()), clock=lambda: TODAY
    )

    res = TestClient(app).post("/api/ask", json={"question": "LLMs for software testing"})

    body = res.json()
    assert res.status_code == 200 and body["status"] == "degraded"
    assert body["trace"]["fallbacks"][:2] == [
        "interpret: model_not_configured",
        "explain: model_not_configured",
    ]
    assert all(p["explanation_source"] == "metadata_only" for p in body["papers"])


# --- The configured contact address and credentials must not leak ------------------------------


def client_with_real_crossref(mailto, respond=None, **settings):
    crossref, seen = mock_crossref_client(mailto, respond)
    return make_client(crossref, **settings), seen


def test_unset_crossref_mailto_sends_no_email_address_to_crossref():
    client, seen = client_with_real_crossref(mailto=None)

    res = client.post("/api/ask", json={"question": "software testing"})

    assert res.status_code == 200
    assert "mailto" not in seen[0].url.params and "@" not in str(seen[0].url)
    assert all("@" not in value for value in seen[0].headers.values())
    assert seen[0].headers["user-agent"] == "paper-finder/0.1"
    assert "@" not in res.text


def test_configured_crossref_mailto_is_sent_only_in_user_agent_and_never_returned(caplog):
    address = "project-contact@example.org"
    client, seen = client_with_real_crossref(mailto=address, crossref_mailto=address)

    with caplog.at_level(logging.DEBUG):
        res = client.post("/api/ask", json={"question": "software testing"})

    request = seen[0]
    assert request.headers["user-agent"] == f"paper-finder/0.1 (mailto:{address})"
    assert "mailto" not in request.url.params
    assert_address_absent(str(request.url), address)
    assert [name for name, value in request.headers.items() if address in value] == ["user-agent"]
    assert res.status_code == 200
    assert_address_absent(res.text, address)
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
    client, _ = client_with_real_crossref(mailto=address, respond=handler, crossref_mailto=address)

    with caplog.at_level(logging.DEBUG):
        res = client.post("/api/ask", json={"question": "software testing"})

    assert res.status_code in (502, 503)
    assert_address_absent(res.text, address)
    assert_address_absent(caplog.text, address)


def test_model_errors_never_expose_credentials_or_provider_text():
    secret = "sk-ant-api03-SECRET-KEY"
    model = FakeModelClient(
        plan=ModelError("model_unavailable", "The model API returned an error.")
    )
    client = make_client(
        FakeCrossrefClient(result=search_result([work()])), model, anthropic_api_key=secret
    )

    res = client.post("/api/ask", json={"question": "software testing"})

    assert res.status_code == 200 and secret not in res.text
