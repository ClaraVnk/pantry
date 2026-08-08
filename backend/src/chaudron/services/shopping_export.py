"""Read a shopping list, render it, and hand it to somewhere it is useful.

The service knows two things and deliberately no third: how to turn rows into
:class:`~chaudron.domain.shopping_export.ExportLine` values, and that something
implementing :class:`~chaudron.domain.shopping_export.ShoppingListExporter` will
take them. It cannot name a destination, cannot hold a token, and has no import
that would let it.

Three decisions are taken here because they are decisions about *the list*.

**Only pending items leave.** ``checked_at IS NULL`` is the filter, and it is the
same one the interface uses to draw the list. Exporting ticked items would
recreate, in somebody's task application, work they have already done.

**The label a household typed is untrusted text.** It reaches a third party's
servers and comes back out on other people's screens, so it goes through
:mod:`chaudron.infra.untrusted_text` first: one bounded line, no control
characters, no bidi overrides, no tag-block smuggling. The domain renderer
collapses whitespace again -- two passes, because the guarantee "one line per
item" must not rest on this module having remembered.

**Quantities travel with their display symbol, not their code.** ``L`` and
``c. à s.`` are for a human reading a task title; ``l`` and ``tbsp`` are ours.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Final, cast

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Product,
    QuantityDimension,
    ShoppingList,
    ShoppingListItem,
    Unit,
)
from chaudron.domain.shopping import ShoppingListNotFoundError
from chaudron.domain.shopping_export import (
    ExportLine,
    ExportReceipt,
    ShoppingListExport,
    ShoppingListExporter,
    render_plain_text,
)
from chaudron.infra.untrusted_text import sanitize

__all__ = ["MAX_LABEL_CHARS", "ShoppingExportService"]

logger = logging.getLogger(__name__)

#: Ceiling on one item name once sanitised. Long enough for "Yaourt nature brassé
#: au lait entier, pot de 4", short enough that one pathological label cannot
#: dominate a shared list or a task title.
MAX_LABEL_CHARS: Final = 200


class ShoppingExportService:
    """Assembles a list for export, and sends it when asked to.

    Two operations, and the split is not cosmetic. :meth:`plain_text` never
    leaves the instance -- it answers the household's own browser, which is what
    ``navigator.share`` then hands to whatever the user picks. :meth:`send` does
    leave, to a named third party, and is the only one that needs a destination,
    a credential and a consent.
    """

    __slots__ = ("_now", "_session")

    def __init__(
        self,
        session: AsyncSession,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        self._session = session
        #: Injected so a test can assert on a fixed instant rather than on
        #: whatever the clock said; ``None`` means read it at call time.
        self._now = now

    # -- reading ----------------------------------------------------------- #

    async def build_export(
        self, household_id: uuid.UUID, shopping_list_id: uuid.UUID
    ) -> ShoppingListExport:
        """The pending items of one of this household's lists, ready to render.

        The household is filtered explicitly even though row-level security
        already restricts the rows (migration ``0004``). Belt and braces is the
        house style for tenant reads: the policy is the control, the predicate is
        what keeps a future refactor from quietly relying on the policy alone.
        """
        shopping_list = await self._session.scalar(
            select(ShoppingList).where(
                ShoppingList.id == shopping_list_id,
                ShoppingList.household_id == household_id,
                ShoppingList.archived_at.is_(None),
            )
        )
        if shopping_list is None:
            raise ShoppingListNotFoundError(f"no shopping list {shopping_list_id}")

        result = await self._session.execute(self._items_query(household_id, shopping_list_id))
        # The three joined columns are cast back to optional on purpose.
        # SQLAlchemy types a mapped column by its declaration, which says
        # `product.name` is `str` -- true of the row, false of an OUTER JOIN that
        # matched nothing. Trusting the declared type here would let the type
        # checker delete the `None` branches that the query actually produces.
        lines = tuple(
            line
            for line in (
                _to_line(
                    row[0],
                    cast("str | None", row[1]),
                    cast("str | None", row[2]),
                    cast("QuantityDimension | None", row[3]),
                )
                for row in result.all()
            )
            if line is not None
        )
        return ShoppingListExport(
            export_id=uuid.uuid4(),
            shopping_list_id=shopping_list.id,
            shopping_list_name=shopping_list.name,
            lines=lines,
            generated_at=self._now or dt.datetime.now(dt.UTC),
        )

    @staticmethod
    def _items_query(
        household_id: uuid.UUID, shopping_list_id: uuid.UUID
    ) -> Select[tuple[ShoppingListItem, str, str, QuantityDimension]]:
        """One statement, two outer joins, no N+1.

        The joins are outer because both sides are legitimately absent: an item
        can be free text with no catalogue product ("du pain"), and it can carry
        no quantity at all. A per-item lookup for the product name would issue one
        query per line of a shopping list, which is the shape this codebase calls
        a bug rather than a style.
        """
        return (
            select(ShoppingListItem, Product.name, Unit.symbol, Unit.dimension)
            .outerjoin(Product, Product.id == ShoppingListItem.product_id)
            .outerjoin(
                Unit,
                (Unit.code == ShoppingListItem.quantity_unit_code)
                & (Unit.dimension == ShoppingListItem.quantity_dimension),
            )
            .where(
                ShoppingListItem.household_id == household_id,
                ShoppingListItem.shopping_list_id == shopping_list_id,
                ShoppingListItem.checked_at.is_(None),
            )
            .order_by(ShoppingListItem.sort_order, ShoppingListItem.created_at)
        )

    async def plain_text(self, household_id: uuid.UUID, shopping_list_id: uuid.UUID) -> str:
        """The list as text: what ``navigator.share({ text })`` sends.

        The research note (§4.7) ranks this first by a distance no other option
        closes -- around ninety per cent of the benefit for a day of work, on iOS,
        Android and desktop at once -- and ADR-0010 adopts it as the primary path.
        Everything below it in that table is a bonus for the people who happen to
        use one particular application.
        """
        export = await self.build_export(household_id, shopping_list_id)
        return render_plain_text(export.lines)

    # -- sending ----------------------------------------------------------- #

    async def send(
        self,
        household_id: uuid.UUID,
        shopping_list_id: uuid.UUID,
        exporter: ShoppingListExporter,
    ) -> ExportReceipt:
        """Hand the list over, and log that it happened without logging what it said.

        The log line is counts and identifiers. A shopping list is a record of
        what identifiable people eat, and a log file is the one place a copy
        survives every retention promise the application makes elsewhere.
        """
        export = await self.build_export(household_id, shopping_list_id)
        receipt = await exporter.send(export)
        logger.info(
            "shopping_list_exported",
            extra={
                "export_id": str(receipt.export_id),
                "shopping_list_id": str(export.shopping_list_id),
                "target": receipt.target,
                "item_count": receipt.accepted_line_count,
            },
        )
        return receipt


def _to_line(
    item: ShoppingListItem,
    product_name: str | None,
    unit_symbol: str | None,
    dimension: QuantityDimension | None,
) -> ExportLine | None:
    """One row as an export line, or ``None`` for a row that names nothing.

    ``target_present`` in the schema already forbids an item with neither a
    product nor a label, so ``None`` is unreachable through the API. It is still
    handled, because the alternative on a row written by some future path is an
    exported line reading "None" -- and a shopping list that lies is worse than
    one that is short.
    """
    raw_name = item.label or product_name
    if raw_name is None:
        return None
    name = sanitize(raw_name, limit=MAX_LABEL_CHARS)
    if not name:
        return None
    quantity = item.quantity_value
    if quantity is not None and quantity <= 0:
        # A zero or negative amount is not a shopping instruction. The item still
        # goes out -- somebody wrote it down -- without a number nobody can act on.
        quantity = None
    return ExportLine(
        name=name,
        quantity=quantity,
        unit_symbol=unit_symbol,
        is_count=dimension is QuantityDimension.COUNT,
    )
