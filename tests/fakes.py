"""Test doubles for the app's external boundaries."""

from app.crossref.client import DEFAULT_ROWS, CrossrefError, SearchResult


class FakeCrossrefClient:
    """Implements the `PaperSearch` protocol; returns a canned result or raises an error."""

    def __init__(self, result: SearchResult | None = None, error: CrossrefError | None = None):
        self.result = result
        self.error = error
        self.queries: list[str] = []
        self.closed = False

    async def search_works(self, query: str, rows: int = DEFAULT_ROWS) -> SearchResult:
        self.queries.append(query)
        if self.error:
            raise self.error
        assert self.result is not None, "FakeCrossrefClient needs a result or an error"
        return self.result

    async def aclose(self) -> None:
        self.closed = True
