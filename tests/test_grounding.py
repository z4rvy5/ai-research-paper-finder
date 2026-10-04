"""The grounding boundary: model output can't add papers or smuggle in unsupported claims."""

import pytest

from app.agent.grounding import (
    fallback_explanation,
    ground_explanations,
    to_candidate,
    violations,
)
from app.crossref.normalize import normalize_works
from app.schemas import ExplanationItem, Explanations
from tests.builders import work


def papers(*items):
    return normalize_works(list(items))


def explanations(**by_ref: str) -> Explanations:
    return Explanations(
        items=[ExplanationItem(ref=ref, explanation=text) for ref, text in by_ref.items()]
    )


TITLE_ONLY = papers(work(doi="10.1/a", title="Unit test generation with language models"))[0]
WITH_ABSTRACT = papers(
    work(
        doi="10.1/b",
        title="Mutation testing with LLMs",
        abstract="We evaluate 12 mutation operators on 3 open-source projects.",
    )
)[0]


def test_candidate_for_the_model_has_no_doi_url_or_authors():
    candidate = to_candidate(WITH_ABSTRACT, "P1")

    assert set(candidate.model_dump()) == {
        "ref",
        "title",
        "year",
        "venue",
        "abstract",
        "evidence_basis",
    }
    assert candidate.evidence_basis == "title_and_abstract"
    assert to_candidate(TITLE_ONLY, "P2").evidence_basis == "title_only"


GOOD = "May be relevant because its title mentions language models and test generation."


def test_acceptable_explanations_pass_unchanged_and_are_marked_as_model_written():
    shown, info = ground_explanations([TITLE_ONLY], explanations(P1=GOOD), ["test"])

    assert shown[0].explanation == GOOD and shown[0].explanation_source == "model"
    assert info.rejected_refs == [] and info.explanation_rewrites == []


@pytest.mark.parametrize(
    ("text", "expected_violation"),
    [
        ("This paper proves that LLMs generate better unit tests overall.", "overclaim"),
        ("The authors demonstrate that the approach is conclusively superior.", "overclaim"),
        ("It definitively establishes the state of the art in this area.", "overclaim"),
        (
            "May be relevant; see https://example.org/other-paper for context.",
            "contains_citation_or_link",
        ),
        (
            "May be relevant, similar to 10.1234/fake.5678 which covers it.",
            "contains_citation_or_link",
        ),
        (
            "Builds on Smith et al. who studied test generation closely.",
            "contains_citation_or_link",
        ),
        (
            "May be relevant, as shown in Smith (2021) on test generation.",
            "contains_citation_or_link",
        ),
        (
            "May be relevant; the abstract describes a new test generator.",
            "claims_beyond_title_only_evidence",
        ),
        (
            "The results show a large improvement for test generation.",
            "claims_beyond_title_only_evidence",
        ),
        ("May be relevant: it achieves 87% coverage on test generation.", "number_not_in_evidence"),
        ("May be relevant <b>bold</b> text about test generation here.", "markup"),
        ("Relevant.", "length"),
        ("x" * 700, "length"),
    ],
)
def test_unsupported_or_overclaiming_text_is_detected(text, expected_violation):
    assert expected_violation in violations(text, TITLE_ONLY)


def test_numbers_are_allowed_when_the_paper_evidence_contains_them():
    text = "May be relevant: its abstract mentions 12 mutation operators and 3 projects."

    assert violations(text, WITH_ABSTRACT) == []
    assert "number_not_in_evidence" in violations(
        "It covers 99 operators in testing work.", WITH_ABSTRACT
    )


def test_abstract_claims_are_not_flagged_when_there_is_an_abstract():
    assert "claims_beyond_title_only_evidence" not in violations(
        "May be relevant; the abstract describes mutation testing with models.", WITH_ABSTRACT
    )


def test_overclaiming_explanation_is_replaced_by_a_deterministic_one_and_recorded():
    bad = "This paper proves that language models solve unit test generation."

    shown, info = ground_explanations([TITLE_ONLY], explanations(P1=bad), ["test", "generation"])

    assert "proves" not in shown[0].explanation
    assert shown[0].explanation_source == "metadata_only"
    assert shown[0].explanation == fallback_explanation(TITLE_ONLY, ["test", "generation"])
    assert info.explanation_rewrites[0].doi == "10.1/a"
    assert info.explanation_rewrites[0].reason == "overclaim"


def test_model_item_for_a_paper_that_was_not_selected_cannot_introduce_a_paper():
    # Only P1 and P2 were selected; the model also returns P3, P5.
    selected = [TITLE_ONLY, WITH_ABSTRACT]
    reply = explanations(P1=GOOD, P2=GOOD, P3="Invented paper about everything.", P5="Another.")

    shown, info = ground_explanations(selected, reply, ["test"])

    assert [p.doi for p in shown] == ["10.1/a", "10.1/b"]  # exactly the selected papers
    assert [r.split(":")[0] for r in info.rejected_refs] == ["P3", "P5"]


def test_duplicate_refs_keep_the_first_item_and_reject_the_rest():
    reply = explanations(P1=GOOD)
    reply.items.append(
        ExplanationItem(ref="P1", explanation="May be relevant, a second note here.")
    )

    shown, info = ground_explanations([TITLE_ONLY], reply, ["test"])

    assert shown[0].explanation == GOOD
    assert info.rejected_refs == ["P1: duplicate item"]


def test_paper_without_a_model_explanation_gets_the_deterministic_one():
    shown, info = ground_explanations(
        [TITLE_ONLY, WITH_ABSTRACT], explanations(P1=GOOD), ["mutation"]
    )

    assert shown[0].explanation_source == "model"
    assert shown[1].explanation_source == "metadata_only"
    assert info.explanation_rewrites[0].reason == "no explanation returned"


def test_model_failure_gives_every_paper_a_deterministic_explanation():
    shown, info = ground_explanations([TITLE_ONLY, WITH_ABSTRACT], None, ["test"])

    assert [p.explanation_source for p in shown] == ["metadata_only", "metadata_only"]
    assert {r.reason for r in info.explanation_rewrites} == {"model unavailable"}


def test_bibliographic_fields_are_copied_from_the_crossref_record_not_from_explanations():
    reply = explanations(P1="May be relevant: title is 'Totally Different Title' by Jane Doe.")

    shown, _ = ground_explanations([TITLE_ONLY], reply, ["test"])

    assert (shown[0].doi, shown[0].title, shown[0].authors, shown[0].year, shown[0].url) == (
        TITLE_ONLY.doi,
        TITLE_ONLY.title,
        TITLE_ONLY.authors,
        TITLE_ONLY.year,
        TITLE_ONLY.url,
    )


def test_evidence_basis_is_decided_by_code_from_the_presence_of_an_abstract():
    shown, _ = ground_explanations(
        [TITLE_ONLY, WITH_ABSTRACT], explanations(P1=GOOD, P2=GOOD), ["x"]
    )

    assert [p.evidence_basis for p in shown] == ["title_only", "title_and_abstract"]


def test_fallback_explanation_names_only_terms_found_in_the_metadata():
    matched = fallback_explanation(TITLE_ONLY, ["unit", "quantum"])
    unmatched = fallback_explanation(TITLE_ONLY, ["quantum"])

    assert "unit" in matched and "quantum" not in matched and "title" in matched
    assert "relevance is uncertain" in unmatched
