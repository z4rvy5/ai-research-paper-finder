"""Deterministic handling of the SearchPlan: sanitizing, bounds, fallback, topic terms.

The model proposes a plan; this module decides what is actually allowed. The model cannot
express URLs, filters or row counts, and everything it can express is bounded here.
"""

import re
import unicodedata

from app.schemas import SearchPlan

SEARCH_ROWS = 25  # fixed by code, never taken from the model
MAX_QUERY_CHARS = 200
MAX_ALT_QUERIES = 2
MAX_AMBIGUITIES = 5
MAX_NOTE_CHARS = 200
MIN_YEAR = 1900
RECENT_WINDOW_YEARS = 3  # what "recent" means when the user gives no years

_STOPWORD_TEXT = """a an and are as at be been being but by can could did do does find for from give
    had has have how i in into is it its me my of on or our papers paper please research show some
    studies study such than that the their them these they this those to us using use used want was
    we were what when where which who why will with would you your about any article articles
    recent recently latest newest new work works literature related regarding"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_RECENCY_RE = re.compile(
    r"\b(recent|recently|latest|newest|(last|past)\s+(few|couple\s+of)\s+years)\b", re.IGNORECASE
)
_WORD_RE = re.compile(r"[^\W_]+")

# ---- Deterministic pre-check for requests to invent papers ----------------------------------
# Deliberately narrow. It only fires on an instruction addressed to the assistant: an invent/
# fabricate verb at the start of a sentence (or after "please", "can you", ...) with a source noun
# as its direct object, a request to write/generate *fake* sources, or "don't search" paired with
# a request for papers. Topics about fabrication stay legitimate research questions: "papers on
# LLMs that fabricate citations", "detecting fake references", "who invented the transistor".
_SOURCE_NOUN = (
    r"(?:papers?|citations?|references?|sources?|dois?|studies|study|articles?|publications?"
    r"|evidence)"
)
_FILLER = (
    r"(?:me|us|my|some|a|an|the|\d+|one|two|three|four|five|six|seven|eight|nine|ten|few|"
    r"several|more|additional|new|good|relevant|supporting|credible|convincing|plausible|"
    r"believable|scholarly|academic|peer-reviewed|realistic|real-looking)"
)
_FAKE_ADJ = (
    r"(?:fake|fictitious|fictional|fabricated|invented|made-up|imaginary|non-?existent|bogus)"
)
_ASSISTANT_LEAD = (
    r"(?:^|[.!?;:]\s+|\b(?:please|just|now|can you|could you|would you|will you|"
    r"you should|you must|i want you to)\s+)"
)
_INVENT_REQUEST_RE = re.compile(
    _ASSISTANT_LEAD
    + r"(?:invent|fabricate|forge|concoct|make\s+up)"
    + rf"(?:\s+{_FILLER}){{0,4}}\s+(?:{_FAKE_ADJ}\s+)?{_SOURCE_NOUN}\b",
    re.IGNORECASE,
)
_FAKE_SOURCES_RE = re.compile(
    r"\b(?:give|write|create|generate|make|produce|provide|draft|add)"
    + rf"(?:\s+{_FILLER}){{0,3}}\s+{_FAKE_ADJ}\s+{_SOURCE_NOUN}\b",
    re.IGNORECASE,
)
_SKIP_SEARCH_RE = re.compile(
    r"(?:^|[.!?;:,]\s+|\b(?:please|just|now)\s+)"
    r"(?:(?:do\s+not|don'?t|never)\s+(?:actually\s+|really\s+)?(?:search|look\s+(?:it\s+)?up|check)\b"
    r"|without\s+(?:actually\s+)?(?:searching|a\s+search|looking\s+(?:it\s+)?up|checking)\b)",
    re.IGNORECASE,
)
_SOURCE_WORD_RE = re.compile(rf"\b{_SOURCE_NOUN}\b", re.IGNORECASE)


def fabrication_request_reason(question: str) -> str | None:
    """Why `question` is an obvious request to invent papers, or None if it isn't one."""
    if _INVENT_REQUEST_RE.search(question) or _FAKE_SOURCES_RE.search(question):
        return "it asks the assistant to invent or fake papers, citations or other sources"
    if _SKIP_SEARCH_RE.search(question) and _SOURCE_WORD_RE.search(question):
        return "it tells the assistant not to search while asking for papers or sources"
    return None


