"""Deterministic filtering, de-duplication and ordering of normalized Crossref papers.

No scoring formula: Crossref's relevance order decides, and the rules below are explicit,
recorded in the trace, and easy to explain.
"""

import re
from dataclasses import dataclass, field

from app.agent.plan import topic_terms
from app.schemas import Merge, Paper, Removal, SearchPlan

PRESENT_LIMIT = 5  # at most this many papers are presented
WINDOW = 12  # only the first N survivors (in Crossref order) are considered for presentation

_NOISE_TITLE_RE = re.compile(
    r"^(front|back)\s+matter\b|^table\s+of\s+contents\b|^contents\b|^index\b|^cover(\s+page)?\b"
    r"|^title\s+page\b|^masthead\b|^editorial\s+board\b",
    re.IGNORECASE,
)
_FILE_TITLE_RE = re.compile(r"\.(pdf|docx?|zip|csv|xlsx?|pptx?)\s*$", re.IGNORECASE)
_ARTICLE_TYPES = {"journal-article", "proceedings-article"}


@dataclass
class Selection:
    survivors: list[Paper]  # everything left after filtering and de-duplication, Crossref order
    selected: list[Paper]  # what is presented (<= PRESENT_LIMIT), in presentation order
    removed: list[Removal] = field(default_factory=list)
    merged: list[Merge] = field(default_factory=list)
    term_matches: dict[str, bool] = field(default_factory=dict)  # doi -> matched a topic term
    ordering_rule: str = ""


def stem(word: str) -> str:
    """A deliberately tiny stemmer so 'testing', 'tests' and 'tested' meet at 'test'."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _stems(text: str) -> set[str]:
    return {stem(word) for word in re.findall(r"[^\W_]+", text.lower())}


def term_match(paper: Paper, terms: list[str]) -> bool:
    """Soft relevance guard: does any query term appear in the title or abstract?"""
    haystack = _stems(f"{paper.title or ''} {paper.abstract or ''}")
    return any(stem(term) in haystack for term in terms)


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", title.casefold()))


def _completeness(paper: Paper) -> tuple[int, int, int, int]:
    """Higher is better: has abstract, has authors, is an article (not a preprint), earlier rank."""
    return (
        int(bool(paper.abstract)),
        int(bool(paper.authors)),
        int(paper.work_type in _ARTICLE_TYPES),
        -paper.crossref_rank,
    )


def _hard_constraint_violation(paper: Paper, plan: SearchPlan) -> str | None:
    if not paper.title:
        return "no_title"
    if _NOISE_TITLE_RE.search(paper.title) or _FILE_TITLE_RE.search(paper.title):
        return "not_a_paper (front matter, index, or an attached file)"
    if plan.work_types and paper.work_type not in {
        work_type.value for work_type in plan.work_types
    }:
        return "work_type_not_requested"
    if plan.from_year is not None or plan.until_year is not None:
        if paper.year is None:
            return "year_unknown_with_date_constraint"
        if plan.from_year is not None and paper.year < plan.from_year:
            return "year_before_requested_range"
        if plan.until_year is not None and paper.year > plan.until_year:
            return "year_after_requested_range"
    return None


def select_papers(
    papers: list[Paper], plan: SearchPlan, terms: list[str] | None = None
) -> Selection:
    """Filter, de-duplicate and order `papers` (given in Crossref order), then pick up to five."""
    terms = topic_terms(plan.topic_query) if terms is None else terms
    removed: list[Removal] = []
    merged: list[Merge] = []

    # 1. Hard constraints: the plan's date/type limits, plus records that aren't papers.
    constrained: list[Paper] = []
    for paper in papers:
        reason = _hard_constraint_violation(paper, plan)
        if reason:
            removed.append(Removal(doi=paper.doi, title=paper.title, reason=reason))
        else:
            constrained.append(paper)

    # 2. De-duplicate: same DOI, then same normalized title within a year. Keep the more complete.
    kept: list[Paper] = []
    for paper in constrained:
        duplicate_of = next((other for other in kept if _is_duplicate(paper, other)), None)
        if duplicate_of is None:
            kept.append(paper)
            continue
        reason = "same_doi" if paper.doi == duplicate_of.doi else "same_title_and_year"
        if _completeness(paper) > _completeness(duplicate_of):
            kept[kept.index(duplicate_of)] = paper
            merged.append(Merge(kept=paper.doi, dropped=duplicate_of.doi, reason=reason))
        else:
            merged.append(Merge(kept=duplicate_of.doi, dropped=paper.doi, reason=reason))
    kept.sort(key=lambda paper: paper.crossref_rank)

    # 3. Soft topic-term guard. A paper without a matching term is NOT dropped, because a relevant
    #    paper may use different words. Only the noise case goes: no match, no abstract, and far
    #    down Crossref's ranking.
    matches = {paper.doi: term_match(paper, terms) for paper in kept}
    survivors: list[Paper] = []
    for paper in kept:
        if not matches[paper.doi] and not paper.abstract and paper.crossref_rank > WINDOW:
            removed.append(
                Removal(doi=paper.doi, title=paper.title, reason="no_term_match_and_no_abstract")
            )
        else:
            survivors.append(paper)

    # 4. Order: the first WINDOW survivors in Crossref's order; within them, papers that match a
    #    query term first, then ones with an abstract, then (only if asked) newest first, then
    #    Crossref's own order.
    recency = plan.recency_requested
    window = sorted(
        survivors[:WINDOW],
        key=lambda paper: (
            not matches[paper.doi],
            not paper.abstract,
            -(paper.year or 0) if recency else 0,
            paper.crossref_rank,
        ),
    )
    rule = (
        f"Crossref relevance order; the first {WINDOW} remaining papers are considered. Among "
        "them, papers matching a query term come first, then papers with an abstract, "
        + ("then newest first, " if recency else "")
        + f"then Crossref's own order. The top {PRESENT_LIMIT} are presented."
    )
    return Selection(
        survivors=survivors,
        selected=window[:PRESENT_LIMIT],
        removed=removed,
        merged=merged,
        term_matches=matches,
        ordering_rule=rule,
    )


def _is_duplicate(a: Paper, b: Paper) -> bool:
    if a.doi == b.doi:
        return True
    if not a.title or not b.title or _title_key(a.title) != _title_key(b.title):
        return False
    return a.year is None or b.year is None or abs(a.year - b.year) <= 1
