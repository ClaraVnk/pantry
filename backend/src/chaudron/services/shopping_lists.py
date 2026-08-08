"""The shopping list a household is holding right now, and what edits it.

Contract 6bis defines this path after the fact, and the reason it was missing is
worth keeping: the depletion proposal said "adding goes through the normal
shopping-list write path", and that path existed nowhere -- the only endpoints
were the import's, so the interface had to route "add one item" through
``import`` then ``confirm``. The write path is here.

Two shapes in this module are decisions rather than conveniences.

**Adding takes an array.** Emptying the fridge after a meal depletes several
items at once and the contract asks for one grouped proposal; an endpoint that
accepted one item would turn a single user gesture into five round trips, five
transactions and five chances to half-succeed. One call, one transaction.

**Declining is durable, and the table says so.** A refusal has no expiry by
design: one that quietly forgot itself after a few months would re-propose
exactly what the user waved away, and they would have no way to know why. The
store is a port over ``declined_repurchase`` (migration ``0007``), and this
service holds a real one rather than an optional one -- accepting a refusal that
cannot be kept is the failure mode the port exists to make impossible.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Product, ShoppingItemOrigin, ShoppingListItem
from chaudron.domain.ports import (
    UNSET,
    InvalidQuantityError,
    Maybe,
    ProductNotFoundError,
    UnknownUnitError,
    UnsetType,
)
from chaudron.domain.shopping import (
    DeclinedRepurchaseStore,
    ItemSource,
    NewShoppingItem,
    ParsedQuantity,
    ShoppingItemNotFoundError,
    ShoppingItemView,
    ShoppingListView,
)
from chaudron.infra.untrusted_text import sanitize
from chaudron.services.shopping_import import (
    MAX_LABEL_LENGTH,
    UnitLexicon,
    resolve_current_list,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ITEMS_PER_CALL",
    "DeclinedRepurchaseService",
    "ShoppingListService",
    "UpdateItemCommand",
]

#: Items one ``POST`` may add. The grouped repurchase proposal is a handful of
#: products; the bound is what stops the endpoint becoming a bulk-import path
#: that skips the review the import endpoint exists to impose.
MAX_ITEMS_PER_CALL: Final = 100

#: Contract ``source`` values onto the persisted ``shopping_item_origin`` enum.
#:
#: The enum has three members and the contract has three values, but they are not
#: the same three: there is no ``import`` member and no ``depleted`` one. Adding
#: them is a migration this change does not own, so:
#:
#: * ``depleted`` maps to ``low_stock``, which is what it means -- the stock ran
#:   out. This is the mapping that matters, because it is the one the "does the
#:   repurchase proposal get used?" question is asked of, and it survives the round
#:   trip intact.
#: * ``import`` maps to ``manual``, and **does not survive**: a line read back
#:   afterwards reports ``manual``. A human reviewed every imported line, so the
#:   value is not wrong, only less precise. Distinguishing the two needs an enum
#:   value.
_ORIGIN_BY_SOURCE: Final[dict[ItemSource, ShoppingItemOrigin]] = {
    "manual": ShoppingItemOrigin.MANUAL,
    "depleted": ShoppingItemOrigin.LOW_STOCK,
    "import": ShoppingItemOrigin.MANUAL,
}

_SOURCE_BY_ORIGIN: Final[dict[ShoppingItemOrigin, ItemSource]] = {
    ShoppingItemOrigin.MANUAL: "manual",
    ShoppingItemOrigin.LOW_STOCK: "depleted",
    ShoppingItemOrigin.RECIPE: "manual",
}


@dataclass(frozen=True, slots=True)
class UpdateItemCommand:
    """A partial edit. ``UNSET`` means "leave this alone", which is not ``None``.

    ``quantity`` set to ``None`` clears it; the schema stores value, unit code and
    dimension together or not at all, so there is no way to clear half of one.
    """

    checked: Maybe[bool] = UNSET
    quantity: Maybe[tuple[Decimal, str] | None] = UNSET


class ShoppingListService:
    """Read and edit the household's current list.

    Takes the session directly, like :class:`ShoppingImportService`: one writer,
    two tables, no second consumer. Never commits.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current(self, household_id: uuid.UUID) -> ShoppingListView:
        """The household's list, created on first read if it has none.

        A read that may write is unusual and deliberate: the alternative is a
        client that has to handle "no list yet" before it can show anything, and a
        household's *first* list is not a decision anybody makes -- it is the
        thing they were already asking for. The creation is idempotent, guarded by
        the partial unique index on ``(household_id) WHERE is_default``.
        """
        target = await resolve_current_list(self._session, household_id)
        return ShoppingListView(
            id=target.id,
            name=target.name,
            items=await self._items(household_id, target.id),
        )

    async def add_items(
        self, household_id: uuid.UUID, items: Sequence[NewShoppingItem]
    ) -> ShoppingListView:
        """Add every item in one transaction, or add none of them.

        Returns the whole list rather than the created rows: the caller has just
        changed what it is displaying, and a second round trip to find out what it
        now looks like is the kind of thing that turns into a stale interface.
        """
        lexicon = await UnitLexicon.load(self._session)
        target = await resolve_current_list(self._session, household_id)
        next_order = await self._next_sort_order(household_id, target.id)

        for offset, item in enumerate(items):
            row = await self._build(household_id, target.id, item, lexicon)
            row.sort_order = next_order + offset
            self._session.add(row)
        await self._session.flush()

        logger.info(
            "shopping_items_added",
            extra={
                "shopping_list_id": str(target.id),
                "item_count": len(items),
                # The mix of sources is the measurement contract 6bis asks for.
                "sources": sorted({item.source for item in items}),
            },
        )
        return ShoppingListView(
            id=target.id, name=target.name, items=await self._items(household_id, target.id)
        )

    async def update_item(
        self, household_id: uuid.UUID, item_id: uuid.UUID, command: UpdateItemCommand
    ) -> ShoppingItemView:
        """Tick, untick, or correct the quantity. Nothing else is editable."""
        row = await self._require_item(household_id, item_id)

        if not isinstance(command.checked, UnsetType):
            # ``func.now()`` rather than a Python timestamp: the column's default
            # is the database clock, and two sources of "now" in one table is how
            # an ordering becomes non-deterministic under clock skew.
            row.checked_at = (
                await self._session.scalar(select(func.now())) if command.checked else None
            )

        if not isinstance(command.quantity, UnsetType):
            if command.quantity is None:
                row.quantity_value = None
                row.quantity_unit_code = None
                row.quantity_dimension = None
            else:
                amount, code = command.quantity
                lexicon = await UnitLexicon.load(self._session)
                unit = lexicon.get(code)
                if unit is None:
                    raise UnknownUnitError(code)
                row.quantity_value = amount
                row.quantity_unit_code = unit.code
                row.quantity_dimension = unit.dimension

        await self._session.flush()
        return await self._view(row)

    async def remove_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> None:
        row = await self._require_item(household_id, item_id)
        await self._session.delete(row)
        await self._session.flush()

    # -- internals ---------------------------------------------------------- #

    async def _build(
        self,
        household_id: uuid.UUID,
        list_id: uuid.UUID,
        item: NewShoppingItem,
        lexicon: UnitLexicon,
    ) -> ShoppingListItem:
        label = sanitize(item.free_text, limit=MAX_LABEL_LENGTH) if item.free_text else None
        product_id: uuid.UUID | None = None
        if item.product_id is not None:
            product = await self._visible_product(household_id, item.product_id)
            if product is None:
                raise ProductNotFoundError(item.product_id)
            product_id = product.id
        if product_id is None and not label:
            raise InvalidQuantityError("an item needs either product_id or free_text")

        unit = None
        if item.unit_code is not None:
            unit = lexicon.get(item.unit_code)
            if unit is None:
                raise UnknownUnitError(item.unit_code)
        if (item.amount is None) != (unit is None):
            raise InvalidQuantityError("amount and unit are set together or not at all")

        return ShoppingListItem(
            household_id=household_id,
            shopping_list_id=list_id,
            product_id=product_id,
            label=label,
            quantity_value=item.amount,
            quantity_unit_code=None if unit is None else unit.code,
            quantity_dimension=None if unit is None else unit.dimension,
            origin=_ORIGIN_BY_SOURCE[item.source],
        )

    async def _visible_product(
        self, household_id: uuid.UUID, product_id: uuid.UUID
    ) -> Product | None:
        found: Product | None = await self._session.scalar(
            select(Product).where(
                Product.id == product_id,
                Product.archived_at.is_(None),
                # Not `in_([household_id, None])`: SQL never matches NULL with IN.
                (Product.household_id == household_id) | Product.household_id.is_(None),
            )
        )
        return found

    async def _require_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> ShoppingListItem:
        row = await self._session.scalar(
            select(ShoppingListItem).where(
                ShoppingListItem.id == item_id,
                # Belt to the row-level policy's braces. RLS already scopes the
                # statement; naming the household here means a misconfigured
                # session fails to find the row rather than editing somebody's.
                ShoppingListItem.household_id == household_id,
            )
        )
        if row is None:
            raise ShoppingItemNotFoundError(f"no shopping list item {item_id}")
        return row

    async def _items(
        self, household_id: uuid.UUID, list_id: uuid.UUID
    ) -> tuple[ShoppingItemView, ...]:
        rows = (
            await self._session.execute(
                select(ShoppingListItem, Product.name)
                .outerjoin(Product, Product.id == ShoppingListItem.product_id)
                .where(
                    ShoppingListItem.household_id == household_id,
                    ShoppingListItem.shopping_list_id == list_id,
                )
                .order_by(ShoppingListItem.sort_order, ShoppingListItem.created_at)
            )
        ).all()
        return tuple(_to_view(row, name) for row, name in rows)

    async def _view(self, row: ShoppingListItem) -> ShoppingItemView:
        name = (
            None
            if row.product_id is None
            else await self._session.scalar(
                select(Product.name).where(Product.id == row.product_id)
            )
        )
        return _to_view(row, name)

    async def _next_sort_order(self, household_id: uuid.UUID, list_id: uuid.UUID) -> int:
        highest = await self._session.scalar(
            select(func.max(ShoppingListItem.sort_order)).where(
                ShoppingListItem.household_id == household_id,
                ShoppingListItem.shopping_list_id == list_id,
            )
        )
        return 0 if highest is None else int(highest) + 1


