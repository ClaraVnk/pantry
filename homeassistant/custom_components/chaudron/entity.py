"""The device every Chaudron entity hangs off.

One device per config entry, and the config entry is one household -- because a
machine token *is* a household. The backend fixes the tenant on the token row
when it is issued and refuses to let a header point it anywhere else
(``api/deps.py``), so "which household is this?" has exactly one answer per
entry and nothing at runtime can change it.

The device carries no household name, because the API deliberately does not
publish one to a machine token: household membership and the people in it leave
through a browser session or not at all. What is shown instead is the instance
the entry points at, which is the one thing the household typed themselves.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import ChaudronCoordinator


class ChaudronEntity(CoordinatorEntity[ChaudronCoordinator]):
    """Shared identity and availability for every platform here."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ChaudronCoordinator) -> None:
        """Attach this entity to the device that stands for the household."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            name=entry.title,
            configuration_url=coordinator.client.base_url,
        )
