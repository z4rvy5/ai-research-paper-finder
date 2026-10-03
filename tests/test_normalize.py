from app.crossref.normalize import doi_url, normalize_work, normalize_works
from tests.conftest import load_crossref_fixture


def test_fixture_records_normalize_to_their_crossref_doi_and_title():
    items = load_crossref_fixture("works_llm_software_testing")["body"]["message"]["items"]

    papers = normalize_works(items)

    assert [p.doi for p in papers] == [item["DOI"].lower() for item in items]
    assert [p.title for p in papers] == [item["title"][0] for item in items]
    assert [p.crossref_rank for p in papers] == list(range(1, len(items) + 1))
    assert all(p.url == f"https://doi.org/{p.doi}" for p in papers)


def test_doi_is_lowercased_and_link_built_from_doi_not_crossref_url_field():
    item = {"DOI": "10.1109/TSE.2025.3562025", "URL": "https://evil.example/x", "title": ["T"]}

    paper = normalize_work(item, rank=1)

    assert paper.doi == "10.1109/tse.2025.3562025"
    assert paper.url == "https://doi.org/10.1109/tse.2025.3562025"


def test_record_without_doi_is_skipped_and_ranks_keep_crossref_positions():
    items = [{"title": ["No DOI"]}, {"DOI": "  ", "title": ["Blank DOI"]}, {"DOI": "10.1/x"}]

    papers = normalize_works(items)

    assert [(p.doi, p.crossref_rank) for p in papers] == [("10.1/x", 3)]


def test_missing_or_empty_title_becomes_none():
    assert normalize_work({"DOI": "10.1/a"}, 1).title is None
    assert normalize_work({"DOI": "10.1/a", "title": []}, 1).title is None
    assert normalize_work({"DOI": "10.1/a", "title": ["", "  "]}, 1).title is None
    assert normalize_work({"DOI": "10.1/a", "title": "not a list"}, 1).title is None


def test_title_whitespace_is_collapsed():
    paper = normalize_work({"DOI": "10.1/a", "title": ["  Large\n  Language   Models "]}, 1)

    assert paper.title == "Large Language Models"


def test_doi_url_percent_encodes_characters_that_would_change_the_url():
    assert doi_url("10.1016/s0140-6736(20)30183-5") == (
        "https://doi.org/10.1016/s0140-6736(20)30183-5"
    )
    assert doi_url("10.1000/a#b?c d") == "https://doi.org/10.1000/a%23b%3Fc%20d"
