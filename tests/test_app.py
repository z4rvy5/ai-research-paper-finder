from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import make_settings


def test_health_reports_ok_and_unconfigured_credentials(client: TestClient):
    res = client.get("/api/health")

    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "db": "ok",  # a real check since M4 (was a placeholder)
        "db_backend": "sqlite",
        "model_configured": False,
        "crossref_mailto_configured": False,
    }


def test_health_reports_configured_credentials_without_exposing_them():
    settings = make_settings(anthropic_api_key="sk-test-secret", crossref_mailto="me@example.org")
    client = TestClient(create_app(settings))

    res = client.get("/api/health")

    assert res.json()["model_configured"] is True
    assert res.json()["crossref_mailto_configured"] is True
    assert "sk-test-secret" not in res.text
    assert "me@example.org" not in res.text


def test_index_page_is_served(client: TestClient):
    res = client.get("/")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert '<form id="ask-form">' in res.text


def test_static_assets_are_served(client: TestClient):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_default_model_is_sonnet_with_explicit_low_effort():
    settings = make_settings()

    assert settings.anthropic_model == "claude-sonnet-5-5"
    assert settings.anthropic_effort == "low"


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("CROSSREF_MAILTO", "dev@example.org")

    settings = Settings(_env_file=None)

    assert settings.anthropic_model == "claude-opus-5-5"
    assert settings.crossref_mailto == "dev@example.org"


def test_health_treats_a_blank_model_key_as_not_configured():
    client = TestClient(create_app(make_settings(anthropic_api_key="  ")))

    assert client.get("/api/health").json()["model_configured"] is False
