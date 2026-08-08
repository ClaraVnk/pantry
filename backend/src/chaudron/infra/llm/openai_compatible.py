"""OpenAI and Mistral AI: one wire format, two capability tables.

Both expose the same ``/v1/chat/completions`` shape, so they share an adapter rather
than duplicating one. What differs is what each (provider, model) pair can actually
do -- and that difference is the point of ADR-0005's insistence that capabilities
belong to the pair, not the vendor: ``mistral-small`` has no vision and a short
context while ``pixtral`` has both, and the household picks between them for cost
reasons we do not get to override.

Mistral AI is also one of the two configurations that keep a household's food
consumption data inside EU jurisdiction (the other being Ollama). That is a
selection criterion the interface surfaces, not a footnote.

These two adapters are deliberately thinner than the Anthropic one: no SDK, no
provider-specific caching directive (OpenAI caches automatically and exposes no
knob). They are covered by the same conformance suite, which is what ADR-0005 says
bounds the cost of an additional provider.
"""

from __future__ import annotations

import base64
from typing import Any, Final

import httpx

from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderContext,
    ProviderNotConfigured,
    ProviderResponseInvalid,
    TokenUsage,
)
from chaudron.infra.llm.base import Completion, CompletionRequest, ProviderTransport
from chaudron.infra.llm.http import GuardedHttpClient, HttpFailure, translate_http_status
from chaudron.infra.llm.settings import LlmSettings
from chaudron.infra.llm.usage import subtract_cached, token_count

__all__ = [
    "MISTRAL_MODELS",
    "MISTRAL_PROVIDER_CODE",
    "OPENAI_MODELS",
    "OPENAI_PROVIDER_CODE",
    "ChatCompletionsTransport",
    "build_chat_client",
    "mistral_capabilities",
    "openai_capabilities",
]

OPENAI_PROVIDER_CODE: Final = "openai"
MISTRAL_PROVIDER_CODE: Final = "mistral"

_OPENAI_BASE_URL: Final = "https://api.openai.com"
_MISTRAL_BASE_URL: Final = "https://api.mistral.ai"

#: (context window, vision, structured output, prompt caching). Hand-maintained, and
#: an unknown model is refused rather than assumed -- see the Anthropic adapter for
#: why guessing here is the expensive option.
OPENAI_MODELS: Final[dict[str, tuple[int, bool, bool, bool]]] = {
    "gpt-4o": (128_000, True, True, True),
    "gpt-4o-mini": (128_000, True, True, True),
}

MISTRAL_MODELS: Final[dict[str, tuple[int, bool, bool, bool]]] = {
    # No vision, short context: the reference *degraded* configuration of the suite.
    "mistral-small-latest": (32_000, False, True, False),
    "pixtral-large-latest": (128_000, True, True, False),
}

_OPENAI_SUBSCRIPTION_HINT: Final = (
    "an OpenAI API key from platform.openai.com is required. A ChatGPT Plus "
    "subscription is a separate product and grants no API access"
)
_MISTRAL_CREDENTIALS_HINT: Final = "an API key from console.mistral.ai is required"

_MAX_OUTPUT_TOKENS: Final = 8_000


def _capabilities(
    provider: str, table: dict[str, tuple[int, bool, bool, bool]], model: str
) -> ProviderCapabilities:
    facts = table.get(model)
    if facts is None:
        known = ", ".join(sorted(table))
        raise ProviderNotConfigured(f"unknown {provider} model {model!r}; known models: {known}")
    context_window, vision, structured_output, prompt_caching = facts
    return ProviderCapabilities(
        provider=provider,
        model=model,
        context_window=context_window,
        supports_structured_output=structured_output,
        supports_vision=vision,
        supports_prompt_caching=prompt_caching,
        source=CapabilitySource.STATIC,
    )


def openai_capabilities(model: str) -> ProviderCapabilities:
    return _capabilities(OPENAI_PROVIDER_CODE, OPENAI_MODELS, model)


def mistral_capabilities(model: str) -> ProviderCapabilities:
    return _capabilities(MISTRAL_PROVIDER_CODE, MISTRAL_MODELS, model)


def build_chat_client(
    provider: str,
    api_key: str,
    settings: LlmSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GuardedHttpClient:
    """A guarded client aimed at the provider's own, non-user-supplied, base URL.

    No allowlist applies here: the host is ours, not the household's, so it is not
    an SSRF primitive. The other bounds -- timeout, response size, no redirects --
    still do.
    """
    base = _OPENAI_BASE_URL if provider == OPENAI_PROVIDER_CODE else _MISTRAL_BASE_URL
    return GuardedHttpClient(
        httpx.URL(base),
        settings,
        transport=transport,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )


class ChatCompletionsTransport(ProviderTransport):
    """One request shape, two providers, capabilities supplied by the caller."""

    def __init__(
        self,
        client: GuardedHttpClient,
        capabilities: ProviderCapabilities,
        *,
        api_key: str | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._secrets = (api_key,) if api_key else ()

    @property
    def _is_openai(self) -> bool:
        return self.capabilities.provider == OPENAI_PROVIDER_CODE

    @property
    def _label(self) -> str:
        return "OpenAI" if self._is_openai else "Mistral AI"

    async def complete(self, request: CompletionRequest) -> Completion:
        result = await self._client.post_json(
            "/v1/chat/completions",
            self._payload(request),
            context=self.context(),
            provider_label=self._label,
        )
        if isinstance(result, HttpFailure):
            raise translate_http_status(
                result,
                self.context(),
                provider_label=self._label,
                credentials_hint=(
                    _OPENAI_SUBSCRIPTION_HINT if self._is_openai else _MISTRAL_CREDENTIALS_HINT
                ),
            )
        return Completion(text=self._text_of(result), usage=_usage_of(result))

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if request.image is not None:
            data_url = f"data:{request.image.media_type};base64," + base64.standard_b64encode(
                request.image.data
            ).decode("ascii")
            # OpenAI expects an object here; Mistral AI expects the bare URL string.
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url} if self._is_openai else data_url,
                }
            )
        content.append({"type": "text", "text": request.user})

        payload: dict[str, Any] = {
            "model": self.capabilities.model,
            "max_tokens": max(request.max_output_tokens, _MAX_OUTPUT_TOKENS),
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": content},
            ],
        }
        if request.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "chaudron_payload",
                    "strict": True,
                    "schema": dict(request.schema),
                },
            }
        return payload

    def _text_of(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
        raise ProviderResponseInvalid(
            f"{self._label} returned no assistant content",
            context=ProviderContext(
                self.capabilities.provider, self.capabilities.model, "empty_response"
            ),
        )


def _usage_of(payload: dict[str, Any]) -> TokenUsage | None:
    """The ``usage`` block of a chat-completions answer, in domain terms.

    ``prompt_tokens`` is the **all-in** prompt count here -- it already contains the
    cached tokens that ``prompt_tokens_details.cached_tokens`` breaks out -- so the
    uncached remainder is what belongs in ``input_tokens``. Mistral AI reports no
    cache detail at all, which simply leaves the cached figure ``None`` and the
    prompt total intact: a provider without a prompt cache is not a provider whose
    cache served nothing.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    cached: int | None = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = token_count(details.get("cached_tokens"))
    reported = TokenUsage(
        input_tokens=subtract_cached(token_count(usage.get("prompt_tokens")), cached),
        output_tokens=token_count(usage.get("completion_tokens")),
        cached_input_tokens=cached,
    )
    return reported if reported.is_known else None
