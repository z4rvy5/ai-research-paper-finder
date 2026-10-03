import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(**overrides) -> Settings:
    """Settings isolated from the developer's environment and `.env` file."""
    defaults = {"anthropic_api_key": None, "crossref_mailto": None}
    return Settings(_env_file=None, **{**defaults, **overrides})


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings()))
