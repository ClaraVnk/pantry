"""Wire doubles: how each provider fails, in each provider's own vocabulary.

These live beside the adapters rather than in the test package for the reason
``tests/README.md`` gives: only the adapter knows what a rate limit looks like on
*its* wire. An Anthropic rate limit and an Ollama connection refusal have nothing in
common, and that asymmetry is precisely what the adapters exist to absorb -- a
double written in a neutral shape would test the harness's imagination instead of
the translation.

They are also the reason the conformance suite costs nothing to run: no credential,
no network, no spend. The counterpart is that they encode an *assumption* about
provider behaviour, which decays as SDKs ship breaking releases. The
``live_provider`` suite is what is supposed to notice; until it exists, treat these
shapes as reviewed-but-unverified.

Every scenario name is the harness's vocabulary (``tests/README.md``), and the
mapping from name to domain error is asserted there, not here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Final

import anthropic
import httpx

__all__ = [
    "SCENARIOS",
    "FakeAnthropicClient",
    "chat_completions_transport",
    "gemini_transport",
    "ollama_transport",
    "valid_receipt_payload",
    "valid_recipe_payload",
]

#: Everything the harness asks an adapter to reproduce. ``missing_*`` scenarios are
#: capability drills rather than failures: the wire behaves normally and the
#: adapter's own degradation logic is what is under test.
SCENARIOS: Final[tuple[str, ...]] = (
    "nominal",
    "connection_refused",
    "timeout",
    "server_error",
    "rate_limited",
    "quota_exhausted",
    "malformed_payload",
    "schema_violation",
)

#: A key-shaped string embedded in the doubles' failure messages. It must never
#: reach a domain error, and a unit test asserts exactly that (security review,
#: SEC-003): providers really do echo the credential they were called with.
LEAKY_KEY: Final = "sk-ant-contract-DOUBLE-0123456789"

#: What every double reports having consumed, in *its own* vendor vocabulary below.
#: Deliberately three distinct numbers: a double that answered 1/1/1 would pass
#: just as well if an adapter read the fields in the wrong order.
USAGE_INPUT: Final = 1_200
USAGE_OUTPUT: Final = 340
USAGE_CACHED: Final = 800


def valid_recipe_payload() -> str:
    return json.dumps(
        {
            "suggestions": [
                {
                    "title": "Gratin de courgettes",
                    "summary": "Simple gratin using what is about to spoil.",
                    "duration_minutes": 35,
                    "servings": 4,
                    "uses_expiring_soon": True,
                    "ingredients": [
                        {"name": "Courgettes", "amount": "600", "unit": "g", "in_stock": True},
                        {"name": "Crème", "amount": "20", "unit": "cl", "in_stock": False},
                    ],
                    "steps": ["Slice the courgettes.", "Bake for 25 minutes."],
                }
            ]
        }
    )


def valid_receipt_payload() -> str:
    return json.dumps(
        {
            "merchant": "Coop",
            "purchased_on": "2026-08-01",
            "currency": "CHF",
            "total": "18.40",
            "lines": [
                {
                    "label": "COURGETTES BIO",
                    "quantity": "0.62",
                    "unit": "kg",
                    "unit_price": "4.90",
                    "total_price": "3.04",
                }
            ],
        }
    )


def _payload_for(port_name: str) -> str:
    return valid_receipt_payload() if port_name == "ReceiptParser" else valid_recipe_payload()


# --------------------------------------------------------------------------- #
# Anthropic -- SDK exceptions, not HTTP
# --------------------------------------------------------------------------- #


class _TextBlock:
    __slots__ = ("text", "type")

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    """Anthropic splits the prompt three ways; the adapter folds writes into input."""

    __slots__ = (
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "input_tokens",
        "output_tokens",
    )

    def __init__(self) -> None:
        self.input_tokens = USAGE_INPUT - 200
        self.cache_creation_input_tokens = 200
        self.cache_read_input_tokens = USAGE_CACHED
        self.output_tokens = USAGE_OUTPUT


class _Message:
    __slots__ = ("content", "stop_reason", "usage")

    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = _Usage()


class _FakeMessages:
    def __init__(self, scenario: str, payload: str) -> None:
        self._scenario = scenario
        self._payload = payload

    async def create(self, **_kwargs: Any) -> Any:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        scenario = self._scenario
        if scenario == "connection_refused":
            raise anthropic.APIConnectionError(request=request)
        if scenario == "timeout":
            raise anthropic.APITimeoutError(request=request)
        if scenario == "server_error":
            raise anthropic.InternalServerError(
                f"overloaded (x-api-key {LEAKY_KEY})",
                response=httpx.Response(529, request=request),
                body=None,
            )
        if scenario == "rate_limited":
            raise anthropic.RateLimitError(
                f"rate limited (x-api-key {LEAKY_KEY})",
                response=httpx.Response(429, request=request),
                body={"error": {"type": "rate_limit_error", "message": "slow down"}},
            )
        if scenario == "quota_exhausted":
            # Anthropic reports an empty balance as a 400, not a 402.
            raise anthropic.BadRequestError(
                "invalid request",
                response=httpx.Response(400, request=request),
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Your credit balance is too low to access the API.",
                    }
                },
            )
        if scenario == "malformed_payload":
            return _Message("I'd be glad to help! Here are some ideas: ...")
        if scenario == "schema_violation":
            return _Message(json.dumps({"unexpected": True}))
        return _Message(self._payload)


class FakeAnthropicClient:
    """Stands in for ``AsyncAnthropic`` at the adapter's own seam."""

    def __init__(self, scenario: str, *, port_name: str = "RecipeGenerator") -> None:
        self._messages = _FakeMessages(scenario, _payload_for(port_name))

    @property
    def messages(self) -> _FakeMessages:
        return self._messages


