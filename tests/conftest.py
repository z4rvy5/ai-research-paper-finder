import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeCrossrefClient

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def make_settings(**overrides) -> Settings:
    """Settings isolated from the developer's environment and `.env` file."""
    defaults = {"anthropic_api_key": None, "crossref_mailto": None}
    return Settings(_env_file=None, **{**defaults, **overrides})


def load_crossref_fixture(name: str) -> dict[str, Any]:
    """A captured Crossref response: {"query", "headers", "body"}.

    Fixtures are recorded by scripts/capture_fixtures.py.
    """
    path = FIXTURE_DIR / "crossref" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def assert_address_absent(text: str, address: str) -> None:
    """Fail if `address` appears in `text` raw or URL-encoded (httpx writes '@' as '%40')."""
    for form in (address, quote(address, safe="")):
        assert form not in text, f"contact address leaked as {form!r}"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings(), crossref=FakeCrossrefClient()))
