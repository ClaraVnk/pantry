"""The provider-independent half of every adapter.

An adapter has exactly two jobs: speak its vendor's protocol, and translate that
vendor's failures into domain errors. Everything else -- how a missing capability is
compensated for, how a malformed answer is retried, what the degraded-mode notice
says -- is identical across the five providers of ADR-0005 and lives here.

That is deliberate. The ADR accepts five adapters on the strength of a bounded
per-provider cost; if each one re-decided its own degradation behaviour, the
"capability x feature" matrix would be five times larger and the taxonomy would
drift into "whatever that adapter happens to do" -- the exact accident it exists to
prevent. A provider contributes a :class:`ProviderTransport`; the two ports below
are written once.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from chaudron.domain.llm_ports import (
    ParsedReceipt,
    ProviderCapabilities,
    ProviderCapabilityUnavailable,
    ProviderContext,
    ProviderResponseInvalid,
    RecipeRequest,
    RecipeSuggestion,
    RecipeSuggestions,
    TokenUsage,
)
from chaudron.infra.llm.payloads import (
    RECEIPT_SCHEMA,
    RECIPE_SCHEMA,
    read_receipt,
    read_recipes,
)
from chaudron.infra.llm.prompts import (
    RECEIPT_SYSTEM_PROMPT,
    RECIPE_SYSTEM_PROMPT,
    emulation_clause,
    receipt_user_prompt,
    recipe_user_prompt,
    trim_inventory,
)

__all__ = [
    "Completion",
    "CompletionRequest",
    "ModelReceiptParser",
    "ModelRecipeGenerator",
    "ProviderTransport",
    "ReceiptImage",
]

#: How much of the stock a short-context model is shown. Small enough to leave room
#: for the instructions and the answer on a 4k-8k local model, large enough that the
#: suggestions are still about *this* household.
DEGRADED_INVENTORY_LIMIT: Final = 25

#: Bounded, as ADR-0005 requires: the emulated path retries, it does not loop. Two
#: attempts catch the common "wrapped the JSON in prose" miss; a third mostly buys
#: latency and tokens on a model that is not going to comply.
EMULATION_ATTEMPTS: Final = 2

_RETRY_REMINDER: Final = (
    "\n\nYour previous answer could not be parsed. Answer again with the JSON object "
    "alone: no prose, no markdown fence, nothing before or after it."
)

_VISION_REMEDY: Final = (
    "choose a model that can read images (or, on Ollama, pull a vision model such as "
    "llava or a multimodal llama) and re-run the capability probe"
)


@dataclass(frozen=True, slots=True)
class ReceiptImage:
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One call to a model, expressed without any vendor vocabulary.

    ``system`` is the stable prefix and ``user`` the volatile remainder; the split is
    load-bearing for prompt caching, and transports that support it put their cache
    breakpoint at the end of ``system``.
    """

    system: str
    user: str
    #: The schema to constrain decoding with, or ``None`` when the provider cannot
    #: and the shape was asked for in ``system`` instead.
    schema: Mapping[str, Any] | None = None
    image: ReceiptImage | None = None
    max_output_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class Completion:
    """One model answer: the text, and what obtaining it consumed.

    ``usage`` is ``None`` when the provider reported nothing -- an older Ollama, a
    gateway that strips the block, a response shape that moved. It is never
    fabricated: see :class:`~chaudron.domain.llm_ports.TokenUsage` for why a zero
    would be worse than a blank.
    """

    text: str
    usage: TokenUsage | None = None


class ProviderTransport(abc.ABC):
    """What a provider must supply: one call, and its failures already translated.

    Implementations raise only :class:`chaudron.domain.llm_ports.LlmError`
    subclasses. Nothing that reaches this boundary may quote an SDK message, which
    can contain the household's API key (security review, SEC-003).
    """

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self._capabilities = capabilities
        self._secrets: tuple[str, ...] = ()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def secrets(self) -> tuple[str, ...]:
        """The credential this transport authenticates with, if it holds one.

        Exposed so that the *readers* -- which quote the model's answer when it
        cannot be parsed -- can remove it by literal match. A pattern cannot be
        relied on here: a key with no published prefix (Mistral AI's is 32 bare
        alphanumerics, a self-hosted gateway's is whatever it chose) is recognised
        by shape only approximately, and the excerpt is exactly the place where a
        gateway that echoes ``Authorization`` puts one.
        """
        return self._secrets

    def context(self, failure_mode: str | None = None) -> ProviderContext:
        return ProviderContext(
            provider=self._capabilities.provider,
            model=self._capabilities.model,
            failure_mode=failure_mode,
        )

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> Completion:
        """Return the model's answer, with the usage the provider reported alongside.

        The answer is raw text (expected to contain the JSON). Reporting usage is
        part of the contract precisely because it is *optional data*: an adapter
        that cannot obtain it returns ``usage=None`` rather than zeros, and the
        absence travels intact to the interface.
        """


