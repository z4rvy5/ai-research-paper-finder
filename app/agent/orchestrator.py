"""The research workflow, as plain deterministic code (not an autonomous tool loop).

  pre-check (code) -> interpret (model) -> search Crossref -> [one refinement search if too few] ->
  filter / de-duplicate / order (code) -> explain (model) -> ground (code) -> response + trace

An obvious request to invent papers is refused by the pre-check before any model or Crossref call.

The model decides only how to read the question and how to word explanations. Code decides every
Crossref call, every filter, which papers are presented, and every bibliographic field shown.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from uuid import uuid4

from app.agent.grounding import ground_explanations, to_candidate
from app.agent.llm import ModelClient, ModelError
from app.agent.plan import (
    SEARCH_ROWS,
    effective_plan,
    fabrication_request_reason,
    fallback_plan,
    topic_terms,
)
from app.agent.ranking import PRESENT_LIMIT, Selection, select_papers
from app.crossref.client import CrossrefError, PaperSearch, SearchResult
from app.crossref.normalize import normalize_works
from app.schemas import (
    AskResponse,
    Counts,
    Failure,
    FilteringInfo,
    GroundingInfo,
    Interpretation,
    Paper,
    RecommendedPaper,
    Removal,
    SearchInfo,
    SearchPlan,
    ShortlistEntry,
    Trace,
    TraceStep,
)

log = logging.getLogger(__name__)

MIN_PRESENTED = 3  # fewer survivors than this triggers the one refinement search

METADATA_LIMITATION = (
    "Relevance is judged from Crossref metadata only (the title and, when Crossref has one, the "
    "abstract). Crossref metadata is deposited by publishers and can be incomplete or wrong, and "
    "this is not a check of what any paper actually concludes."
)
REFUSED_MESSAGES = {
    "fabrication_request": (
        "I can only recommend papers that Crossref actually returns, so I did not invent any. "
        "Describe the topic you want papers about and I will search for it."
    ),
    "out_of_scope": (
        "This doesn't look like a request to find scholarly papers, so no search was run. "
        "Try asking about a research topic."
    ),
}


class SearchFailed(Exception):
    """The primary Crossref search failed. Carries the error and a safe trace of what was tried."""

    def __init__(self, error: CrossrefError, trace: Trace):
        super().__init__(error.message)
        self.error = error
        self.trace = trace


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


@dataclass
class _Run:
    """Accumulates the trace while a request is processed."""

    question: str
    request_id: str = field(default_factory=lambda: uuid4().hex[:12])
    started: float = field(default_factory=time.perf_counter)
    steps: list[TraceStep] = field(default_factory=list)
    searches: list[SearchInfo] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    returned: int = 0  # raw Crossref records across all searches

    def step(self, name: str, status: str, started: float, summary: str) -> None:
        self.steps.append(
            TraceStep(name=name, status=status, duration_ms=_ms(started), summary=summary)
        )


class Orchestrator:
    def __init__(
        self,
        *,
        model: ModelClient,
        crossref: PaperSearch,
        model_name: str | None = None,
        clock: Callable[[], date] = date.today,
    ):
        self._model = model
        self._crossref = crossref
        self._model_name = model_name
        self._clock = clock

    async def ask(self, question: str) -> AskResponse:
        run = _Run(question)
        if reason := fabrication_request_reason(question):
            return self._precheck_refusal(run, reason)  # before any model or Crossref call
        current_year = self._clock().year

        plan, source, adjustments, assumptions = await self._interpret(run, current_year)
        interpretation = Interpretation(
            source=source, plan=plan, assumptions=assumptions, adjustments=adjustments
        )
        if plan.intent != "find_papers":
            run.step("search_papers", "skipped", time.perf_counter(), f"not run: {plan.intent}")
            return self._response(
                run,
                "refused",
                interpretation,
                [],
                None,
                GroundingInfo(),
                REFUSED_MESSAGES[plan.intent],
            )

        # Search. A Crossref failure on the primary search raises SearchFailed, which the HTTP
        # layer turns into an error response that still carries the trace (nothing to recommend).
        terms = topic_terms(plan.topic_query)
        search_started = time.perf_counter()
        try:
            papers, no_doi = await self._search(run, plan, plan.topic_query, "primary", 0)
        except CrossrefError as exc:
            raise self._search_failed(run, interpretation, plan, exc, search_started) from exc
        filter_seconds = 0.0  # time spent in select_papers only, not in the refinement search
        started = time.perf_counter()
        selection = select_papers(papers, plan, terms)
        filter_seconds += time.perf_counter() - started

        if len(selection.survivors) < MIN_PRESENTED and plan.alt_queries:
            alt = plan.alt_queries[0]
            extra = await self._refine(run, plan, alt, rank_offset=run.returned)
            if extra is not None:
                more_papers, more_no_doi = extra
                papers, no_doi = papers + more_papers, no_doi + more_no_doi
                terms = list(dict.fromkeys(terms + topic_terms(alt)))
                started = time.perf_counter()
                selection = select_papers(papers, plan, terms)
                filter_seconds += time.perf_counter() - started

        started = time.perf_counter() - filter_seconds  # so the step reports the filtering time
        selection.removed.extend(
            Removal(doi=None, title=None, reason="no_doi") for _ in range(no_doi)
        )
        run.step(
            "filter_papers",
            "ok",
            started,
            f"{run.returned} returned -> {len(selection.survivors)} remain "
            f"({len(selection.removed)} removed, {len(selection.merged)} duplicates merged) "
            f"-> presenting {len(selection.selected)}",
        )

        if not selection.selected:
            run.step("explain", "skipped", time.perf_counter(), "no papers to explain")
            return self._response(
                run,
                "no_results",
                interpretation,
                [],
                selection,
                GroundingInfo(),
            )

        presented, grounding = await self._explain(run, selection, terms)
        status = "degraded" if run.fallbacks else "ok"
        return self._response(run, status, interpretation, presented, selection, grounding)

    # ---- stages ---------------------------------------------------------------------------

    async def _interpret(
        self, run: _Run, current_year: int
    ) -> tuple[SearchPlan, str, list[str], list[str]]:
        started = time.perf_counter()
        try:
            raw_plan = await self._model.interpret(run.question)
            plan, adjustments, assumptions = effective_plan(raw_plan, current_year)
            if plan.intent == "find_papers" and not plan.topic_query:
                raise ModelError("model_output_invalid", "The plan had no usable search query.")
        except ModelError as exc:
            plan, adjustments, assumptions = effective_plan(
                fallback_plan(run.question), current_year
            )
            run.fallbacks.append(f"interpret: {exc.code}")
            run.step(
                "interpret",
                "degraded",
                started,
                f"model failed ({exc.code}); used keywords from the question instead",
            )
            return plan, "fallback", adjustments, assumptions
        run.step("interpret", "ok", started, f"intent={plan.intent}, query={plan.topic_query!r}")
        return plan, "model", adjustments, assumptions

    async def _search(
        self, run: _Run, plan: SearchPlan, query: str, purpose: str, rank_offset: int
    ) -> tuple[list[Paper], int]:
        started = time.perf_counter()
        filters = self._filters(plan)
        result: SearchResult = await self._crossref.search_works(
            query,
            SEARCH_ROWS,
            from_year=plan.from_year,
            until_year=plan.until_year,
            types=filters["types"],
        )
        run.returned += len(result.items)
        run.searches.append(
            SearchInfo(
                purpose=purpose,
                query=query,
                filters=filters,
                rows=SEARCH_ROWS,
                url=result.url,
                http_status=result.http_status,
                total_results=result.total_results,
                returned=len(result.items),
                rate_limit=result.rate_limit,
                retries=result.retries,
            )
        )
        papers = normalize_works(result.items, rank_offset)
        run.step(
            "search_papers",
            "ok",
            started,
            f"{purpose}: {len(result.items)} of {result.total_results:,} Crossref matches",
        )
        return papers, len(result.items) - len(papers)

    async def _refine(
        self, run: _Run, plan: SearchPlan, query: str, rank_offset: int
    ) -> tuple[list[Paper], int] | None:
        """One bounded second search. If it fails, the primary results are still used."""
        try:
            return await self._search(run, plan, query, "refinement", rank_offset)
        except CrossrefError as exc:
            run.fallbacks.append(f"search_papers (refinement): {exc.code}")
            run.step(
                "search_papers", "error", time.perf_counter(), f"refinement failed: {exc.code}"
            )
            return None

    async def _explain(
        self, run: _Run, selection: Selection, terms: list[str]
    ) -> tuple[list[RecommendedPaper], GroundingInfo]:
        started = time.perf_counter()
        candidates = [
            to_candidate(paper, f"P{index}") for index, paper in enumerate(selection.selected, 1)
        ]
        explanations = None
        try:
            explanations = await self._model.explain(run.question, candidates)
            run.step(
                "explain", "ok", started, f"model wrote explanations for {len(candidates)} papers"
            )
        except ModelError as exc:
            run.fallbacks.append(f"explain: {exc.code}")
            run.step("explain", "degraded", started, f"model failed ({exc.code})")

        started = time.perf_counter()
        presented, grounding = ground_explanations(selection.selected, explanations, terms)
        replaced = sum(1 for paper in presented if paper.explanation_source == "metadata_only")
        if explanations is not None and replaced:
            run.fallbacks.append(
                f"explain: {replaced} model explanation(s) replaced by metadata-only text"
            )
        run.step(
            "ground",
            "degraded" if replaced else "ok",
            started,
            f"{len(presented) - replaced} model explanations accepted, {replaced} replaced, "
            f"{len(grounding.rejected_refs)} items rejected",
        )
        return presented, grounding

    @staticmethod
    def _filters(plan: SearchPlan) -> dict[str, object]:
        return {
            "from_year": plan.from_year,
            "until_year": plan.until_year,
            "types": [work_type.value for work_type in plan.work_types],
        }

    def _precheck_refusal(self, run: _Run, reason: str) -> AskResponse:
        started = time.perf_counter()
        interpretation = Interpretation(
            source="precheck", plan=SearchPlan(intent="fabrication_request", topic_query="")
        )
        run.step(
            "interpret",
            "skipped",
            started,
            f"not run: a deterministic rule recognised the request first ({reason})",
        )
        run.step("search_papers", "skipped", started, "not run: no search was made")
        return self._response(
            run,
            "refused",
            interpretation,
            [],
            None,
            GroundingInfo(),
            REFUSED_MESSAGES["fabrication_request"],
        )

    def _search_failed(
        self,
        run: _Run,
        interpretation: Interpretation,
        plan: SearchPlan,
        exc: CrossrefError,
        started: float,
    ) -> SearchFailed:
        """A minimal trace for a failed primary search: fixed-text fields only."""
        http = f"HTTP {exc.status}" if exc.status else "no response"
        run.step("search_papers", "error", started, f"primary: failed - {exc.code} ({http})")
        run.limitations.append(
            f"Crossref could not be searched ({exc.code}), so no recommendations could be "
            "produced. No papers were invented."
        )
        trace = Trace(
            request_id=run.request_id,
            duration_ms=_ms(run.started),
            model=self._model_name,
            interpretation=interpretation,
            steps=run.steps,
            counts=Counts(returned=0, surviving=0, selected=0),
            fallbacks=run.fallbacks,
            limitations=run.limitations,
            failure=Failure(
                stage="search_papers",
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                http_status=exc.status,
                query=plan.topic_query,
                filters=self._filters(plan),
                rate_limit=exc.rate_limit,
                retries=exc.retries,
            ),
        )
        return SearchFailed(exc, trace)

    # ---- response ---------------------------------------------------------------------------

    def _response(
        self,
        run: _Run,
        status: str,
        interpretation: Interpretation,
        presented: list[RecommendedPaper],
        selection: Selection | None,
        grounding: GroundingInfo,
        message: str | None = None,
    ) -> AskResponse:
        counts = Counts(
            returned=run.returned,
            surviving=len(selection.survivors) if selection else 0,
            selected=len(presented),
        )
        limitations = self._limitations(run, interpretation, presented, counts)
        filtering = FilteringInfo()
        if selection is not None:
            filtering = FilteringInfo(
                removed=selection.removed,
                duplicates_merged=selection.merged,
                ordering_rule=selection.ordering_rule,
                shortlisted=[
                    ShortlistEntry(
                        ref=f"P{index}", doi=paper.doi, term_match=selection.term_matches[paper.doi]
                    )
                    for index, paper in enumerate(selection.selected, start=1)
                ],
            )
        trace = Trace(
            request_id=run.request_id,
            duration_ms=_ms(run.started),
            model=self._model_name,
            interpretation=interpretation,
            searches=run.searches,
            steps=run.steps,
            counts=counts,
            filtering=filtering,
            grounding=grounding,
            fallbacks=run.fallbacks,
            limitations=limitations,
        )
        return AskResponse(
            status=status,
            question=run.question,
            summary=message or self._summary(status, counts, run),
            papers=presented,
            search=run.searches[0] if run.searches else None,
            limitations=limitations,
            trace=trace,
        )

    @staticmethod
    def _summary(status: str, counts: Counts, run: _Run) -> str:
        total = run.searches[0].total_results if run.searches else 0
        text = (
            f"Crossref returned {counts.returned} records ({total:,} matches overall); "
            f"{counts.surviving} remained after filtering and de-duplication; "
            f"presenting {counts.selected}."
        )
        return (
            f"No suitable papers were found for this question. {text}"
            if status == "no_results"
            else text
        )

    @staticmethod
    def _limitations(
        run: _Run, interpretation: Interpretation, presented: list[RecommendedPaper], counts: Counts
    ) -> list[str]:
        notes = list(run.limitations)
        if not presented:
            if interpretation.plan.intent != "find_papers":
                notes.append("No search was run, so no papers are recommended.")
            elif counts.returned:
                notes.append(
                    f"Crossref returned {counts.returned} records but none passed the filters; "
                    "the trace lists why each was removed."
                )
            else:
                notes.append("Crossref returned no records for this search.")
            return notes
        notes.append(METADATA_LIMITATION)
        without_abstract = sum(1 for paper in presented if not paper.abstract)
        if without_abstract:
            notes.append(
                f"{without_abstract} of {len(presented)} papers have no abstract in Crossref, so "
                "their explanations rely on the title alone."
            )
        if len(presented) < MIN_PRESENTED:
            notes.append(
                f"Only {len(presented)} paper(s) passed the filters, fewer than the usual "
                f"{MIN_PRESENTED}-{PRESENT_LIMIT}."
            )
        plan = interpretation.plan
        if plan.from_year or plan.until_year or plan.work_types:
            notes.append(
                "Date and type constraints rely on Crossref's publication dates and type labels, "
                "which can be missing or inconsistent."
            )
        if run.fallbacks:
            notes.append(
                "Some steps used deterministic fallbacks instead of the model; see the trace."
            )
        return notes
