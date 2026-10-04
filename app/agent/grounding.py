"""The evidence/grounding boundary.

What this guarantees: bibliographic fields (title, authors, year, venue, DOI, link, abstract) of
a presented paper always come from its Crossref record. Model output is reduced to slot refs
plus prose, and the refs are mapped back to papers we selected ourselves, so the model cannot
add, remove or alter a paper.

What it does NOT guarantee: model *prose* can still be wrong or misleading in ways these
lexical checks don't catch. The checks catch common overclaims; the UI also labels the text as an
AI explanation based on limited metadata.
"""

import re

from app.agent.ranking import stem
from app.schemas import (
    Candidate,
    EvidenceBasis,
    Explanations,
    GroundingInfo,
    Paper,
    RecommendedPaper,
    Removal,
)

MAX_EXPLANATION_CHARS = 600
MIN_EXPLANATION_CHARS = 15

_OVERCLAIM_RE = re.compile(
    r"\b(prove[sd]?|proven|proof|demonstrates?\s+that|establish(?:es|ed)?|confirm(?:s|ed)?"
    r"|conclusive(?:ly)?|definitive(?:ly)?|guarantee[sd]?|undeniabl[ey]|irrefutabl[ey])\b",
    re.IGNORECASE,
)
# Claims about content the model can't have seen when only the title is available.
_BEYOND_TITLE_RE = re.compile(
    r"\b(abstract|the\s+authors?|authors?\s+(?:find|found|report|show|conclude|demonstrate)"
    r"|results?\s+(?:show|indicate|suggest|demonstrate)|found\s+that|shows?\s+that|reports?\s+that"
    r"|conclud(?:es|ed)|outperform\w*|achiev(?:es|ed)|experiments?\s+(?:show|demonstrate))\b",
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(
    r"https?://|www\.|\bdoi\b|\b10\.\d{4,9}/|\bet\s+al\b|\((?:19|20)\d{2}\)", re.I
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def to_candidate(paper: Paper, ref: str) -> Candidate:
    """What the explanation call may see. No DOI, URL or author names: nothing to echo back."""
    return Candidate(
        ref=ref,
        title=paper.title or "",
        year=paper.year,
        venue=paper.venue,
        abstract=paper.abstract,
        evidence_basis=evidence_basis(paper),
    )


def evidence_basis(paper: Paper) -> EvidenceBasis:
    return "title_and_abstract" if paper.abstract else "title_only"


def violations(text: str, paper: Paper) -> list[str]:
    """Why a model-written explanation can't be shown as is (empty list = acceptable)."""
    found: list[str] = []
    if not MIN_EXPLANATION_CHARS <= len(text) <= MAX_EXPLANATION_CHARS:
        found.append("length")
    if "<" in text or ">" in text:
        found.append("markup")
    if _SOURCE_RE.search(text):
        found.append("contains_citation_or_link")
    if _OVERCLAIM_RE.search(text):
        found.append("overclaim")
    if not paper.abstract and _BEYOND_TITLE_RE.search(text):
        found.append("claims_beyond_title_only_evidence")
    evidence = f"{paper.title or ''} {paper.abstract or ''} {paper.year or ''}"
    known_numbers = set(_NUMBER_RE.findall(evidence))
    if any(number not in known_numbers for number in _NUMBER_RE.findall(text)):
        found.append("number_not_in_evidence")
    return found


def fallback_explanation(paper: Paper, terms: list[str]) -> str:
    """Deterministic text built only from the paper's own metadata and the query terms."""
    basis = "title and abstract" if paper.abstract else "title"
    haystack = {
        stem(word)
        for word in re.findall(r"[^\W_]+", f"{paper.title} {paper.abstract or ''}".lower())
    }
    matched = [term for term in terms if stem(term) in haystack]
    if matched:
        return (
            f"Matches your query terms ({', '.join(matched)}) in its {basis}. "
            "Relevance is judged only from this Crossref metadata."
        )
    return (
        f"Returned by Crossref for this query, but its {basis} doesn't contain your query terms, "
        "so its relevance is uncertain."
    )


def ground_explanations(
    selected: list[Paper], explanations: Explanations | None, terms: list[str]
) -> tuple[list[RecommendedPaper], GroundingInfo]:
    """Attach explanations to the papers WE selected. `explanations=None` means the model failed.

    Slot P1 is selected[0], P2 is selected[1], and so on. Items whose ref isn't one of those slots
    are dropped, a paper's explanation is checked before use, and anything unsupported is replaced
    by `fallback_explanation`.
    """
    info = GroundingInfo()
    by_ref: dict[str, str] = {}
    if explanations is not None:
        valid_refs = {f"P{i}" for i in range(1, len(selected) + 1)}
        for item in explanations.items:
            if item.ref not in valid_refs:
                info.rejected_refs.append(
                    f"{item.ref}: not one of the {len(selected)} selected papers"
                )
            elif item.ref in by_ref:
                info.rejected_refs.append(f"{item.ref}: duplicate item")
            else:
                by_ref[item.ref] = " ".join(item.explanation.split())

    papers: list[RecommendedPaper] = []
    for index, paper in enumerate(selected, start=1):
        text = by_ref.get(f"P{index}")
        source = "model"
        if text is None:
            reason = "model unavailable" if explanations is None else "no explanation returned"
            text, source = fallback_explanation(paper, terms), "metadata_only"
            info.explanation_rewrites.append(
                Removal(doi=paper.doi, title=paper.title, reason=reason)
            )
        elif problems := violations(text, paper):
            text, source = fallback_explanation(paper, terms), "metadata_only"
            info.explanation_rewrites.append(
                Removal(doi=paper.doi, title=paper.title, reason=", ".join(problems))
            )
        papers.append(
            RecommendedPaper(
                **paper.model_dump(),
                explanation=text,
                explanation_source=source,
                evidence_basis=evidence_basis(paper),
            )
        )
    return papers, info
