"""Turn raw Crossref work records into `Paper` objects.

Everything here is deterministic text/field extraction. Crossref metadata is untrusted: it is
reduced to plain text and plain fields, and nothing in it is ever interpreted as instructions.
"""

import html
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

from app.schemas import Paper

DOI_RESOLVER = "https://doi.org/"
MAX_ABSTRACT_CHARS = 1500
PLAUSIBLE_YEARS = range(1500, 2200)

_TAG_RE = re.compile(r"<[^>]*>")
_LEADING_ABSTRACT_RE = re.compile(r"^\s*abstract\b[\s:.\-]*", re.IGNORECASE)


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

    title = _first_text(item.get("title"))
    authors = _authors(item.get("author"))
    year = _year(item)
    abstract = abstract_text(item.get("abstract"))

    missing = [
        name
        for name, present in (
            ("title", title),
            ("authors", authors),
            ("year", year),
            ("abstract", abstract),
        )
        if not present
    ]
    return Paper(
        doi=doi,
        title=title,
        url=doi_url(doi),
        crossref_rank=rank,
        authors=authors,
        year=year,
        venue=_first_text(item.get("container-title")),
        abstract=abstract,
        work_type=item.get("type") if isinstance(item.get("type"), str) else None,
        missing_fields=missing,
    )


def normalize_works(items: list[dict[str, Any]], rank_offset: int = 0) -> list[Paper]:
    """Normalize in Crossref's order; rank is the 1-based position in that order.

    `rank_offset` continues the ranking across searches (a refinement search's records rank
    after the primary search's records).
    """
    papers = (
        normalize_work(item, rank_offset + position) for position, item in enumerate(items, start=1)
    )
    return [paper for paper in papers if paper is not None]


def strip_markup(text: str) -> str:
    """Plain text from a string that may contain markup (titles can contain <i>, <sub>, ...)."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())


def abstract_text(raw: Any) -> str | None:
    """Plain text from Crossref's abstract, which is JATS XML (e.g. `<jats:p>...</jats:p>`).

    This extracts character data only: tags and attributes are discarded and the result is never
    treated as HTML. Anything that could carry entity/DOCTYPE tricks is not parsed as XML at all.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _jats_text(raw)
    text = _LEADING_ABSTRACT_RE.sub("", text)
    if not text:
        return None
    if len(text) > MAX_ABSTRACT_CHARS:
        text = text[:MAX_ABSTRACT_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _jats_text(raw: str) -> str:
    if "<!" not in raw:  # no DOCTYPE / ENTITY / CDATA / comments: safe to parse as XML
        wrapped = f'<root xmlns:jats="http://www.ncbi.nlm.nih.gov/JATS1">{raw}</root>'
        try:
            root = ET.fromstring(wrapped)
        except ET.ParseError:
            pass  # e.g. HTML-only entities like &nbsp; are not valid XML
        else:
            return _tidy(" ".join(root.itertext()))
    return _tidy(strip_markup(raw))


def _tidy(text: str) -> str:
    """Collapse whitespace and close the gaps that joining text pieces leaves before punctuation."""
    return re.sub(r"\s+([.,;:!?)\]])", r"\1", " ".join(text.split()))


def _authors(value: Any) -> list[str]:
    """Display names in Crossref order. Placeholders (a family name of '-') are dropped."""
    if not isinstance(value, list):
        return []
    names = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        parts = [_name_part(entry.get("given")), _name_part(entry.get("family"))]
        name = " ".join(part for part in parts if part) or _name_part(entry.get("name"))
        if name:
            names.append(name)
    return names


def _name_part(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = strip_markup(value)
    return "" if text in {"", "-", "–", "—", "?"} else text


def _year(item: dict[str, Any]) -> int | None:
    """`issued`, falling back to `published`. date-parts can be partial, e.g. [[2025]]."""
    for key in ("issued", "published"):
        date = item.get(key)
        parts = date.get("date-parts") if isinstance(date, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if isinstance(year, int) and not isinstance(year, bool) and year in PLAUSIBLE_YEARS:
                return year
    return None


def _first_text(value: Any) -> str | None:
    """Crossref string fields like `title` are arrays; take the first non-empty entry."""
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, str):
            text = strip_markup(entry)
            if text:
                return text
    return None
