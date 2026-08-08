"""The ADR-0005 taxonomy, checked as behaviour rather than as a declaration.

The conformance suite verifies that every adapter *declares* a case for each
capability it lacks. This file checks the three cases actually do what the ADR says
they do, on the shared implementation every adapter inherits:

* ``unavailable`` -- refuse, with the reason and the remedy;
* ``emulated`` -- still return a valid domain object, retrying a bounded number of
  times, with the loss in the failure rate rather than in the type;
* ``degraded`` -- answer *and* say what was left out.
"""

from __future__ import annotations

import datetime as dt

import pytest

from chaudron.domain.llm_ports import (
    CapabilitySource,
    DegradationStrategy,
    InventoryItem,
    ProviderCapabilities,
    ProviderCapabilityUnavailable,
    ProviderResponseInvalid,
    RecipeRequest,
    RecipeSuggestions,
    TokenUsage,
)
from chaudron.infra.llm.base import (
    DEGRADED_INVENTORY_LIMIT,
    EMULATION_ATTEMPTS,
    Completion,
    CompletionRequest,
    ModelReceiptParser,
    ModelRecipeGenerator,
    ProviderTransport,
)
from chaudron.infra.llm.doubles import valid_recipe_payload


def _capabilities(
    *,
    structured_output: bool = True,
    vision: bool = True,
    prompt_caching: bool = True,
    context_window: int = 1_000_000,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider="fake",
        model="fake-1",
        context_window=context_window,
        supports_structured_output=structured_output,
        supports_vision=vision,
        supports_prompt_caching=prompt_caching,
    )


