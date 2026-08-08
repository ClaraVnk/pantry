"""The balance arithmetic, and the four ways it is tempting to get it wrong.

Every test here is about a benchmark this application must *not* invent:

* a ceiling read as a floor ("sufficient but limited" becoming "eat more");
* a marker with no published figure producing one anyway;
* a daily benchmark compared against a weekly count;
* an unresolved product silently counting as zero rather than as unknown.

The reference rows themselves are checked against the published source in
``test_reference_tables.py``; this file only exercises the arithmetic over them.
"""

from __future__ import annotations

from decimal import Decimal

from chaudron.domain.balance import (
    WINDOW_DAYS,
    Guideline,
    Observation,
    evaluate,
    shortfall_sentence,
)
from chaudron.domain.dietary import PNNS_GUIDELINES
from chaudron.domain.models import PnnsDirection, PnnsMarker, PnnsUnit

FISH = Guideline(
    marker=PnnsMarker.FISH,
    label="Poisson",
    direction=PnnsDirection.AT_LEAST,
    amount=Decimal(2),
    unit=PnnsUnit.SERVING,
    window_days=7,
    statement="Poisson 2 fois par semaine, dont un poisson gras",
    source_url="https://example.test/poisson",
)

RED_MEAT = Guideline(
    marker=PnnsMarker.RED_MEAT,
    label="Viande hors volaille",
    direction=PnnsDirection.AT_MOST,
    amount=Decimal(500),
    unit=PnnsUnit.GRAM,
    window_days=7,
    statement="Limiter les viandes autres que la volaille à 500 g par semaine",
    source_url="https://example.test/viande",
)

DAIRY = Guideline(
    marker=PnnsMarker.DAIRY,
    label="Produits laitiers",
    direction=PnnsDirection.AROUND,
    amount=Decimal(2),
    unit=PnnsUnit.SERVING,
    window_days=1,
    statement="2 produits laitiers par jour pour les adultes",
    source_url="https://example.test/laitiers",
)


def test_a_floor_that_is_not_met_becomes_a_gap_in_words() -> None:
    result = evaluate(
        (FISH,),
        {},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset({PnnsMarker.FISH}),
    )

    (gap,) = result.gaps
    assert gap.marker is PnnsMarker.FISH
    assert (gap.observed, gap.shortfall) == (0, 2)
    assert gap.target == "2 par semaine"
    # The wording and the URL travel with the figure, so a household can open the
    # page this application is quoting rather than take its word for it.
    assert gap.source_url == "https://example.test/poisson"
    assert shortfall_sentence(result.gaps) == "il manque deux portions de poisson"


def test_a_ceiling_that_is_exceeded_becomes_an_excess_in_grams() -> None:
    result = evaluate(
        (RED_MEAT,),
        {PnnsMarker.RED_MEAT: Observation(servings=3, grams=Decimal(780))},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset(),
    )

    (excess,) = result.excesses
    assert excess.observed_grams == 780
    assert excess.target == "500 g par semaine"
    assert not result.gaps


def test_dairy_is_a_ceiling_and_never_a_floor() -> None:
    """ "Suffisante mais limitée" is the source's own wording (ADR-0009).

    Read as a floor, this benchmark would have the application urge more cheese
    onto somebody already above the mark -- advice the page it cites does not
    give.
    """
    short = evaluate(
        (DAIRY,),
        {PnnsMarker.DAIRY: Observation(servings=1)},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset({PnnsMarker.DAIRY}),
    )
    over = evaluate(
        (DAIRY,),
        {PnnsMarker.DAIRY: Observation(servings=30)},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset(),
    )

    assert short.gaps == ()
    assert [excess.marker for excess in over.excesses] == [PnnsMarker.DAIRY]


def test_a_daily_benchmark_is_rescaled_to_the_window() -> None:
    """Two a day is fourteen a week, and the target string says so.

    Comparing a seven-day count against a one-day figure is the arithmetic slip
    that would tell a household eating exactly right that it is six portions
    over.
    """
    over = evaluate(
        (DAIRY,),
        {PnnsMarker.DAIRY: Observation(servings=15)},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset(),
    )

    (excess,) = over.excesses
    assert excess.target == "14 par semaine"


def test_a_marker_with_no_published_figure_produces_nothing() -> None:
    """Added fats and sugary foods carry no number in any official source.

    They resolve to a marker so a bar of chocolate is not counted as
    unidentified, and they generate no advice at all. Inventing a threshold to
    fill the table is the failure this whole module is written against.
    """
    markers = {guideline.marker for guideline in PNNS_GUIDELINES}

    assert PnnsMarker.ADDED_FATS not in markers
    assert PnnsMarker.SUGARY_FOODS not in markers
    assert PnnsMarker.STARCHY_FOODS not in markers


def test_the_uncategorised_count_is_carried_even_at_zero() -> None:
    """Absent and zero say the same thing to a naive client. One is a lie."""
    empty = evaluate(
        (),
        {},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset(),
    )

    assert empty.uncategorised_product_count == 0
    assert empty.gaps == () and empty.excesses == ()


def test_a_gap_nothing_in_stock_can_fill_is_reported_as_unsatisfiable() -> None:
    """The difference between an explanation and a reproach.

    "You are one fish short" next to an empty freezer is a scolding; the same
    sentence with ``satisfiable_from_stock: false`` is a statement of fact the
    household can act on -- or ignore.
    """
    result = evaluate(
        (FISH,),
        {},
        reference="pnns-2019",
        uncategorised_product_count=0,
        markers_in_stock=frozenset({PnnsMarker.DAIRY}),
    )

    assert result.gaps
    assert result.satisfiable_from_stock is False


def test_the_window_is_seven_days() -> None:
    """The rolling window every figure is expressed over, and the one the
    ``target`` strings above are rescaled to."""
    assert WINDOW_DAYS == 7
