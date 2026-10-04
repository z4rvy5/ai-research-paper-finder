"""A daily cap on model calls, applied as a wrapper around any ModelClient.

When the cap is reached the wrapper raises `ModelError("model_daily_limit")`. The orchestrator
already turns any ModelError into its deterministic fallbacks, so the demo keeps answering (as
`degraded`, with the reason in the trace) instead of failing or running up cost.
"""

from collections.abc import Sequence

from app.agent.llm import ModelClient, ModelError
from app.limits import DailyCallBudget
from app.schemas import Candidate, Explanations, SearchPlan


class BudgetedModelClient:
    def __init__(self, inner: ModelClient, budget: DailyCallBudget):
        self._inner = inner
        self._budget = budget

    async def interpret(self, question: str) -> SearchPlan:
        self._spend()
        return await self._inner.interpret(question)

    async def explain(self, question: str, candidates: Sequence[Candidate]) -> Explanations:
        self._spend()
        return await self._inner.explain(question, candidates)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _spend(self) -> None:
        if not self._budget.try_acquire():
            raise ModelError(
                "model_daily_limit",
                "The daily limit for model calls on this demo has been reached.",
            )