def _to_view(row: ShoppingListItem, product_name: str | None) -> ShoppingItemView:
    quantity = (
        None
        if row.quantity_value is None
        or row.quantity_unit_code is None
        or row.quantity_dimension is None
        else ParsedQuantity(
            amount=row.quantity_value,
            unit_code=row.quantity_unit_code,
            dimension=row.quantity_dimension,
        )
    )
    return ShoppingItemView(
        id=row.id,
        product_id=row.product_id,
        product_name=product_name,
        free_text=row.label,
        quantity=quantity,
        source=_SOURCE_BY_ORIGIN[row.origin],
        checked=row.checked_at is not None,
        sort_order=row.sort_order,
    )


class DeclinedRepurchaseService:
    """Remember, and forget on request, that a household waved a proposal away.

    Thin by design -- the store does the work and the transaction is the
    request's -- but it is where the two gestures are *named*, and where the log
    line that answers "is anyone using this?" is written. The product count is
    logged; the products are not. What a household refuses to buy is exactly the
    kind of detail ``docs/security-model.md`` keeps out of the log.
    """

    def __init__(self, store: DeclinedRepurchaseStore) -> None:
        self._store = store

    async def decline(self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]) -> None:
        await self._store.decline(household_id, product_ids)
        logger.info("repurchase_declined", extra={"product_count": len(product_ids)})

    async def undecline(self, household_id: uuid.UUID, product_id: uuid.UUID) -> None:
        await self._store.undecline(household_id, product_id)
        logger.info("repurchase_decline_cleared")
