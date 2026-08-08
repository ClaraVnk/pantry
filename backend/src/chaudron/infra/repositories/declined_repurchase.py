"""The durable memory of repurchase proposals a household waved away.

Contract 6bis asks for a refusal that survives: a product dismissed once must not
be offered again every time it is finished. That is per-household state with no
expiry by design, so it is a table -- an in-memory stand-in would let a user
decline a proposal, watch it be accepted, and meet it again after the next
restart with nothing on screen to explain why.

Three rows of behaviour worth naming, because each one is a decision:

*Declining is idempotent.* ``ON CONFLICT DO NOTHING`` on the unique
``(household_id, product_id)`` rather than a read-then-insert. The gesture that
produces a duplicate is real -- the same proposal dismissed on the phone and on
the tablet -- and it must be a no-op, not a ``500`` and not a lost refusal.

*Revoking is a delete.* The row *is* the refusal, so there is no state to flip
and no tombstone to keep: people change their minds, and a decision taken in
three seconds over a bin has no business leaving a trace that outlives it.

*The tenant is in every predicate anyway.* Row-level security already scopes the
table (migration ``0007``), and the ``WHERE household_id = ...`` written here is
the second lock rather than the first: the policies protect the application role,
these predicates protect the maintenance identities that own the table and are
therefore exempt from them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import DeclinedRepurchase

__all__ = ["SqlDeclinedRepurchaseStore"]


class SqlDeclinedRepurchaseStore:
    """:class:`chaudron.domain.shopping.DeclinedRepurchaseStore` over PostgreSQL.

    Takes the session and never commits: the transaction belongs to the request
    (``infra/db.py``), so declining and whatever else the same call writes either
    both happen or neither does.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def declined_products(
        self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """Which of *product_ids* this household has refused. One query, any size.

        Called on the depletion path, where the alternative -- one lookup per
        depleted product -- would turn emptying a fridge into five round trips.
        """
        if not product_ids:
            # `IN ()` is not valid SQL and SQLAlchemy renders it as a false
            # predicate; asking the database for an answer we already have is
            # simply waste on a path that runs on every removal.
            return frozenset()
        rows = await self._session.scalars(
            select(DeclinedRepurchase.product_id).where(
                DeclinedRepurchase.household_id == household_id,
                DeclinedRepurchase.product_id.in_(product_ids),
            )
        )
        return frozenset(rows)

    async def decline(self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]) -> None:
        """Remember the refusal of every product named, once each.

        ``declined_by_user_id`` is left NULL: there are no user accounts in this
        slice (``api/deps.py``), and the refusal belongs to the household anyway.
        The column is there so the answer can be filled in later without a
        migration, not so it can be guessed now.
        """
        unique = list(dict.fromkeys(product_ids))
        if not unique:
            return
        await self._session.execute(
            insert(DeclinedRepurchase)
            .values(
                [
                    {
                        "id": uuid.uuid7(),
                        "household_id": household_id,
                        "product_id": product_id,
                    }
                    for product_id in unique
                ]
            )
            .on_conflict_do_nothing(constraint="uq_declined_repurchase_household_product")
        )

    async def undecline(self, household_id: uuid.UUID, product_id: uuid.UUID) -> None:
        """Forget one refusal. Silent when there was none: the end state is the same."""
        await self._session.execute(
            delete(DeclinedRepurchase).where(
                DeclinedRepurchase.household_id == household_id,
                DeclinedRepurchase.product_id == product_id,
            )
        )