def sanitize_query(text: str) -> str:
    """Letters, digits, spaces, hyphens and apostrophes only; whitespace collapsed; capped."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s\-']|_", " ", text)
    text = " ".join(text.split())
    if len(text) > MAX_QUERY_CHARS:
        text = text[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return text


def topic_terms(query: str) -> list[str]:
    """Distinct non-stopword terms of a query, lowercased, in order."""
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(query.lower()):
        if word not in STOPWORDS and len(word) >= 2:
            seen.setdefault(word)
    return list(seen)


def fallback_plan(question: str) -> SearchPlan:
    """The plan used when the model can't provide one: the question's keywords, no filters."""
    keywords = " ".join(topic_terms(question)) or question
    return SearchPlan(
        intent="find_papers",
        topic_query=sanitize_query(keywords),
        recency_requested=bool(_RECENCY_RE.search(question)),
    )


def effective_plan(plan: SearchPlan, current_year: int) -> tuple[SearchPlan, list[str], list[str]]:
    """Validate and bound a plan. Returns (plan, adjustments, assumptions).

    `adjustments` are things corrected or dropped from the model's plan; `assumptions` are
    decisions code made (shown in the trace so the user can see them).
    """
    adjustments: list[str] = []
    assumptions: list[str] = []

    topic_query = sanitize_query(plan.topic_query)
    if topic_query != plan.topic_query.strip():
        adjustments.append("topic_query was cleaned (unsupported characters removed or shortened)")

    alt_queries: list[str] = []
    for alt in plan.alt_queries:
        cleaned = sanitize_query(alt)
        if cleaned and cleaned.lower() != topic_query.lower() and cleaned not in alt_queries:
            alt_queries.append(cleaned)
    if len(alt_queries) > MAX_ALT_QUERIES:
        adjustments.append(f"only the first {MAX_ALT_QUERIES} alternative queries are used")
        alt_queries = alt_queries[:MAX_ALT_QUERIES]

    from_year, until_year = plan.from_year, plan.until_year
    upper = current_year + 1
    for name, value in (("from_year", from_year), ("until_year", until_year)):
        if value is not None and not MIN_YEAR <= value <= upper:
            adjustments.append(f"{name} {value} is outside {MIN_YEAR}-{upper} and was ignored")
            if name == "from_year":
                from_year = None
            else:
                until_year = None
    if from_year is not None and until_year is not None and from_year > until_year:
        adjustments.append(f"from_year {from_year} is after until_year {until_year}; both ignored")
        from_year = until_year = None

    if plan.recency_requested and from_year is None:
        from_year = current_year - RECENT_WINDOW_YEARS
        assumptions.append(
            f"'recent' interpreted as publication year {from_year} or later "
            f"(the last {RECENT_WINDOW_YEARS} years)"
        )

    return (
        plan.model_copy(
            update={
                "topic_query": topic_query,
                "alt_queries": alt_queries,
                "from_year": from_year,
                "until_year": until_year,
                "work_types": list(dict.fromkeys(plan.work_types)),
                "ambiguities": [_note(text) for text in plan.ambiguities[:MAX_AMBIGUITIES]],
            }
        ),
        adjustments,
        assumptions,
    )


def _note(text: str) -> str:
    """A model-written note, reduced to short plain text before it is shown anywhere."""
    text = " ".join(re.sub(r"<[^>]*>", " ", text).split())
    return text[:MAX_NOTE_CHARS]
