"""The model boundary: the only code that talks to the Anthropic API.

Two bounded calls, each returning a validated Pydantic object (structured outputs):
  interpret(question)            -> SearchPlan
  explain(question, candidates)  -> Explanations

Every failure becomes a `ModelError` with a stable `code`; the orchestrator turns those into
deterministic fallbacks, so a model problem never fails a request. Timeout behavior is explicit:
each HTTP attempt gets MODEL_TIMEOUT_S and failed attempts are retried MODEL_MAX_RETRIES times,
so one call takes at most about (MODEL_MAX_RETRIES + 1) x MODEL_TIMEOUT_S plus backoff.
"""

import logging
from collections.abc import Sequence
from typing import Protocol, TypeVar

import anthropic
import pydantic

from app.agent import prompts
from app.config import Settings
from app.schemas import Candidate, Explanations, SearchPlan

log = logging.getLogger(__name__)

MODEL_TIMEOUT_S = 20.0
MODEL_MAX_RETRIES = 1
INTERPRET_MAX_TOKENS = 4000  # adaptive thinking tokens count toward max_tokens
EXPLAIN_MAX_TOKENS = 6000

T = TypeVar("T", bound=pydantic.BaseModel)


class ModelError(Exception):
    """A model call failed. `code` is stable; `message` is safe to show (no secrets, no detail)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ModelClient(Protocol):
    """The boundary the rest of the app depends on; tests substitute a fake."""

    async def interpret(self, question: str) -> SearchPlan: ...

    async def explain(self, question: str, candidates: Sequence[Candidate]) -> Explanations: ...

    async def aclose(self) -> None: ...


class AnthropicModelClient:
    def __init__(self, settings: Settings, *, client: anthropic.AsyncAnthropic | None = None):
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._client = client
        if self._client is None and settings.model_api_key is not None:
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.model_api_key,
                timeout=MODEL_TIMEOUT_S,
                max_retries=MODEL_MAX_RETRIES,
            )

    async def interpret(self, question: str) -> SearchPlan:
        return await self._parse(
            system=prompts.INTERPRET_SYSTEM,
            user=prompts.interpret_user_message(question),
            output_format=SearchPlan,
            max_tokens=INTERPRET_MAX_TOKENS,
        )

    async def explain(self, question: str, candidates: Sequence[Candidate]) -> Explanations:
        return await self._parse(
            system=prompts.EXPLAIN_SYSTEM,
            user=prompts.explain_user_message(question, candidates),
            output_format=Explanations,
            max_tokens=EXPLAIN_MAX_TOKENS,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def _parse(self, *, system: str, user: str, output_format: type[T], max_tokens: int) -> T:
        if self._client is None:
            raise ModelError("model_not_configured", "No model API key is configured.")
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
                # Explicit, because the default effort differs between models.
                output_config={"effort": self._effort},
            )
        except anthropic.APITimeoutError as exc:  # before APIConnectionError, its parent
            raise ModelError("model_timeout", "The model did not respond in time.") from exc
        except anthropic.RateLimitError as exc:
            raise ModelError("model_rate_limited", "The model API is rate limiting.") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelError("model_unavailable", "Could not reach the model API.") from exc
        except anthropic.APIStatusError as exc:
            # Log the status only: never the body, and never anything credential-shaped.
            log.warning("Model API returned HTTP %s", exc.status_code)
            raise ModelError("model_unavailable", "The model API returned an error.") from exc
        except anthropic.APIError as exc:
            raise ModelError("model_unavailable", "The model API call failed.") from exc
        except (pydantic.ValidationError, ValueError) as exc:  # output didn't match the schema
            raise ModelError("model_output_invalid", "The model's output was not valid.") from exc

        if response.stop_reason == "refusal":
            raise ModelError("model_refused", "The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise ModelError("model_output_invalid", "The model's output was cut off.")
        if not isinstance(response.parsed_output, output_format):
            raise ModelError("model_output_invalid", "The model returned no structured output.")
        return response.parsed_output