# --------------------------------------------------------------------------- #
# HTTP providers
# --------------------------------------------------------------------------- #


def _http_double(
    scenario: str,
    envelope: Callable[[str], Mapping[str, Any]],
    payload: str,
    *,
    quota_status: int,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "connection_refused":
            raise httpx.ConnectError("connection refused", request=request)
        if scenario == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if scenario == "server_error":
            return httpx.Response(503, json={"error": "service unavailable"}, request=request)
        if scenario == "rate_limited":
            return httpx.Response(
                429,
                json={"error": {"message": f"rate limit reached for key {LEAKY_KEY}"}},
                request=request,
            )
        if scenario == "quota_exhausted":
            return httpx.Response(
                quota_status,
                json={"error": {"message": "insufficient quota for this account"}},
                request=request,
            )
        if scenario == "malformed_payload":
            return httpx.Response(200, text="<html>gateway</html>", request=request)
        if scenario == "schema_violation":
            return httpx.Response(
                200, json=envelope(json.dumps({"unexpected": True})), request=request
            )
        return httpx.Response(200, json=envelope(payload), request=request)

    return httpx.MockTransport(handler)


def ollama_transport(scenario: str, *, port_name: str = "RecipeGenerator") -> httpx.MockTransport:
    """Ollama's ``/api/chat`` envelope.

    ``quota_exhausted`` is a 429: a bare Ollama has no billing, but a queue-limited
    instance -- or the reverse proxy every real deployment puts in front of one --
    answers exactly that, and the household's remedy ("wait, or size the instance")
    is the same one the domain error carries.
    """
    return _http_double(
        scenario,
        lambda text: {
            "model": "llama3.2",
            "message": {"role": "assistant", "content": text},
            # Ollama counts evaluation, not billing, and has no cache figure at all.
            "prompt_eval_count": USAGE_INPUT,
            "eval_count": USAGE_OUTPUT,
        },
        _payload_for(port_name),
        quota_status=429,
    )


def chat_completions_transport(
    scenario: str, *, port_name: str = "RecipeGenerator"
) -> httpx.MockTransport:
    """The OpenAI / Mistral AI envelope, and their 402 for an exhausted balance."""
    return _http_double(
        scenario,
        lambda text: {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            # `prompt_tokens` is all-in here: it already contains the cached part,
            # which the adapter has to subtract back out.
            "usage": {
                "prompt_tokens": USAGE_INPUT + USAGE_CACHED,
                "completion_tokens": USAGE_OUTPUT,
                "total_tokens": USAGE_INPUT + USAGE_CACHED + USAGE_OUTPUT,
                "prompt_tokens_details": {"cached_tokens": USAGE_CACHED},
            },
        },
        _payload_for(port_name),
        quota_status=402,
    )


def gemini_transport(scenario: str, *, port_name: str = "RecipeGenerator") -> httpx.MockTransport:
    """Google's ``generateContent`` envelope."""
    return _http_double(
        scenario,
        lambda text: {
            "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
            # camelCase, and an all-in prompt count like the OpenAI shape.
            "usageMetadata": {
                "promptTokenCount": USAGE_INPUT + USAGE_CACHED,
                "candidatesTokenCount": USAGE_OUTPUT,
                "cachedContentTokenCount": USAGE_CACHED,
                "totalTokenCount": USAGE_INPUT + USAGE_CACHED + USAGE_OUTPUT,
            },
        },
        _payload_for(port_name),
        quota_status=429,
    )
