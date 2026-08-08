"""Storage locations and their live item counts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import InventoryLot, StorageLocation
from chaudron.domain.ports import LocationDraft, LocationNameTakenError, LocationSummary


class SqlLocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_with_counts(self, household_id: uuid.UUID) -> Sequence[LocationSummary]:
        """Locations with the number of lots currently in them.

        A correlated scalar subquery rather than a ``LEFT JOIN … GROUP BY``: it
        keeps the count on the partial index ``ix_inventory_lot_location_active``
        and leaves the outer query returning exactly one row per location, with
        no aggregate over the location columns.
        """
        item_count = (
            select(func.count())
            .select_from(InventoryLot)
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.storage_location_id == StorageLocation.id,
                InventoryLot.depleted_at.is_(None),
            )
            .scalar_subquery()
        )

        rows = await self._session.execute(
            select(
                StorageLocation.id,
                StorageLocation.name,
                StorageLocation.kind,
                item_count.label("item_count"),
            )
            .where(
                StorageLocation.household_id == household_id,
                StorageLocation.archived_at.is_(None),
            )
            .order_by(StorageLocation.sort_order, StorageLocation.name)
        )
        return [
            LocationSummary(id=row.id, name=row.name, kind=row.kind, item_count=row.item_count)
            for row in rows
        ]

    async def create(self, household_id: uuid.UUID, draft: LocationDraft) -> LocationSummary:
        """Insert one active location and return it, already counted at zero.

        ``sort_order`` keeps its ``0`` default, so the list stays ordered by name
        (:meth:`list_with_counts` sorts on ``sort_order, name``). Reordering by
        hand is a feature nobody has asked for; alphabetical is what a household
        that has just created "Cave" and "Frigo" expects, and it costs no extra
        query to compute a rank nothing would use.

        The savepoint is not decoration. ``uq_storage_location_name`` is enforced
        by PostgreSQL, and an ``IntegrityError`` aborts the transaction it is
        raised in -- which is the request's own, the one the ``409`` still has to
        be written from. Rolling back to a savepoint leaves the outer transaction
        usable, and it also expunges the rejected instance, so no later flush
        replays the same ``INSERT``.

        **The ``add`` belongs inside the ``async with``, and moving it out is the
        bug this shape exists to avoid.** ``Session.begin_nested`` flushes the
        session while taking its snapshot -- *before* the ``SAVEPOINT`` reaches
        the server. An instance added beforehand is therefore inserted by that
        flush, outside the savepoint, and the savepoint protects nothing.
        """
        location = StorageLocation(household_id=household_id, name=draft.name, kind=draft.kind)
        try:
            async with self._session.begin_nested():
                self._session.add(location)
                await self._session.flush()
        except IntegrityError as exc:
            raise LocationNameTakenError(draft.name) from exc

        return LocationSummary(id=location.id, name=location.name, kind=location.kind, item_count=0)

    async def exists(self, household_id: uuid.UUID, location_id: uuid.UUID) -> bool:
        found = await self._session.scalar(
            select(StorageLocation.id).where(
                StorageLocation.id == location_id,
                StorageLocation.household_id == household_id,
                StorageLocation.archived_at.is_(None),
            )
        )
        return found is not None
