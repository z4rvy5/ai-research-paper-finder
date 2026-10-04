"""API request/response models. These are the documented contracts of the HTTP API."""

import re
import unicodedata
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.crossref.client import RateLimitInfo

QUESTION_MIN_CHARS = 3
QUESTION_MAX_CHARS = 500
MAX_DOI_CHARS = 255

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)
_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")


def normalize_doi(value: str) -> str:
    """A canonical (lowercase, prefix-free) DOI, or ValueError. Used for every DOI we accept."""
    text = value.strip()
    for prefix in _DOI_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = text.lower()
    if (
        len(text) > MAX_DOI_CHARS
        or not _DOI_RE.fullmatch(text)
        or any(unicodedata.category(ch) == "Cc" for ch in text)
    ):
        raise ValueError("Enter a DOI such as 10.1000/xyz123.")
    return text


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool


class ErrorResponse(BaseModel):
    error: ErrorBody
    # Present only when an upstream failure happened after the request was understood, so the
    # caller can still see what was attempted. Absent for validation and unexpected errors.
    trace: "Trace | None" = None


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
    authors: list[str] = []  # display names; empty when Crossref lists none
    year: int | None = None
    venue: str | None = None  # first `container-title`
    abstract: str | None = None  # plain text extracted from Crossref's JATS XML, truncated
    work_type: str | None = None  # Crossref `type`, e.g. "journal-article"
    missing_fields: list[str] = []  # subset of: title, authors, year, abstract


EvidenceBasis = Literal["title_only", "title_and_abstract"]


class RecommendedPaper(Paper):
    """A selected paper plus its explanation. All bibliographic fields are Crossref's."""

    explanation: str
    explanation_source: Literal["model", "metadata_only"]
    evidence_basis: EvidenceBasis  # decided by code from whether Crossref supplied an abstract


# ---- Model boundary types (structured outputs) -------------------------------------------------
# The API's structured outputs don't support length/count/range constraints, so these schemas stay
# tolerant and the code in app/agent/plan.py enforces the bounds deterministically.


class WorkType(StrEnum):
    """Crossref work types a plan may ask for (all valid values of Crossref's `type` filter)."""

    JOURNAL_ARTICLE = "journal-article"
    PROCEEDINGS_ARTICLE = "proceedings-article"
    POSTED_CONTENT = "posted-content"  # preprints
    BOOK_CHAPTER = "book-chapter"
    BOOK = "book"
    DISSERTATION = "dissertation"


