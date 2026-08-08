"""Anthropic adapter -- the reference implementation, and the instance default.

Two provider features are used deliberately rather than incidentally:

* **Constrained decoding.** The recipe and receipt schemas are handed to
  ``output_config.format``, so the model cannot answer with something the reader
  would reject. This is the branch the emulated path in :mod:`chaudron.infra.llm.base`
  exists to approximate.
* **Prompt caching.** The cache breakpoint goes at the end of the *stable* system
  block. The inventory -- volatile by nature, different on every call and for every
  household -- is sent afterwards, in the user turn. Placing it before the
  breakpoint would invalidate the cached prefix on every request, silently: no
  error, just a bill.

Capabilities are ``static`` here: they are a property of the (provider, model) pair,
knowable without a network call. The table is hand-maintained, which ADR-0005 lists
as a real cost -- so an unknown model is rejected rather than assumed capable. A
table that promises vision a model does not have produces a fabricated shopping
list; a table that is merely out of date produces an actionable error.
"""

from __future__ import annotations

import base64
from typing import Any, Final, Protocol, cast

import anthropic

from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
    TokenUsage,
)
from chaudron.infra.llm.base import Completion, CompletionRequest, ProviderTransport
from chaudron.infra.llm.usage import token_count

__all__ = [
    "ANTHROPIC_MODELS",
    "DEFAULT_ANTHROPIC_MODEL",
    "PROVIDER_CODE",
    "AnthropicClientLike",
    "AnthropicTransport",
    "anthropic_capabilities",
]

PROVIDER_CODE: Final = "anthropic"

#: The model the documentation, the ``instance_owner`` mode and the contract suite
#: all name.
DEFAULT_ANTHROPIC_MODEL: Final = "claude-opus-5"


#: (context window, vision, structured output, prompt caching). Hand-maintained;
#: every new model published by Anthropic needs a line here before a household can
#: select it. See the module docstring for why that is preferred to a default.
ANTHROPIC_MODELS: Final[dict[str, tuple[int, bool, bool, bool]]] = {
    "claude-opus-5": (1_000_000, True, True, True),
    "claude-opus-4-8": (1_000_000, True, True, True),
    "claude-sonnet-5": (1_000_000, True, True, True),
    "claude-haiku-4-5": (200_000, True, True, True),
}

#: Reasoning depth. Both features are transcription-shaped rather than
#: problem-solving-shaped, and adaptive thinking at low effort is markedly cheaper
#: than the default without measurable loss on either. Kept explicit so the choice
#: is reviewable rather than inherited from an API default that may move.
_EFFORT: Final = "low"

#: Enough for three recipes or a long till receipt, with headroom for thinking.
_MAX_OUTPUT_TOKENS: Final = 8_000

_SUBSCRIPTION_HINT: Final = (
    "an Anthropic API key from console.anthropic.com is required. A Claude Pro or "
    "Claude Max subscription is a separate product and grants no API access"
)

#: Substrings Anthropic uses when the account is out of credit. Matched to *route*
#: the failure, never echoed: an SDK message can contain the key it was called with
#: (security review, SEC-003).
_BILLING_MARKERS: Final = ("credit balance", "billing", "quota", "insufficient")


class AnthropicMessagesLike(Protocol):
    """The one SDK method this adapter uses."""

    async def create(self, **kwargs: Any) -> Any: ...


class AnthropicClientLike(Protocol):
    """The seam the contract doubles substitute for ``AsyncAnthropic``."""

    @property
    def messages(self) -> AnthropicMessagesLike: ...


def anthropic_capabilities(model: str) -> ProviderCapabilities:
    """Static capabilities for one Anthropic model, or a configuration error."""
    facts = ANTHROPIC_MODELS.get(model)
    if facts is None:
        known = ", ".join(sorted(ANTHROPIC_MODELS))
        raise ProviderNotConfigured(
            f"unknown Anthropic model {model!r}; this instance knows: {known}"
        )
    context_window, vision, structured_output, prompt_caching = facts
    return ProviderCapabilities(
        provider=PROVIDER_CODE,
        model=model,
        context_window=context_window,
        supports_structured_output=structured_output,
        supports_vision=vision,
        supports_prompt_caching=prompt_caching,
        source=CapabilitySource.STATIC,
    )


def build_client(api_key: str, *, timeout_seconds: float) -> AnthropicClientLike:
    """Wrap the real SDK client behind the narrow seam this adapter depends on."""
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)
    return cast(AnthropicClientLike, client)


