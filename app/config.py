"""Runtime configuration, read from environment variables (and `.env` in development)."""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Model provider. The key stays server-side; it is never sent to the browser.
    # Optional at startup so the app (and /api/health) can run before credentials are set.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_effort: Literal["low", "medium", "high"] = "low"

    # Crossref polite pool: the contact address is sent as `mailto` and in the User-Agent.
    crossref_mailto: str | None = None

    database_url: str = "sqlite:///./data/app.db"
    max_daily_model_calls: int = 500
