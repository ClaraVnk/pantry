"""Google Gemini adapter.

Gemini is the one first-rank provider whose structured-output dialect is not JSON
Schema but a restricted OpenAPI subset: nullable types are a ``nullable`` flag
rather than a type union, and ``additionalProperties`` is rejected outright. Sending
our schema verbatim would 400.

:func:`to_gemini_schema` is the translation. It is deliberately small and total: it
loses no constraint that matters to the readers in
:mod:`chaudron.infra.llm.payloads`, and anything it cannot express is simply dropped
rather than approximated -- the server-side reader is the real gate either way.

The alternative -- declaring Gemini as "no structured output" and routing it through
the emulated path -- would have been less code and a worse product: it would trade a
guaranteed shape for a prompt-and-pray, for a provider that has the feature.
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
    "GEMINI_MODELS",
    "PROVIDER_CODE",
    "GeminiTransport",
    "build_gemini_client",
    "gemini_capabilities",
    "to_gemini_schema",
]

PROVIDER_CODE: Final = "gemini"
_LABEL: Final = "Google AI"
_BASE_URL: Final = "https://generativelanguage.googleapis.com"

#: (context window, vision, structured output, prompt caching). Hand-maintained.
GEMINI_MODELS: Final[dict[str, tuple[int, bool, bool, bool]]] = {
    "gemini-1.5-pro": (2_000_000, True, True, True),
    "gemini-1.5-flash": (1_000_000, True, True, True),
}

_CREDENTIALS_HINT: Final = (
    "an API key from aistudio.google.com is required. A Gemini Advanced subscription "
    "is a separate product and grants no API access"
)

_MAX_OUTPUT_TOKENS: Final = 8_000


def gemini_capabilities(model: str) -> ProviderCapabilities:
    facts = GEMINI_MODELS.get(model)
    if facts is None:
        known = ", ".join(sorted(GEMINI_MODELS))
        raise ProviderNotConfigured(f"unknown Gemini model {model!r}; known models: {known}")
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


def build_gemini_client(
    api_key: str,
    settings: LlmSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GuardedHttpClient:
    """Guarded client for Google's endpoint.

    The key travels in a header rather than the documented ``?key=`` query
    parameter: query strings end up in access logs, proxies and error reports, and a
    credential that reaches a log survives every rotation policy (SEC-003).
    """
    return GuardedHttpClient(
        httpx.URL(_BASE_URL),
        settings,
        transport=transport,
        headers={"x-goog-api-key": api_key, "Accept": "application/json"},
    )


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a JSON Schema fragment into Gemini's OpenAPI-flavoured dialect."""
    result: dict[str, Any] = {}
    raw_type = schema.get("type")
    nullable = False
    if isinstance(raw_type, list):
        types = [entry for entry in raw_type if entry != "null"]
        nullable = len(types) != len(raw_type)
        raw_type = types[0] if types else "string"
    if isinstance(raw_type, str):
        result["type"] = raw_type.upper()
    if nullable:
        result["nullable"] = True
    if "description" in schema:
        result["description"] = schema["description"]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            name: to_gemini_schema(value)
            for name, value in properties.items()
            if isinstance(value, dict)
        }
    items = schema.get("items")
    if isinstance(items, dict):
        result["items"] = to_gemini_schema(items)
    required = schema.get("required")
    if isinstance(required, list) and required:
        result["required"] = list(required)
    # `additionalProperties` has no equivalent and is rejected by the API; the
    # server-side reader enforces the closed shape instead.
    return result


class GeminiTransport(ProviderTransport):
    """Speaks ``models/{model}:generateContent``."""

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

    async def complete(self, request: CompletionRequest) -> Completion:
        path = f"/v1beta/models/{self.capabilities.model}:generateContent"
        result = await self._client.post_json(
            path,
            self._payload(request),
            context=self.context(),
            provider_label=_LABEL,
        )
        if isinstance(result, HttpFailure):
            raise translate_http_status(
                result,
                self.context(),
                provider_label=_LABEL,
                credentials_hint=_CREDENTIALS_HINT,
            )
        return Completion(text=self._text_of(result), usage=_usage_of(result))

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if request.image is not None:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": request.image.media_type,
                        "data": base64.standard_b64encode(request.image.data).decode("ascii"),
                    }
                }
            )
        parts.append({"text": request.user})

        generation_config: dict[str, Any] = {
            "maxOutputTokens": max(request.max_output_tokens, _MAX_OUTPUT_TOKENS),
            "temperature": 0.2,
        }
        if request.schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = to_gemini_schema(dict(request.schema))

        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

    def _text_of(self, payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates")
        texts: list[str] = []
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                content = first.get("content")
                if isinstance(content, dict):
                    for part in content.get("parts") or ():
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
        joined = "".join(texts)
        if not joined.strip():
            raise ProviderResponseInvalid(
                f"{_LABEL} returned no candidate text",
                context=ProviderContext(
                    self.capabilities.provider, self.capabilities.model, "empty_response"
                ),
            )
        return joined


def _usage_of(payload: dict[str, Any]) -> TokenUsage | None:
    """Google's ``usageMetadata``, in domain terms.

    ``promptTokenCount`` is the all-in prompt figure and ``cachedContentTokenCount``
    a subset of it, so the same subtraction as the OpenAI shape applies. Note the
    camelCase: this is the one adapter whose wire format does not use snake_case,
    and a key read with the wrong spelling would silently report "unknown" forever.
    """
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    cached = token_count(usage.get("cachedContentTokenCount"))
    reported = TokenUsage(
        input_tokens=subtract_cached(token_count(usage.get("promptTokenCount")), cached),
        output_tokens=token_count(usage.get("candidatesTokenCount")),
        cached_input_tokens=cached,
    )
    return reported if reported.is_known else None
