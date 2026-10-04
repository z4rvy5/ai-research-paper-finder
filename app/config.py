"""Runtime configuration, read from environment variables (and `.env` in development)."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Model provider. The key stays server-side; it is never sent to the browser.
    # Optional at startup so the app (and /api/health) can run before credentials are set.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_effort: Literal["low", "medium", "high"] = "low"

    # Optional Crossref polite-pool contact address. If set, it is sent in the User-Agent header
    # only (never in URLs); if unset, no address is sent.
    crossref_mailto: str | None = None

    database_url: str = DEFAULT_DATABASE_URL
    # Cost and abuse limits for a public demo (in-memory, per process; see app/limits.py).
    # When the daily model-call cap is reached the app keeps answering with its deterministic
    # fallbacks instead of calling the model. 0 means "never call the model".
    max_daily_model_calls: int = 500
    # Questions per client address per minute; 0 disables the limit.
    ask_rate_limit_per_minute: int = 20
    # Reading-list saves per client address per minute (its own limit, separate from asks), and
    # the most papers one client id may keep saved. 0 disables either.
    save_rate_limit_per_minute: int = 20
    max_saved_per_client: int = 200

    @field_validator("database_url", mode="before")
    @classmethod
    def _blank_database_url_means_the_default(cls, value: object) -> object:
        """A blank DATABASE_URL (a copied `.env.example`, an empty dashboard value) falls back to
        the default, as a blank model key is treated as unset."""
        if isinstance(value, str) and not value.strip():
            return DEFAULT_DATABASE_URL
        return value

    @property
    def model_api_key(self) -> str | None:
        """The model API key, or None when unset or blank (a copied `.env.example` is blank)."""
        if self.anthropic_api_key is None:
            return None
        return self.anthropic_api_key.get_secret_value().strip() or None
