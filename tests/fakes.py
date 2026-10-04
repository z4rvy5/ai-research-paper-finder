"""Test doubles for the app's two external boundaries: Crossref and the model."""

from collections.abc import Callable, Sequence
from typing import Any

import psycopg
from sqlalchemy import create_engine

from app.crossref.client import DEFAULT_ROWS, CrossrefError, SearchResult
from app.schemas import Candidate, ExplanationItem, Explanations, SearchPlan
from app.storage.reading_list import ReadingListRepo, normalize_database_url

DEFAULT_PLAN = SearchPlan(
    intent="find_papers", topic_query="large language models software testing"
)


class FakeCrossrefClient:
    """Implements `PaperSearch`.

    Give it one `result` (returned for every call), one `error`, or a `responses` list that is
    consumed in order (the last entry repeats). Every call is recorded in `calls`.
    """

    def __init__(
        self,
        result: SearchResult | None = None,
        error: CrossrefError | None = None,
        responses: Sequence[SearchResult | CrossrefError] | None = None,
        works: dict[str, dict[str, Any] | CrossrefError] | None = None,
    ):
        self.works = works or {}  # doi -> raw Crossref record (or an error) for get_work
        self.get_work_calls: list[str] = []
        self._responses = (
            list(responses) if responses else ([error or result] if (error or result) else [])
        )
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    @property
    def queries(self) -> list[str]:
        return [call["query"] for call in self.calls]

    async def search_works(
        self,
        query: str,
        rows: int = DEFAULT_ROWS,
        *,
        from_year: int | None = None,
        until_year: int | None = None,
        types: Sequence[str] = (),
    ) -> SearchResult:
        self.calls.append(
            {
                "query": query,
                "rows": rows,
                "from_year": from_year,
                "until_year": until_year,
                "types": list(types),
            }
        )
        assert self._responses, "FakeCrossrefClient needs a result, an error or responses"
        response = self._responses[min(len(self.calls), len(self._responses)) - 1]
        if isinstance(response, CrossrefError):
            raise response
        return response

    async def get_work(self, doi: str) -> dict[str, Any] | None:
        self.get_work_calls.append(doi)
        outcome = self.works.get(doi)  # a missing DOI is a Crossref 404
        if isinstance(outcome, CrossrefError):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


def default_explanations(question: str, candidates: Sequence[Candidate]) -> Explanations:
    """A well-behaved model: one modest, supported sentence per candidate."""
    return Explanations(
        items=[
            ExplanationItem(
                ref=candidate.ref,  # type: ignore[arg-type]
                explanation=(
                    "May be relevant to your question based on its title only."
                    if candidate.evidence_basis == "title_only"
                    else "May be relevant to your question based on its title and abstract."
                ),
            )
            for candidate in candidates
        ]
    )


class FakeModelClient:
    """Implements `ModelClient` with scripted behavior.

    `plan` and `explanations` may each be a value, an Exception instance (raised when called),
    or (for explanations) a callable `(question, candidates) -> Explanations`.
    """

    def __init__(
        self,
        plan: SearchPlan | Exception | None = None,
        explanations: Explanations | Exception | Callable[..., Explanations] | None = None,
    ):
        self._plan = DEFAULT_PLAN if plan is None else plan
        self._explanations = default_explanations if explanations is None else explanations
        self.interpret_questions: list[str] = []
        self.explain_calls: list[tuple[str, list[Candidate]]] = []
        self.closed = False

    async def interpret(self, question: str) -> SearchPlan:
        self.interpret_questions.append(question)
        if isinstance(self._plan, Exception):
            raise self._plan
        return self._plan

    async def explain(self, question: str, candidates: Sequence[Candidate]) -> Explanations:
        self.explain_calls.append((question, list(candidates)))
        if isinstance(self._explanations, Exception):
            raise self._explanations
        if callable(self._explanations):
            return self._explanations(question, candidates)
        return self._explanations

    async def aclose(self) -> None:
        self.closed = True


UNREACHABLE_DATABASE_URL = (
    "postgresql://appuser:pa55w0rd-TOPSECRET@127.0.0.1:1/neondb?sslmode=require"
)
DATABASE_SECRETS = ("pa55w0rd", "TOPSECRET", "appuser", "127.0.0.1", "neondb")


def failing_repo() -> ReadingListRepo:
    """A repository whose driver fails instantly with a message full of connection details,
    like a real driver error (host, user, password) would be."""

    def refuse():
        raise psycopg.OperationalError(
            'connection to server at "127.0.0.1", port 1 failed: FATAL: password authentication '
            'failed for user "appuser" (pa55w0rd-TOPSECRET) database "neondb"'
        )

    return ReadingListRepo(
        create_engine(normalize_database_url(UNREACHABLE_DATABASE_URL), creator=refuse)
    )