class AnthropicTransport(ProviderTransport):
    """Speaks the Messages API, and translates everything it can raise."""

    def __init__(
        self,
        client: AnthropicClientLike,
        capabilities: ProviderCapabilities,
        *,
        api_key: str | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        # Held only to scrub it out of any diagnostic we build. Never logged, never
        # interpolated into a message.
        self._secrets = (api_key,) if api_key else ()

    async def complete(self, request: CompletionRequest) -> Completion:
        payload = self._payload(request)
        try:
            message = await self._client.messages.create(**payload)
        except anthropic.AnthropicError as exc:
            raise self._translate(exc) from None
        return Completion(text=self._text_of(message), usage=_usage_of(message))

    # -- request ---------------------------------------------------------- #

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": request.system}
        if self.capabilities.supports_prompt_caching:
            # The breakpoint sits at the end of the stable prefix. Everything after
            # it -- the household's stock -- is expected to differ every time.
            system_block["cache_control"] = {"type": "ephemeral"}

        content: list[dict[str, Any]] = []
        if request.image is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": request.image.media_type,
                        "data": _b64(request.image.data),
                    },
                }
            )
        content.append({"type": "text", "text": request.user})

        output_config: dict[str, Any] = {"effort": _EFFORT}
        if request.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": dict(request.schema)}

        return {
            "model": self.capabilities.model,
            "max_tokens": max(request.max_output_tokens, _MAX_OUTPUT_TOKENS),
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "system": [system_block],
            "messages": [{"role": "user", "content": content}],
        }

    # -- response --------------------------------------------------------- #

    def _text_of(self, message: object) -> str:
        blocks = getattr(message, "content", None)
        parts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts)
        if not joined.strip():
            stop_reason = getattr(message, "stop_reason", None)
            raise ProviderResponseInvalid(
                "Anthropic returned no text content"
                + (f" (stop_reason={stop_reason!r})" if isinstance(stop_reason, str) else ""),
                context=self.context("empty_response"),
            )
        return joined

    # -- failure translation ---------------------------------------------- #

    def _translate(self, exc: anthropic.AnthropicError) -> Exception:
        """Map an SDK failure onto the domain, without quoting the SDK.

        Nothing derived from ``exc`` reaches a message except a status code and our
        own routing decision. That is the whole point: an SDK error string can carry
        the household's API key, and a key in a log line outlives every rotation.
        """
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderUnavailable(
                "Anthropic did not answer within the configured timeout",
                context=self.context("timeout"),
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderUnavailable(
                "Anthropic could not be reached", context=self.context("connection_refused")
            )
        if isinstance(exc, anthropic.APIStatusError):
            return self._translate_status(exc)
        return ProviderUnavailable(
            "the Anthropic client failed before a response was received",
            context=self.context("client_error"),
        )

    def _translate_status(self, exc: anthropic.APIStatusError) -> Exception:
        status = exc.status_code
        if status in (401, 403):
            return ProviderNotConfigured(
                f"Anthropic rejected the credentials ({status}); {_SUBSCRIPTION_HINT}",
                context=self.context("invalid_credentials"),
            )
        if status == 429:
            return ProviderQuotaExceeded(
                "Anthropic rate limit reached; the household's own limits apply",
                context=self.context("rate_limited"),
            )
        if status == 400 and self._looks_like_billing(exc):
            return ProviderQuotaExceeded(
                "the Anthropic account has no credit left for this request",
                context=self.context("quota_exhausted"),
            )
        if status == 413:
            return ProviderUnavailable(
                "Anthropic refused the request as too large",
                context=self.context("request_too_large"),
            )
        if status >= 500:
            return ProviderUnavailable(
                f"Anthropic returned a server error ({status})",
                context=self.context("server_error"),
            )
        return ProviderUnavailable(
            f"Anthropic returned an unexpected status ({status})",
            context=self.context("unexpected_status"),
        )

    @staticmethod
    def _looks_like_billing(exc: anthropic.APIStatusError) -> bool:
        haystack = ""
        body = exc.body
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    haystack = message
        if not haystack:
            haystack = str(exc)
        lowered = haystack.lower()
        return any(marker in lowered for marker in _BILLING_MARKERS)


def _usage_of(message: object) -> TokenUsage | None:
    """Anthropic's four counters, mapped onto the domain's three.

    The SDK reports ``input_tokens`` (uncached), ``cache_read_input_tokens`` (served
    from the prompt cache at ~0.1x) and ``cache_creation_input_tokens`` (written to
    the cache at ~1.25x) as three separate figures, plus ``output_tokens``.

    Cache *writes* are folded into :attr:`TokenUsage.input_tokens` rather than
    dropped. They are prompt tokens the household was billed for, at a premium; the
    domain's split is "paid full rate or better" against "served cheap from cache",
    and a write belongs on the first side. Dropping them would under-report the
    prompt of the very first call of a cached conversation -- the one call that
    proves prompt caching is doing anything.
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    uncached = token_count(getattr(usage, "input_tokens", None))
    written = token_count(getattr(usage, "cache_creation_input_tokens", None))
    reported = TokenUsage(
        input_tokens=uncached if written is None else (uncached or 0) + written,
        output_tokens=token_count(getattr(usage, "output_tokens", None)),
        cached_input_tokens=token_count(getattr(usage, "cache_read_input_tokens", None)),
    )
    return reported if reported.is_known else None


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
