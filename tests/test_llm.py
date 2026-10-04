"""AnthropicModelClient against a stubbed SDK client: no live Anthropic calls."""

import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pydantic
import pytest

from app.agent.llm import (
    EXPLAIN_MAX_TOKENS,
    INTERPRET_MAX_TOKENS,
    MODEL_MAX_RETRIES,
    MODEL_TIMEOUT_S,
    AnthropicModelClient,
    ModelError,
)
from app.schemas import Candidate, ExplanationItem, Explanations, SearchPlan
from tests.conftest import make_settings

pytestmark = pytest.mark.anyio

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
PLAN = SearchPlan(intent="find_papers", topic_query="software testing")
EXPLANATIONS = Explanations(items=[ExplanationItem(ref="P1", explanation="May be relevant here.")])
CANDIDATE = Candidate(
    ref="P1", title="T", year=2024, venue=None, abstract=None, evidence_basis="title_only"
)


def reply(parsed, stop_reason="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


def status_error(cls, status):
    response = httpx2.Response(status, request=REQUEST, text="secret-body sk-ant-LEAK")
    return cls("provider said no", response=response, body=None)


class StubSDK:
    """Stands in for anthropic.AsyncAnthropic: returns or raises one scripted outcome."""

    def __init__(self, outcome):
        self.calls: list[dict] = []
        self.closed = False
        self.messages = SimpleNamespace(parse=self._parse)
        self._outcome = outcome

    async def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def close(self):
        self.closed = True


def client_for(outcome, **settings):
    sdk = StubSDK(outcome)
    return AnthropicModelClient(make_settings(**settings), client=sdk), sdk


async def test_without_an_api_key_both_calls_fail_with_model_not_configured():
    client = AnthropicModelClient(make_settings())

    for call in (client.interpret("q"), client.explain("q", [CANDIDATE])):
        with pytest.raises(ModelError) as exc_info:
            await call
        assert exc_info.value.code == "model_not_configured"


async def test_interpret_sends_the_configured_model_explicit_effort_and_structured_output():
    client, sdk = client_for(reply(PLAN))

    result = await client.interpret("software testing with LLMs")

    assert result == PLAN
    call = sdk.calls[0]
    assert call["model"] == "claude-sonnet-5-5"  # the approved default
    assert call["output_format"] is SearchPlan
    assert call["output_config"] == {"effort": "low"}  # explicit: defaults differ per model
    assert call["max_tokens"] == INTERPRET_MAX_TOKENS
    assert (
        "thinking" not in call and "temperature" not in call
    )  # unsupported/unneeded on Sonnet 5.5
    assert "tools" not in call and "tool_choice" not in call  # no tool loop, no forced tool use
    assert call["messages"] == [
        {"role": "user", "content": "<user_question>software testing with LLMs</user_question>"}
    ]


async def test_model_and_effort_come_from_configuration():
    client, sdk = client_for(
        reply(EXPLANATIONS), anthropic_model="claude-opus-5-5", anthropic_effort="medium"
    )

    await client.explain("q", [CANDIDATE])

    call = sdk.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "medium"}
    assert call["output_format"] is Explanations and call["max_tokens"] == EXPLAIN_MAX_TOKENS
    assert '<candidate ref="P1" evidence="title_only">' in call["messages"][0]["content"]


async def test_sdk_client_is_built_with_explicit_timeout_and_retry_limits():
    client = AnthropicModelClient(make_settings(anthropic_api_key="sk-ant-test-value"))

    sdk = client._client
    assert sdk.timeout == MODEL_TIMEOUT_S and sdk.max_retries == MODEL_MAX_RETRIES
    await client.aclose()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (anthropic.APITimeoutError(request=REQUEST), "model_timeout"),
        (status_error(anthropic.RateLimitError, 429), "model_rate_limited"),
        (anthropic.APIConnectionError(request=REQUEST), "model_unavailable"),
        (status_error(anthropic.InternalServerError, 500), "model_unavailable"),
        (status_error(anthropic.AuthenticationError, 401), "model_unavailable"),
        (status_error(anthropic.BadRequestError, 400), "model_unavailable"),
        (anthropic.APIError("generic", request=REQUEST, body=None), "model_unavailable"),
    ],
    ids=["timeout", "rate-limit", "connection", "5xx", "401", "400", "other-api-error"],
)
async def test_sdk_failures_become_explicit_model_error_codes(failure, code):
    client, _ = client_for(failure)

    with pytest.raises(ModelError) as exc_info:
        await client.interpret("q")

    assert exc_info.value.code == code


async def test_schema_validation_failure_is_model_output_invalid():
    try:
        SearchPlan.model_validate({"intent": "bogus"})
    except pydantic.ValidationError as exc:
        validation_error = exc
    client, _ = client_for(validation_error)

    with pytest.raises(ModelError) as exc_info:
        await client.interpret("q")

    assert exc_info.value.code == "model_output_invalid"


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (reply(None, stop_reason="refusal"), "model_refused"),
        (reply(PLAN, stop_reason="max_tokens"), "model_output_invalid"),
        (reply(None), "model_output_invalid"),
        (reply(EXPLANATIONS), "model_output_invalid"),  # right shape for the other call
    ],
    ids=["refusal", "truncated", "no-output", "wrong-type"],
)
async def test_bad_responses_are_rejected_before_use(outcome, code):
    client, _ = client_for(outcome)

    with pytest.raises(ModelError) as exc_info:
        await client.interpret("q")

    assert exc_info.value.code == code


async def test_errors_and_logs_never_contain_provider_bodies_or_credentials(caplog):
    client, _ = client_for(
        status_error(anthropic.AuthenticationError, 401), anthropic_api_key="sk-ant-CONFIGURED"
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(ModelError) as exc_info:
        await client.interpret("q")

    shown = f"{exc_info.value} {exc_info.value.message} {exc_info.value.code} {caplog.text}"
    assert "sk-ant" not in shown and "secret-body" not in shown and "provider said no" not in shown


async def test_aclose_closes_the_sdk_client():
    client, sdk = client_for(reply(PLAN))

    await client.aclose()

    assert sdk.closed


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_api_key_counts_as_not_configured(blank):
    # `.env.example` ships `ANTHROPIC_API_KEY=` (blank); copying it must not look configured.
    settings = make_settings(anthropic_api_key=blank)
    client = AnthropicModelClient(settings)

    assert settings.model_api_key is None and client._client is None
    with pytest.raises(ModelError) as exc_info:
        await client.interpret("q")
    assert exc_info.value.code == "model_not_configured"