class ModelRecipeGenerator:
    """:class:`~chaudron.domain.llm_ports.RecipeGenerator` over any transport."""

    def __init__(self, transport: ProviderTransport) -> None:
        self._transport = transport

    async def suggest(self, request: RecipeRequest) -> list[RecipeSuggestion]:
        capabilities = self._transport.capabilities
        native_schema = capabilities.supports_structured_output

        inventory = request.inventory
        notice: str | None = None
        if not capabilities.supports_long_context:
            inventory, trimmed = trim_inventory(inventory, keep=DEGRADED_INVENTORY_LIMIT)
            notice = _long_context_notice(
                capabilities=capabilities,
                trimmed=trimmed,
                kept=len(inventory),
                total=len(request.inventory),
            )

        system = RECIPE_SYSTEM_PROMPT
        if not native_schema:
            system += emulation_clause(RECIPE_SCHEMA)

        user = recipe_user_prompt(request, inventory)
        completion = await self._complete_with_retry(
            system=system,
            user=user,
            schema=RECIPE_SCHEMA if native_schema else None,
            native_schema=native_schema,
        )
        suggestions = read_recipes(
            completion.text,
            context=self._transport.context("schema_violation"),
            secrets=self._transport.secrets,
        )
        return RecipeSuggestions(
            suggestions[: request.max_suggestions],
            degradation_notice=notice,
            usage=completion.usage,
        )

    async def _complete_with_retry(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any] | None,
        native_schema: bool,
    ) -> Completion:
        """The answer, and the usage of **every** attempt it took to get it.

        Totalling the attempts rather than reporting the last one is the honest
        reading: a household on a model without native structured output pays for
        the retry too, and that surcharge is the concrete form of the abstract
        "emulated" case in ``DEGRADATION_POLICY``.
        """
        attempts = 1 if native_schema else EMULATION_ATTEMPTS
        last: ProviderResponseInvalid | None = None
        spent: list[TokenUsage | None] = []
        for attempt in range(attempts):
            body = user if attempt == 0 else user + _RETRY_REMINDER
            completion = await self._transport.complete(
                CompletionRequest(system=system, user=body, schema=schema)
            )
            spent.append(completion.usage)
            if attempt == attempts - 1:
                return Completion(text=completion.text, usage=TokenUsage.total(spent))
            try:
                read_recipes(
                    completion.text,
                    context=self._transport.context("schema_violation"),
                    secrets=self._transport.secrets,
                )
            except ProviderResponseInvalid as exc:
                last = exc
                continue
            return Completion(text=completion.text, usage=TokenUsage.total(spent))
        raise (
            last
            if last is not None
            else ProviderResponseInvalid(  # pragma: no cover
                "provider produced no answer", context=self._transport.context()
            )
        )


class ModelReceiptParser:
    """:class:`~chaudron.domain.llm_ports.ReceiptParser` over any transport.

    Refuses outright when the configured model has no vision: a model that has not
    seen the image would happily produce a plausible receipt, and a fabricated
    grocery list is worse than a disabled button.
    """

    def __init__(self, transport: ProviderTransport) -> None:
        self._transport = transport

    async def parse(self, image: bytes, media_type: str) -> ParsedReceipt:
        capabilities = self._transport.capabilities
        if not capabilities.supports_vision:
            raise ProviderCapabilityUnavailable(
                "vision",
                context=self._transport.context("capability_absent"),
                remedy=_VISION_REMEDY,
            )
        native_schema = capabilities.supports_structured_output
        system = RECEIPT_SYSTEM_PROMPT
        if not native_schema:
            system += emulation_clause(RECEIPT_SCHEMA)

        attempts = 1 if native_schema else EMULATION_ATTEMPTS
        failure: ProviderResponseInvalid | None = None
        for attempt in range(attempts):
            user = receipt_user_prompt()
            if attempt:
                user += _RETRY_REMINDER
            completion = await self._transport.complete(
                CompletionRequest(
                    system=system,
                    user=user,
                    schema=RECEIPT_SCHEMA if native_schema else None,
                    image=ReceiptImage(data=image, media_type=media_type),
                )
            )
            try:
                # Receipt usage is measured by the transport but not carried into
                # `ParsedReceipt`: the receipt-import flow has no column and no
                # contract field to put it in yet, and widening a domain type for a
                # reader that does not exist is the abstraction to postpone.
                return read_receipt(
                    completion.text,
                    context=self._transport.context("schema_violation"),
                    secrets=self._transport.secrets,
                )
            except ProviderResponseInvalid as exc:
                failure = exc
        raise (
            failure
            if failure is not None
            else ProviderResponseInvalid(  # pragma: no cover
                "provider produced no answer", context=self._transport.context()
            )
        )


def _long_context_notice(
    *, capabilities: ProviderCapabilities, trimmed: bool, kept: int, total: int
) -> str:
    """Text for the persistent degraded-mode indicator, in plain language.

    Emitted whenever the capability is absent, not only when the trimming actually
    cut something: the user has to know the ceiling exists *before* their stock
    grows past it, which is precisely when they would otherwise notice.
    """
    base = (
        f"{capabilities.provider}/{capabilities.model} has a short context window "
        f"({capabilities.context_window} tokens), so suggestions are computed on a "
        f"subset of the inventory: the {DEGRADED_INVENTORY_LIMIT} items closest to "
        "their expiry date."
    )
    if trimmed:
        return f"{base} {kept} of {total} items were sent."
    return f"{base} All {total} items fitted this time."
