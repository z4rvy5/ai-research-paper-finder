"""Security headers, the error shape for unknown routes, the ask rate limit and the daily cap."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import CONTENT_SECURITY_POLICY, create_app
from tests.builders import fixture_result
from tests.conftest import make_settings
from tests.fakes import FakeCrossrefClient, FakeModelClient

STATIC = Path(__file__).parent.parent / "app" / "static"
QUESTION = {"question": "LLMs for software testing"}


def make_client(model=None, crossref=None, **settings) -> TestClient:
    app = create_app(
        make_settings(**settings),
        crossref=crossref or FakeCrossrefClient(result=fixture_result()),
        model=model or FakeModelClient(),
    )
    return TestClient(app)


# --- Security headers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/"),
        ("get", "/static/app.js"),
        ("get", "/api/health"),
        ("post", "/api/ask"),
        ("get", "/api/no-such-route"),
        ("get", "/api/reading-list"),
    ],
)
def test_every_response_carries_the_security_headers(method, path):
    client = make_client()

    res = getattr(client, method)(path, **({"json": QUESTION} if method == "post" else {}))

    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_the_policy_blocks_everything_but_our_own_origin_and_framing():
    policy = dict(part.strip().split(" ", 1) for part in CONTENT_SECURITY_POLICY.split(";"))

    assert policy["default-src"] == "'self'"  # no third-party scripts, styles, images or fetches
    assert policy["frame-ancestors"] == "'none'" and policy["base-uri"] == "'none'"
    assert (
        "unsafe-inline" not in CONTENT_SECURITY_POLICY
        and "unsafe-eval" not in CONTENT_SECURITY_POLICY
    )


class PageAudit(HTMLParser):
    """Collects everything a `default-src 'self'` policy would block in our page."""

    def __init__(self):
        super().__init__()
        self.inline_scripts, self.inline_styles, self.event_handlers, self.external = 0, 0, [], []
        self._in_script = self._in_style = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self._in_script = True
            if "src" not in attributes:
                self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        self.event_handlers += [name for name in attributes if name.startswith("on")]
        if "style" in attributes:
            self.inline_styles += 1
        for name in ("src", "href"):
            value = attributes.get(name) or ""
            if value.startswith(("http:", "https:", "//")) and tag != "a":
                self.external.append(value)

    def handle_endtag(self, tag):
        self._in_script = self._in_script and tag != "script"


def test_the_page_works_under_the_policy_no_inline_scripts_styles_handlers_or_cdn_assets():
    audit = PageAudit()
    audit.feed((STATIC / "index.html").read_text(encoding="utf-8"))

    assert audit.inline_scripts == 0 and audit.inline_styles == 0
    assert audit.event_handlers == [] and audit.external == []


def test_the_scripts_never_assign_markup_or_inline_styles():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "setAttribute(",
    ):
        assert forbidden not in code, forbidden
    assert ".style." not in code and ".cssText" not in code  # CSP style-src would block them


def test_the_interactive_docs_are_exempt_from_the_csp_but_keep_the_other_headers():
    res = make_client().get("/docs")

    assert res.status_code == 200
    assert "content-security-policy" not in res.headers
    assert res.headers["x-content-type-options"] == "nosniff"


# --- Unknown routes use the error envelope ------------------------------------------------


@pytest.mark.parametrize("path", ["/api/no-such-route", "/static/missing.js", "/nope"])
def test_unknown_paths_return_the_standard_error_envelope(path):
    res = make_client().get(path)

    assert res.status_code == 404
    assert res.json() == {
        "error": {"code": "not_found", "message": "Not found.", "retryable": False}
    }


def test_a_wrong_method_is_a_405_in_the_error_envelope_and_keeps_the_allow_header():
    res = make_client().get("/api/ask")

    assert res.status_code == 405
    assert res.json()["error"]["code"] == "method_not_allowed"
    assert "POST" in res.headers["allow"]


# --- Rate limit on /api/ask ---------------------------------------------------------------


def test_asks_beyond_the_per_minute_limit_get_a_429_with_retry_after_and_no_work_is_done():
    model = FakeModelClient()
    client = make_client(model, ask_rate_limit_per_minute=3)

    statuses = [client.post("/api/ask", json=QUESTION).status_code for _ in range(3)]
    res = client.post("/api/ask", json=QUESTION)

    assert statuses == [200, 200, 200] and res.status_code == 429
    assert (
        res.json()["error"]["code"] == "rate_limited" and res.json()["error"]["retryable"] is True
    )
    assert set(res.json()) == {"error"}
    assert 1 <= int(res.headers["retry-after"]) <= 60
    assert len(model.interpret_questions) == 3  # the blocked request never reached the model


def test_clients_are_limited_separately_by_their_forwarded_address():
    client = make_client(ask_rate_limit_per_minute=1)

    def ask_as(address):
        return client.post(
            "/api/ask", json=QUESTION, headers={"X-Forwarded-For": address}
        ).status_code

    assert ask_as("203.0.113.1") == 200
    assert ask_as("203.0.113.1") == 429  # same client
    assert ask_as("203.0.113.2") == 200  # a different client is unaffected


def test_a_forged_forwarded_for_prefix_does_not_bypass_the_limit():
    # Our proxy appends the real address LAST; anything a client prepends comes earlier.
    client = make_client(ask_rate_limit_per_minute=2)

    statuses = [
        client.post(
            "/api/ask", json=QUESTION, headers={"X-Forwarded-For": f"10.9.8.{n}, 198.51.100.7"}
        ).status_code
        for n in range(4)
    ]

    assert statuses == [200, 200, 429, 429]


def test_invalid_questions_count_towards_the_limit():
    client = make_client(ask_rate_limit_per_minute=2)

    bad = [client.post("/api/ask", json={"question": ""}).status_code for _ in range(2)]

    assert bad == [422, 422] and client.post("/api/ask", json=QUESTION).status_code == 429


def test_only_ask_is_rate_limited():
    client = make_client(ask_rate_limit_per_minute=1)
    client.post("/api/ask", json=QUESTION)

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_a_limit_of_zero_disables_rate_limiting():
    client = make_client(ask_rate_limit_per_minute=0)

    assert all(client.post("/api/ask", json=QUESTION).status_code == 200 for _ in range(30))


# --- Daily model-call cap -----------------------------------------------------------------


def test_after_the_daily_cap_answers_continue_using_deterministic_fallbacks():
    model = FakeModelClient()
    client = make_client(model, max_daily_model_calls=2, ask_rate_limit_per_minute=0)

    first = client.post("/api/ask", json=QUESTION).json()  # spends both calls (interpret + explain)
    second = client.post("/api/ask", json=QUESTION).json()

    assert first["status"] == "ok"
    assert second["status"] == "degraded"
    assert second["trace"]["fallbacks"] == [
        "interpret: model_daily_limit",
        "explain: model_daily_limit",
    ]
    assert second["trace"]["interpretation"]["source"] == "fallback"
    assert len(second["papers"]) >= 1 and all(
        p["explanation_source"] == "metadata_only" for p in second["papers"]
    )
    assert len(model.interpret_questions) == 1 and len(model.explain_calls) == 1  # no more calls


def test_a_zero_daily_cap_never_calls_the_model_but_still_answers():
    model = FakeModelClient()

    res = make_client(model, max_daily_model_calls=0).post("/api/ask", json=QUESTION)

    assert res.status_code == 200 and res.json()["status"] == "degraded"
    assert model.interpret_questions == [] and model.explain_calls == []
    assert all(p["doi"] for p in res.json()["papers"])  # real Crossref papers, no model involved
