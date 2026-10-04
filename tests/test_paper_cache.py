from app.schemas import Paper, RecommendedPaper
from app.storage.paper_cache import PaperCache


def recommended(doi="10.1000/a", **overrides) -> RecommendedPaper:
    fields = dict(
        doi=doi,
        title="A title",
        url=f"https://doi.org/{doi}",
        crossref_rank=3,
        authors=["Ada Lovelace"],
        year=2024,
        explanation="May be relevant.",
        explanation_source="model",
        evidence_basis="title_only",
    )
    return RecommendedPaper(**{**fields, **overrides})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_stores_only_the_crossref_paper_fields_not_the_explanation():
    cache = PaperCache()
    cache.put(recommended())

    cached = cache.get("10.1000/a")

    assert type(cached) is Paper
    assert cached.title == "A title" and cached.authors == ["Ada Lovelace"]
    assert not hasattr(cached, "explanation")


def test_returns_an_independent_copy():
    cache = PaperCache()
    cache.put(recommended())

    first = cache.get("10.1000/a")
    first.title = "mutated"

    assert cache.get("10.1000/a").title == "A title"


def test_unknown_doi_is_a_miss():
    assert PaperCache().get("10.1000/missing") is None


def test_entries_expire_after_the_ttl():
    clock = Clock()
    cache = PaperCache(ttl_seconds=60, clock=clock)
    cache.put(recommended())

    clock.now = 59
    assert cache.get("10.1000/a") is not None
    clock.now = 61
    assert cache.get("10.1000/a") is None


def test_oldest_entries_are_evicted_first():
    cache = PaperCache(max_entries=2)
    for doi in ("10.1000/a", "10.1000/b", "10.1000/c"):
        cache.put(recommended(doi))

    assert cache.get("10.1000/a") is None
    assert cache.get("10.1000/b") is not None and cache.get("10.1000/c") is not None


def test_putting_the_same_doi_again_refreshes_it():
    cache = PaperCache(max_entries=2)
    cache.put(recommended("10.1000/a"))
    cache.put(recommended("10.1000/b"))
    cache.put(recommended("10.1000/a", title="newer"))
    cache.put(recommended("10.1000/c"))

    assert cache.get("10.1000/b") is None  # the oldest was evicted, not the refreshed one
    assert cache.get("10.1000/a").title == "newer"
