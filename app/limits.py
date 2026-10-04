"""Small in-process abuse and cost limits, sized for a single-process public demo.

They live in memory, so they reset when the server restarts and are not shared between
instances. That is deliberate: they exist to stop casual abuse and runaway model cost on a free
demo deployment, not to be a distributed rate-limiting system.
"""

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime


class SlidingWindowLimiter:
    """At most `max_events` per key within `window_seconds`. `max_events <= 0` disables it."""

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        self._max_events = max_events
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str) -> float | None:
        """Record an attempt. Returns None if it is allowed, else the seconds until it would be."""
        if self._max_events <= 0:
            return None
        now = self._clock()
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self._window:
            events.popleft()
        if len(events) >= self._max_events:
            return max(events[0] + self._window - now, 0.0)
        events.append(now)
        if len(self._events) > self._max_keys:
            self._drop_idle_keys(now)
        return None

    def _drop_idle_keys(self, now: float) -> None:
        """Keep memory bounded: forget keys with no recent events, then the oldest if still big."""
        for key in [k for k, ev in self._events.items() if not ev or ev[-1] <= now - self._window]:
            del self._events[key]
        while len(self._events) > self._max_keys:
            del self._events[next(iter(self._events))]


class DailyCallBudget:
    """At most `limit` calls per UTC day. A limit of 0 (or less) allows none."""

    def __init__(self, limit: int, *, today: Callable[[], date] | None = None):
        self._limit = limit
        self._today = today or (lambda: datetime.now(UTC).date())
        self._day = self._today()
        self._used = 0

    def try_acquire(self) -> bool:
        day = self._today()
        if day != self._day:
            self._day, self._used = day, 0
        if self._used >= self._limit:
            return False
        self._used += 1
        return True
