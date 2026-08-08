"""The screen and the resolver, asked the questions that decide whether they protect.

No database and no model: everything here is a total function over frozen data,
which is the point of putting the safety-critical half of ADR-0009 in
``domain/constraints.py``. A property proved against a mock proves the mock.

The file is organised as three questions:

* does an undocumented product survive an allergy? (it must not);
* can a specific ingredient resolve to a generic product? (it must not);
* does the infant table reach a product that carries no category? (it must).
"""

from __future__ import annotations

import uuid

import pytest

from chaudron.domain.constraints import (
    INFANT_BANDS,
    PANTRY_STAPLES,
    HouseholdConstraints,
    InfantRule,
    Person,
    ProductFacts,
    StockIndex,
    StockLine,
    WithholdReason,
    content_tokens,
    staples_allowed_for,
    union_of,
    withhold_reason,
)
from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    Diet,
    InfantRiskKind,
    InfantTexture,
    PnnsMarker,
)

# --------------------------------------------------------------------------- #
# Fixtures, written out rather than parametrised: each one is a claim
# --------------------------------------------------------------------------- #


def facts(
    name: str = "Produit",
    *,
    state: AllergenDataState = AllergenDataState.DECLARED,
    risk: frozenset[Allergen] = frozenset(),
    markers: frozenset[PnnsMarker] = frozenset(),
    categories: tuple[str, ...] = (),
) -> ProductFacts:
    return ProductFacts(
        product_id=uuid.uuid7(),
        name=name,
        allergen_state=state,
        allergens_risk=risk,
        pnns_markers=markers,
        category_tags=categories,
    )


def undocumented(name: str = "Farine de blé") -> ProductFacts:
    """What the database actually produces for a product nobody has documented.

    ``allergens_risk`` is a generated column carrying **all fourteen** in that
    state (``domain/models.py``), and reproducing that here rather than passing
    an empty set is the whole reason this helper exists: a fixture that made
    "unknown" look empty would let every test below pass for the wrong reason.
    """
    return facts(name, state=AllergenDataState.UNKNOWN, risk=frozenset(Allergen))


def line(product: ProductFacts) -> StockLine:
    return StockLine(
        inventory_item_id=uuid.uuid7(),
        product=product,
        quantity="1",
        unit="piece",
        expires_on=None,
    )


def person(
    *,
    band: AgeBand = AgeBand.ADULT,
    diet: Diet = Diet.OMNIVORE,
    allergens: frozenset[Allergen] = frozenset(),
    texture: InfantTexture | None = None,
    free_text: str | None = None,
) -> Person:
    return Person(
        id=uuid.uuid7(),
        display_name="Camille",
        age_band=band,
        diet=diet,
        allergens=allergens,
        infant_texture=texture,
        free_text_restrictions=free_text,
    )


def constraints(*people: Person) -> HouseholdConstraints:
    return union_of(people)


HONEY_RULE = InfantRule(
    rule_code="honey",
    label="Miel",
    risk=InfantRiskKind.MICROBIOLOGICAL,
    applies_to_bands=frozenset({AgeBand.INFANT_6_9M}),
    category_tags=frozenset({"en:honeys"}),
    name_patterns=("miel",),
    statement="Ne pas donner de miel avant 12 mois.",
    source_url="https://example.test/anses",
)


# --------------------------------------------------------------------------- #
# "Unknown" is not "free of"
# --------------------------------------------------------------------------- #


def test_an_undocumented_product_is_withheld_from_anybody_with_an_allergy() -> None:
    """The headline, at the layer a service actually calls.

    The database half of this property is proved in
    ``test_allergen_unknown_is_not_safe.py``; this is the same claim one level
    up, where a screen written against ``allergens_contains`` would let the
    product straight through.
    """
    reason = withhold_reason(
        undocumented(), constraints(person(allergens=frozenset({Allergen.PEANUTS}))), ()
    )

    assert reason is WithholdReason.ALLERGEN


def test_a_documented_product_that_declared_nothing_survives() -> None:
    """The contrast case, without which the test above passes for the wrong reason."""
    reason = withhold_reason(
        facts("Riz basmati"), constraints(person(allergens=frozenset({Allergen.PEANUTS}))), ()
    )

    assert reason is None


def test_an_undocumented_product_survives_when_nobody_declared_an_allergy() -> None:
    """Over-exclusion has a cost, and it is not paid by households with no allergy.

    Withholding every undocumented product from everybody would empty the
    average pantry -- Open Food Facts is a wiki and most fresh products carry no
    allergen data. The exclusion is triggered by a declared allergy, and by
    nothing else.
    """
    assert withhold_reason(undocumented(), constraints(person()), ()) is None


