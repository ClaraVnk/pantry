"""Constants for the Chaudron integration."""

from __future__ import annotations

from datetime import timedelta
from logging import Logger, getLogger
from typing import Final

LOGGER: Logger = getLogger(__package__)

DOMAIN: Final = "chaudron"
MANUFACTURER: Final = "Chaudron"

# --------------------------------------------------------------------------- #
# Configuration keys
# --------------------------------------------------------------------------- #

CONF_EXPIRING_WITHIN_DAYS: Final = "expiring_within_days"
CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"

#: Ten minutes. A food inventory is edited by hand, a few times a day at most, so
#: anything faster spends a self-hosted instance's CPU to learn nothing. The value
#: is an option rather than a constant because a household that fills its fridge
#: from the phone wants the dashboard to catch up sooner than the next quarter
#: hour, and one running on a Raspberry Pi wants the opposite.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=10)
MIN_SCAN_INTERVAL_MINUTES: Final = 1
MAX_SCAN_INTERVAL_MINUTES: Final = 1440

#: "Soon" for the expiring-soon sensor. Three days is roughly what a cook can act
#: on: long enough to plan a meal around it, short enough that the number is not
#: permanently non-zero and therefore ignorable.
DEFAULT_EXPIRING_WITHIN_DAYS: Final = 3
MIN_EXPIRING_WITHIN_DAYS: Final = 1
MAX_EXPIRING_WITHIN_DAYS: Final = 30

#: How long one HTTP call may take. Generous, because the backend may be behind a
#: home connection and a cold Postgres pool; short enough that a hung instance
#: does not hold a coordinator refresh past its own interval.
REQUEST_TIMEOUT_SECONDS: Final = 30

# --------------------------------------------------------------------------- #
# The API this integration speaks
# --------------------------------------------------------------------------- #

#: Every machine token the backend issues starts with this. Checked in the config
#: flow so that pasting a session cookie, a password or a provider key is refused
#: at the form rather than by a 401 five seconds later.
TOKEN_PREFIX: Final = "chdr_"

SCOPE_INVENTORY_READ: Final = "inventory:read"
SCOPE_INVENTORY_WRITE: Final = "inventory:write"
SCOPE_SHOPPING_READ: Final = "shopping:read"
SCOPE_SHOPPING_WRITE: Final = "shopping:write"
SCOPE_BUDGET_READ: Final = "budget:read"

#: Without this one there is nothing to show, so the config flow refuses a token
#: that does not hold it. Everything else is additive: a token with only
#: ``inventory:read`` yields the stock sensors and no shopping list.
REQUIRED_SCOPES: Final = frozenset({SCOPE_INVENTORY_READ})

#: What the documentation tells a household to tick when creating the token. The
#: two write scopes the backend offers are handled differently: ``inventory:write``
#: is never used by this integration at all -- Home Assistant has no honest entity
#: for "add a lot of yoghurt with a best-before date" -- and ``shopping:write``
#: only unlocks editing the to-do list.
RECOMMENDED_SCOPES: Final = (
    SCOPE_INVENTORY_READ,
    SCOPE_SHOPPING_READ,
    SCOPE_SHOPPING_WRITE,
    SCOPE_BUDGET_READ,
)

#: The backend caps a page at 200 items, and this integration stops after five of
#: them. A household with more than a thousand lots *about to expire* is not a
#: household; refusing to walk the whole table protects the instance from a poll
#: that grows without bound.
INVENTORY_PAGE_SIZE: Final = 200
MAX_INVENTORY_PAGES: Final = 5

#: How many expiring lots the sensors carry as a state attribute. The recorder is
#: told not to store them (``_unrecorded_attributes``), but they still travel to
#: every connected browser on every state change, so the list is the one an
#: automation reads out loud rather than the whole shelf.
MAX_ATTRIBUTE_ITEMS: Final = 20

# -- Paths, relative to the instance root ----------------------------------- #
#
# ``ops/Caddyfile.example`` publishes the API at the site root, so the base URL a
# household types is ``https://chaudron.example.org`` and every path below hangs
# off it directly.

PATH_INVENTORY: Final = "/v1/inventory"
PATH_LOCATIONS: Final = "/v1/locations"
PATH_SHOPPING_LIST: Final = "/v1/shopping-lists/current"
PATH_SHOPPING_ITEMS: Final = "/v1/shopping-lists/current/items"
PATH_BUDGET: Final = "/v1/budget"
