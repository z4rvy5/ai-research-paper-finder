"""scripts/smoke_test.py, run entirely offline against the in-process app (never a live service).

The script is what a reviewer or the developer runs against a real deployment, so it must pass on a
healthy app and fail on an unhealthy one.
"""

import httpx2
import pytest

from app.main import create_app
from scripts import smoke_test
from tests.builders import fixture_result, search_result, work
from tests.conftest import make_settings
from tests.fakes import FakeCrossrefClient, FakeModelClient, failing_repo

pytestmark = pytest.mark.anyio


def client_for(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")


def make_app(**kwargs):
    settings = make_settings(**kwargs.pop("settings", {}))
    model = kwargs.pop("model", FakeModelClient())
    crossref = kwargs.pop("crossref", FakeCrossrefClient(result=fixture_result()))
    return create_app(settings, crossref=crossref, model=model, **kwargs)


def failed(checks):
    return [check.name for check in checks if not check.ok]


async def test_a_healthy_app_passes_every_check():
    async with client_for(make_app()) as client:
        checks = await smoke_test.run_smoke(client)

    assert failed(checks) == [] and len(checks) >= 18


async def test_expect_model_passes_when_the_answer_was_written_by_the_model():
    async with client_for(make_app()) as client:
        checks = await smoke_test.run_smoke(client, expect_model=True)

    assert failed(checks) == ["health: a model key is configured"]  # the fake has no real key


async def test_expect_model_fails_when_the_answer_used_fallbacks():
    # The real model client with no key: every answer is "degraded".
    app = create_app(make_settings(), crossref=FakeCrossrefClient(result=fixture_result()))
    async with client_for(app) as client:
        checks = await smoke_test.run_smoke(client, expect_model=True)

    assert "ask: written by the model (no fallbacks)" in failed(checks)


async def test_expect_postgres_fails_on_a_sqlite_deployment():
    async with client_for(make_app()) as client:
        checks = await smoke_test.run_smoke(client, expect_postgres=True)

    assert failed(checks) == ["health: database is Postgres, not a throwaway SQLite file"]


async def test_a_broken_database_is_reported():
    async with client_for(make_app(repo=failing_repo())) as client:
        checks = await smoke_test.run_smoke(client)

    names = failed(checks)
    assert "health: process up and database answering" in names
    assert "reading list: save returns 201" in names


async def test_secret_looking_text_in_a_response_is_caught():
    leaky = FakeModelClient(
        explanations=lambda question, candidates: __import__(
            "app.schemas", fromlist=["x"]
        ).Explanations(
            items=[
                __import__("app.schemas", fromlist=["x"]).ExplanationItem(
                    ref=c.ref, explanation="May be relevant to password testing work."
                )
                for c in candidates
            ]
        )
    )
    async with client_for(make_app(model=leaky)) as client:
        checks = await smoke_test.run_smoke(client)

    assert failed(checks) == ["no secret-looking text in any response"]


async def test_an_unreachable_service_fails_cleanly_after_waiting(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(smoke_test.asyncio, "sleep", fake_sleep)

    def refuse(request):
        raise httpx2.ConnectError("no route", request=request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(refuse), base_url="http://down.example"
    ) as client:
        checks = await smoke_test.run_smoke(client)

    assert failed(checks) == ["service responds"] and len(sleeps) == 12  # waited for it to wake


async def test_the_script_finds_nothing_to_recommend_without_crashing():
    app = make_app(
        crossref=FakeCrossrefClient(result=search_result([work(doi="10.1/x", title="Index")]))
    )
    async with client_for(app) as client:
        checks = await smoke_test.run_smoke(client)

    assert "ask: 1-5 papers" in failed(checks)  # reported, not an exception
