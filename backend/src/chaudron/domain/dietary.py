"""Dietary vocabularies, catalogue-tag mappings, and the two safety reference tables.

This module is pure: constants, dataclasses and total functions over them. It
knows nothing about a session, a request or a household. That is what lets the
infant restrictions and the allergen normalisation be tested exhaustively for
the price of a list comprehension, and what keeps the one piece of the product
with physical consequences out of reach of a mock.

Three things live here and nowhere else.

*The mapping from Open Food Facts tags to the fourteen regulated allergens.*
Verified against the live taxonomy rather than copied from the API contract --
see :data:`_ALLERGEN_BY_TAG` for what that verification actually turned up.

*The PNNS benchmarks and the infant restrictions*, as data. They are seeded into
reference tables by an Alembic migration, and the tables are the runtime source
of truth; the literals below are where the migration reads them from, so that
adding a rule is a diff a nutritionist could review rather than a hand-written
``INSERT``.

*The rules that turn a catalogue record into markers.* Deliberately here and not
in the infrastructure adapter: the same resolution has to run over a product
that arrived from a receipt or was typed in by hand, and a second implementation
of "is this a fish" would disagree with the first within a month.

Everything user-facing in the data below is French, because it is displayed
verbatim to a French-speaking household and quoting an official benchmark in
translation makes it uncheckable against the source cited next to it.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    InfantRiskKind,
    PnnsDirection,
    PnnsMarker,
    PnnsUnit,
)

__all__ = [
    "ALLERGEN_TAGS",
    "INFANT_AGE_BANDS",
    "INFANT_RESTRICTIONS",
    "PNNS_2019",
    "PNNS_GUIDELINES",
    "AllergenAssessment",
    "AllergenPresence",
    "InfantRestrictionSpec",
    "NutritionReferenceSpec",
    "PnnsGuidelineSpec",
    "allergen_presence",
    "assess_allergens",
    "markers_for_categories",
]


# --------------------------------------------------------------------------- #
# The tri-state
# --------------------------------------------------------------------------- #


class AllergenPresence(enum.StrEnum):
    """State of one allergen on one product. Three values, and never two.

    Not a database column: it is *read* from
    ``(allergen_state, allergens_contains, allergens_may_contain)`` by
    :func:`allergen_presence`, and storing it per product per allergen would be
    a fourteen-row join whose rows could contradict the arrays they came from.

    ``UNKNOWN`` is the default and means **no data**. It does not mean "free of".
    For filtering it is treated exactly as ``MAY_CONTAIN``; for display it is
    "not verified", never a tick. The interface may state that no allergen was
    *declared* among the identified products, alongside the count of products
    with no data -- never that a recipe is free of anything (ADR-0009).
    """

    CONTAINS = "contains"
    MAY_CONTAIN = "may_contain"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Open Food Facts allergen tags
# --------------------------------------------------------------------------- #
#
# Checked against the live taxonomy on 2026-08-04:
#   https://static.openfoodfacts.org/data/taxonomies/allergens.json
#   https://github.com/openfoodfacts/openfoodfacts-server/blob/main/taxonomies/allergens.txt
#
# Four findings that the API contract's starting table could not have told us,
# and that change the code:
#
# 1. **The taxonomy is flat.** Not one entry has a parent or a child, and
#    `allergens_hierarchy` is byte-identical to `allergens_tags` on every product
#    inspected. Almonds, hazelnuts, walnuts, cashews and pistachios are declared
#    as *synonyms* of `en:nuts`, not as children of it -- Nutella comes back as
#    `["en:milk","en:nuts","en:soybeans"]`. So there is no subtree to walk, and
#    equally no way to tell which tree nut a product actually contains.
# 2. The contract's four riskiest guesses are all correct: `en:gluten` (not
#    `en:cereals-containing-gluten`), `en:sesame-seeds`,
#    `en:sulphur-dioxide-and-sulphites` and `en:soybeans`.
# 3. **Tags are not always canonical.** Open Food Facts prefixes any value it
#    cannot resolve with the contributor's own language -- over 1400 products
#    carry `fr:avoine`, which is oats, which is a gluten cereal. The same product
#    can carry `fr:Avoine` and `fr:avoine` as two separate entries, so comparison
#    must be case-folded. This is why an unrecognised tag is not ignored below.
# 4. The taxonomy also holds thirteen non-EU allergens (`en:pork`, `en:kiwi`,
#    `en:gelatin`...) for other jurisdictions. Those are *understood* and simply
#    out of scope -- unlike `fr:avoine`, they say nothing about the fourteen.

#: The canonical tag Open Food Facts publishes for each regulated allergen.
ALLERGEN_TAGS: Final[Mapping[Allergen, str]] = {
    Allergen.GLUTEN: "en:gluten",
    Allergen.CRUSTACEANS: "en:crustaceans",
    Allergen.EGGS: "en:eggs",
    Allergen.FISH: "en:fish",
    Allergen.PEANUTS: "en:peanuts",
    Allergen.SOYBEANS: "en:soybeans",
    Allergen.MILK: "en:milk",
    Allergen.NUTS: "en:nuts",
    Allergen.CELERY: "en:celery",
    Allergen.MUSTARD: "en:mustard",
    Allergen.SESAME: "en:sesame-seeds",
    Allergen.SULPHITES: "en:sulphur-dioxide-and-sulphites",
    Allergen.LUPIN: "en:lupin",
    Allergen.MOLLUSCS: "en:molluscs",
}

#: Reverse index, plus the language-prefixed forms seen often enough in real
#: data to be worth resolving rather than degrading. Each of these is a cereal
#: containing gluten written in French by a contributor whose entry the taxonomy
#: did not recognise; leaving them unmapped would cost accuracy for no safety,
#: since they resolve to an allergen the same product almost always also
#: declares canonically.
_ALLERGEN_BY_TAG: Final[Mapping[str, Allergen]] = {
    **{tag: allergen for allergen, tag in ALLERGEN_TAGS.items()},
    "fr:avoine": Allergen.GLUTEN,
    "fr:ble": Allergen.GLUTEN,
    "fr:blé": Allergen.GLUTEN,
    "fr:orge": Allergen.GLUTEN,
    "fr:seigle": Allergen.GLUTEN,
    "fr:epeautre": Allergen.GLUTEN,
    "fr:épeautre": Allergen.GLUTEN,
}

#: Canonical taxonomy entries that are not one of the fourteen. They are
#: recognised vocabulary -- other jurisdictions require them -- so meeting one
#: does not mean the record is unreadable. Kept explicit rather than inferred
#: from an ``en:`` prefix, because Open Food Facts also emits non-canonical
#: values that begin with ``en:`` (free text a contributor typed in English).
_OUT_OF_SCOPE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "en:none",
        "en:apple",
        "en:banana",
        "en:beef",
        "en:chicken",
        "en:gelatin",
        "en:kiwi",
        "en:matsutake",
        "en:orange",
        "en:peach",
        "en:pork",
        "en:red-caviar",
        "en:yamaimo",
    }
)


@dataclass(frozen=True, slots=True)
class AllergenAssessment:
    """What an upstream record says about the fourteen allergens.

    ``state`` is the whole point. ``DECLARED`` licenses the statement "none of
    the fourteen was declared on this product"; ``UNKNOWN`` licenses nothing at
    all, and the product is withheld from anybody with a declared allergy.
    """

    state: AllergenDataState
    contains: tuple[Allergen, ...] = ()
    may_contain: tuple[Allergen, ...] = ()
    #: Tags we could not resolve, kept for observability. Their presence is what
    #: forced ``state`` back to ``UNKNOWN``; a rising count here means the
    #: mapping above has fallen behind the wiki and should be revisited.
    unreadable_tags: tuple[str, ...] = ()


def _normalise(tags: object) -> list[str] | None:
    """Case-folded tag list, or ``None`` when the field carries no information.

    Open Food Facts has three ways of saying "no data" for the same field: the
    key is absent, the key holds ``[]``, or -- for the PNNS group fields -- it
    holds the string ``"unknown"``. Absent and empty are not distinguishable in
    any useful way here (a product last saved before the field existed simply
    lacks it), so both become ``None`` and neither becomes "declared nothing".
    """
    if not isinstance(tags, list):
        return None
    cleaned = [tag.strip().lower() for tag in tags if isinstance(tag, str) and tag.strip()]
    return cleaned or None


def assess_allergens(payload: Mapping[str, object]) -> AllergenAssessment:
    """Read ``allergens_tags`` and ``traces_tags`` off an Open Food Facts product.

    Two rules, both of which fail towards withholding food rather than serving
    it.

    *No fields means no data.* A product whose record carries neither list comes
    back ``UNKNOWN`` -- never as an empty declaration, which downstream would
    read as "contains nothing".

    *A list we cannot fully read is not a declaration.* One unresolved tag sends
    the whole assessment back to ``UNKNOWN``, keeping whatever was resolved in
    the lists for display. The alternative is to silently drop the tag, and the
    tag we would be dropping is `fr:avoine` -- oats, a gluten cereal, on
    thousands of products. Certifying "no gluten declared" from a list one line
    of which we did not understand is the failure this whole mechanism exists to
    prevent.
    """
    declared = _normalise(payload.get("allergens_tags"))
    traces = _normalise(payload.get("traces_tags"))
    if declared is None and traces is None:
        return AllergenAssessment(state=AllergenDataState.UNKNOWN)

    contains: set[Allergen] = set()
    may_contain: set[Allergen] = set()
    unreadable: set[str] = set()

    for tag in declared or ():
        if (allergen := _ALLERGEN_BY_TAG.get(tag)) is not None:
            contains.add(allergen)
        elif tag not in _OUT_OF_SCOPE_TAGS:
            unreadable.add(tag)
    for tag in traces or ():
        if (allergen := _ALLERGEN_BY_TAG.get(tag)) is not None:
            may_contain.add(allergen)
        elif tag not in _OUT_OF_SCOPE_TAGS:
            unreadable.add(tag)

    # A declared presence outranks a trace: "contains milk" and "may contain
    # traces of milk" on the same product is one fact stated twice, and the
    # schema refuses to hold both (ck_product_allergen_states_disjoint).
    may_contain -= contains

    state = AllergenDataState.UNKNOWN if unreadable else AllergenDataState.DECLARED
    return AllergenAssessment(
        state=state,
        contains=tuple(sorted(contains)),
        may_contain=tuple(sorted(may_contain)),
        unreadable_tags=tuple(sorted(unreadable)),
    )


def allergen_presence(
    state: AllergenDataState,
    contains: Iterable[Allergen],
    may_contain: Iterable[Allergen],
    allergen: Allergen,
) -> AllergenPresence | None:
    """The state of one allergen on one product. ``None`` means "declared absent".

    Four answers from three enum members, and the asymmetry is deliberate. The
    three members are the three *risks*; ``None`` is the only answer that is not
    a risk, and it is reachable only from a record that actually declared
    something. So a caller writing ``if allergen_presence(...) is not None:
    withhold`` gets the correct behaviour for a product nobody has documented,
    without having had to think about it.

    The order of the tests is the rest of the property: ``UNKNOWN`` is checked
    **first**, before either list is consulted. A version that looked at the
    lists first would answer "in neither list" for a product with no data at
    all, which is the sentence this function exists to make unsayable.

    Even ``None`` licenses only "not declared among the products identified" --
    never "free of". Nothing in a wiki record is a promise (ADR-0009).
    """
    if state is AllergenDataState.UNKNOWN:
        return AllergenPresence.UNKNOWN
    if allergen in contains:
        return AllergenPresence.CONTAINS
    if allergen in may_contain:
        return AllergenPresence.MAY_CONTAIN
    return None


# --------------------------------------------------------------------------- #
# Food-group markers from catalogue categories
# --------------------------------------------------------------------------- #
#
# Two sources, in this order of preference, both verified on 2026-08-04:
#
# * ``food_groups_tags`` -- Open Food Facts' own PNNS-derived classification
#   (https://static.openfoodfacts.org/data/taxonomies/food_groups.json). Reliable
#   where it is populated.
# * ``categories_tags`` -- the raw taxonomy, needed for the two markers
#   ``food_groups_tags`` cannot supply. There is no ``en:red-meat`` food group at
#   all, and the whole-grain branches of the food-group taxonomy
#   (``en:whole-grain-bread`` and friends) exist but are assigned by **no**
#   category, so they never appear on a product. Both had to be built by hand
#   from category tags.

_FOOD_GROUP_MARKERS: Final[Mapping[str, PnnsMarker]] = {
    "en:fruits-and-vegetables": PnnsMarker.FRUITS_VEGETABLES,
    "en:fruits": PnnsMarker.FRUITS_VEGETABLES,
    "en:dried-fruits": PnnsMarker.FRUITS_VEGETABLES,
    "en:vegetables": PnnsMarker.FRUITS_VEGETABLES,
    "en:soups": PnnsMarker.FRUITS_VEGETABLES,
    "en:legumes": PnnsMarker.LEGUMES,
    "en:cereals": PnnsMarker.STARCHY_FOODS,
    "en:bread": PnnsMarker.STARCHY_FOODS,
    "en:potatoes": PnnsMarker.STARCHY_FOODS,
    "en:breakfast-cereals": PnnsMarker.STARCHY_FOODS,
    "en:cereals-and-potatoes": PnnsMarker.STARCHY_FOODS,
    "en:nuts": PnnsMarker.NUTS,
    "en:milk-and-dairy-products": PnnsMarker.DAIRY,
    "en:milk-and-yogurt": PnnsMarker.DAIRY,
    "en:cheese": PnnsMarker.DAIRY,
    "en:dairy-desserts": PnnsMarker.DAIRY,
    "en:ice-cream": PnnsMarker.DAIRY,
    "en:fish-and-seafood": PnnsMarker.FISH,
    "en:lean-fish": PnnsMarker.FISH,
    "en:fatty-fish": PnnsMarker.OILY_FISH,
    "en:processed-meat": PnnsMarker.PROCESSED_MEAT,
    "en:offals": PnnsMarker.RED_MEAT,
    "en:poultry": PnnsMarker.POULTRY,
    "en:eggs": PnnsMarker.EGGS,
    "en:fats": PnnsMarker.ADDED_FATS,
    "en:vegetable-margarines": PnnsMarker.ADDED_FATS,
    "en:dressings-and-sauces": PnnsMarker.ADDED_FATS,
    "en:chocolate-products": PnnsMarker.SUGARY_FOODS,
    "en:sweets": PnnsMarker.SUGARY_FOODS,
    "en:biscuits-and-cakes": PnnsMarker.SUGARY_FOODS,
    "en:pastries": PnnsMarker.SUGARY_FOODS,
    "en:sweetened-beverages": PnnsMarker.SUGARY_DRINKS,
    "en:artificially-sweetened-beverages": PnnsMarker.SUGARY_DRINKS,
    "en:fruit-nectars": PnnsMarker.SUGARY_DRINKS,
    "en:fruit-juices": PnnsMarker.SUGARY_DRINKS,
    "en:unsweetened-beverages": PnnsMarker.UNSWEETENED_DRINKS,
    "en:waters-and-flavored-waters": PnnsMarker.UNSWEETENED_DRINKS,
    "en:teas-and-herbal-teas-and-coffees": PnnsMarker.UNSWEETENED_DRINKS,
}

#: ``en:meat-other-than-poultry`` is the closest food group to the PNNS
#: "viande hors volaille" ceiling, but it also holds rabbit and game. Category
#: tags are what actually name the four meats the benchmark lists -- pork, beef,
#: veal, mutton and lamb -- plus offal.
_RED_MEAT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "en:meats",
        "en:beef",
        "en:beef-meat",
        "en:pork",
        "en:pork-meat",
        "en:veal",
        "en:veal-meat",
        "en:lamb",
        "en:lamb-meat",
        "en:mutton",
        "en:mutton-meat",
        "en:horse-meat",
        "en:offals",
        "en:minced-beef",
        "en:ground-beef",
    }
)

#: Whole grains have to come from categories: the food-group taxonomy defines
#: the branch and no category ever assigns it, so it is dead on every product.
_WHOLE_GRAIN_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "en:wholemeal-breads",
        "en:whole-grain-breads",
        "en:wholemeal-pastas",
        "en:whole-grain-pastas",
        "en:whole-grain-rices",
        "en:brown-rices",
        "en:wholemeal-flours",
        "en:whole-grain-flours",
        "en:bulgur",
        "en:quinoa",
        "en:spelt",
        "en:oat-flakes",
        "en:rolled-oats",
    }
)

_CATEGORY_MARKERS: Final[Mapping[str, PnnsMarker]] = {
    **dict.fromkeys(_RED_MEAT_CATEGORIES, PnnsMarker.RED_MEAT),
    **dict.fromkeys(_WHOLE_GRAIN_CATEGORIES, PnnsMarker.WHOLE_GRAINS),
}


def markers_for_categories(
    category_tags: object, food_group_tags: object = None
) -> tuple[PnnsMarker, ...]:
    """Resolve a product's food-group markers. Empty means *unresolved*.

    Empty is a load-bearing answer, not a fallback. A product whose categories
    resolve to nothing contributes to no marker and is counted separately, so a
    badly catalogued pantry shows up as missing evidence rather than as a
    confident "you are one fish short this week" that the household cannot argue
    with (ADR-0009).
    """
    markers: set[PnnsMarker] = set()
    for tag in _normalise(food_group_tags) or ():
        if (marker := _FOOD_GROUP_MARKERS.get(tag)) is not None:
            markers.add(marker)
    for tag in _normalise(category_tags) or ():
        if (marker := _CATEGORY_MARKERS.get(tag)) is not None:
            markers.add(marker)
    # A fatty fish is a fish. The parent is not implied by the taxonomy -- the
    # food-group tree marks `en:fatty-fish` under `en:fish-and-seafood`, but a
    # product may carry only the leaf.
    if PnnsMarker.OILY_FISH in markers:
        markers.add(PnnsMarker.FISH)
    return tuple(sorted(markers))


# --------------------------------------------------------------------------- #
# Reference editions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NutritionReferenceSpec:
    version: str
    label: str
    published_on: date
    source_url: str
    is_current: bool = False


@dataclass(frozen=True, slots=True)
class PnnsGuidelineSpec:
    marker: PnnsMarker
    label: str
    direction: PnnsDirection
    amount: Decimal
    unit: PnnsUnit
    window_days: int
    statement: str
    source_url: str
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class InfantRestrictionSpec:
    rule_code: str
    label: str
    applies_to_bands: tuple[AgeBand, ...]
    risk: InfantRiskKind
    statement: str
    source_url: str
    category_tags: tuple[str, ...] = ()
    name_patterns: tuple[str, ...] = ()
    sort_order: int = 0


#: The edition this application quotes. Everything below carries its URL, so a
#: household can open the page and check what it is being told.
PNNS_2019: Final = NutritionReferenceSpec(
    version="pnns-2019",
    label="Repères alimentaires du PNNS (Santé publique France, 2019)",
    published_on=date(2019, 1, 22),
    source_url=(
        "https://www.mangerbouger.fr/l-essentiel/"
        "les-recommandations-sur-l-alimentation-l-activite-physique-et-la-sedentarite/"
    ),
    is_current=True,
)

_MB: Final = (
    "https://www.mangerbouger.fr/l-essentiel/"
    "les-recommandations-sur-l-alimentation-l-activite-physique-et-la-sedentarite/"
)

#: Ten benchmarks, verified page by page on mangerbouger.fr on 2026-08-04. The
#: statements are quoted, not paraphrased: a benchmark reworded by us stops
#: being checkable against the URL beside it.
#:
#: Four markers of :class:`PnnsMarker` deliberately have no row. Added fats,
#: sugary foods, starchy foods, poultry, eggs and unsweetened drinks are all
#: covered by official advice that carries **no figure** -- "limit", "favour",
#: "in small quantities". Inventing a number to make the table look complete is
#: the failure mode this file is written against.
PNNS_GUIDELINES: Final[Sequence[PnnsGuidelineSpec]] = (
    PnnsGuidelineSpec(
        marker=PnnsMarker.FRUITS_VEGETABLES,
        label="Fruits et légumes",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("5"),
        unit=PnnsUnit.SERVING,
        window_days=1,
        statement="Au moins 5 fruits et légumes par jour",
        source_url=f"{_MB}augmenter/augmenter-les-fruits-et-legumes",
        sort_order=10,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.LEGUMES,
        label="Légumes secs",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("2"),
        unit=PnnsUnit.SERVING,
        window_days=7,
        statement="Consommer des légumes secs au moins 2 fois par semaine",
        source_url=f"{_MB}augmenter/augmenter-les-legumes-secs",
        sort_order=20,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.WHOLE_GRAINS,
        label="Féculents complets",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("1"),
        unit=PnnsUnit.SERVING,
        window_days=1,
        statement=(
            "Le pain complet ou aux céréales, les pâtes, la semoule et le riz "
            "complets : au choix au moins une fois par jour"
        ),
        source_url=f"{_MB}aller-vers/aller-vers-les-feculents-complets",
        sort_order=30,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.NUTS,
        label="Fruits à coque",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("1"),
        unit=PnnsUnit.SERVING,
        window_days=1,
        statement=(
            "Consommer une petite poignée par jour de fruits à coque non salés : "
            "noix, noisettes, amandes et pistaches"
        ),
        source_url=(
            f"{_MB}augmenter/augmenter-les-fruits-a-coque-non-sales-noix-noisettes-amandes-etc"
        ),
        sort_order=40,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.DAIRY,
        label="Produits laitiers",
        # Not a floor. The source page is titled "aller vers une consommation
        # suffisante *mais limitée*", and reading it as "at least" would have
        # this application push more cheese on somebody already above the mark.
        direction=PnnsDirection.AROUND,
        amount=Decimal("2"),
        unit=PnnsUnit.SERVING,
        window_days=1,
        statement="2 produits laitiers par jour pour les adultes",
        source_url=(
            f"{_MB}aller-vers/"
            "aller-vers-une-consommation-de-produits-laitiers-suffisante-mais-limitee"
        ),
        sort_order=50,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.FISH,
        label="Poisson",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("2"),
        unit=PnnsUnit.SERVING,
        window_days=7,
        statement="Poisson 2 fois par semaine, dont un poisson gras",
        source_url=f"{_MB}aller-vers/aller-vers-les-poissons-gras-et-maigres-en-alternance",
        sort_order=60,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.OILY_FISH,
        label="Poisson gras",
        direction=PnnsDirection.AT_LEAST,
        amount=Decimal("1"),
        unit=PnnsUnit.SERVING,
        window_days=7,
        statement=("Dont un poisson gras par semaine (sardines, maquereau, hareng, saumon)"),
        source_url=f"{_MB}aller-vers/aller-vers-les-poissons-gras-et-maigres-en-alternance",
        sort_order=70,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.RED_MEAT,
        label="Viande hors volaille",
        direction=PnnsDirection.AT_MOST,
        amount=Decimal("500"),
        unit=PnnsUnit.GRAM,
        window_days=7,
        statement=(
            "Limiter les viandes autres que la volaille (porc, bœuf, veau, mouton, "
            "agneau, abats) à 500 g par semaine"
        ),
        # The typo in the slug is the official one.
        source_url=f"{_MB}reduire/reduire-la-viande-porc-baeuf-veau-mouton-agneau-abats",
        sort_order=80,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.PROCESSED_MEAT,
        label="Charcuterie",
        direction=PnnsDirection.AT_MOST,
        amount=Decimal("150"),
        unit=PnnsUnit.GRAM,
        window_days=7,
        statement="Limiter la charcuterie à 150 g par semaine",
        source_url=f"{_MB}reduire/reduire-la-charcuterie",
        sort_order=90,
    ),
    PnnsGuidelineSpec(
        marker=PnnsMarker.SUGARY_DRINKS,
        label="Boissons sucrées",
        direction=PnnsDirection.AT_MOST,
        amount=Decimal("1"),
        unit=PnnsUnit.SERVING,
        window_days=1,
        statement="Limiter les boissons sucrées : pas plus d'un verre par jour",
        source_url=(
            f"{_MB}reduire/"
            "reduire-les-boissons-sucrees-aliments-gras-sucres-sales-et-ultra-transformes"
        ),
        sort_order=100,
    ),
)


# --------------------------------------------------------------------------- #
# Infant restrictions
# --------------------------------------------------------------------------- #

#: The bands a young child can be in. Used to build the ``applies_to_bands``
#: arrays below without repeating six literals per rule.
INFANT_AGE_BANDS: Final[tuple[AgeBand, ...]] = (
    AgeBand.INFANT_4_6M,
    AgeBand.INFANT_6_9M,
    AgeBand.INFANT_9_12M,
    AgeBand.INFANT_12_36M,
)

#: Under twelve months: the three infant bands that stop at one year.
_UNDER_ONE: Final[tuple[AgeBand, ...]] = (
    AgeBand.INFANT_4_6M,
    AgeBand.INFANT_6_9M,
    AgeBand.INFANT_9_12M,
)

#: Under three years, plus ``CHILD`` where the source says five years and the
#: schema has no finer band. Erring towards the older band withholds a food from
#: a four-year-old who could have had it; the other direction is a choking risk.
_UNDER_THREE: Final[tuple[AgeBand, ...]] = INFANT_AGE_BANDS
_UNDER_FIVE: Final[tuple[AgeBand, ...]] = (*INFANT_AGE_BANDS, AgeBand.CHILD)

_ANSES_0_3: Final = "https://www.anses.fr/fr/system/files/NUT2017SA0145.pdf"
_SPF_DIVERSIFICATION: Final = (
    "https://www.mangerbouger.fr/content/show/1498/file/"
    "Tableau_diversification_alimentaire_jusqu%27a%203%20ans.pdf"
)
_SPF_BROCHURE: Final = (
    "https://www.mangerbouger.fr/content/show/1500/file/Brochure-SPF-Mangerbougerfr.pdf"
)
_ANSES_BEVERAGES: Final = "https://www.anses.fr/fr/system/files/NUT2011sa0261.pdf"

#: Nine rules, each read off ANSES opinion 2017-SA-0145 (12 June 2019) or the
#: Santé publique France diversification material, on 2026-08-04.
#:
#: Two corrections the verification produced, both worth keeping in view because
#: the obvious version of this table gets them wrong:
#:
#: * **Raw-milk cheese is a three-year rule, not a one-year rule, and it has an
#:   official exception** -- hard cooked-curd cheeses (comté, emmental) are
#:   explicitly allowed. A rule without the exception would withhold the one
#:   cheese a French household always has.
#: * **The official choking list is short**: whole tree nuts, peanuts and whole
#:   grapes. Cherry tomatoes and raw carrot sticks, which everyone repeats, are
#:   not named as forbidden anywhere official -- cherry tomatoes appear only as
#:   an example of something to *cut in half*. They are matched here all the
#:   same, because a withheld tomato costs a suggestion and the source is
#:   explicit that small round firm foods are the hazard; but the statement says
#:   what the source says and no more.
#:
#: No arsenic rule for rice drinks. That warning is the UK Food Standards
#: Agency's; the word does not appear in either ANSES opinion or in the Santé
#: publique France brochure, and the French objection to plant drinks is
#: nutritional. A safety table that cites a source for a rule the source does
#: not contain is worse than one rule short.
INFANT_RESTRICTIONS: Final[Sequence[InfantRestrictionSpec]] = (
    InfantRestrictionSpec(
        rule_code="honey",
        label="Miel",
        applies_to_bands=_UNDER_ONE,
        risk=InfantRiskKind.MICROBIOLOGICAL,
        statement=(
            "Ne pas donner de miel aux nourrissons de moins de 12 mois : risque de "
            "botulisme infantile lié aux spores de Clostridium botulinum (ANSES)."
        ),
        source_url=_ANSES_0_3,
        category_tags=("en:honeys", "en:honey"),
        name_patterns=("miel",),
        sort_order=10,
    ),
    InfantRestrictionSpec(
        rule_code="raw_milk",
        label="Lait cru et fromages au lait cru",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.MICROBIOLOGICAL,
        statement=(
            "Éviter le lait cru et les fromages au lait cru jusqu'à 3 ans, à "
            "l'exception des fromages à pâte pressée cuite comme le gruyère ou le "
            "comté (ANSES)."
        ),
        source_url=_ANSES_0_3,
        category_tags=("en:raw-milk-cheeses", "en:raw-milks"),
        name_patterns=("lait cru", "au lait cru"),
        sort_order=20,
    ),
    InfantRestrictionSpec(
        rule_code="choking_small_firm",
        label="Petits aliments durs et ronds",
        applies_to_bands=_UNDER_FIVE,
        risk=InfantRiskKind.CHOKING,
        statement=(
            "Les petits aliments de forme cylindrique ou sphérique qui résistent à "
            "l'écrasement — fruits à coque, arachide, grains de raisin — ne doivent "
            "pas être consommés entiers : risque d'étouffement (ANSES). Santé "
            "publique France étend la consigne aux moins de 5 ans."
        ),
        source_url=_ANSES_0_3,
        category_tags=(
            "en:nuts",
            "en:peanuts",
            "en:hazelnuts",
            "en:almonds",
            "en:walnuts",
            "en:pistachios",
            "en:cashew-nuts",
            "en:grapes",
            "en:cherry-tomatoes",
            "en:popcorn",
            "en:candies",
            "en:chewing-gum",
        ),
        name_patterns=(
            "cacahuète",
            "cacahuete",
            "arachide",
            "noisette",
            "amande entière",
            "raisin",
            "tomate cerise",
            "pop-corn",
            "bonbon",
        ),
        sort_order=30,
    ),
    InfantRestrictionSpec(
        rule_code="added_salt",
        label="Sel ajouté et produits salés",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.NUTRITIONAL,
        statement=(
            "Ne pas ajouter de sel lors de la préparation des repas jusqu'à 3 ans, "
            "et ne pas donner de produits salés type produits apéritifs (ANSES, "
            "Santé publique France)."
        ),
        source_url=_SPF_DIVERSIFICATION,
        category_tags=("en:salty-snacks", "en:crisps", "en:aperitif-biscuits", "en:salts"),
        name_patterns=("chips", "apéritif", "aperitif", "biscuit salé"),
        sort_order=40,
    ),
    InfantRestrictionSpec(
        rule_code="added_sugar",
        label="Produits et boissons sucrés",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.NUTRITIONAL,
        statement=(
            "Avant 3 ans, éviter les aliments et boissons sucrés. La seule boisson "
            "recommandée est l'eau ; les jus de fruits, même sans sucres ajoutés, "
            "ne sont pas recommandés (Santé publique France)."
        ),
        source_url=_SPF_BROCHURE,
        category_tags=(
            "en:sodas",
            "en:sweetened-beverages",
            "en:fruit-juices",
            "en:fruit-nectars",
            "en:syrups",
            "en:candies",
            "en:chocolate-candies",
        ),
        name_patterns=("soda", "sirop", "nectar", "jus de fruits"),
        sort_order=50,
    ),
    InfantRestrictionSpec(
        rule_code="raw_animal_products",
        label="Viande, poisson et œufs crus ou peu cuits",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.MICROBIOLOGICAL,
        statement=(
            "Éviter jusqu'à 3 ans les viandes crues ou peu cuites, les œufs crus ou "
            "insuffisamment cuits, les coquillages crus et le poisson cru : cuire à "
            "cœur (ANSES)."
        ),
        source_url=_ANSES_0_3,
        category_tags=(
            "en:raw-meats",
            "en:carpaccios",
            "en:tartares",
            "en:sushis",
            "en:smoked-salmon",
            "en:raw-shellfish",
            "en:oysters",
        ),
        name_patterns=("tartare", "carpaccio", "sushi", "huître", "huitre", "mayonnaise"),
        sort_order=60,
    ),
    InfantRestrictionSpec(
        rule_code="deli_meat",
        label="Charcuterie hors jambon blanc",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.NUTRITIONAL,
        statement=(
            "En dehors du jambon blanc, la charcuterie (saucisses, pâtés…) ne doit "
            "être donnée qu'exceptionnellement avant 3 ans (Santé publique France)."
        ),
        source_url=_SPF_DIVERSIFICATION,
        category_tags=("en:prepared-meats", "en:sausages", "en:pates", "en:dry-sausages"),
        name_patterns=("saucisson", "pâté", "pate en croute", "chorizo", "rillettes"),
        sort_order=70,
    ),
    InfantRestrictionSpec(
        rule_code="predatory_fish",
        label="Poissons à forte teneur en méthylmercure",
        applies_to_bands=_UNDER_THREE,
        risk=InfantRiskKind.TOXICOLOGICAL,
        statement=(
            "L'espadon, le marlin, le siki, le requin et la lamproie sont à éviter "
            "chez les enfants de moins de 3 ans en raison du risque lié au "
            "méthylmercure (ANSES)."
        ),
        source_url=_ANSES_0_3,
        category_tags=("en:swordfish", "en:marlin", "en:sharks", "en:lampreys"),
        name_patterns=("espadon", "marlin", "siki", "requin", "lamproie"),
        sort_order=80,
    ),
    InfantRestrictionSpec(
        rule_code="plant_based_drinks",
        label="Boissons végétales en substitut de lait",
        applies_to_bands=_UNDER_ONE,
        risk=InfantRiskKind.NUTRITIONAL,
        statement=(
            "Les préparations pour nourrissons ne doivent pas être substituées par "
            "des boissons végétales chez les enfants de moins de 1 an : risque de "
            "carences nutritionnelles graves (ANSES). Les produits à base de soja "
            "sont par ailleurs déconseillés avant 3 ans."
        ),
        source_url=_ANSES_BEVERAGES,
        category_tags=(
            "en:plant-based-milk-substitutes",
            "en:soy-milks",
            "en:almond-milks",
            "en:rice-milks",
            "en:oat-milks",
        ),
        name_patterns=("boisson végétale", "lait d'amande", "lait de riz", "lait de soja"),
        sort_order=90,
    ),
)
