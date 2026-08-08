"""Counting what left the stock, and comparing it to the published benchmarks.

The whole feature rests on one decision recorded in ADR-0009: **the source of
what was eaten is ``stock_movement``**, with kind ``consumption``, over a rolling
seven days. No separate meal journal is created, because a journal the household
has to fill in is a journal that stays empty, and the ledger already carries the
fact.

The arithmetic itself lives in :mod:`chaudron.domain.balance`, where it can be
tested without a database. What is here is the two queries and the honesty that
goes with them: a product whose categories resolve to no marker counts for
nothing and is *counted as such*, because a badly catalogued pantry has to show
up as missing evidence rather than as a confident "you are one fish short".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.balance import (
    WINDOW_DAYS,
    Guideline,
    Observation,
    WeeklyBalance,
    evaluate,
)
from chaudron.domain.models import (
    InventoryLot,
    NutritionReference,
    PnnsGuideline,
    PnnsMarker,
    Product,
    QuantityDimension,
    StockMovement,
    StockMovementKind,
)

__all__ = ["BalanceService"]

#: What the response says when the shortfalls cannot be filled from what is
#: there. Quoted from the contract, so the client can match on it if it must.
STOCK_CANNOT_FILL: str = "Le stock ne permet pas de combler ces écarts cette semaine."


class BalanceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def weekly(
        self,
        household_id: uuid.UUID,
        *,
        markers_in_stock: frozenset[PnnsMarker] | None = None,
        note: str | None = None,
        window_days: int = WINDOW_DAYS,
    ) -> WeeklyBalance:
        """The rolling-window balance, against the current reference edition.

        ``markers_in_stock`` is what the eaters at this table may actually be
        offered -- the screened inventory, not the whole pantry. Passing ``None``
        reads the unscreened stock, which is the right answer for the dashboard
        (contract 8) and the wrong one for a suggestion.
        """
        reference = await self._current_reference()
        guidelines = await self._guidelines(reference)
        observations, uncategorised = await self._observations(household_id, window_days)
        markers = (
            await self._markers_in_stock(household_id)
            if markers_in_stock is None
            else markers_in_stock
        )
        balance = evaluate(
            guidelines,
            observations,
            reference=reference,
            window_days=window_days,
            uncategorised_product_count=uncategorised,
            markers_in_stock=markers,
            note=note,
        )
        if balance.gaps and not balance.satisfiable_from_stock:
            return _with_note(balance, STOCK_CANNOT_FILL)
        return balance

    async def _current_reference(self) -> str:
        version = await self._session.scalar(
            select(NutritionReference.version).where(NutritionReference.is_current)
        )
        if version is None:  # pragma: no cover - seeded by migration 0005
            raise RuntimeError("no current nutrition reference is seeded")
        return version

    async def _guidelines(self, reference: str) -> tuple[Guideline, ...]:
        rows = (
            await self._session.scalars(
                select(PnnsGuideline).where(PnnsGuideline.reference_version == reference)
            )
        ).all()
        return tuple(
            Guideline(
                marker=row.marker,
                label=row.label,
                direction=row.direction,
                amount=row.amount,
                unit=row.unit,
                window_days=row.window_days,
                statement=row.statement,
                source_url=row.source_url,
                sort_order=row.sort_order,
            )
            for row in rows
        )

    async def _observations(
        self, household_id: uuid.UUID, window_days: int
    ) -> tuple[dict[PnnsMarker, Observation], int]:
        """What was consumed per marker, and how many products said nothing.

        ``kind = 'consumption'`` and nothing else. A ``waste`` movement is food
        that left the house without being eaten, and an ``adjustment`` is a
        typing error being fixed -- counting either as a meal would make the
        balance say a household ate what it threw away (contract 6).
        """
        since = datetime.now(UTC) - timedelta(days=window_days)
        rows = (
            await self._session.execute(
                select(
                    Product.id.label("product_id"),
                    Product.pnns_markers.label("markers"),
                    StockMovement.delta_canonical.label("delta"),
                    StockMovement.quantity_dimension.label("dimension"),
                )
                .join(InventoryLot, InventoryLot.id == StockMovement.inventory_lot_id)
                .join(Product, Product.id == InventoryLot.product_id)
                .where(
                    StockMovement.household_id == household_id,
                    StockMovement.kind == StockMovementKind.CONSUMPTION,
                    StockMovement.occurred_at >= since,
                )
            )
        ).all()

        servings: dict[PnnsMarker, int] = {}
        grams: dict[PnnsMarker, Decimal] = {}
        uncategorised: set[uuid.UUID] = set()
        for row in rows:
            markers = _markers(row)
            if not markers:
                uncategorised.add(row.product_id)
                continue
            mass = abs(row.delta) if row.dimension is QuantityDimension.MASS else Decimal(0)
            for marker in markers:
                servings[marker] = servings.get(marker, 0) + 1
                grams[marker] = grams.get(marker, Decimal(0)) + mass
        observations = {
            marker: Observation(servings=count, grams=grams.get(marker, Decimal(0)))
            for marker, count in servings.items()
        }
        return observations, len(uncategorised)

    async def _markers_in_stock(self, household_id: uuid.UUID) -> frozenset[PnnsMarker]:
        rows = (
            await self._session.scalars(
                select(Product.pnns_markers)
                .join(InventoryLot, InventoryLot.product_id == Product.id)
                .where(
                    InventoryLot.household_id == household_id,
                    InventoryLot.depleted_at.is_(None),
                )
                .distinct()
            )
        ).all()
        markers: set[PnnsMarker] = set()
        for value in rows:
            markers.update(value or ())
        return frozenset(markers)


def _markers(row: Any) -> tuple[PnnsMarker, ...]:
    return tuple(row.markers or ())


def _with_note(balance: WeeklyBalance, sentence: str) -> WeeklyBalance:
    """Append a sentence to the note without losing the one already there.

    Two things can need saying at once -- the stock cannot fill the gaps, *and*
    no suggestion used the yoghurt that expires tomorrow (contract 5). Dropping
    either would be the silence that section exists to forbid.
    """
    note = sentence if balance.note is None else f"{balance.note} {sentence}"
    return WeeklyBalance(
        reference=balance.reference,
        window_days=balance.window_days,
        uncategorised_product_count=balance.uncategorised_product_count,
        gaps=balance.gaps,
        excesses=balance.excesses,
        satisfiable_from_stock=balance.satisfiable_from_stock,
        note=note,
    )
