"""API request/response models. These are the documented contracts of the HTTP API."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    # "not_checked" until the reading-list database is wired in.
    db: Literal["ok", "unavailable", "not_checked"]
    model_configured: bool
    crossref_mailto_configured: bool
