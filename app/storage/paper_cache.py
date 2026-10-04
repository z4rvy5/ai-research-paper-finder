"""A small in-process cache of papers the server itself just recommended.

Saving a paper takes only a DOI. If that paper was recommended moments ago, its Crossref-derived
record is here, so saving needs no second Crossref request. Only papers produced by the server
(from Crossref records) ever enter the cache; nothing a client sends does.
"""

import time
from collections import OrderedDict
from collections.abc import Callable

from app.schemas import Paper


class PaperCache:
    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, Paper]] = OrderedDict()

    def put(self, paper: Paper) -> None:
        # Keep only the base Paper fields (drop explanations etc.) in an independent copy.
        base = Paper.model_validate(paper.model_dump(include=set(Paper.model_fields)))
        self._entries.pop(base.doi, None)
        self._entries[base.doi] = (self._clock(), base)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # evict the oldest

    def get(self, doi: str) -> Paper | None:
        entry = self._entries.get(doi)
        if entry is None:
            return None
        stored_at, paper = entry
        if self._clock() - stored_at > self._ttl:
            del self._entries[doi]
            return None
        return paper.model_copy()
