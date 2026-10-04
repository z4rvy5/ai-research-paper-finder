"""Deterministic filtering, de-duplication and ordering (no model involved)."""

from app.agent.ranking import PRESENT_LIMIT, WINDOW, select_papers, stem, term_match
from app.crossref.normalize import normalize_works
from app.schemas import SearchPlan, WorkType
from tests.builders import many_works, work
from tests.conftest import load_crossref_fixture


def plan(**overrides) -> SearchPlan:
    return SearchPlan(
        **{"intent": "find_papers", "topic_query": "software testing language models", **overrides}
    )


def select(items, **plan_overrides):
    return select_papers(normalize_works(items), plan(**plan_overrides))


def reasons(selection):
    return {removal.doi: removal.reason for removal in selection.removed}


def test_selects_at_most_five_papers_and_keeps_counts_distinct():
    selection = select(many_works(9))

    assert len(selection.selected) == PRESENT_LIMIT == 5
    assert len(selection.survivors) == 9  # survivors != selected


def test_without_other_signals_crossref_order_is_preserved():
    selection = select(many_works(4))

    assert [p.crossref_rank for p in selection.selected] == [1, 2, 3, 4]


def test_work_type_constraint_removes_other_types_with_a_reason():
    items = [
        work(doi="10.1/j", work_type="journal-article"),
        work(doi="10.1/p", work_type="posted-content"),
        work(doi="10.1/x", work_type=None),
    ]

    selection = select(items, work_types=[WorkType.JOURNAL_ARTICLE])

    assert [p.doi for p in selection.selected] == ["10.1/j"]
    assert reasons(selection) == {
        "10.1/p": "work_type_not_requested",
        "10.1/x": "work_type_not_requested",
    }


def test_date_constraint_removes_papers_outside_the_range_and_unknown_years():
    items = [
        work(doi="10.1/old", year=2015),
        work(doi="10.1/in", year=2022),
        work(doi="10.1/new", year=2027),
        work(doi="10.1/none", year=None),
    ]

    selection = select(items, from_year=2020, until_year=2024)

    assert [p.doi for p in selection.selected] == ["10.1/in"]
    assert reasons(selection) == {
        "10.1/old": "year_before_requested_range",
        "10.1/new": "year_after_requested_range",
        "10.1/none": "year_unknown_with_date_constraint",
    }


def test_year_is_not_required_when_no_date_constraint_was_asked_for():
    assert [p.doi for p in select([work(doi="10.1/none", year=None)]).selected] == ["10.1/none"]


def test_records_without_title_and_non_paper_records_are_removed():
    items = [
        work(doi="10.1/ok"),
        work(doi="10.1/notitle", title=None),
        work(doi="10.1/toc", title="Table of Contents"),
        work(doi="10.1/file", title="Software Testing Survey_supp1-3368208.pdf"),
        work(doi="10.1/idx", title="Index"),
    ]

    selection = select(items)

    assert [p.doi for p in selection.selected] == ["10.1/ok"]
    assert set(reasons(selection)) == {"10.1/notitle", "10.1/toc", "10.1/file", "10.1/idx"}


def test_the_real_fixture_noise_record_style_is_recognised():
    papers = normalize_works([work(doi="10.1109/tse.2024.3368208/mm1", title="Survey_supp1.pdf")])

    assert select_papers(papers, plan()).selected == []


def test_duplicate_dois_are_merged_keeping_the_first_when_equally_complete():
    items = [work(doi="10.1/a"), work(doi="10.1/A")]  # DOIs are case-insensitive

    selection = select(items)

    assert [p.doi for p in selection.selected] == ["10.1/a"]
    assert [(m.kept, m.dropped, m.reason) for m in selection.merged] == [
        ("10.1/a", "10.1/a", "same_doi")
    ]


def test_preprint_and_published_version_merge_keeping_the_more_complete_record():
    title = "Integrating Large Language Models into Automated Software Testing"
    items = [
        work(doi="10.20944/preprint", title=title, work_type="posted-content", year=2025),
        work(doi="10.3390/journal", title=title, work_type="journal-article", year=2025),
    ]

    selection = select(items)

    assert [p.doi for p in selection.selected] == ["10.3390/journal"]
    assert [(m.kept, m.dropped, m.reason) for m in selection.merged] == [
        ("10.3390/journal", "10.20944/preprint", "same_title_and_year")
    ]


