"""The shopping list, as Home Assistant edits it."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.todo import (
    ATTR_ITEM,
    ATTR_RENAME,
    ATTR_STATUS,
    DOMAIN as TODO_DOMAIN,
    TodoServices,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import SHOPPING_ITEMS_URL, SHOPPING_PAYLOAD, mock_instance

ENTITY = "todo.chaudron_example_org_shopping_list"
NOW = "2026-08-06 12:00:00+00:00"
UNCHECKED_ITEM = SHOPPING_PAYLOAD["items"][0]["id"]


@pytest.fixture(autouse=True)
async def _loaded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    mock_instance(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_the_quantity_travels_in_the_summary(hass: HomeAssistant) -> None:
    """``TodoItem`` has no quantity field, and dropping it would be a lie."""
    result = await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.GET_ITEMS,
        {ATTR_ENTITY_ID: ENTITY},
        blocking=True,
        return_response=True,
    )
    summaries = [item["summary"] for item in result[ENTITY]["items"]]
    assert summaries == ["Farine (1.000 kg)", "Lait demi-écrémé"]


async def test_adding_an_item_posts_free_text(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Which product it names is Chaudron's decision, not this integration's."""
    aioclient_mock.post(SHOPPING_ITEMS_URL, status=201, json=SHOPPING_PAYLOAD)

    await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.ADD_ITEM,
        {ATTR_ENTITY_ID: ENTITY, ATTR_ITEM: "Beurre demi-sel"},
        blocking=True,
    )

    posted = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posted[-1][2] == {
        "items": [{"free_text": "Beurre demi-sel", "source": "manual"}]
    }


async def test_ticking_an_item_patches_only_the_tick(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The one field ``UpdateShoppingItemIn`` accepts alongside a quantity."""
    aioclient_mock.patch(f"{SHOPPING_ITEMS_URL}/{UNCHECKED_ITEM}", json={})

    await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.UPDATE_ITEM,
        {
            ATTR_ENTITY_ID: ENTITY,
            ATTR_ITEM: "Farine (1.000 kg)",
            ATTR_STATUS: "completed",
        },
        blocking=True,
    )

    patched = [call for call in aioclient_mock.mock_calls if call[0] == "PATCH"]
    assert patched[-1][2] == {"checked": True}


async def test_renaming_is_refused_rather_than_silently_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The API has no route for it, so an optimistic card would lie for ten minutes."""
    before = aioclient_mock.call_count

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            TODO_DOMAIN,
            TodoServices.UPDATE_ITEM,
            {
                ATTR_ENTITY_ID: ENTITY,
                ATTR_ITEM: "Farine (1.000 kg)",
                ATTR_RENAME: "Farine T65",
            },
            blocking=True,
        )

    assert aioclient_mock.call_count == before


async def test_removing_completed_items_deletes_them_one_by_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The API has no bulk delete, and a burst would meet the household's cap."""
    checked = SHOPPING_PAYLOAD["items"][1]["id"]
    aioclient_mock.delete(f"{SHOPPING_ITEMS_URL}/{checked}", status=204)

    await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.REMOVE_COMPLETED_ITEMS,
        {ATTR_ENTITY_ID: ENTITY},
        blocking=True,
    )

    deleted = [call for call in aioclient_mock.mock_calls if call[0] == "DELETE"]
    assert len(deleted) == 1
    assert str(deleted[0][1]).endswith(checked)
