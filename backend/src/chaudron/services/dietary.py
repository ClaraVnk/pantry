"""Reading the eaters, the safety tables and the stock they may be shown.

Everything here is a read. The one write path over ``household_person`` is
:mod:`chaudron.services.members`; keeping them apart means the screen that
decides what a model may see cannot also change who is at the table.

The queries go through the request's session rather than a repository port, for
the reason ``services/recipes.py`` already gives: a port with one implementation
and one caller is indirection without a second reader to justify it. The one
thing that is *not* negotiable is which columns they select --
``product.allergens_risk`` and never ``product.allergens_contains`` -- and that
is asserted by a test rather than by this paragraph.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from chaudron.domain.constraints import (
    HouseholdConstraints,
    InfantRule,
    Person,
    ProductFacts,
    StockLine,
    WithholdReason,
    union_of,
    withhold_reason,
)
from chaudron.domain.models import (
    AgeBand,
    AllergenDataState,
    HouseholdPerson,
    InfantFoodRestriction,
    InventoryLot,
    NutritionReference,
    PnnsMarker,
    Product,
)
from chaudron.domain.ports import MemberNotInHouseholdError
from chaudron.domain.shelf_life import effective_expiry_sql, join_shelf_life_guideline

__all__ = ["MAX_SCREENED_ITEMS", "DietaryService", "ScreenedStock"]

#: How many stock lines one screen loads. Mirrors ``MAX_INVENTORY_ITEMS`` in the
#: recipe service: the screen runs over exactly what would be sent, so a bound
#: applied afterwards would let a withheld product displace a safe one.
MAX_SCREENED_ITEMS = 300


@dataclass(frozen=True, slots=True)
class ScreenedStock:
    """The inventory split in two, and why the second half is not in the first.

    ``withheld`` is a count and a set of reasons, never a list of products. The
    interface has to be able to say "seven products were set aside because of a
    declared allergy" without naming them -- naming them would tell everybody at
    the table what one person is allergic to.
    """

    offered: tuple[StockLine, ...]
    withheld: int
    reasons: frozenset[WithholdReason]
    #: Transmitted products that carry no allergen data at all. They are only
    #: ever transmitted when nobody at the table declared an allergy -- otherwise
    #: ``allergens_risk`` holds all fourteen and the screen removed them.
    unverified: int

    @property
    def markers(self) -> frozenset[PnnsMarker]:
        markers: set[PnnsMarker] = set()
        for line in self.offered:
            markers |= line.product.pnns_markers
        return frozenset(markers)


class DietaryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- The eaters -------------------------------------------------------- #

    async def constraints_for(
        self, household_id: uuid.UUID, member_ids: Sequence[uuid.UUID]
    ) -> HouseholdConstraints:
        """The union of the named members' constraints; everybody when unnamed.

        An id from another household is a 422 and not a silent omission: quietly
        cooking for a smaller set than the caller asked for would drop somebody's
        allergy without telling anyone.
        """
        query = (
            select(HouseholdPerson)
            .where(HouseholdPerson.household_id == household_id)
            # The two sensitive columns are deferred on the model, so a plain
            # select does not load them. Asking for them explicitly is what makes
            # this the only place in the code that reads an allergy, and makes
            # that fact greppable (``domain/models.py``).
            .options(
                undefer(HouseholdPerson.allergens),
                undefer(HouseholdPerson.free_text_restrictions),
            )
            .order_by(HouseholdPerson.sort_order, HouseholdPerson.display_name)
        )
        if member_ids:
            query = query.where(HouseholdPerson.id.in_(list(member_ids)))
        rows = (await self._session.scalars(query)).all()
        if member_ids:
            found = {row.id for row in rows}
            for member_id in member_ids:
                if member_id not in found:
                    raise MemberNotInHouseholdError(member_id)
        return union_of(_to_person(row) for row in rows)

    # -- The safety table -------------------------------------------------- #

    async def infant_rules(self, bands: frozenset[AgeBand]) -> tuple[InfantRule, ...]:
        """The withholding rules that apply to the bands at this table.

        Narrowed by band in the query rather than in Python: every row loaded
        here becomes a rule applied to every product, and loading the honey rule
        for a household of adults would withhold honey from adults.
        """
        if not bands:
            return ()
        version = await self._current_reference()
        rows = (
            await self._session.scalars(
                select(InfantFoodRestriction)
                .where(InfantFoodRestriction.reference_version == version)
                .where(InfantFoodRestriction.applies_to_bands.overlap(list(bands)))
                .order_by(InfantFoodRestriction.sort_order, InfantFoodRestriction.rule_code)
            )
        ).all()
        return tuple(
            InfantRule(
                rule_code=row.rule_code,
                label=row.label,
                risk=row.risk,
                applies_to_bands=frozenset(row.applies_to_bands),
                category_tags=frozenset(tag.strip().lower() for tag in row.category_tags),
                name_patterns=tuple(row.name_patterns),
                statement=row.statement,
                source_url=row.source_url,
            )
            for row in rows
        )

    async def _current_reference(self) -> str:
        version = await self._session.scalar(
            select(NutritionReference.version).where(NutritionReference.is_current)
        )
        if version is None:  # pragma: no cover - seeded by migration 0005
            raise RuntimeError("no current nutrition reference is seeded")
        return version

    # -- The stock --------------------------------------------------------- #

    async def stock(
        self,
        household_id: uuid.UUID,
        location_ids: Sequence[uuid.UUID] = (),
        *,
        limit: int = MAX_SCREENED_ITEMS,
    ) -> tuple[StockLine, ...]:
        """Active lots with the four product columns the screen and the balance need.

        Ordered by expiry: contract 5 requires the inventory handed to the model
        to be ordered by urgency rather than leaving it to infer a priority from
        a date, and the same order makes an ambiguous ingredient resolve to
        whatever is closest to spoiling.

        The expiry in question is the effective one -- the printed date shortened
        by the after-opening rule (``domain/shelf_life.py``). Ordering on the
        printed date would sort an opened pot behind a sealed one it will outlast
        by weeks, and ``expires_in_days`` in the prompt would tell the model the
        opposite of the truth.
        """
        query = (
            join_shelf_life_guideline(
                select(
                    InventoryLot.id.label("lot_id"),
                    InventoryLot.quantity_value.label("quantity_value"),
                    InventoryLot.quantity_unit_code.label("unit_code"),
                    effective_expiry_sql().label("expires_on"),
                    Product.id.label("product_id"),
                    Product.name.label("product_name"),
                    Product.allergen_state.label("allergen_state"),
                    # The generated column, and the only one a filter may read:
                    # it carries all fourteen allergens when nobody has
                    # documented the product, so the natural exclusion withholds
                    # it (ADR-0009).
                    Product.allergens_risk.label("allergens_risk"),
                    Product.pnns_markers.label("pnns_markers"),
                    Product.category_tag.label("category_tag"),
                    # The taxonomy list, extracted server-side rather than by
                    # shipping the whole Open Food Facts record: three hundred
                    # raw payloads is megabytes over the wire for one array each.
                    Product.off_payload["categories_tags"].label("categories_tags"),
                ).join(Product, Product.id == InventoryLot.product_id)
            )
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
            )
            .order_by(
                effective_expiry_sql().asc().nullslast(),
                InventoryLot.created_at.desc(),
                InventoryLot.id,
            )
            .limit(limit)
        )
        if location_ids:
            query = query.where(InventoryLot.storage_location_id.in_(list(location_ids)))
        rows = (await self._session.execute(query)).all()
        return tuple(_to_line(row) for row in rows)

    def screen(
        self,
        lines: Sequence[StockLine],
        constraints: HouseholdConstraints,
        rules: Sequence[InfantRule],
    ) -> ScreenedStock:
        """Remove what these eaters must not be offered. The model sees the rest.

        Before the call, so the model cannot propose what it never saw. The
        counts that come back exist so the interface can explain a short list of
        suggestions as a consequence rather than as a failure (contract 3).
        """
        offered: list[StockLine] = []
        reasons: set[WithholdReason] = set()
        withheld = 0
        for line in lines:
            reason = withhold_reason(line.product, constraints, rules)
            if reason is None:
                offered.append(line)
            else:
                reasons.add(reason)
                withheld += 1
        unverified = sum(
            1 for line in offered if line.product.allergen_state is AllergenDataState.UNKNOWN
        )
        return ScreenedStock(
            offered=tuple(offered),
            withheld=withheld,
            reasons=frozenset(reasons),
            unverified=unverified,
        )


def _to_person(row: HouseholdPerson) -> Person:
    return Person(
        id=row.id,
        display_name=row.display_name,
        age_band=row.age_band,
        diet=row.diet,
        allergens=frozenset(row.allergens),
        infant_texture=row.infant_texture,
        free_text_restrictions=row.free_text_restrictions,
    )


def _to_line(row: Any) -> StockLine:
    return StockLine(
        inventory_item_id=row.lot_id,
        product=ProductFacts(
            product_id=row.product_id,
            name=row.product_name,
            allergen_state=row.allergen_state,
            allergens_risk=frozenset(row.allergens_risk or ()),
            pnns_markers=frozenset(row.pnns_markers or ()),
            category_tags=_category_tags(row.category_tag, row.categories_tags),
        ),
        quantity=_amount(row.quantity_value),
        unit=row.unit_code,
        expires_on=row.expires_on,
    )


def _category_tags(category_tag: str | None, categories_tags: object) -> tuple[str, ...]:
    """Both taxonomy sources, deduplicated. Either may be absent, neither is trusted.

    ``category_tag`` is the single tag this application resolved at ingestion;
    ``categories_tags`` is the raw Open Food Facts list, which is the only place
    a leaf like ``en:raw-milk-cheeses`` appears. Reading one and not the other
    would silently halve the reach of the infant rules.
    """
    tags: list[str] = []
    if category_tag:
        tags.append(category_tag)
    if isinstance(categories_tags, list):
        tags.extend(tag for tag in categories_tags if isinstance(tag, str) and tag)
    return tuple(dict.fromkeys(tags))


def _amount(value: Decimal) -> str:
    """``1.500`` -> ``1.5``, and never an exponent: this ends up in a prompt."""
    text = format(value, "f")
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"