class _ScriptedTransport(ProviderTransport):
    """Replays a fixed list of answers and records what it was asked.

    ``usage`` is what every scripted answer claims to have consumed. ``None`` stands
    in for a provider that reported nothing, which is a case the accumulation has to
    handle rather than round down to zero.
    """

    def __init__(
        self,
        capabilities: ProviderCapabilities,
        answers: list[str],
        *,
        usage: TokenUsage | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._answers = answers
        self._usage = usage
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if not self._answers:
            raise AssertionError("transport called more times than scripted")
        return Completion(text=self._answers.pop(0), usage=self._usage)


def _request(items: int) -> RecipeRequest:
    return RecipeRequest(
        inventory=tuple(
            InventoryItem(name=f"Item {index}", expires_in_days=index) for index in range(items)
        ),
        max_suggestions=3,
    )


# -- unavailable ------------------------------------------------------------ #


async def test_no_vision_refuses_with_a_reason_and_a_remedy() -> None:
    """Never a raw error, and above all never a receipt invented from nothing."""
    transport = _ScriptedTransport(_capabilities(vision=False), [])
    with pytest.raises(ProviderCapabilityUnavailable) as raised:
        await ModelReceiptParser(transport).parse(b"\x89PNG", "image/png")
    assert raised.value.capability == "vision"
    assert raised.value.remedy
    # The model was never called: it cannot be given the chance to make one up.
    assert transport.requests == []


# -- emulated --------------------------------------------------------------- #


async def test_emulation_asks_for_the_schema_in_the_prompt() -> None:
    transport = _ScriptedTransport(_capabilities(structured_output=False), [valid_recipe_payload()])
    result = await ModelRecipeGenerator(transport).suggest(_request(3))
    assert result[0].title
    sent = transport.requests[0]
    assert sent.schema is None
    assert "JSON Schema" in sent.system


async def test_native_structured_output_hands_the_schema_over_instead() -> None:
    transport = _ScriptedTransport(_capabilities(), [valid_recipe_payload()])
    await ModelRecipeGenerator(transport).suggest(_request(3))
    sent = transport.requests[0]
    assert sent.schema is not None
    assert "JSON Schema" not in sent.system


async def test_emulation_retries_once_then_succeeds() -> None:
    """The documented loss is a higher failure rate, absorbed by a bounded retry."""
    transport = _ScriptedTransport(
        _capabilities(structured_output=False),
        ["Sure! Here are some ideas:", valid_recipe_payload()],
    )
    result = await ModelRecipeGenerator(transport).suggest(_request(3))
    assert len(result) == 1
    assert len(transport.requests) == EMULATION_ATTEMPTS
    assert "could not be parsed" in transport.requests[1].user


async def test_emulation_gives_up_rather_than_looping() -> None:
    transport = _ScriptedTransport(
        _capabilities(structured_output=False), ["nope"] * EMULATION_ATTEMPTS
    )
    with pytest.raises(ProviderResponseInvalid):
        await ModelRecipeGenerator(transport).suggest(_request(3))
    assert len(transport.requests) == EMULATION_ATTEMPTS


async def test_a_native_provider_does_not_retry() -> None:
    transport = _ScriptedTransport(_capabilities(), ["nope"])
    with pytest.raises(ProviderResponseInvalid):
        await ModelRecipeGenerator(transport).suggest(_request(3))
    assert len(transport.requests) == 1


# -- what the emulated path costs ------------------------------------------- #


async def test_a_retry_is_billed_on_top_rather_than_replacing_the_first_attempt() -> None:
    """The surcharge of the emulated case, as a number instead of a paragraph.

    ADR-0005 accepts "emulated" on the promise that the loss is a higher failure
    rate rather than a broken type. It is also a *cost*, and this is where that cost
    becomes visible: the retry the household paid for is added to the total, not
    overwritten by it. Reporting only the successful attempt would make the
    degraded path look exactly as cheap as the native one.
    """
    per_call = TokenUsage(input_tokens=1_000, output_tokens=200, cached_input_tokens=50)
    transport = _ScriptedTransport(
        _capabilities(structured_output=False),
        ["Sure! Here are some ideas:", valid_recipe_payload()],
        usage=per_call,
    )

    result = await ModelRecipeGenerator(transport).suggest(_request(3))

    assert len(transport.requests) == EMULATION_ATTEMPTS
    assert isinstance(result, RecipeSuggestions)
    assert result.usage == TokenUsage(
        input_tokens=2_000, output_tokens=400, cached_input_tokens=100
    )


async def test_a_single_native_call_reports_exactly_what_it_consumed() -> None:
    per_call = TokenUsage(input_tokens=1_000, output_tokens=200, cached_input_tokens=50)
    transport = _ScriptedTransport(_capabilities(), [valid_recipe_payload()], usage=per_call)

    result = await ModelRecipeGenerator(transport).suggest(_request(3))

    assert isinstance(result, RecipeSuggestions)
    assert result.usage == per_call


async def test_a_provider_that_reports_nothing_yields_no_usage_at_all() -> None:
    """The load-bearing negative: silence must not arrive as a free call."""
    transport = _ScriptedTransport(_capabilities(), [valid_recipe_payload()], usage=None)

    result = await ModelRecipeGenerator(transport).suggest(_request(3))

    assert isinstance(result, RecipeSuggestions)
    assert result.usage is None, "unknown usage must stay unknown, never become zero"


# -- degraded --------------------------------------------------------------- #


async def test_short_context_trims_the_inventory_and_says_so() -> None:
    transport = _ScriptedTransport(_capabilities(context_window=8192), [valid_recipe_payload()])
    result = await ModelRecipeGenerator(transport).suggest(_request(80))
    assert isinstance(result, RecipeSuggestions)
    notice = result.degradation_notice
    assert notice is not None
    assert str(DEGRADED_INVENTORY_LIMIT) in notice
    assert "80" in notice
    # And the trimming is not arbitrary: what is closest to spoiling is kept.
    sent = transport.requests[0].user
    assert '"Item 0"' in sent
    assert "Item 79" not in sent


async def test_the_notice_is_emitted_even_when_nothing_was_cut() -> None:
    """The ceiling must be known before the stock grows past it, not after."""
    transport = _ScriptedTransport(_capabilities(context_window=8192), [valid_recipe_payload()])
    result = await ModelRecipeGenerator(transport).suggest(_request(2))
    assert isinstance(result, RecipeSuggestions)
    assert result.degradation_notice is not None
    assert "All 2 items" in result.degradation_notice


async def test_a_full_context_provider_carries_no_notice() -> None:
    transport = _ScriptedTransport(_capabilities(), [valid_recipe_payload()])
    result = await ModelRecipeGenerator(transport).suggest(_request(80))
    assert isinstance(result, RecipeSuggestions)
    assert result.degradation_notice is None
    assert result == list(result)  # still a plain list to every caller


# -- the decision table itself ---------------------------------------------- #


def test_every_missing_capability_has_exactly_one_declared_case() -> None:
    capabilities = _capabilities(
        structured_output=False, vision=False, prompt_caching=False, context_window=4096
    )
    assert set(capabilities.missing) == {
        "structured_output",
        "vision",
        "prompt_caching",
        "long_context",
    }
    assert capabilities.degradation_for("vision") is DegradationStrategy.UNAVAILABLE
    assert capabilities.degradation_for("structured_output") is DegradationStrategy.EMULATED
    assert capabilities.degradation_for("prompt_caching") is DegradationStrategy.EMULATED
    assert capabilities.degradation_for("long_context") is DegradationStrategy.DEGRADED


def test_a_present_capability_declares_nothing() -> None:
    assert _capabilities().degradation_for("vision") is None
    assert not _capabilities().is_degraded


def test_probed_capabilities_must_be_timestamped() -> None:
    """The whole point of the provenance: only a probe can go stale."""
    with pytest.raises(ValueError, match="probed"):
        ProviderCapabilities(
            provider="ollama",
            model="llama3.2",
            context_window=8192,
            supports_structured_output=True,
            supports_vision=False,
            supports_prompt_caching=False,
            source=CapabilitySource.PROBED,
        )


def test_static_capabilities_cannot_pretend_to_be_probed() -> None:
    with pytest.raises(ValueError, match="static"):
        ProviderCapabilities(
            provider="anthropic",
            model="claude-opus-5",
            context_window=1_000_000,
            supports_structured_output=True,
            supports_vision=True,
            supports_prompt_caching=True,
            probed_at=dt.datetime.now(dt.UTC),
        )


def test_staleness_is_only_meaningful_for_a_probe() -> None:
    now = dt.datetime(2026, 8, 3, tzinfo=dt.UTC)
    fresh = ProviderCapabilities(
        provider="ollama",
        model="llama3.2",
        context_window=8192,
        supports_structured_output=True,
        supports_vision=False,
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=now - dt.timedelta(days=1),
    )
    stale = ProviderCapabilities(
        provider="ollama",
        model="llama3.2",
        context_window=8192,
        supports_structured_output=True,
        supports_vision=False,
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=now - dt.timedelta(days=90),
    )
    horizon = dt.timedelta(days=30)
    assert fresh.stale_since(now, horizon) is None
    assert stale.stale_since(now, horizon) == dt.timedelta(days=60)
    assert _capabilities().stale_since(now, horizon) is None
