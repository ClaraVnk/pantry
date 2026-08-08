"""The Chaudron integration: a household's food stock, in Home Assistant.

Set up from the user interface only. There is no YAML schema and there will not
be one, for one reason: the credential. A machine token is a bearer value that
opens one household's inventory, and ``configuration.yaml`` is a file people
paste into forum posts and commit to their dotfiles repository. It lives in the
config entry, which Home Assistant keeps in ``.storage`` and never renders.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ChaudronClient
from .const import (
    CONF_EXPIRING_WITHIN_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_EXPIRING_WITHIN_DAYS,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import ChaudronConfigEntry, ChaudronCoordinator

# ``todo`` is forwarded unconditionally even though a token without
# ``shopping:read`` will produce no entity on it. The alternative -- forwarding
# it only when the scope is present -- makes the unload list depend on runtime
# state, and an unload that does not mirror its setup is how a reload leaves an
# orphan behind.
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: ChaudronConfigEntry) -> bool:
    """Connect one household and start polling it."""
    client = ChaudronClient(
        # Home Assistant's shared session: connection pooling, its own DNS cache
        # and one place where the whole instance's outbound TLS is configured.
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_ACCESS_TOKEN],
    )
    coordinator = ChaudronCoordinator(
        hass,
        entry,
        client,
        update_interval=_scan_interval(entry),
        expiring_within_days=entry.options.get(
            CONF_EXPIRING_WITHIN_DAYS, DEFAULT_EXPIRING_WITHIN_DAYS
        ),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ChaudronConfigEntry) -> bool:
    """Stop polling and remove every entity this entry owns."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _scan_interval(entry: ChaudronConfigEntry) -> timedelta:
    """The household's chosen interval, or the default it never had to think about."""
    minutes = entry.options.get(CONF_SCAN_INTERVAL_MINUTES)
    return DEFAULT_SCAN_INTERVAL if minutes is None else timedelta(minutes=minutes)
