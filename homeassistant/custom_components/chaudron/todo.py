"""The household's shopping list, as a Home Assistant to-do list.

The honest fit. Chaudron's list is a set of lines, each either bought or not,
which is what a ``todo`` entity is -- so it appears in the to-do panel, on a
list card, and in the voice assistant's "add milk to the shopping list" intent
without this integration writing any of that.

**What it cannot do, and why the buttons are missing rather than broken.**

*No due dates, no descriptions.* ``shopping_list_item`` has neither column. A
feature flag declared here would put a date picker in front of a household and
throw away what they typed.

*No renaming.* ``PATCH /v1/shopping-lists/current/items/{id}`` accepts a tick and
a quantity, and nothing else (``api/schemas.py``, ``UpdateShoppingItemIn``). Home
Assistant's update service carries a summary regardless, so a changed one is
refused with a sentence rather than silently dropped -- the alternative is a
household renaming an item, watching the card update optimistically, and finding
the old name back ten minutes later.

*No reordering.* The rows carry a ``sort_order`` and the API publishes no route
that changes it.

*Read-only without ``shopping:write``.* The features are computed from what the
token turned out to hold, so a viewer's token yields a list that displays and
refuses to be edited, which is exactly what the backend would do anyway -- only
without the round trip.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ChaudronError, ShoppingItem
from .const import DOMAIN
from .coordinator import ChaudronConfigEntry, ChaudronCoordinator
from .entity import ChaudronEntity

#: Everything the API supports, and not one flag more.
_EDITABLE = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChaudronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the list entity, when the token opens the list at all."""
    coordinator = entry.runtime_data
    if not coordinator.has_shopping_list:
        return
    async_add_entities([ChaudronShoppingList(coordinator)])


class ChaudronShoppingList(ChaudronEntity, TodoListEntity):
    """The household's current list."""

    _attr_translation_key = "shopping_list"

    def __init__(self, coordinator: ChaudronCoordinator) -> None:
        """Declare only the edits the token and the API both allow."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_shopping_list"
        self._attr_supported_features = (
            _EDITABLE
            if coordinator.can_edit_shopping_list
            else TodoListEntityFeature(0)
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """The list as Home Assistant models it, or ``None`` while unknown."""
        if (shopping := self.coordinator.data.shopping) is None:
            return None
        return [_as_todo_item(item) for item in shopping.items]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add a line. The summary is stored as free text; see the client."""
        await self._async_write(
            self.coordinator.client.async_add_shopping_item(item.summary or "")
        )
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Tick or untick. A changed summary is refused, not dropped."""
        if item.uid is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="item_without_identifier"
            )
        current = self._item(item.uid)
        if current is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="item_not_found"
            )
        # Compared against the summary Home Assistant was *shown*, quantity
        # suffix included, because that is the string a card hands straight back
        # when the household only ticked the box.
        displayed = _as_todo_item(current).summary
        if item.summary is not None and item.summary != displayed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="rename_not_supported",
                translation_placeholders={"summary": displayed or ""},
            )

        checked = item.status == TodoItemStatus.COMPLETED
        if checked == current.checked:
            # Nothing to say to the instance. Home Assistant calls this service
            # for any edit, including the ones that changed a field this list
            # does not carry.
            return
        await self._async_write(
            self.coordinator.client.async_set_shopping_item_checked(
                current.id, checked=checked
            )
        )
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Remove lines, one call each -- the API has no bulk delete.

        Sequential rather than gathered: the backend caps requests per household
        and a "clear completed" on a long list would otherwise arrive as one
        burst against that cap. One refresh at the end, not one per line.
        """
        for uid in uids:
            await self._async_write(
                self.coordinator.client.async_delete_shopping_item(uid)
            )
        await self.coordinator.async_request_refresh()

    def _item(self, uid: str) -> ShoppingItem | None:
        if (shopping := self.coordinator.data.shopping) is None:
            return None
        return next((item for item in shopping.items if item.id == uid), None)

    async def _async_write(self, call: Coroutine[Any, Any, None]) -> None:
        """Run one mutation, turning a transport failure into a spoken sentence."""
        try:
            await call
        except ChaudronError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="shopping_write_failed",
                translation_placeholders={"error": str(err)},
            ) from err


def _as_todo_item(item: ShoppingItem) -> TodoItem:
    """One list line. The quantity joins the summary, having nowhere else to go.

    ``TodoItem`` has no quantity field, and dropping "2 kg" from "2 kg de farine"
    would make the list wrong rather than merely terser.
    """
    summary = item.summary
    if item.quantity and item.unit:
        summary = f"{summary} ({item.quantity} {item.unit})"
    return TodoItem(
        uid=item.id,
        summary=summary,
        status=TodoItemStatus.COMPLETED
        if item.checked
        else TodoItemStatus.NEEDS_ACTION,
    )
