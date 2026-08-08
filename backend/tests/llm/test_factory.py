"""Who is allowed to spend whose money (ADR-0007), asserted rather than documented.

``instance_owner`` is the only mode whose cost lands on the operator. The classic
failure it prevents is an operator sharing their instance and discovering the
arrangement on the next invoice, so the lock is tested from three angles: the wrong
household, no designated household at all, and a designated household with no key.
"""

from __future__ import annotations

import uuid

import pytest

from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderNotConfigured,
    ProviderNotPermitted,
)
from chaudron.domain.models import LlmProviderMode
from chaudron.infra.llm.factory import HouseholdProviderConfig, LlmProviderFactory
from chaudron.infra.llm.settings import (
    INSTANCE_OWNER_ENV_VAR,
    INSTANCE_OWNER_KEY_ENV_VAR,
    LlmSettings,
)

_OWNER = uuid.UUID(int=1)
_STRANGER = uuid.UUID(int=2)

_SETTINGS = LlmSettings(
    ollama_allowed_hosts=frozenset({"ollama"}),
    instance_owner_household_id=_OWNER,
    instance_owner_api_key="sk-ant-instance-owner-key",
)


def _config(
    household: uuid.UUID,
    mode: LlmProviderMode,
    *,
    provider_code: str = "anthropic",
    model: str = "claude-opus-5",
    base_url: str | None = None,
    api_key: str | None = None,
    probed_capabilities: ProviderCapabilities | None = None,
) -> HouseholdProviderConfig:
    return HouseholdProviderConfig(
        household_id=household,
        mode=mode,
        provider_code=provider_code,
        model=model,
        base_url=base_url,
        api_key=api_key,
        probed_capabilities=probed_capabilities,
    )


async def test_instance_owner_is_refused_to_every_other_household() -> None:
    factory = LlmProviderFactory(_SETTINGS)
    config = _config(_STRANGER, LlmProviderMode.INSTANCE_OWNER)
    with pytest.raises(ProviderNotPermitted) as raised:
        await factory.build_transport(config)
    message = str(raised.value)
    assert "reserved" in message
    # The refusal has to be actionable, not merely correct.
    assert "your own API key" in message


async def test_instance_owner_is_granted_to_the_designated_household() -> None:
    factory = LlmProviderFactory(_SETTINGS)
    transport = await factory.build_transport(_config(_OWNER, LlmProviderMode.INSTANCE_OWNER))
    assert transport.capabilities.model == "claude-opus-5"


async def test_instance_owner_is_closed_by_default() -> None:
    """No designated household means nobody -- including the operator."""
    factory = LlmProviderFactory(LlmSettings())
    with pytest.raises(ProviderNotPermitted) as raised:
        await factory.build_transport(_config(_OWNER, LlmProviderMode.INSTANCE_OWNER))
    assert INSTANCE_OWNER_ENV_VAR in str(raised.value)


async def test_designated_household_without_a_key_gets_the_remedy() -> None:
    settings = LlmSettings(instance_owner_household_id=_OWNER)
    factory = LlmProviderFactory(settings)
    with pytest.raises(ProviderNotConfigured) as raised:
        await factory.build_transport(_config(_OWNER, LlmProviderMode.INSTANCE_OWNER))
    assert INSTANCE_OWNER_KEY_ENV_VAR in str(raised.value)


async def test_byok_requires_a_stored_key() -> None:
    factory = LlmProviderFactory(_SETTINGS)
    with pytest.raises(ProviderNotConfigured):
        await factory.build_transport(_config(_STRANGER, LlmProviderMode.BYOK))


async def test_ollama_cannot_be_selected_as_a_byok_provider() -> None:
    """Ollama has no key; offering it in the BYOK list would only confuse."""
    factory = LlmProviderFactory(_SETTINGS)
    config = _config(
        _STRANGER, LlmProviderMode.BYOK, provider_code="ollama", model="llama3.2", api_key="x"
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        await factory.build_transport(config)
    assert "household key" in str(raised.value)


def test_ollama_without_a_probe_is_refused() -> None:
    """A configuration whose abilities nobody established cannot promise a feature."""
    factory = LlmProviderFactory(_SETTINGS)
    config = _config(
        _STRANGER,
        LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llama3.2",
        base_url="http://ollama:11434",
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        factory.capabilities_for(config)
    assert "probed" in str(raised.value)


async def test_ollama_uses_the_stored_probe() -> None:
    import datetime as dt

    probed = ProviderCapabilities(
        provider="ollama",
        model="llava",
        context_window=8192,
        supports_structured_output=True,
        supports_vision=True,
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=dt.datetime(2026, 3, 3, tzinfo=dt.UTC),
    )
    factory = LlmProviderFactory(_SETTINGS)
    config = _config(
        _STRANGER,
        LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llava",
        base_url="http://ollama:11434",
        probed_capabilities=probed,
    )
    transport = await factory.build_transport(config)
    assert transport.capabilities is probed
    assert transport.capabilities.source == "probed"


def test_unknown_model_is_refused_rather_than_assumed_capable() -> None:
    """A stale table must fail loudly, never promise a capability that is absent."""
    factory = LlmProviderFactory(_SETTINGS)
    config = _config(
        _STRANGER, LlmProviderMode.BYOK, model="claude-from-the-future", api_key="sk-ant-x"
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        factory.capabilities_for(config)
    assert "claude-opus-5" in str(raised.value)
