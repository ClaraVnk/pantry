"""The two safety reference tables, and the guidance table beside them.

These rows are the product's only claim to authority: they are quoted to a
household as public-health advice, with a URL next to them. So the tests check
two different kinds of thing.

*That the tables say what the module says.* The migration seeds them from
``chaudron.domain.dietary`` and ``chaudron.domain.shelf_life``, and if the two
ever drift the database wins silently -- a rule reviewed in a pull request would
stop being the rule applied at runtime.

*That the rows are the shape a safety control has to be.* Every restriction
applies to somebody, matches something, and cites a source. A rule that applies
to no age band or matches no product is worse than a missing rule: it looks like
protection on the screen and does nothing.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.dietary import (
    INFANT_AGE_BANDS,
    INFANT_RESTRICTIONS,
    PNNS_2019,
    PNNS_GUIDELINES,
    InfantRestrictionSpec,
    PnnsGuidelineSpec,
)
from chaudron.domain.models import (
    AgeBand,
    InfantFoodRestriction,
    NutritionReference,
    PnnsGuideline,
    ShelfLifeGuideline,
)
from chaudron.domain.shelf_life import SHELF_LIFE_GUIDELINES, ShelfLifeSpec

_RESTRICTION_IDS = [rule.rule_code for rule in INFANT_RESTRICTIONS]
_GUIDELINE_IDS = [guideline.marker.value for guideline in PNNS_GUIDELINES]
_SHELF_LIFE_IDS = [guideline.family.value for guideline in SHELF_LIFE_GUIDELINES]


# --------------------------------------------------------------------------- #
# Shape: what a safety row must have to be one
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rule", INFANT_RESTRICTIONS, ids=_RESTRICTION_IDS)
def test_every_infant_rule_protects_somebody_and_matches_something(
    rule: InfantRestrictionSpec,
) -> None:
    """A rule with no age band or no matcher is decoration that reads as protection."""
    assert rule.applies_to_bands, f"{rule.rule_code} applies to no age band"
    assert rule.category_tags or rule.name_patterns, f"{rule.rule_code} can never match a product"
    assert rule.statement.strip()
    assert rule.source_url.startswith("https://")


@pytest.mark.parametrize("rule", INFANT_RESTRICTIONS, ids=_RESTRICTION_IDS)
def test_no_infant_rule_reaches_an_adult(rule: InfantRestrictionSpec) -> None:
    """The table is a child-safety control, not a household-wide diet.

    Withholding honey from the adults because a baby lives here would make the
    feature so annoying that somebody would turn the whole thing off -- which is
    how a safety control ends up protecting nobody.
    """
    assert AgeBand.ADULT not in rule.applies_to_bands


@pytest.mark.parametrize("rule", INFANT_RESTRICTIONS, ids=_RESTRICTION_IDS)
def test_a_rule_that_reaches_children_also_reaches_infants(
    rule: InfantRestrictionSpec,
) -> None:
    """Age bands are not ordered by the enum, so coverage has to be asserted.

    ``applies_to_bands`` is an explicit list precisely so that inserting a band
    in the middle of the enum cannot silently change what a rule means -- and
    the price of that choice is that a gap is possible. This is the test that
    catches one: a choking rule listing ``child`` but not the infant bands would
    protect the four-year-old and not the baby.
    """
    if AgeBand.CHILD in rule.applies_to_bands:
        missing = set(INFANT_AGE_BANDS) - set(rule.applies_to_bands)
        assert not missing, f"{rule.rule_code} skips {sorted(band.value for band in missing)}"


def test_honey_is_forbidden_before_twelve_months_and_allowed_after() -> None:
    """The one threshold in this file worth naming in a test of its own.

    Infant botulism is the textbook example of a risk with a hard age boundary,
    and "no honey before one year" is the rule a French parent will check first.
    Getting the band list wrong here would be invisible in every other test.
    """
    honey = next(rule for rule in INFANT_RESTRICTIONS if rule.rule_code == "honey")

    assert AgeBand.INFANT_4_6M in honey.applies_to_bands
    assert AgeBand.INFANT_6_9M in honey.applies_to_bands
    assert AgeBand.INFANT_9_12M in honey.applies_to_bands
    # Twelve to thirty-six months is past the threshold; keeping honey banned
    # there would be this application inventing a rule ANSES does not state.
    assert AgeBand.INFANT_12_36M not in honey.applies_to_bands
    assert AgeBand.CHILD not in honey.applies_to_bands


@pytest.mark.parametrize("guideline", PNNS_GUIDELINES, ids=_GUIDELINE_IDS)
def test_every_benchmark_is_quantified_windowed_and_sourced(
    guideline: PnnsGuidelineSpec,
) -> None:
    """No benchmark without a number, a window and the page it was read from.

    The argument for frequency benchmarks over an opaque score is that the
    household can go and check them. That only holds while every row carries the
    URL it came from.
    """
    assert guideline.amount > 0
    assert guideline.window_days > 0
    assert guideline.statement.strip()
    assert guideline.source_url.startswith("https://www.mangerbouger.fr/")


def test_benchmarks_are_declared_once_per_marker() -> None:
    markers = [guideline.marker for guideline in PNNS_GUIDELINES]
    assert len(markers) == len(set(markers))


@pytest.mark.parametrize("guideline", SHELF_LIFE_GUIDELINES, ids=_SHELF_LIFE_IDS)
def test_every_shelf_life_row_suggests_something_and_cites_a_source(
    guideline: ShelfLifeSpec,
) -> None:
    assert guideline.unopened_days is not None or guideline.opened_days is not None
    assert guideline.source_url.startswith("https://")


def test_shelf_life_is_declared_once_per_family() -> None:
    families = [guideline.family for guideline in SHELF_LIFE_GUIDELINES]
    assert len(families) == len(set(families))


# --------------------------------------------------------------------------- #
# The tables agree with the module
# --------------------------------------------------------------------------- #


async def test_the_current_edition_is_seeded_and_marked_current(
    db_session: AsyncSession,
) -> None:
    reference = await db_session.get(NutritionReference, PNNS_2019.version)

    assert reference is not None
    assert reference.is_current is True
    assert reference.source_url == PNNS_2019.source_url


async def test_seeded_benchmarks_match_the_reviewed_module(
    db_session: AsyncSession,
) -> None:
    """Drift here means the rule in review is not the rule at runtime."""
    rows = (
        await db_session.scalars(
            sa.select(PnnsGuideline).where(PnnsGuideline.reference_version == PNNS_2019.version)
        )
    ).all()
    seeded = {
        row.marker: (row.direction, row.amount, row.unit, row.window_days, row.statement)
        for row in rows
    }
    expected = {
        guideline.marker: (
            guideline.direction,
            guideline.amount,
            guideline.unit,
            guideline.window_days,
            guideline.statement,
        )
        for guideline in PNNS_GUIDELINES
    }

    assert seeded == expected


async def test_seeded_infant_rules_match_the_reviewed_module(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.scalars(
            sa.select(InfantFoodRestriction).where(
                InfantFoodRestriction.reference_version == PNNS_2019.version
            )
        )
    ).all()
    seeded = {row.rule_code: (sorted(row.applies_to_bands), row.risk) for row in rows}
    expected = {
        rule.rule_code: (sorted(rule.applies_to_bands), rule.risk) for rule in INFANT_RESTRICTIONS
    }

    assert seeded == expected


async def test_seeded_shelf_lives_match_the_reviewed_module(
    db_session: AsyncSession,
) -> None:
    rows = (await db_session.scalars(sa.select(ShelfLifeGuideline))).all()
    seeded = {row.family: (row.unopened_days, row.opened_days, row.date_kind) for row in rows}
    expected = {
        guideline.family: (
            guideline.unopened_days,
            guideline.opened_days,
            guideline.date_kind,
        )
        for guideline in SHELF_LIFE_GUIDELINES
    }

    assert seeded == expected


async def test_a_benchmark_cannot_be_orphaned_from_its_edition(
    db_session: AsyncSession,
) -> None:
    """``ON DELETE RESTRICT``: a revision may be published, never made to vanish.

    A persisted suggestion carries the edition it was judged by. Deleting an
    edition out from under it would leave a shortfall referring to numbers
    nobody can look up -- history rewritten by a maintenance script.
    """
    with pytest.raises(sa.exc.IntegrityError, match="fk_pnns_guideline_reference_version"):
        await db_session.execute(
            sa.delete(NutritionReference).where(NutritionReference.version == PNNS_2019.version)
        )
