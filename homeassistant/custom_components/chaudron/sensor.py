"""What a household wants on a dashboard, and nothing it would have to invent.

Five readings of the inventory and the list, plus one figure per currency the
household actually spends in. Every one of them comes out of the single snapshot
the coordinator already fetched; none of them polls.

**The expiring list travels as an attribute and is not recorded.** "Which things
go off this week" is what an automation reads out at 18:00, so it has to be
available to a template -- but it is a list of product names, which is the
household's shopping habits in plain text and, under the backend's own threat
model, asset A3. Writing it into the recorder database every ten minutes forever
would be a slow leak of exactly that into a file nobody thinks of as sensitive.
``_unrecorded_attributes`` keeps it live and out of history.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api import CurrencySpend, InventoryLot
from .const import MAX_ATTRIBUTE_ITEMS
from .coordinator import ChaudronConfigEntry, ChaudronCoordinator, ChaudronSnapshot
from .entity import ChaudronEntity


@dataclass(frozen=True, kw_only=True)
class ChaudronSensorDescription(SensorEntityDescription):
    """A reading, and how to take it from a snapshot."""

    value_fn: Callable[[ChaudronSnapshot], StateType | date]
    attributes_fn: Callable[[ChaudronSnapshot], dict[str, Any]] | None = None
    #: Whether this token can serve the reading at all. Evaluated once, after the
    #: first refresh has established what the token holds.
    exists_fn: Callable[[ChaudronCoordinator], bool] = lambda _: True


SENSORS: tuple[ChaudronSensorDescription, ...] = (
    ChaudronSensorDescription(
        key="items_in_stock",
        translation_key="items_in_stock",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.stock_count,
    ),
    ChaudronSensorDescription(
        key="expiring_soon",
        translation_key="expiring_soon",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: len(snapshot.expiring_soon),
        attributes_fn=lambda snapshot: _lots_attribute(
            snapshot.expiring_soon, snapshot
        ),
    ),
    ChaudronSensorDescription(
        key="expired",
        translation_key="expired",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: len(snapshot.expired),
        attributes_fn=lambda snapshot: _lots_attribute(snapshot.expired, snapshot),
    ),
    ChaudronSensorDescription(
        key="next_expiry",
        translation_key="next_expiry",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda snapshot: snapshot.next_expiry,
    ),
    ChaudronSensorDescription(
        key="shopping_list_items",
        translation_key="shopping_list_items",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: _unchecked(snapshot),
        exists_fn=lambda coordinator: coordinator.has_shopping_list,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChaudronConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors this token can actually feed."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        ChaudronSensor(coordinator, description)
        for description in SENSORS
        if description.exists_fn(coordinator)
    ]

    # One spend sensor per currency observed on the first refresh, rather than
    # one sensor whose unit changes underneath it. A monetary sensor's unit is
    # its currency; a household that buys in EUR at home and CHF across the
    # border has two figures, and folding them into one would either add
    # incomparable numbers or make the unit -- and therefore the recorder's
    # statistics -- change from poll to poll.
    if (budget := coordinator.data.budget) is not None:
        entities.extend(
            ChaudronSpendSensor(coordinator, spend.currency)
            for spend in budget.currencies
        )

    async_add_entities(entities)


class ChaudronSensor(ChaudronEntity, SensorEntity):
    """One reading of the household's stock or list."""

    entity_description: ChaudronSensorDescription
    _unrecorded_attributes = frozenset({"items", "truncated"})

    def __init__(
        self, coordinator: ChaudronCoordinator, description: ChaudronSensorDescription
    ) -> None:
        """Bind one description to the shared coordinator."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> StateType | date:
        """The reading this description takes from the current snapshot."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """What the description chose to expose, if anything."""
        if (build := self.entity_description.attributes_fn) is None:
            return None
        return build(self.coordinator.data)


class ChaudronSpendSensor(ChaudronEntity, SensorEntity):
    """What the household spent this calendar month, in one currency.

    ``None`` rather than zero when the currency disappears from a later period:
    "we bought nothing in Swiss francs in March" and "we spent CHF 0.00" are the
    same claim, but a zero would enter the recorder's statistics as a real
    measurement and flatten a yearly graph.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "food_spend"

    def __init__(self, coordinator: ChaudronCoordinator, currency: str) -> None:
        """Bind this sensor to one currency, which is also its unit."""
        super().__init__(coordinator)
        self._currency = currency
        self._attr_native_unit_of_measurement = currency
        self._attr_translation_placeholders = {"currency": currency}
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_food_spend_{currency.lower()}"
        )

    @property
    def _spend(self) -> CurrencySpend | None:
        if (budget := self.coordinator.data.budget) is None:
            return None
        return next(
            (entry for entry in budget.currencies if entry.currency == self._currency),
            None,
        )

    @property
    def native_value(self) -> StateType:
        """What was spent in this currency, or ``None`` if it was not."""
        return None if (spend := self._spend) is None else spend.spent

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The period, the target, and what the figure does not count."""
        budget = self.coordinator.data.budget
        spend = self._spend
        if budget is None or spend is None:
            return None
        return {
            "period_start": budget.period_start,
            "period_end": budget.period_end,
            "target": spend.target,
            "receipt_count": spend.receipt_count,
            # What the figure does not count. The backend publishes it because a
            # spend total computed from receipts is only as complete as the
            # receipts imported, and a number without that caveat reads as fact.
            "receipts_missing_total": budget.receipts_missing_total,
        }


def _unchecked(snapshot: ChaudronSnapshot) -> int:
    """Items still to buy. A ticked item is done, not outstanding."""
    if snapshot.shopping is None:
        return 0
    return sum(1 for item in snapshot.shopping.items if not item.checked)


def _lots_attribute(
    lots: tuple[InventoryLot, ...], snapshot: ChaudronSnapshot
) -> dict[str, Any]:
    """The first few lots, plus an honest flag when the page was cut short."""
    return {
        "items": [
            {
                "name": lot.product_name,
                "brand": lot.brand,
                "location": lot.location_name,
                "quantity": lot.quantity,
                "unit": lot.unit,
                "expires_on": lot.effective_expires_on,
            }
            for lot in lots[:MAX_ATTRIBUTE_ITEMS]
        ],
        "truncated": snapshot.truncated or len(lots) > MAX_ATTRIBUTE_ITEMS,
    }
