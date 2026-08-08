"""What the entities read out of one poll."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import mock_instance

#: Between the two fixture lots: one is six months past its date, the other is
#: two days away, which is inside the three-day horizon the entry configures.
NOW = "2026-08-06 12:00:00+00:00"


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_fully_scoped_token_produces_every_entity(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One poll, six sensors and one list."""
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.chaudron_example_org_items_in_stock").state == "42"
    assert hass.states.get("sensor.chaudron_example_org_expired").state == "1"
    assert hass.states.get("sensor.chaudron_example_org_expiring_soon").state == "1"
    assert (
        hass.states.get("sensor.chaudron_example_org_next_expiry").state == "2026-08-08"
    )
    # Two lines on the list, one of them already ticked.
    assert (
        hass.states.get("sensor.chaudron_example_org_shopping_list_items").state == "1"
    )
    assert (
        hass.states.get("sensor.chaudron_example_org_food_spend_eur").state == "184.20"
    )
    assert hass.states.get("todo.chaudron_example_org_shopping_list").state == "1"


async def test_the_expiring_sensor_names_what_is_expiring(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The attribute an automation reads out at six o'clock."""
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)

    state = hass.states.get("sensor.chaudron_example_org_expiring_soon")
    assert [item["name"] for item in state.attributes["items"]] == ["Crème fraîche"]
    assert state.attributes["truncated"] is False


async def test_a_token_without_the_shopping_scope_loses_only_the_list(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 403 on one surface must not take the inventory down with it."""
    mock_instance(aioclient_mock, shopping=False, budget=False)
    await _setup(hass, config_entry, freezer)

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.chaudron_example_org_items_in_stock").state == "42"
    assert hass.states.get("sensor.chaudron_example_org_shopping_list_items") is None
    assert hass.states.get("todo.chaudron_example_org_shopping_list") is None
    assert hass.states.get("sensor.chaudron_example_org_food_spend_eur") is None


async def test_the_spend_sensor_carries_what_it_does_not_count(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A total computed from receipts is only as complete as the receipts."""
    mock_instance(aioclient_mock)
    await _setup(hass, config_entry, freezer)

    state = hass.states.get("sensor.chaudron_example_org_food_spend_eur")
    assert state.attributes["unit_of_measurement"] == "EUR"
    assert state.attributes["target"] == "400.00"
    assert state.attributes["receipts_missing_total"] == 1
