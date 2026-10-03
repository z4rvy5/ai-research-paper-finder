"""Turn raw Crossref work records into `Paper` objects.

Milestone 2 extracts identity fields only (DOI, title, link, rank). Authors, year, venue,
JATS abstract text and missing-field tracking are added in milestone 3.
"""

from typing import Any
from urllib.parse import quote

from app.schemas import Paper

DOI_RESOLVER = "https://doi.org/"


def doi_url(doi: str) -> str:
    # Characters outside this set (e.g. '#', '?', '%', spaces) are percent-encoded so they
    # can't change the meaning of the URL. doi.org resolves encoded DOIs.
    return DOI_RESOLVER + quote(doi, safe="/:;()-._")


def normalize_work(item: dict[str, Any], rank: int) -> Paper | None:
    """Return a Paper, or None when the record has no usable DOI (nothing to cite)."""
    doi = item.get("DOI")
    if not isinstance(doi, str) or not doi.strip():
        return None
    doi = doi.strip().lower()
    return Paper(
        doi=doi, title=_first_text(item.get("title")), url=doi_url(doi), crossref_rank=rank
    )


def normalize_works(items: list[dict[str, Any]]) -> list[Paper]:
    """Normalize in Crossref's order; rank is the 1-based position in that order."""
    papers = (normalize_work(item, rank) for rank, item in enumerate(items, start=1))
    return [paper for paper in papers if paper is not None]


def _first_text(value: Any) -> str | None:
    """Crossref string fields like `title` are arrays; take the first non-empty entry."""
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            return " ".join(entry.split())
    return None
