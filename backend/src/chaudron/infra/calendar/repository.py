"""The reads and the one write the feed performs, and the tenancy rule each obeys.

:func:`list_feed_households` runs **before** a household is known -- it is
what resolving a credential scans -- so it deliberately runs unscoped. That is
sound because ``household`` carries no ``household_id`` column and migration
``0004`` therefore attaches no policy to it: it is the table the tenant is
*derived from*, exactly as ``api/deps.py`` reads it to validate an
``X-Household-Id``. It returns identifiers and revocation counters, and nothing
else.

:func:`revoke_feed` and :func:`get_feed_epoch` touch the same unprotected table,
and both are reached only from a route guarded by ``OwnerDep``: the household is
already resolved and the caller already proven to own it, so the ``WHERE id =``
in each of them is the whole of the isolation and is written out rather than
inherited from a policy that does not exist here.

:class:`SqlExpiringLotReader` runs **after**, and returns inventory -- asset A3,
the one the security model calls potentially sensitive under article 9. It is
called on a transaction where the tenant has already been posted, so row-level
security applies, *and* it repeats ``household_id`` in its ``WHERE`` clause. Two
barriers on purpose: the policy is the guarantee, the predicate is what keeps the
query index-friendly and what a reader of this file can check without opening
``psql``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Household, InventoryLot, Product, StorageLocation, Unit
from chaudron.domain.shelf_life import effective_expiry_sql, join_shelf_life_guideline
from chaudron.infra.calendar.credentials import FeedHousehold


@dataclass(frozen=True, slots=True)
class ExpiringLot:
    """One dated, still-active lot. Only the columns the feed is allowed to publish.

    The row this is built from carries a barcode, a price, a receipt line and the
    account that entered it. None of them are selected: what is not read cannot
    be leaked by a later change to the renderer.
    """

    lot_id: uuid.UUID
    product_name: str
    quantity_value: Decimal
    #: The unit's display symbol ("L", "pc", "c. à s."), not its code: the feed is
    #: read by a human on a lock screen, and "1 pc" is what they wrote down.
    quantity_symbol: str
    location_name: str | None
    #: The date printed on the pack, or ``None`` when nobody entered one. Not
    #: what the task is due on -- see below -- and carried only so a reader of
    #: this dataclass can tell the two apart.
    best_before: date | None
    #: When the lot becomes unfit: ``min(best_before, opened_at + opened_days)``
    #: (``docs/data-model.md`` 7.4). Never ``None`` here, because the query only
    #: returns lots whose window this date falls inside.
    effective_expiry: date
    created_at: datetime
    updated_at: datetime


async def list_feed_households(session: AsyncSession) -> Sequence[FeedHousehold]:
    """Every household a credential may name, with the counter that revokes it.

    Archived ones are not among them. Archiving is how a household leaves the
    instance (``SqlHouseholdRepository`` says the same of ``X-Household-Id``); a
    feed that kept answering afterwards would make the departure cosmetic.

    ``calendar_feed_epoch`` is read here rather than looked up once the household
    is known, because the household is *not* known yet: the epoch is an input to
    the derivation that decides which household this is.
    """
    rows = await session.execute(
        select(Household.id, Household.calendar_feed_epoch)
        .where(Household.archived_at.is_(None))
        .order_by(Household.id)
    )
    return [FeedHousehold(id=row[0], feed_epoch=row[1]) for row in rows]


async def count_feed_households(session: AsyncSession) -> int:
    """How many households a credential resolution would have to walk through.

    Read at startup so that :data:`MAX_RESOLVABLE_HOUSEHOLDS` is a thing the
    operator is told about while they can still act on it, rather than a ``503``
    the first subscriber discovers (``api/main.py``).
    """
    total: int = await session.scalar(  # type: ignore[assignment]
        select(func.count()).select_from(Household).where(Household.archived_at.is_(None))
    )
    return total


async def get_feed_epoch(session: AsyncSession, household_id: uuid.UUID) -> int | None:
    """This household's revocation counter, or ``None`` if there is no such household."""
    epoch: int | None = await session.scalar(
        select(Household.calendar_feed_epoch).where(
            Household.id == household_id, Household.archived_at.is_(None)
        )
    )
    return epoch


async def revoke_feed(session: AsyncSession, household_id: uuid.UUID) -> int | None:
    """Increment the counter and return its new value, or ``None`` if no such row.

    One statement, read-modify-write inside PostgreSQL rather than in Python: two
    owners tapping "revoke" at the same moment must produce two increments, not
    one. Returning the new value rather than re-reading it keeps the credential
    the caller is handed back and the row in the database provably the same epoch.
    """
    new_epoch: int | None = await session.scalar(
        update(Household)
        .where(Household.id == household_id, Household.archived_at.is_(None))
        .values(calendar_feed_epoch=Household.calendar_feed_epoch + 1)
        .returning(Household.calendar_feed_epoch)
    )
    return new_epoch


async def get_household_timezone(session: AsyncSession, household_id: uuid.UUID) -> str | None:
    """The household's IANA zone, used to place the reminder at a civil hour."""
    zone: str | None = await session.scalar(
        select(Household.timezone).where(Household.id == household_id)
    )
    return zone


class SqlExpiringLotReader:
    """Lots with a date inside the published window, soonest first."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_expiring(
        self,
        household_id: uuid.UUID,
        *,
        earliest: date,
        latest: date,
        limit: int,
    ) -> Sequence[ExpiringLot]:
        """Read at most ``limit`` lots due between ``earliest`` and ``latest``.

        "Due" is the *effective* date -- the printed one shortened by the
        after-opening rule (``domain/shelf_life.py``). Windowing on the printed
        date instead would publish nothing for a jar opened three weeks ago whose
        packaging still says next month, which is the case the rule exists for.

        The lower bound is not cosmetic. Ordering ascending with a cap means that
        without it, a household with two years of forgotten expired lots would
        publish two hundred tasks about yoghurt from 2024 and never reach
        tomorrow's milk.

        **Not backed by ``ix_inventory_lot_expiry_active`` any more**, and no
        index can replace it: the expression spans two tables, and PostgreSQL
        indexes one. What is left is ``ix_inventory_lot_location_active``, which
        still narrows to this household's active lots before the expression is
        evaluated -- and the number of those is bounded by what one household
        stores, not by the size of the instance.
        """
        expiry = effective_expiry_sql()
        rows = await self._session.execute(
            join_shelf_life_guideline(
                select(
                    InventoryLot.id,
                    Product.name,
                    InventoryLot.quantity_value,
                    Unit.symbol,
                    StorageLocation.name,
                    InventoryLot.best_before,
                    expiry.label("effective_expiry"),
                    InventoryLot.created_at,
                    InventoryLot.updated_at,
                )
                .join(Product, Product.id == InventoryLot.product_id)
                .join(Unit, Unit.code == InventoryLot.quantity_unit_code)
                .outerjoin(StorageLocation, StorageLocation.id == InventoryLot.storage_location_id)
            )
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
                expiry.is_not(None),
                expiry >= earliest,
                expiry <= latest,
            )
            # Ties broken by identifier so two polls a millisecond apart cannot
            # disagree on the order and produce a different collection tag.
            .order_by(expiry.asc(), InventoryLot.id.asc())
            .limit(limit)
        )
        return [
            ExpiringLot(
                lot_id=row[0],
                product_name=row[1],
                quantity_value=row[2],
                quantity_symbol=row[3],
                location_name=row[4],
                best_before=row[5],
                effective_expiry=row[6],
                created_at=row[7],
                updated_at=row[8],
            )
            for row in rows
        ]