def test_a_trace_withholds_exactly_like_a_declaration() -> None:
    milk_traces = facts("Chocolat noir", risk=frozenset({Allergen.MILK}))

    reason = withhold_reason(
        milk_traces, constraints(person(allergens=frozenset({Allergen.MILK}))), ()
    )

    assert reason is WithholdReason.ALLERGEN


# --------------------------------------------------------------------------- #
# Diet
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("diet", "marker", "withheld"),
    [
        (Diet.VEGETARIAN, PnnsMarker.RED_MEAT, True),
        (Diet.VEGETARIAN, PnnsMarker.FISH, True),
        (Diet.VEGETARIAN, PnnsMarker.DAIRY, False),
        (Diet.VEGAN, PnnsMarker.DAIRY, True),
        (Diet.VEGAN, PnnsMarker.EGGS, True),
        (Diet.PESCATARIAN, PnnsMarker.FISH, False),
        (Diet.PESCATARIAN, PnnsMarker.POULTRY, True),
        (Diet.OMNIVORE, PnnsMarker.RED_MEAT, False),
    ],
)
def test_a_diet_withholds_on_positive_evidence(
    diet: Diet, marker: PnnsMarker, withheld: bool
) -> None:
    product = facts("Quelque chose", markers=frozenset({marker}))

    reason = withhold_reason(product, constraints(person(diet=diet)), ())

    assert (reason is WithholdReason.DIET) is withheld


def test_an_uncategorised_product_is_not_withheld_by_a_diet() -> None:
    """And the post-call check is what makes the diet a guarantee anyway.

    Unlike the allergen column, nothing in the schema makes an unresolved
    category maximally excluding, and treating it as such would withhold every
    hand-typed product from every vegetarian -- the most common constraint there
    is. The pre-filter acts on evidence; the guarantee is that an invented
    ingredient cannot resolve afterwards.
    """
    assert (
        withhold_reason(facts("Truc du marché"), constraints(person(diet=Diet.VEGAN)), ()) is None
    )


def test_the_strictest_diet_at_the_table_wins() -> None:
    union = constraints(person(diet=Diet.OMNIVORE), person(diet=Diet.VEGAN))

    assert union.diet is Diet.VEGAN
    assert PnnsMarker.DAIRY in union.excluded_markers


def test_the_allergies_of_everybody_at_the_table_are_unioned() -> None:
    union = constraints(
        person(allergens=frozenset({Allergen.NUTS})),
        person(allergens=frozenset({Allergen.CELERY})),
    )

    assert union.excluded_allergens == frozenset({Allergen.NUTS, Allergen.CELERY})


def test_the_youngest_texture_wins() -> None:
    union = constraints(
        person(band=AgeBand.INFANT_12_36M, texture=InfantTexture.PIECES),
        person(band=AgeBand.INFANT_6_9M, texture=InfantTexture.SMOOTH),
    )

    assert union.infant_texture is InfantTexture.SMOOTH
    assert union.has_infant


# --------------------------------------------------------------------------- #
# The infant table
# --------------------------------------------------------------------------- #


def test_an_infant_rule_matches_on_a_catalogue_category() -> None:
    honey = facts("Acacia", categories=("en:honeys",))

    reason = withhold_reason(honey, constraints(person(band=AgeBand.INFANT_6_9M)), (HONEY_RULE,))

    assert reason is WithholdReason.INFANT_RULE


def test_an_infant_rule_matches_a_hand_typed_product_with_no_category() -> None:
    """The half the catalogue cannot cover, and the reason the table has two.

    "Miel de châtaignier" typed in at the market carries no category tag at all.
    A rule that only read categories would let it through, and botulism is not a
    graceful degradation.
    """
    honey = facts("Miel de châtaignier")

    reason = withhold_reason(honey, constraints(person(band=AgeBand.INFANT_6_9M)), (HONEY_RULE,))

    assert reason is WithholdReason.INFANT_RULE


def test_a_pattern_does_not_fire_in_the_middle_of_a_word() -> None:
    """ "Miel" must not match "caramel". Word-start anchoring, not substring."""
    caramel = facts("Bonbons au caramel")

    reason = withhold_reason(caramel, constraints(person(band=AgeBand.INFANT_6_9M)), (HONEY_RULE,))

    assert reason is None


def test_rules_narrowed_to_other_bands_do_not_reach_the_table() -> None:
    """A household of adults is not served the infant table.

    The narrowing happens in the query (``services/dietary.infant_rules``); this
    asserts the screen does not re-apply what it was not given, so the two halves
    cannot both assume the other did it.
    """
    honey = facts("Miel de châtaignier")

    assert withhold_reason(honey, constraints(person()), ()) is None


