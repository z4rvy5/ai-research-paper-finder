"""The in-memory rate limiter, the daily call budget, and the model wrapper that uses it."""

from datetime import date

import pytest

from app.agent.budget import BudgetedModelClient
from app.agent.llm import ModelError
from app.limits import DailyCallBudget, SlidingWindowLimiter
from app.schemas import Candidate
from tests.fakes import FakeModelClient

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- SlidingWindowLimiter -----------------------------------------------------------------


def test_allows_up_to_the_limit_then_reports_how_long_to_wait():
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)

    assert [limiter.check("a") for _ in range(3)] == [None, None, None]
    clock.now += 10
    wait = limiter.check("a")

    assert wait == pytest.approx(50.0)  # the oldest attempt leaves the window in 50 s


def test_attempts_are_allowed_again_once_the_window_has_passed():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.check("a")
    assert limiter.check("a") is not None

    clock.now += 61

    assert limiter.check("a") is None


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(1, 60, clock=Clock())

    assert limiter.check("a") is None and limiter.check("b") is None
    assert limiter.check("a") is not None


def test_blocked_attempts_do_not_extend_the_block():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.check("a")
    for _ in range(50):  # hammering while blocked records nothing
        limiter.check("a")
    clock.now += 61

    assert limiter.check("a") is None


@pytest.mark.parametrize("disabled", [0, -1])
def test_a_non_positive_limit_disables_it(disabled):
    limiter = SlidingWindowLimiter(disabled, 60, clock=Clock())

    assert all(limiter.check("a") is None for _ in range(100))


def test_memory_is_bounded_by_forgetting_idle_keys():
    clock = Clock()
    limiter = SlidingWindowLimiter(5, 60, clock=clock, max_keys=10)
    for index in range(10):
        limiter.check(f"old-{index}")
    clock.now += 120  # all of those are now idle
    for index in range(10):
        limiter.check(f"new-{index}")

    assert len(limiter._events) <= 11


def test_memory_is_bounded_even_when_every_key_is_active():
    limiter = SlidingWindowLimiter(5, 60, clock=Clock(), max_keys=10)

    for index in range(100):
        limiter.check(f"key-{index}")

    assert len(limiter._events) <= 11


# --- DailyCallBudget ----------------------------------------------------------------------


def test_budget_allows_its_limit_and_then_refuses():
    budget = DailyCallBudget(2, today=lambda: date(2026, 10, 3))

    assert [budget.try_acquire() for _ in range(4)] == [True, True, False, False]


def test_budget_resets_on_the_next_utc_day():
    day = [date(2026, 10, 3)]
    budget = DailyCallBudget(1, today=lambda: day[0])
    assert budget.try_acquire() and not budget.try_acquire()

    day[0] = date(2026, 10, 4)

    assert budget.try_acquire()


def test_a_zero_budget_allows_nothing():
    assert DailyCallBudget(0).try_acquire() is False


# --- BudgetedModelClient ------------------------------------------------------------------

CANDIDATE = Candidate(
    ref="P1", title="T", year=2024, venue=None, abstract=None, evidence_basis="title_only"
)


async def test_both_model_calls_spend_from_one_budget_and_then_raise_a_clear_error():
    inner = FakeModelClient()
    client = BudgetedModelClient(inner, DailyCallBudget(2))

    await client.interpret("q")
    await client.explain("q", [CANDIDATE])
    for call in (client.interpret("q"), client.explain("q", [CANDIDATE])):
        with pytest.raises(ModelError) as exc_info:
            await call
        assert exc_info.value.code == "model_daily_limit"

    assert len(inner.interpret_questions) == 1 and len(inner.explain_calls) == 1  # no further calls


async def test_the_budget_error_text_is_fixed_and_safe():
    client = BudgetedModelClient(FakeModelClient(), DailyCallBudget(0))

    with pytest.raises(ModelError) as exc_info:
        await client.interpret("q")

    assert (
        exc_info.value.message == "The daily limit for model calls on this demo has been reached."
    )


async def test_closing_the_wrapper_closes_the_inner_client():
    inner = FakeModelClient()

    await BudgetedModelClient(inner, DailyCallBudget(1)).aclose()

    assert inner.closed
