"""What the Open Food Facts adapter makes of the taxonomy fields.

The payload shapes below are not invented: they are the ones observed on the
live API on 2026-08-04 while verifying the tag mapping, including the awkward
ones -- a product that returns nothing but its barcode, a product whose fields
are present and empty, and a product carrying a French-prefixed tag alongside
canonical ones.

The point of testing the adapter separately from
``tests/domain/test_allergen_unknown_is_not_safe.py`` is that this is where the
*wiring* can be wrong while every domain function stays correct: reading
``allergens`` instead of ``allergens_tags``, sanitising a tag until it stops
matching, or taking the first category tag where the most specific one is last.
"""

from __future__ import annotations

from typing import Any

from chaudron.domain.models import Allergen, AllergenDataState, FoodFamily, PnnsMarker
from chaudron.infra.openfoodfacts import _to_record

_TRUSTED = "openfoodfacts.org"


def _record(product: dict[str, Any]) -> Any:
    return _to_record("00003017620422", product, trusted_suffix=_TRUSTED)


def test_a_bare_payload_yields_no_allergen_claim() -> None:
    """Some products come back with nothing but their code. That is not a clean bill."""
    record = _record({})

    assert record.allergen_state is AllergenDataState.UNKNOWN
    assert record.allergens_contains == ()
    assert record.pnns_markers == ()
    assert record.food_family is None


def test_a_documented_product_carries_its_declarations() -> None:
    """Nutella, verbatim from the v3 API."""
    record = _record(
        {
            "product_name_fr": "Nutella",
            "allergens_tags": ["en:milk", "en:nuts", "en:soybeans"],
            "traces_tags": [],
            "categories_tags": [
                "en:breakfasts",
                "en:spreads",
                "en:sweet-spreads",
                "en:hazelnut-spreads",
            ],
            "food_groups_tags": ["en:sugary-snacks", "en:chocolate-products"],
        }
    )

    assert record.allergen_state is AllergenDataState.DECLARED
    assert set(record.allergens_contains) == {Allergen.MILK, Allergen.NUTS, Allergen.SOYBEANS}
    assert record.allergens_may_contain == ()
    assert PnnsMarker.SUGARY_FOODS in record.pnns_markers


def test_a_language_prefixed_tag_withdraws_the_claim() -> None:
    """``fr:sarrasin`` is a tag the taxonomy did not resolve, so neither do we.

    The record keeps what it could read -- gluten is still reported -- but its
    state drops back to ``UNKNOWN``, because a list containing one line we could
    not parse does not support the sentence "nothing else was declared".
    """
    record = _record({"allergens_tags": ["en:gluten", "fr:sarrasin"]})

    assert record.allergen_state is AllergenDataState.UNKNOWN
    assert Allergen.GLUTEN in record.allergens_contains


def test_traces_survive_the_projection() -> None:
    record = _record({"allergens_tags": ["en:gluten"], "traces_tags": ["en:nuts"]})

    assert record.allergens_contains == (Allergen.GLUTEN,)
    assert record.allergens_may_contain == (Allergen.NUTS,)


def test_a_fatty_fish_is_also_a_fish() -> None:
    """Two benchmarks, one product, and the leaf tag does not imply the parent.

    "Two fish a week, one of them oily" needs the salmon to count towards both,
    and Open Food Facts marks it only as ``en:fatty-fish``.
    """
    record = _record(
        {
            "categories_tags": ["en:seafood", "en:fishes", "en:salmons"],
            "food_groups_tags": ["en:fish-meat-eggs", "en:fatty-fish"],
        }
    )

    assert PnnsMarker.FISH in record.pnns_markers
    assert PnnsMarker.OILY_FISH in record.pnns_markers


def test_red_meat_comes_from_the_categories_not_the_food_groups() -> None:
    """There is no ``en:red-meat`` food group; the 500 g benchmark needs one anyway.

    This test is the reason the category ruleset exists. If somebody later
    "simplifies" the resolver down to ``food_groups_tags``, the red-meat ceiling
    stops counting anything at all and the balance silently reports zero.
    """
    record = _record(
        {
            "categories_tags": ["en:meats", "en:beef", "en:minced-beef"],
            "food_groups_tags": ["en:fish-meat-eggs", "en:meat-other-than-poultry"],
        }
    )

    assert PnnsMarker.RED_MEAT in record.pnns_markers


def test_whole_grains_come_from_the_categories_too() -> None:
    """The whole-grain food-group branch exists and is assigned by no category."""
    record = _record(
        {
            "categories_tags": ["en:cereals-and-potatoes", "en:breads", "en:wholemeal-breads"],
            "food_groups_tags": ["en:cereals-and-potatoes", "en:bread"],
        }
    )

    assert PnnsMarker.WHOLE_GRAINS in record.pnns_markers
    assert PnnsMarker.STARCHY_FOODS in record.pnns_markers


def test_an_uncategorised_product_resolves_to_nothing_rather_than_to_a_guess() -> None:
    """Empty markers and no family: the two "we do not know" answers, kept apart
    from every "we know and it is none of them"."""
    record = _record({"product_name_fr": "Bocal sans étiquette"})

    assert record.pnns_markers == ()
    assert record.food_family is None


def test_a_keeping_family_is_resolved_from_the_most_specific_category() -> None:
    """``categories_tags`` runs general to specific, so the answer is at the end."""
    record = _record({"categories_tags": ["en:dairies", "en:fermented-foods", "en:yogurts"]})

    assert record.food_family is FoodFamily.FRESH_DAIRY