def test_a_record_with_an_abstract_beats_an_otherwise_identical_one_without():
    title = "Language models for software testing"
    items = [
        work(doi="10.1/bare", title=title),
        work(doi="10.1/rich", title=title, abstract="Text."),
    ]

    assert [p.doi for p in select(items).selected] == ["10.1/rich"]


def test_same_title_far_apart_in_years_is_not_a_duplicate():
    title = "Language models for software testing"
    items = [work(doi="10.1/a", title=title, year=2019), work(doi="10.1/b", title=title, year=2024)]

    selection = select(items)

    assert len(selection.selected) == 2 and selection.merged == []


def test_topic_term_guard_is_soft_papers_with_other_wording_are_kept():
    items = [
        work(doi="10.1/words", title="Quality assurance with generative AI"),  # no query term
        work(doi="10.1/match", title="Software testing with language models"),
    ]

    selection = select(items)

    assert {p.doi for p in selection.survivors} == {"10.1/words", "10.1/match"}
    assert selection.term_matches == {"10.1/words": False, "10.1/match": True}
    assert [p.doi for p in selection.selected] == ["10.1/match", "10.1/words"]  # matches first


def test_guard_drops_only_unmatched_abstractless_noise_far_down_the_ranking():
    items = [work(doi=f"10.1/m{i}", title=f"Software testing {i}") for i in range(WINDOW)]
    items += [
        work(doi="10.1/tail-noise", title="Cooking pasta"),
        work(doi="10.1/tail-abstract", title="Baking bread", abstract="Mentions testing."),
        work(doi="10.1/tail-other", title="Different wording entirely", abstract="Unrelated text."),
    ]

    selection = select(items)

    assert reasons(selection) == {"10.1/tail-noise": "no_term_match_and_no_abstract"}
    assert "10.1/tail-other" in {p.doi for p in selection.survivors}


def test_ordering_prefers_term_match_then_abstract_then_crossref_order():
    items = [
        work(doi="10.1/nomatch", title="Unrelated title"),
        work(doi="10.1/match-bare", title="Software testing"),
        work(doi="10.1/match-abstract", title="Software testing again", abstract="Text."),
    ]

    order = [p.doi for p in select(items).selected]

    assert order == ["10.1/match-abstract", "10.1/match-bare", "10.1/nomatch"]


def test_recency_orders_newest_first_only_when_requested():
    items = [work(doi="10.1/old", year=2021), work(doi="10.1/new", year=2025)]

    assert [p.doi for p in select(items).selected] == ["10.1/old", "10.1/new"]
    recent = select(items, recency_requested=True)
    assert [p.doi for p in recent.selected] == ["10.1/new", "10.1/old"]
    assert "newest first" in recent.ordering_rule


def test_only_the_first_window_of_survivors_is_considered_for_presentation():
    items = many_works(WINDOW + 5)

    selection = select(items)

    assert max(p.crossref_rank for p in selection.selected) <= WINDOW
    assert len(selection.survivors) == WINDOW + 5


def test_stemmer_and_term_match_handle_simple_inflections():
    assert stem("testing") == stem("tests") == stem("tested") == "test"
    assert term_match(normalize_works([work(title="Automated tests")])[0], ["testing"])


def test_real_fixture_merges_the_duplicate_record_and_drops_nothing_else():
    # In the captured Crossref data, ranks 4 and 5 are the same paper under two DOIs.
    items = load_crossref_fixture("works_llm_software_testing")["body"]["message"]["items"]

    selection = select_papers(normalize_works(items), plan(topic_query="llms software testing"))

    assert len(items) == 5 and len(selection.selected) == 4
    assert selection.removed == []
    assert [(m.kept, m.dropped, m.reason) for m in selection.merged] == [
        (
            "10.63282/3050-9246/icrtcsit-139",
            "10.63282/3050-9246/icrtcsit-128",
            "same_title_and_year",
        )
    ]
    assert selection.ordering_rule  # the rule is always stated for the trace