class SearchPlan(BaseModel):
    """The model's interpretation of a question. It cannot express URLs, filters or row counts."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["find_papers", "out_of_scope", "fabrication_request"]
    topic_query: str  # short keyword phrase to search, not a sentence of instructions
    alt_queries: list[str] = []  # at most 2 are used; for one bounded refinement search
    from_year: int | None = None
    until_year: int | None = None
    work_types: list[WorkType] = []
    recency_requested: bool = False
    ambiguities: list[str] = []


class ExplanationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: Literal["P1", "P2", "P3", "P4", "P5"]  # a slot, never a DOI or title
    explanation: str


class Explanations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExplanationItem]


class Candidate(BaseModel):
    """What the explanation call is allowed to see about a selected paper."""

    ref: str
    title: str
    year: int | None
    venue: str | None
    abstract: str | None
    evidence_basis: EvidenceBasis


# ---- Trace (returned in the response; never persisted) ----------------------------------------


class SavePaperRequest(BaseModel):
    """Saving takes ONLY a DOI. The server looks the metadata up itself, so a client can never
    store bibliographic data of its own choosing."""

    model_config = ConfigDict(extra="forbid")

    doi: str = Field(max_length=2 * MAX_DOI_CHARS)  # raw cap before normalization

    @field_validator("doi")
    @classmethod
    def clean_doi(cls, value: str) -> str:
        return normalize_doi(value)


class SavedPaper(BaseModel):
    """A paper in a reading list: Crossref metadata as stored when it was saved."""

    doi: str
    title: str | None
    url: str
    authors: list[str]
    year: int | None
    venue: str | None
    abstract: str | None
    work_type: str | None
    missing_fields: list[str]
    saved_at: str  # ISO 8601, UTC


class ReadingListResponse(BaseModel):
    items: list[SavedPaper]


class SearchInfo(BaseModel):
    """One Crossref request and what came back. The URL has no contact address in it."""

    purpose: Literal["primary", "refinement"] = "primary"
    query: str
    filters: dict[str, Any] = {}  # from_year, until_year, types actually sent
    rows: int | None = None
    url: str
    http_status: int
    total_results: int
    returned: int
    rate_limit: RateLimitInfo
    retries: int = 0  # transient Crossref failures retried before this request succeeded


class TraceStep(BaseModel):
    name: Literal["interpret", "search_papers", "filter_papers", "explain", "ground"]
    status: Literal["ok", "degraded", "error", "skipped"]
    duration_ms: int
    summary: str


class Interpretation(BaseModel):
    # "precheck": a deterministic rule recognised the request before any model or search call.
    source: Literal["model", "fallback", "precheck"]
    plan: SearchPlan  # the effective plan, after deterministic validation
    assumptions: list[str] = []  # things code decided, e.g. the window for "recent"
    adjustments: list[str] = []  # things code corrected or dropped from the model's plan


class Counts(BaseModel):
    returned: int  # records Crossref returned, across all searches
    surviving: int  # records left after filtering and de-duplication
    selected: int  # records presented


class Removal(BaseModel):
    doi: str | None
    title: str | None
    reason: str


class Merge(BaseModel):
    kept: str
    dropped: str
    reason: str


class ShortlistEntry(BaseModel):
    ref: str
    doi: str
    term_match: bool


class FilteringInfo(BaseModel):
    removed: list[Removal] = []
    duplicates_merged: list[Merge] = []
    ordering_rule: str = ""
    shortlisted: list[ShortlistEntry] = []


class GroundingInfo(BaseModel):
    rejected_refs: list[str] = []  # model explanation items that didn't match a selected paper
    explanation_rewrites: list[Removal] = []  # model text replaced by a deterministic fallback


class Failure(BaseModel):
    """Why no recommendations could be produced. Only safe, fixed-text fields."""

    stage: Literal["search_papers"]
    code: str  # the same stable code as the error body, e.g. "upstream_rate_limited"
    message: str
    retryable: bool
    http_status: int | None  # Crossref's status when it answered; None for network failures
    query: str  # the search terms that were being sent
    filters: dict[str, Any] = {}  # from_year, until_year, types
    rate_limit: RateLimitInfo | None = None  # Crossref's rate-limit headers, when it answered
    retries: int = 0  # retries used before giving up


class Trace(BaseModel):
    request_id: str
    duration_ms: int
    model: str | None  # the configured model id; never a credential
    interpretation: Interpretation
    searches: list[SearchInfo] = []
    steps: list[TraceStep] = []
    counts: Counts
    filtering: FilteringInfo = FilteringInfo()
    grounding: GroundingInfo = GroundingInfo()
    fallbacks: list[str] = []  # each stage that fell back or failed, with the reason
    limitations: list[str] = []
    failure: Failure | None = None  # set only on an error response


class AskResponse(BaseModel):
    """`ok`: model-written explanations. `degraded`: some stage used a deterministic fallback.
    `no_results`: nothing survived. `refused`: the request wasn't a search for papers."""

    status: Literal["ok", "degraded", "no_results", "refused"]
    question: str
    summary: str  # deterministic one-liner: counts, not model text
    papers: list[RecommendedPaper]
    search: SearchInfo | None = None  # the primary Crossref request (kept from M2)
    limitations: list[str] = []
    trace: Trace


class HealthResponse(BaseModel):
    status: Literal["ok"]
    # "not_checked" until the reading-list database is wired in.
    db: Literal["ok", "unavailable", "not_checked"]
    # Which database is configured. A deployment check can confirm it is "postgresql" and not a
    # throwaway SQLite file on an ephemeral disk.
    db_backend: Literal["sqlite", "postgresql"]
    model_configured: bool
    crossref_mailto_configured: bool


# ErrorResponse refers to Trace, which is defined after it.
ErrorResponse.model_rebuild()
