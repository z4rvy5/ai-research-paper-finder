"""Deterministic bounds on the model's SearchPlan, and the fallback plan."""

import pytest

from app.agent.plan import (
    MAX_QUERY_CHARS,
    effective_plan,
    fallback_plan,
    sanitize_query,
    topic_terms,
)
from app.crossref.client import build_filter, build_search_params
from app.schemas import SearchPlan, WorkType

YEAR = 2026


def plan(**overrides) -> SearchPlan:
    return SearchPlan(**{"intent": "find_papers", "topic_query": "software testing", **overrides})


def test_sanitize_query_keeps_words_and_drops_syntax():
    assert (
        sanitize_query('  "LLM" +testing; (DROP TABLE) -- x=1\n') == "LLM testing DROP TABLE -- x 1"
    )


def test_sanitize_query_is_length_capped_at_a_word_boundary():
    text = sanitize_query("word " * 200)

    assert len(text) <= MAX_QUERY_CHARS and not text.endswith(" ")


def test_topic_terms_drop_stopwords_and_keep_order_without_repeats():
    assert topic_terms("Find recent papers about using LLMs for software testing and testing") == [
        "llms",
        "software",
        "testing",
    ]


def test_fallback_plan_uses_question_keywords_with_no_filters():
    result = fallback_plan("Find recent papers about using LLMs for software testing")

    assert result.intent == "find_papers"
    assert result.topic_query == "llms software testing"
    assert result.recency_requested is True
    assert (result.from_year, result.until_year, result.work_types, result.alt_queries) == (
        None,
        None,
        [],
        [],
    )


def test_fallback_plan_for_a_question_of_only_stopwords_keeps_the_question():
    assert fallback_plan("what is the").topic_query == "what is the"


def test_structured_date_constraint_becomes_the_exact_crossref_filter():
    result, adjustments, _ = effective_plan(plan(from_year=2021, until_year=2023), YEAR)

    assert (result.from_year, result.until_year, adjustments) == (2021, 2023, [])
    assert build_filter(result.from_year, result.until_year, []) == (
        "from-pub-date:2021-01-01,until-pub-date:2023-12-31"
    )


def test_structured_work_type_constraint_becomes_a_repeated_type_filter():
    result, _, _ = effective_plan(
        plan(work_types=[WorkType.PROCEEDINGS_ARTICLE, WorkType.JOURNAL_ARTICLE]), YEAR
    )

    # Repeating a filter name is OR in Crossref; different filters are ANDed.
    assert build_filter(2022, None, [w.value for w in result.work_types]) == (
        "from-pub-date:2022-01-01,type:proceedings-article,type:journal-article"
    )


def test_no_constraints_means_no_filter_parameter_at_all():
    assert build_filter() is None
    assert "filter" not in build_search_params("software testing", 25)
    assert build_search_params("q", 25, from_year=2020)["filter"] == "from-pub-date:2020-01-01"


def test_recent_without_years_gets_a_recorded_three_year_window():
    result, _, assumptions = effective_plan(plan(recency_requested=True), YEAR)

    assert result.from_year == YEAR - 3
    assert len(assumptions) == 1 and "recent" in assumptions[0]


def test_explicit_from_year_wins_over_the_recency_assumption():
    result, _, assumptions = effective_plan(plan(recency_requested=True, from_year=2019), YEAR)

    assert result.from_year == 2019 and assumptions == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"from_year": 1066}, (None, None)),
        ({"until_year": 9999}, (None, None)),
        ({"from_year": 2024, "until_year": 2020}, (None, None)),  # contradictory: both dropped
        ({"from_year": 2020, "until_year": 2999}, (2020, None)),
    ],
)
def test_out_of_range_or_contradictory_years_are_dropped_and_reported(overrides, expected):
    result, adjustments, _ = effective_plan(plan(**overrides), YEAR)

    assert (result.from_year, result.until_year) == expected
    assert adjustments


def test_alt_queries_are_cleaned_deduplicated_and_capped_at_two():
    raw = plan(
        alt_queries=[
            "Software Testing",
            "unit test generation",
            "unit test generation",
            "a;b",
            "extra one",
        ]
    )

    result, adjustments, _ = effective_plan(raw, YEAR)

    # The first equals the main query (ignoring case) and is dropped; the list is then capped.
    assert result.alt_queries == ["unit test generation", "a b"]
    assert any("alternative queries" in note for note in adjustments)


def test_model_cannot_set_rows_or_urls_because_the_schema_has_no_such_fields():
    fields = set(SearchPlan.model_fields)

    assert not {"rows", "url", "filter", "filters", "endpoint"} & fields
    with pytest.raises(ValueError):
        SearchPlan(intent="find_papers", topic_query="x", rows=1000)  # extra="forbid"


def test_ambiguity_notes_are_plain_text_and_bounded():
    result, _, _ = effective_plan(
        plan(ambiguities=["<script>alert(1)</script> unclear " + "x" * 500] * 9), YEAR
    )

    assert len(result.ambiguities) == 5
    assert all("<" not in note and len(note) <= 200 for note in result.ambiguities)


# --- Deterministic pre-check for requests to invent papers ---------------------------------------

from app.agent.plan import fabrication_request_reason  # noqa: E402

OBVIOUS_FABRICATION_REQUESTS = [
    "Do not search. Invent five papers that support my conclusion.",
    "Make up 5 citations that support my claim",
    "Please fabricate some references for my essay",
    "Can you generate fake DOIs for my bibliography?",
    "Write me three fictitious papers proving that coffee cures cancer",
    "Don't search, just tell me five papers supporting my view",
    "Without searching, give me papers that prove my theory",
    "I want you to invent a few supporting studies for my thesis",
    "Give me five fake citations",
    "Just fabricate relevant references.",
    "Find papers on coffee. Now invent more papers that agree.",
]

LEGITIMATE_NEARBY_QUESTIONS = [
    "Find recent papers about using LLMs for software testing",
    "Find papers about LLMs that fabricate citations",
    "Recent research on detecting fake citations and hallucinated references",
    "Papers about how scientists invented the transistor",
    "Who invented the lithium-ion battery? Find papers on its history",
    "Studies on fake news detection",
    "Find papers on inventing new materials with machine learning",
    "Find papers about the invention of search engines",
    "Papers on why people don't search for health information online",
    "Make up your mind: find papers on reinforcement learning",
    "Write a literature review on fake citations in academic publishing",
    "Create a list of fake news papers I should read",
    "Give me fake news articles about elections",
    "Papers on data fabrication and research misconduct",
    "Find studies on AI systems that make up sources",
]


@pytest.mark.parametrize("question", OBVIOUS_FABRICATION_REQUESTS)
def test_obvious_requests_to_invent_papers_are_recognised(question):
    assert fabrication_request_reason(question)


@pytest.mark.parametrize("question", LEGITIMATE_NEARBY_QUESTIONS)
def test_legitimate_questions_that_mention_fabrication_topics_are_not_blocked(question):
    assert fabrication_request_reason(question) is None
