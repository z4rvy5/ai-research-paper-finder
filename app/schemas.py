"""API request/response models. These are the documented contracts of the HTTP API."""

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crossref.client import RateLimitInfo

QUESTION_MIN_CHARS = 3
QUESTION_MAX_CHARS = 500


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool


class ErrorResponse(BaseModel):
    error: ErrorBody


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The raw cap rejects oversized payloads before any processing.
    question: str = Field(max_length=4 * QUESTION_MAX_CHARS)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        # Collapse all whitespace (including newlines from the textarea) to single spaces.
        value = " ".join(value.split())
        if any(unicodedata.category(ch) == "Cc" for ch in value):
            raise ValueError("Question must not contain control characters.")
        if not QUESTION_MIN_CHARS <= len(value) <= QUESTION_MAX_CHARS:
            raise ValueError(
                f"Question must be {QUESTION_MIN_CHARS}-{QUESTION_MAX_CHARS} characters long."
            )
        return value


class Paper(BaseModel):
    """A work normalized from a Crossref record. Every field comes from Crossref data."""

    doi: str  # lowercased
    title: str | None
    url: str  # https://doi.org/<doi>, built from the DOI
    crossref_rank: int  # 1-based position in Crossref's relevance-ordered results


class SearchInfo(BaseModel):
    """What was sent to Crossref and what came back. Becomes a trace step in milestone 7."""

    query: str
    url: str  # the request URL; the contact address is never in it
    http_status: int
    total_results: int
    returned: int
    rate_limit: RateLimitInfo


class AskResponse(BaseModel):
    status: Literal["ok", "no_results"]
    papers: list[Paper]
    search: SearchInfo


class HealthResponse(BaseModel):
    status: Literal["ok"]
    # "not_checked" until the reading-list database is wired in.
    db: Literal["ok", "unavailable", "not_checked"]
    model_configured: bool
    crossref_mailto_configured: bool
