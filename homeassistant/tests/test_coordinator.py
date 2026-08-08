"""How the poll behaves when the instance pushes back."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import INVENTORY_URL, LOCATIONS_URL, TOKEN, mock_instance

NOW = "2026-08-06 12:00:00+00:00"


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_429_keeps_the_last_reading_rather_than_asking_again(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The delay is obeyed, not merely logged.

    The backend's ``Retry-After`` is longer than the update interval here, so a
    coordinator that ignored it would spend the whole budget re-earning the same
    refusal.
    """
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)
    assert hass.states.get("sensor.chaudron_example_org_items_in_stock").state == "42"

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        INVENTORY_URL,
        status=429,
        headers={"Retry-After": "1800"},
        json={"type": "https://chaudron.dev/problems/rate-limited"},
    )

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    first_refusal = aioclient_mock.call_count

    # The next scheduled poll falls inside the delay the instance asked for.
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert aioclient_mock.call_count == first_refusal
    # And the last good figure is still on the card.
    assert hass.states.get("sensor.chaudron_example_org_items_in_stock").state == "42"


async def test_a_revoked_token_starts_re_authentication(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 401 mid-life is not a transient failure; retrying cannot fix it."""
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        INVENTORY_URL,
        status=401,
        json={"type": "https://chaudron.dev/problems/token-not-accepted"},
    )

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert any(
        flow["handler"] == "chaudron" and flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_an_unreachable_instance_does_not_load_the_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Retried later rather than reported as a broken installation."""
    freezer.move_to(NOW)
    aioclient_mock.get(INVENTORY_URL, exc=TimeoutError)
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_the_token_is_sent_as_a_bearer_and_nowhere_else(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One header, no query parameter, no cookie."""
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)

    for _method, url, _data, headers in aioclient_mock.mock_calls:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(url)
    assert LOCATIONS_URL not in [str(call[1]) for call in aioclient_mock.mock_calls]
