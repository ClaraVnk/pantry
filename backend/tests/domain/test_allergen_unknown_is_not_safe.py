"""The one property of ADR-0009 whose failure has a physical cost.

"No allergen data" and "no allergen" are different statements, and every test in
this file exists because the code that conflates them is easy to write, passes
review, and reads perfectly. There is no clever assertion here -- just the same
question asked at each of the three layers that could get it wrong: the
normaliser reading Open Food Facts, the domain function reporting a state, and
PostgreSQL answering the query a service will actually run.

The database half runs against a real PostgreSQL, because the property is
carried by a generated column and a check constraint. Asserting it against the
model objects would test the docstring.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.dietary import (
    ALLERGEN_TAGS,
    AllergenPresence,
    allergen_presence,
    assess_allergens,
)
from chaudron.domain.models import Allergen, AllergenDataState, Product, ProductSource
from chaudron.domain.ports import CatalogRecord
from chaudron.infra.repositories.products import SqlProductRepository

# --------------------------------------------------------------------------- #
# Reading the catalogue
# --------------------------------------------------------------------------- #


def test_a_product_with_no_allergen_fields_is_unknown_not_empty() -> None:
    """The majority case on a wiki, and the one that must not read as "clear".

    Open Food Facts omits the keys entirely on a product nobody has documented.
    An implementation doing ``payload.get("allergens_tags", [])`` produces an
    empty declaration, which every later reader is entitled to interpret as
    "contains none of the fourteen".
    """
    assessment = assess_allergens({"product_name": "Farine de blé"})

    assert assessment.state is AllergenDataState.UNKNOWN
    assert assessment.contains == ()
    assert assessment.may_contain == ()


def test_an_empty_tag_list_is_also_unknown() -> None:
    """The other shape of "no data" the API emits, and it means the same thing.

    Both forms occur in production data: products saved before the field existed
    lack the key, products saved since carry ``[]``. Neither is a declaration.
    """
    assessment = assess_allergens({"allergens_tags": [], "traces_tags": []})

    assert assessment.state is AllergenDataState.UNKNOWN


def test_a_declared_product_with_no_allergens_is_declared() -> None:
    """The contrast case, without which the test above passes for the wrong reason."""
    assessment = assess_allergens({"allergens_tags": ["en:none"], "traces_tags": []})

    assert assessment.state is AllergenDataState.DECLARED
    assert assessment.contains == ()


def test_an_unreadable_tag_sends_the_whole_record_back_to_unknown() -> None:
    """A list one line of which we cannot read is not a declaration.

    ``fr:sarrasin`` is buckwheat: harmless, outside the fourteen, and *not* in
    the taxonomy -- Open Food Facts prefixed it with the contributor's language
    because it could not resolve it. The same mechanism produces ``fr:avoine``,
    which is oats, which is a gluten cereal, on thousands of products. Dropping
    what we cannot parse and certifying the rest is how a gluten declaration
    goes missing.
    """
    assessment = assess_allergens({"allergens_tags": ["en:milk", "fr:sarrasin"]})

    assert assessment.state is AllergenDataState.UNKNOWN
    assert Allergen.MILK in assessment.contains
    assert assessment.unreadable_tags == ("fr:sarrasin",)


def test_a_known_non_european_allergen_does_not_degrade_the_record() -> None:
    """``en:pork`` is vocabulary we understand and do not act on.

    Without this distinction the rule above would degrade every Japanese-market
    product to unknown, and the degradation would stop meaning anything.
    """
    assessment = assess_allergens({"allergens_tags": ["en:milk", "en:pork"]})

    assert assessment.state is AllergenDataState.DECLARED
    assert assessment.contains == (Allergen.MILK,)


def test_tags_are_case_folded() -> None:
    """Real products carry ``fr:Avoine`` and ``fr:avoine`` as two separate entries."""
    assessment = assess_allergens({"allergens_tags": ["EN:MILK", "en:milk"]})

    assert assessment.contains == (Allergen.MILK,)


def test_a_declared_presence_outranks_a_trace() -> None:
    """One allergen, one state. The schema refuses to hold both, so nor do we."""
    assessment = assess_allergens(
        {"allergens_tags": ["en:milk"], "traces_tags": ["en:milk", "en:nuts"]}
    )

    assert assessment.contains == (Allergen.MILK,)
    assert assessment.may_contain == (Allergen.NUTS,)


def test_every_regulated_allergen_has_a_tag_and_they_are_all_distinct() -> None:
    """The mapping covers the fourteen and collides on none of them.

    Cheap, and it is the test that fails when somebody adds a fifteenth member
    to the enum and forgets that a product then has no way to declare it.
    """
    assert set(ALLERGEN_TAGS) == set(Allergen)
    assert len(set(ALLERGEN_TAGS.values())) == len(Allergen)


# --------------------------------------------------------------------------- #
# Reporting a state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("allergen", list(Allergen), ids=[a.value for a in Allergen])
def test_no_data_reports_unknown_for_every_allergen(allergen: Allergen) -> None:
    """Not one of the fourteen escapes the unknown state through an empty list."""
    assert (
        allergen_presence(AllergenDataState.UNKNOWN, (), (), allergen) is AllergenPresence.UNKNOWN
    )


def test_declared_absence_is_reported_as_absence_and_only_then() -> None:
    """``None`` is reachable only from a record that declared something.

    This is the asymmetry the whole design rests on: a caller writing
    ``if allergen_presence(...) is not None: withhold`` withholds the
    undocumented product without having considered it.
    """
    assert allergen_presence(AllergenDataState.DECLARED, (), (), Allergen.PEANUTS) is None
    assert allergen_presence(AllergenDataState.UNKNOWN, (), (), Allergen.PEANUTS) is not None


def test_states_are_reported_from_the_lists_when_data_exists() -> None:
    presence = allergen_presence(
        AllergenDataState.DECLARED, (Allergen.MILK,), (Allergen.NUTS,), Allergen.MILK
    )
    traces = allergen_presence(
        AllergenDataState.DECLARED, (Allergen.MILK,), (Allergen.NUTS,), Allergen.NUTS
    )

    assert presence is AllergenPresence.CONTAINS
    assert traces is AllergenPresence.MAY_CONTAIN


# --------------------------------------------------------------------------- #
# Answering the query
# --------------------------------------------------------------------------- #
#
# From here on, PostgreSQL. `allergens_risk` is a generated column and the
# invariants are check constraints; asserting them in Python would assert
# nothing about what a service will actually run.


async def _add(session: AsyncSession, **columns: object) -> uuid.UUID:
    product_id = uuid.uuid7()
    await session.execute(
        sa.insert(Product).values(
            id=product_id,
            household_id=None,
            name="fixture",
            source=ProductSource.OPEN_FOOD_FACTS,
            **columns,
        )
    )
    return product_id


async def test_the_obvious_exclusion_query_withholds_the_undocumented_product(
    db_session: AsyncSession,
) -> None:
    """The headline. Written the way a service would write it, with no special case.

    Three products, one query, and the only one that comes back is the one whose
    record actually says it has no peanuts. The undocumented product is excluded
    by the shape of the schema, not by the author of this query having
    remembered that ``unknown`` exists -- which is the entire point, because the
    author who forgets is the one this mechanism has to survive.
    """
    silent = await _add(db_session)
    declared_clear = await _add(db_session, allergen_state=AllergenDataState.DECLARED)
    declared_peanuts = await _add(
        db_session,
        allergen_state=AllergenDataState.DECLARED,
        allergens_contains=[Allergen.PEANUTS],
    )

    excluded = sa.select(Product.id).where(
        Product.id.in_([silent, declared_clear, declared_peanuts]),
        ~Product.allergens_risk.overlap([Allergen.PEANUTS]),
    )
    survivors = set((await db_session.scalars(excluded)).all())

    assert survivors == {declared_clear}, (
        "a product with no allergen data survived a peanut exclusion: 'unknown' is "
        "being read as 'contains no peanut'"
    )


async def test_an_undocumented_product_carries_every_allergen_as_a_risk(
    db_session: AsyncSession,
) -> None:
    """Not fourteen separate rules: one generated column, maximally excluding."""
    product_id = await _add(db_session)

    risk = await db_session.scalar(
        sa.select(Product.allergens_risk).where(Product.id == product_id)
    )

    assert risk is not None
    assert set(risk) == set(Allergen)


async def test_a_trace_is_a_risk_just_like_a_declaration(
    db_session: AsyncSession,
) -> None:
    """``may_contain`` filters exactly as ``contains`` does (ADR-0009)."""
    product_id = await _add(
        db_session,
        allergen_state=AllergenDataState.DECLARED,
        allergens_may_contain=[Allergen.NUTS],
    )

    risk = await db_session.scalar(
        sa.select(Product.allergens_risk).where(Product.id == product_id)
    )

    assert risk == [Allergen.NUTS]


async def test_the_risk_column_cannot_be_written(db_session: AsyncSession) -> None:
    """Generated, therefore not something a hurried ``UPDATE`` can quietly clear.

    The failure mode this closes is not malice, it is a repository that copies
    every column of a catalogue record onto a row and happens to include this
    one, silently replacing the guarantee with whatever the caller believed.
    """
    with pytest.raises(ProgrammingError):
        await db_session.execute(
            sa.insert(Product).values(
                id=uuid.uuid7(),
                household_id=None,
                name="fixture",
                source=ProductSource.OPEN_FOOD_FACTS,
                allergens_risk=[],
            )
        )


async def test_an_unknown_product_cannot_carry_declarations(
    db_session: AsyncSession,
) -> None:
    """A row that says "no data" while listing allergens lies about its provenance."""
    with pytest.raises(IntegrityError, match="allergen_unknown_is_empty"):
        await _add(db_session, allergens_contains=[Allergen.MILK])


async def test_an_allergen_cannot_be_in_two_states_at_once(
    db_session: AsyncSession,
) -> None:
    """ "Contains milk" and "may contain traces of milk" is one fact stated twice.

    Held together, whichever a reader consults first becomes the answer -- and
    the two answers lead to different sentences on screen.
    """
    with pytest.raises(IntegrityError, match="allergen_states_disjoint"):
        await _add(
            db_session,
            allergen_state=AllergenDataState.DECLARED,
            allergens_contains=[Allergen.MILK],
            allergens_may_contain=[Allergen.MILK],
        )


async def test_a_refresh_that_loses_the_data_loses_the_claim(
    db_session: AsyncSession,
) -> None:
    """A contributor blanking a wiki page must revoke what it used to support.

    The tempting implementation only writes the allergen columns when the record
    has something to say, which leaves yesterday's declaration in place while the
    upstream page now says nothing. The product would keep looking documented
    forever, on the strength of an edit that has since been undone.
    """
    repository = SqlProductRepository(db_session)
    gtin = "00003017620422"
    await repository.upsert_public(
        CatalogRecord(
            gtin=gtin,
            name="Pot de confiture",
            allergen_state=AllergenDataState.DECLARED,
            allergens_contains=(Allergen.NUTS,),
        )
    )

    await repository.upsert_public(CatalogRecord(gtin=gtin, name="Pot de confiture"))

    state, risk = (
        await db_session.execute(
            sa.select(Product.allergen_state, Product.allergens_risk).where(
                Product.gtin == gtin, Product.household_id.is_(None)
            )
        )
    ).one()
    assert state is AllergenDataState.UNKNOWN
    assert set(risk) == set(Allergen)


async def test_a_null_element_cannot_enter_an_allergen_list(
    db_session: AsyncSession,
) -> None:
    """NULL inside the array makes every containment test answer "unknown".

    Which is the one answer an allergen filter must never give: ``NOT (x && y)``
    with a NULL in ``y`` is NULL, and a NULL predicate excludes the row from a
    ``WHERE`` -- silently turning an exclusion filter into a disclosure.
    """
    with pytest.raises(IntegrityError, match="tag_arrays_wellformed"):
        await _add(
            db_session,
            allergen_state=AllergenDataState.DECLARED,
            allergens_contains=[Allergen.MILK, None],
        )
