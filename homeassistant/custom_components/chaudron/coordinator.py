"""One poll for every entity this integration owns.

Four requests per refresh, and the number is the point. Every sensor here is a
different reading of the same two answers -- what is in stock, and what is about
to go off -- so an entity that fetched for itself would multiply the load on a
self-hosted instance by the number of things the household chose to display.

**Optional surfaces degrade, they do not fail.** A token issued with only
``inventory:read`` is a legitimate configuration, not a broken one. The backend
answers ``403 insufficient-scope`` for the shopping list and the budget and
*echoes back the scopes the token does hold* (``api/errors.py``), so the first
refusal is enough to learn the whole grant: the surface is dropped, the refresh
succeeds, and the entities that depended on it are never created.

**A 429 is obeyed rather than absorbed.** The backend's limiters are per
household and answer with ``Retry-After`` (``api/throttling.py``). Home
Assistant's coordinator has no notion of that header, so the delay is held here:
until it has passed, a refresh returns the last good snapshot instead of asking
again. A polling integration that ignored the header would spend its whole
budget re-earning the same refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Budget,
    ChaudronAuthError,
    ChaudronClient,
    ChaudronError,
    ChaudronRateLimitError,
    ChaudronScopeError,
    ExpiringPage,
    InventoryLot,
    ShoppingList,
)
from .const import (
    DOMAIN,
    LOGGER,
    SCOPE_BUDGET_READ,
    SCOPE_SHOPPING_READ,
    SCOPE_SHOPPING_WRITE,
)

type ChaudronConfigEntry = ConfigEntry[ChaudronCoordinator]


@dataclass(frozen=True, slots=True)
class ChaudronSnapshot:
    """Everything the entities read, computed once per refresh.

    The split between ``expired`` and ``expiring_soon`` is done here rather than
    in two API calls because the backend's filter has no floor: asking for "at or
    before today + 3 days" returns the lot that went off last week as well, and
    one request that answers both questions is cheaper than two that each answer
    half.
    """

    stock_count: int
    expired: tuple[InventoryLot, ...]
    expiring_soon: tuple[InventoryLot, ...]
    next_expiry: date | None
    truncated: bool
    shopping: ShoppingList | None
    budget: Budget | None


class ChaudronCoordinator(DataUpdateCoordinator[ChaudronSnapshot]):
    """Polls one household, at the interval that household chose."""

    config_entry: ChaudronConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ChaudronConfigEntry,
        client: ChaudronClient,
        *,
        update_interval: timedelta,
        expiring_within_days: int,
    ) -> None:
        """Wire one household's client to Home Assistant's refresh loop."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client
        self.expiring_within_days = expiring_within_days
        #: Scopes the token was observed to hold. Empty until a ``403`` teaches
        #: us the answer, which is why the two flags below start optimistic: a
        #: fully-scoped token must not pay a probe on every install.
        self.granted_scopes: frozenset[str] = frozenset()
        self._shopping_available = True
        self._budget_available = True
        self._retry_at: float | None = None

    @property
    def has_shopping_list(self) -> bool:
        """Whether the token opened the shopping list on the first refresh."""
        return self._shopping_available

    @property
    def can_edit_shopping_list(self) -> bool:
        """Whether ticking, adding and removing items will be accepted.

        Known only once a ``403`` has named the grant. Before that -- which is
        every install whose token holds every scope -- the honest default is that
        writing works, because the alternative is a read-only to-do list for the
        households that configured it correctly.
        """
        return self._shopping_available and (
            not self.granted_scopes or SCOPE_SHOPPING_WRITE in self.granted_scopes
        )

    @property
    def has_budget(self) -> bool:
        """Whether the token opened the budget on the first refresh."""
        return self._budget_available

    async def _async_update_data(self) -> ChaudronSnapshot:
        if (held := self._still_rate_limited()) is not None:
            if self.data is not None:
                LOGGER.debug(
                    "Chaudron asked to wait %ss; keeping the last reading", held
                )
                return self.data
            raise UpdateFailed(f"the instance is rate limiting this token for {held}s")

        try:
            stock_count = await self.client.async_stock_count()
            expiring = await self.client.async_expiring(self.expiring_within_days)
            shopping = await self._async_optional_shopping()
            budget = await self._async_optional_budget()
        except ChaudronAuthError as err:
            # Re-authentication rather than a failed update: the credential is
            # gone and no amount of retrying brings it back.
            raise ConfigEntryAuthFailed(
                "the instance no longer accepts this access token"
            ) from err
        except ChaudronRateLimitError as err:
            self._retry_at = monotonic() + err.retry_after
            raise UpdateFailed(f"rate limited for {err.retry_after}s") from err
        except ChaudronError as err:
            raise UpdateFailed(str(err)) from err

        self._retry_at = None
        return _snapshot(
            stock_count=stock_count,
            expiring=expiring,
            shopping=shopping,
            budget=budget,
        )

    def _still_rate_limited(self) -> int | None:
        """Seconds left on the backend's ``Retry-After``, or ``None``."""
        if self._retry_at is None:
            return None
        remaining = self._retry_at - monotonic()
        if remaining <= 0:
            self._retry_at = None
            return None
        return int(remaining) + 1

    async def _async_optional_shopping(self) -> ShoppingList | None:
        if not self._shopping_available:
            return None
        try:
            return await self.client.async_shopping_list()
        except ChaudronScopeError as err:
            self._note_scopes(err, SCOPE_SHOPPING_READ)
            self._shopping_available = False
            return None

    async def _async_optional_budget(self) -> Budget | None:
        if not self._budget_available:
            return None
        try:
            return await self.client.async_budget()
        except ChaudronScopeError as err:
            self._note_scopes(err, SCOPE_BUDGET_READ)
            self._budget_available = False
            return None

    def _note_scopes(self, err: ChaudronScopeError, missing: str) -> None:
        """Record what the token turned out to hold, and say so once."""
        self.granted_scopes = frozenset(err.granted)
        LOGGER.info(
            "The Chaudron access token does not hold %s; the entities that need it "
            "will not be created. Scopes held: %s",
            missing,
            ", ".join(sorted(self.granted_scopes)) or "none reported",
        )


def _snapshot(
    *,
    stock_count: int,
    expiring: ExpiringPage,
    shopping: ShoppingList | None,
    budget: Budget | None,
) -> ChaudronSnapshot:
    """Sort one filtered page into the three answers the sensors want.

    ``today`` comes from Home Assistant's own timezone rather than the host's,
    because the backend computed its horizon in the household's timezone and a
    boundary drawn twice in two zones is a lot that is expired on one card and
    fresh on another.
    """
    today = dt_util.now().date()
    expired: list[InventoryLot] = []
    soon: list[InventoryLot] = []
    next_expiry: date | None = None

    for lot in expiring.lots:
        due = lot.effective_expires_on
        if due is None:
            # The backend's filter excludes dateless lots, so this is defensive
            # rather than expected -- and a lot with no date is neither expired
            # nor expiring.
            continue
        if due < today:
            expired.append(lot)
            continue
        soon.append(lot)
        if next_expiry is None or due < next_expiry:
            next_expiry = due

    return ChaudronSnapshot(
        stock_count=stock_count,
        expired=tuple(expired),
        expiring_soon=tuple(soon),
        next_expiry=next_expiry,
        truncated=expiring.truncated,
        shopping=shopping,
        budget=budget,
    )