def test_every_infant_band_is_a_band_the_texture_check_recognises() -> None:
    """The two lists that must not drift: bands with a texture, and infant bands."""
    assert set(AgeBand) >= INFANT_BANDS
    assert AgeBand.ADULT not in INFANT_BANDS
    assert AgeBand.CHILD not in INFANT_BANDS


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #


def index(*names: str) -> StockIndex:
    return StockIndex([line(facts(name)) for name in names], PANTRY_STAPLES)


def test_a_generic_ingredient_resolves_to_a_specific_product() -> None:
    """ "Crème" against "Crème fraîche épaisse". Safe: the line was screened."""
    resolution = index("Crème fraîche épaisse").resolve("Crème")

    assert resolution.resolved
    assert resolution.line is not None
    assert resolution.line.product.name == "Crème fraîche épaisse"


def test_a_specific_ingredient_does_not_resolve_to_a_generic_product() -> None:
    """The direction that matters, and the one a substring matcher gets wrong.

    "Beurre de cacahuète" contains "beurre"; a containment test therefore
    certifies a peanut butter as butter, in a household that excluded peanuts.
    Every content token of the ingredient has to be in the product name, and
    "cacahuete" is not.
    """
    resolution = index("Beurre doux").resolve("Beurre de cacahuète")

    assert not resolution.resolved


def test_an_ingredient_the_household_does_not_own_does_not_resolve() -> None:
    assert not index("Crème fraîche épaisse").resolve("Courgettes").resolved


def test_stopwords_and_quantities_do_not_defeat_a_match() -> None:
    assert index("Pommes de terre").resolve("200 g de pommes de terre").resolved


def test_a_staple_resolves_on_its_exact_token_set() -> None:
    resolution = index().resolve("huile d'olive")

    assert resolution.staple is not None
    assert resolution.line is None


def test_an_ambiguous_staple_label_does_not_resolve() -> None:
    """Bare "huile" is refused, and the refusal is the point.

    Peanut oil and sesame oil are ordinary French pantry items and both are on
    the regulated list. A staple entry is matched on its whole token set, so
    "huile" matches nothing and the suggestion is discarded rather than served to
    somebody allergic to peanuts.
    """
    assert not index().resolve("huile").resolved


def test_salt_is_withdrawn_from_the_staples_when_a_toddler_eats() -> None:
    """The ANSES "no added salt before three" rule, applied to the escape hatch.

    The staple list is the one way an ingredient may resolve to something outside
    the inventory; it would be a hole in the screen if the rules stopped at the
    inventory's edge.
    """
    salt_rule = InfantRule(
        rule_code="added_salt",
        label="Sel ajouté",
        risk=InfantRiskKind.NUTRITIONAL,
        applies_to_bands=frozenset({AgeBand.INFANT_12_36M}),
        category_tags=frozenset({"en:salts"}),
        name_patterns=("chips",),
        statement="Ne pas ajouter de sel avant 3 ans.",
        source_url="https://example.test/anses",
    )
    toddler = constraints(person(band=AgeBand.INFANT_12_36M, texture=InfantTexture.SOFT_PIECES))

    allowed = staples_allowed_for(toddler, (salt_rule,))

    labels = {staple.label for staple in allowed}
    assert "sel" not in labels
    assert "eau" in labels


def test_every_staple_declares_what_it_carries() -> None:
    """The closed list has no permissive default; a seventh entry must say so."""
    for staple in PANTRY_STAPLES:
        assert staple.tokens, f"{staple.label} tokenises to nothing and would match everything"
        assert staple.tokens == content_tokens(staple.label)
        assert isinstance(staple.allergens, frozenset)


def test_an_empty_label_resolves_to_nothing_rather_than_to_everything() -> None:
    """A label of punctuation tokenises to the empty set, which is a subset of
    every product name. Returning a match there would let "..." resolve to the
    first lot in the fridge."""
    assert not index("Crème fraîche épaisse").resolve("  --  ").resolved


def test_free_text_travels_as_a_preference_and_never_as_a_filter() -> None:
    """Contract 4bis, third class. There is nowhere else for it to go.

    ``HouseholdConstraints`` has no field a screen reads it from, which is the
    enforcement: the free text cannot become a filter because no filter can see
    it.
    """
    union = constraints(person(free_text="pas de coriandre"))

    assert union.preferences == ("pas de coriandre",)
    assert withhold_reason(facts("Coriandre fraîche"), union, ()) is None
