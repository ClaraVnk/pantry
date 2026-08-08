"""Ollama adapter -- local inference, server to server, with *probed* capabilities.

Two things make this adapter structurally different from the four hosted ones, and
both come straight from the ADRs.

**The capabilities are asked for, not looked up.** The same endpoint serves a vision
model or a text-only one depending on a free-text model name the household chose; no
embedded table can know. :func:`probe_capabilities` interrogates the instance, and
the answer is stamped with the date it was obtained so the interface can say "this is
what your Ollama replied on the 3rd of March" and offer a refresh. That is what
``CapabilitySource.PROBED`` buys, and it is also three error paths no other adapter
has: unreachable instance, stale declaration, refresh.

**The URL is hostile input.** It is supplied by the household and dialled by the
server. Every call goes through :class:`~chaudron.infra.llm.http.GuardedHttpClient`,
whose guard is described in that module: allowlist, scheme restriction, pinned DNS,
no redirects, bounded time and size.

Only the co-located topology is supported (ADR-0007): the instance must be reachable
*from the server*. The browser-mediated path for an Ollama behind a NAT is a
documented extension, not a work in progress.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any, Final

import httpx

from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderContext,
    ProviderResponseInvalid,
    TokenUsage,
)
from chaudron.infra.llm.base import Completion, CompletionRequest, ProviderTransport
from chaudron.infra.llm.http import (
    GuardedHttpClient,
    HttpFailure,
    Resolver,
    resolve_pin,
    translate_http_status,
    validate_ollama_base_url,
)
from chaudron.infra.llm.settings import _DEFAULT_MAX_NUM_CTX, LlmSettings
from chaudron.infra.llm.usage import token_count

__all__ = [
    "PROVIDER_CODE",
    "OllamaTransport",
    "build_guarded_client",
    "build_pinned_client",
    "probe_capabilities",
]

PROVIDER_CODE: Final = "ollama"
_LABEL: Final = "Ollama"

#: Structured outputs (``format`` as a JSON Schema) are enforced by the Ollama
#: runtime rather than by the model, so the capability is a property of the server
#: version. Below this, ``format`` only accepts the string ``"json"`` and the schema
#: is not enforced -- which is the emulated path, not the native one.
_STRUCTURED_OUTPUT_MIN_VERSION: Final = (0, 5, 0)

#: When ``/api/show`` reports no context length at all. Deliberately small: assuming
#: a large window we have not been told about is how the degraded mode silently
#: stops being applied.
_FALLBACK_CONTEXT_WINDOW: Final = 4096

_UNREACHABLE_HINT: Final = (
    "the instance must be reachable from the Chaudron server (co-located topology); "
    "an Ollama running on your own machine or LAN is not supported in v1"
)


def build_guarded_client(
    base_url: str,
    settings: LlmSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    pinned_addresses: frozenset[str] | None = None,
) -> GuardedHttpClient:
    """Validate the household's URL and wrap it in the guarded client.

    Raises :class:`ProviderNotConfigured` for anything the allowlist refuses, which
    is what makes "your URL is not permitted" a save-time message rather than a
    runtime one.
    """
    url = validate_ollama_base_url(base_url, settings)
    return GuardedHttpClient(
        url,
        settings,
        transport=transport,
        resolver=resolver,
        pinned_addresses=pinned_addresses,
    )


async def build_pinned_client(
    base_url: str,
    settings: LlmSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> GuardedHttpClient:
    """:func:`build_guarded_client`, with the pin *obtained* rather than supplied.

    This is the production entry point, and the reason the whole build path is
    asynchronous. The rebinding check needs two DNS answers taken at different
    moments; only a caller that can await a resolver is able to take the first one.
    A synchronous build could pass a resolver but never a pin, which yields a client
    whose check returns on its first line -- present, plausible, and inert.

    ``build_guarded_client`` stays synchronous beside it because the conformance
    suite and the SSRF tests supply their own pin and must not need an event loop
    to construct a client.
    """
    if resolver is None:
        # No resolver means no pinning by design: the first-party providers, and the
        # tests that exercise the other guards on their own.
        return build_guarded_client(base_url, settings, transport=transport)
    # Validated twice on this path -- here for the URL to resolve, then again inside
    # the builder. It is a pure function over a short string; paying it keeps a
    # single construction path rather than two that can drift.
    url = validate_ollama_base_url(base_url, settings)
    return build_guarded_client(
        base_url,
        settings,
        transport=transport,
        resolver=resolver,
        pinned_addresses=await resolve_pin(url, resolver),
    )


async def probe_capabilities(
    client: GuardedHttpClient,
    model: str,
    *,
    now: dt.datetime | None = None,
) -> ProviderCapabilities:
    """Ask the instance what the configured model can do.

    Called when a household saves an ``ollama`` configuration, and again whenever it
    asks for a refresh. An unreachable instance fails the save rather than storing a
    configuration whose abilities nobody knows -- ADR-0007 is explicit that guessing
    here is worse than refusing.
    """
    context = _context(model)
    version = await _get_version(client, model)
    shown = await client.post_json(
        "/api/show", {"model": model}, context=context, provider_label=_LABEL
    )
    if isinstance(shown, HttpFailure):
        raise translate_http_status(
            shown,
            context,
            provider_label=_LABEL,
            not_found_hint=f"pull it first with `ollama pull {model}`",
        )

    raw_capabilities = shown.get("capabilities")
    capabilities = {entry.lower() for entry in raw_capabilities or () if isinstance(entry, str)}
    if not capabilities:
        raise ProviderResponseInvalid(
            "Ollama did not report the model's capabilities; the instance is "
            "probably too old to be probed",
            context=context,
        )

    return ProviderCapabilities(
        provider=PROVIDER_CODE,
        model=model,
        context_window=_context_window(shown),
        supports_structured_output=version >= _STRUCTURED_OUTPUT_MIN_VERSION,
        supports_vision="vision" in capabilities,
        # Nothing in Ollama's API resembles a reusable prompt prefix; the runtime's
        # own KV cache is not something we can address or rely on across calls.
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=now or dt.datetime.now(dt.UTC),
    )


async def _get_version(client: GuardedHttpClient, model: str) -> tuple[int, ...]:
    context = _context(model)
    # `/api/version` answers GET only. It was previously called with POST, on the
    # written assumption that Ollama tolerated it — real Ollama 0.32 returns 405,
    # so every capability probe failed and no `ollama` household could ever be
    # marked configured. The hand-written test double had encoded the assumption
    # rather than the behaviour, which is why the suite stayed green.
    payload = await client.get_json("/api/version", context=context, provider_label=_LABEL)
    if isinstance(payload, HttpFailure):
        raise translate_http_status(payload, context, provider_label=_LABEL)
    raw = payload.get("version")
    if not isinstance(raw, str):
        return (0, 0, 0)
    parts: list[int] = []
    for chunk in raw.split("-")[0].split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts) or (0, 0, 0)


def _context_window(shown: dict[str, Any]) -> int:
    info = shown.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if (
                isinstance(key, str)
                and key.endswith(".context_length")
                and isinstance(value, int)
                and value > 0
            ):
                return value
    return _FALLBACK_CONTEXT_WINDOW


def _context(model: str) -> ProviderContext:
    return ProviderContext(provider=PROVIDER_CODE, model=model)


class OllamaTransport(ProviderTransport):
    """Speaks ``/api/chat``, with the household's probed capabilities attached."""

    def __init__(
        self,
        client: GuardedHttpClient,
        capabilities: ProviderCapabilities,
        *,
        max_num_ctx: int = _DEFAULT_MAX_NUM_CTX,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._max_num_ctx = max_num_ctx

    async def complete(self, request: CompletionRequest) -> Completion:
        payload = self._payload(request)
        result = await self._client.post_json(
            "/api/chat",
            payload,
            context=self.context(),
            provider_label=_LABEL,
        )
        if isinstance(result, HttpFailure):
            raise translate_http_status(
                result,
                self.context(),
                provider_label=_LABEL,
                not_found_hint=(
                    f"pull it first with `ollama pull {self.capabilities.model}`; "
                    + _UNREACHABLE_HINT
                ),
            )
        return Completion(text=self._text_of(result), usage=_usage_of(result))

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        user: dict[str, Any] = {"role": "user", "content": request.user}
        if request.image is not None:
            # Ollama takes images as bare base64 on the message, with no media type:
            # the runtime sniffs the format itself.
            user["images"] = [base64.standard_b64encode(request.image.data).decode("ascii")]
        payload: dict[str, Any] = {
            "model": self.capabilities.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": request.system},
                user,
            ],
            "options": {
                # Two failure modes pull in opposite directions here.
                #
                # Ollama's own default window is far smaller than most models
                # support, and it truncates a full inventory in silence — the
                # suggestions then quietly ignore half the fridge.
                #
                # Asking for the model's *maximum* is worse. Ollama allocates the
                # KV cache for the declared window up front, whatever the prompt
                # length: on a 7.5 GB host with ~1 GB free, one suggestion at
                # 32768 took over ten minutes, and the 3B model was OOM-killed
                # outright. The same request at 4096 answered in five seconds.
                #
                # So: as much window as the model offers, bounded by what this
                # instance is willing to allocate.
                "num_ctx": min(self.capabilities.context_window, self._max_num_ctx),
                "temperature": 0.2,
            },
        }
        if request.schema is not None:
            payload["format"] = dict(request.schema)
        return payload

    def _text_of(self, payload: dict[str, Any]) -> str:
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseInvalid(
                "Ollama returned no message content",
                context=self.context("empty_response"),
            )
        return content


def _usage_of(payload: dict[str, Any]) -> TokenUsage | None:
    """Ollama's counters, which are named after evaluation rather than billing.

    ``prompt_eval_count`` is the prompt it evaluated and ``eval_count`` what it
    generated. Two Ollama-specific facts are encoded here rather than assumed:

    * ``prompt_eval_count`` is **omitted** when the runtime answered entirely from
      its own KV cache, so its absence is a real "unknown", not an error;
    * ``cached_input_tokens`` is ``0``, and that zero is a measurement rather than a
      default. Ollama exposes no addressable prompt cache -- which is exactly why
      ``supports_prompt_caching`` is ``False`` and the stable prefix is resent every
      call -- so nothing was served from one. This is the one place a zero is the
      truthful answer, and it is what makes the degraded-mode notice ("your model
      does not cache the instructions") a number the household can read.
    """
    reported = TokenUsage(
        input_tokens=token_count(payload.get("prompt_eval_count")),
        output_tokens=token_count(payload.get("eval_count")),
        cached_input_tokens=0,
    )
    # `is_known` is always true thanks to the zero above, so the real question is
    # whether the runtime said anything at all about the tokens it evaluated.
    if reported.input_tokens is None and reported.output_tokens is None:
        return None
    return reported
