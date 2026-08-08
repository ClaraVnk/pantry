"""A diagnostics download ends up on a public issue tracker."""

from __future__ import annotations

import json

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.chaudron.diagnostics import async_get_config_entry_diagnostics

from .conftest import TOKEN, mock_instance

NOW = "2026-08-06 12:00:00+00:00"


async def test_nothing_in_the_report_identifies_the_household_or_its_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Neither the credential nor a single product name may appear."""
    freezer.move_to(NOW)
    mock_instance(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    rendered = json.dumps(report, default=str)

    assert TOKEN not in rendered
    assert config_entry.unique_id not in rendered
    for secret in ("Yaourt nature", "Crème fraîche", "Farine", "Lait demi-écrémé"):
        assert secret not in rendered

    # And it still says enough to diagnose something.
    assert report["snapshot"]["stock_count"] == 42
    assert report["snapshot"]["expired_count"] == 1
    assert report["snapshot"]["shopping_item_count"] == 2
    assert report["coordinator"]["last_update_success"] is True
