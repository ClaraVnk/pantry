"""Coarse keeping families, and the indicative durations attached to them.

Why this exists at all: nobody types eighteen expiry dates while putting the
shopping away. Without a proposed date most lots arrive with none, and every
feature that depends on one -- the urgency ranking that is the whole point of
the product, the expiry alerts, the weekly balance -- degrades to guesswork. So
the application proposes, and the household corrects.

Two rules keep the proposal from becoming a lie.

*The proposal is labelled.* What comes out of here lands in
``InventoryLot.best_before`` alongside ``expiry_date_source = 'estimated'``,
which the interface renders differently from a date somebody read off the
packaging. The application must never assert a use-by date it invented; on
fresh meat that distinction is the difference between advice and a hazard.

*An unresolved product gets nothing.* :func:`family_for_categories` returns
``None`` rather than a cautious default. "Probably seven days" on a product
whose categories we could not read is exactly the fabricated fact the rest of
this codebase is written against -- and unlike a missing date, a wrong one is
invisible.

The families are deliberately coarse. A finer table would look more
authoritative without being more right: the real shelf life of *this* pack
depends on the cold chain it travelled through, which no reference knows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, Date, Select, func

from chaudron.domain.models import (
    ExpiryDateKind,
    FoodFamily,
    InventoryLot,
    Product,
    ShelfLifeGuideline,
)

__all__ = [
    "SHELF_LIFE_GUIDELINES",
    "ShelfLifeSpec",
    "effective_expiry",
    "effective_expiry_sql",
    "family_for_categories",
    "join_shelf_life_guideline",
]


@dataclass(frozen=True, slots=True)
class ShelfLifeSpec:
    """One family's indicative durations, and the page they were read from."""

    family: FoodFamily
    label: str
    #: Days from purchase, in the family's normal storage place. ``None`` where
    #: no honest number exists.
    unopened_days: int | None
    #: Days from opening, refrigerated. Frequently the shorter and more useful
    #: of the two, and the only one the application can enforce on its own,
    #: since it records ``opened_at``.
    opened_days: int | None
    #: A use-by is a safety limit, a best-before a quality one. Guessing it from
    #: the family is how dry pasta starts raising alarms and minced meat stops.
    date_kind: ExpiryDateKind
    source_url: str
    note: str | None = None


# --------------------------------------------------------------------------- #
# Resolution from catalogue categories
# --------------------------------------------------------------------------- #
#
# ``categories_tags`` runs from the most general ancestor to the most specific
# leaf -- verified on live products, and confirmed by Open Food Facts' own code,
# which iterates it reversed when it wants "the" category. So the scan below
# runs from the end: a tin of tomatoes is `en:vegetables` before it is
# `en:canned-tomatoes`, and only the second one says how long it keeps.

_FAMILY_BY_CATEGORY: Final[Mapping[str, FoodFamily]] = {
    # Canned and jarred first in intent, since these tags sit deepest and would
    # otherwise be shadowed by the produce or fish ancestors above them.
    "en:canned-foods": FoodFamily.CANNED,
    "en:canned-vegetables": FoodFamily.CANNED,
    "en:canned-tomatoes": FoodFamily.CANNED,
    "en:canned-fishes": FoodFamily.CANNED,
    "en:canned-legumes": FoodFamily.CANNED,
    "en:preserves": FoodFamily.CANNED,
    "en:frozen-foods": FoodFamily.FROZEN,
    "en:frozen-vegetables": FoodFamily.FROZEN,
    "en:frozen-fishes": FoodFamily.FROZEN,
    "en:frozen-meats": FoodFamily.FROZEN,
    "en:ice-cream": FoodFamily.FROZEN,
    "en:dairies": FoodFamily.FRESH_DAIRY,
    "en:milks": FoodFamily.FRESH_DAIRY,
    "en:yogurts": FoodFamily.FRESH_DAIRY,
    "en:creams": FoodFamily.FRESH_DAIRY,
    "en:fresh-dairy-desserts": FoodFamily.FRESH_DAIRY,
    "en:fromage-blancs": FoodFamily.FRESH_DAIRY,
    "en:cheeses": FoodFamily.CHEESE,
    "en:hard-cheeses": FoodFamily.CHEESE,
    "en:cooked-pressed-cheeses": FoodFamily.CHEESE,
    "en:meats": FoodFamily.FRESH_MEAT,
    "en:fresh-meats": FoodFamily.FRESH_MEAT,
    "en:poultry": FoodFamily.FRESH_MEAT,
    "en:beef": FoodFamily.FRESH_MEAT,
    "en:pork": FoodFamily.FRESH_MEAT,
    "en:prepared-meats": FoodFamily.FRESH_MEAT,
    "en:seafood": FoodFamily.FRESH_FISH,
    "en:fishes": FoodFamily.FRESH_FISH,
    "en:fresh-fishes": FoodFamily.FRESH_FISH,
    "en:shellfishes": FoodFamily.FRESH_FISH,
    "en:eggs": FoodFamily.EGGS,
    "en:chicken-eggs": FoodFamily.EGGS,
    "en:fresh-foods": FoodFamily.PRODUCE,
    "en:fruits": FoodFamily.PRODUCE,
    "en:vegetables": FoodFamily.PRODUCE,
    "en:fresh-vegetables": FoodFamily.PRODUCE,
    "en:fresh-fruits": FoodFamily.PRODUCE,
    "en:salads": FoodFamily.PRODUCE,
    "en:cereals-and-potatoes": FoodFamily.DRY_GOODS,
    "en:pastas": FoodFamily.DRY_GOODS,
    "en:rices": FoodFamily.DRY_GOODS,
    "en:flours": FoodFamily.DRY_GOODS,
    "en:sugars": FoodFamily.DRY_GOODS,
    "en:legumes": FoodFamily.DRY_GOODS,
    "en:lentils": FoodFamily.DRY_GOODS,
    "en:vegetable-oils": FoodFamily.DRY_GOODS,
    "en:olive-oils": FoodFamily.DRY_GOODS,
    "en:honeys": FoodFamily.DRY_GOODS,
    "en:groceries": FoodFamily.DRY_GOODS,
}


def family_for_categories(category_tags: object) -> FoodFamily | None:
    """Resolve a keeping family, most specific category first.

    ``None`` is a real answer and the one that must survive refactoring: it
    means no date is proposed for this product at all. The temptation to return
    a "safe" default here is the whole reason the docstring at the top of this
    module exists.
    """
    if not isinstance(category_tags, list):
        return None
    for tag in reversed(category_tags):
        if not isinstance(tag, str):
            continue
        if (family := _FAMILY_BY_CATEGORY.get(tag.strip().lower())) is not None:
            return family
    return None


# --------------------------------------------------------------------------- #
# Durations
# --------------------------------------------------------------------------- #

#
# Sources, checked on 2026-08-04. What the verification actually established is
# worth recording, because it decided the shape of this table:
#
# * **The French authorities publish rules, not day counts.** ANSES and Santé
#   publique France give fridge temperatures, zones, and the DLC/DDM
#   distinction -- but no per-family shelf-life table. Exactly one citable
#   French number exists and it is the useful one: **three days** for an opened
#   perishable, for a thawed food and for a homemade leftover.
# * **So every "unopened" figure below except eggs comes from the USDA
#   FoodKeeper**, and is labelled as such in its row. Where FoodKeeper gives a
#   range, the *short* end is taken: this is a pre-filled estimate a household
#   may never revisit, and an estimate that runs long on raw chicken is the one
#   failure this table could actually cause.
# * **Which date type a family carries is law, not opinion.** Regulation (EU)
#   1169/2011 article 24(1) makes the use-by date the mandatory form for
#   microbiologically highly perishable foods; ANSES assigns the families
#   (meat, fish, some dairy and charcuterie to the use-by; pasta, rice, sugar,
#   salt, flour to the best-before).
# * **Opening voids the printed date** -- ANSES is explicit that an opened
#   product does not keep its original use-by. The consumer of this table must
#   therefore compute ``min(printed date, opened_at + opened_days)`` and never
#   an extension.
#
# One inference is flagged rather than hidden: no French source reachable here
# classifies hard cheese, and it is recorded as a best-before because it plainly
# fails the "highly perishable" test of article 24(1). The row says so.

_ANSES_DATES: Final = (
    "https://www.anses.fr/fr/content/"
    "date-limite-de-consommation-dlc-et-date-de-durabilite-minimale-ddm"
)
_ANSES_STORAGE: Final = (
    "https://www.anses.fr/fr/content/"
    "comment-bien-conserver-ses-aliments-et-ne-pas-interrompre-la-chaine-du-froid"
)
_ANSES_EGGS: Final = "https://www.anses.fr/fr/content/consommation-des-oeufs"
_FOODKEEPER: Final = "https://www.foodsafety.gov/food-safety-charts/cold-food-storage-charts"

#: The three-day ceiling ANSES puts on anything perishable once it is open. It
#: is the only French figure available, it is more conservative than every
#: FoodKeeper equivalent, and that is the right bias for a number nobody asked
#: for.
_ANSES_OPENED_DAYS: Final = 3

SHELF_LIFE_GUIDELINES: Final[Sequence[ShelfLifeSpec]] = (
    ShelfLifeSpec(
        family=FoodFamily.FRESH_DAIRY,
        label="Produits laitiers frais",
        unopened_days=7,
        opened_days=_ANSES_OPENED_DAYS,
        date_kind=ExpiryDateKind.USE_BY,
        source_url=_ANSES_DATES,
        note=(
            "Durée après ouverture : repère ANSES de 3 jours pour un aliment "
            "sensible entamé. La date imprimée sur l'emballage prime toujours."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.CHEESE,
        label="Fromages à pâte dure",
        unopened_days=180,
        opened_days=21,
        # Inferred, and said so in the note: a hard cheese is not
        # "microbiologiquement très périssable" in the sense of article 24(1),
        # so it carries a best-before -- but no French source reachable here
        # states it in as many words.
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_FOODKEEPER,
        note=(
            "Ordres de grandeur USDA FoodKeeper. Le type de date (DDM) est déduit "
            "du règlement (UE) 1169/2011 art. 24, faute de source française "
            "classant explicitement les pâtes pressées."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.FRESH_MEAT,
        label="Viandes fraîches",
        # FoodKeeper gives 1-2 days for minced meat and poultry, 3-5 for whole
        # cuts. The short end, because this table cannot tell them apart.
        unopened_days=2,
        opened_days=2,
        date_kind=ExpiryDateKind.USE_BY,
        source_url=_FOODKEEPER,
        note=(
            "Borne basse de la fourchette USDA FoodKeeper (viande hachée et "
            "volaille). La DLC imprimée prime ; l'ouverture l'annule (ANSES)."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.FRESH_FISH,
        label="Poissons et fruits de mer frais",
        unopened_days=2,
        opened_days=1,
        date_kind=ExpiryDateKind.USE_BY,
        source_url=_FOODKEEPER,
        note="Ordres de grandeur USDA FoodKeeper. La DLC imprimée prime.",
    ),
    ShelfLifeSpec(
        family=FoodFamily.EGGS,
        label="Œufs",
        # The printed date is 28 days after *laying* (Reg. (EC) 589/2008 art.
        # 13), and eggs must reach the consumer within 21 days of it (Reg. (EC)
        # 853/2004). A household knows neither date, so 21 days from purchase is
        # the conservative stand-in -- and the printed one always wins.
        unopened_days=21,
        opened_days=None,
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_ANSES_EGGS,
        note=(
            "La date imprimée est une DDM fixée à 28 jours après la ponte. "
            "21 jours à partir de l'achat est une estimation prudente ; la date "
            "de l'emballage prime."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.PRODUCE,
        label="Fruits et légumes frais",
        unopened_days=7,
        opened_days=3,
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_FOODKEEPER,
        note=(
            "Les fruits et légumes non transformés sont dispensés de date "
            "(règlement (UE) 1169/2011, annexe X). L'estimation est indicative."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.FROZEN,
        label="Surgelés",
        # Safe indefinitely at -18 °C; the figure is about quality, not safety.
        unopened_days=365,
        opened_days=_ANSES_OPENED_DAYS,
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_ANSES_STORAGE,
        note=(
            "À -18 °C la conservation est sûre au-delà de la date : le repère "
            "porte sur la qualité. Après décongélation, 3 jours (ANSES), et ne "
            "jamais recongeler."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.DRY_GOODS,
        label="Épicerie sèche",
        unopened_days=365,
        opened_days=180,
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_ANSES_DATES,
        note=(
            "ANSES : le sel, le sucre, les pâtes, le riz et la farine ne "
            "présentent pas de risque après la DDM. Ordres de grandeur USDA."
        ),
    ),
    ShelfLifeSpec(
        family=FoodFamily.CANNED,
        label="Conserves",
        unopened_days=730,
        opened_days=3,
        date_kind=ExpiryDateKind.BEST_BEFORE,
        source_url=_ANSES_DATES,
        note=(
            "Une fois ouverte, une conserve se transvase dans un contenant "
            "fermé et se consomme sous 3 jours."
        ),
    ),
)


# --------------------------------------------------------------------------- #
# The effective expiry date
# --------------------------------------------------------------------------- #
#
# ``docs/data-model.md`` section 7.4 defines when a lot actually becomes unfit:
#
#     min(best_before, opened_at + shelf_life_guideline.opened_days)
#
# Two properties decide the implementation, and both are safety properties.
#
# *The clamp only ever shortens.* ANSES is explicit that opening voids the
# printed date; a formula able to push a date outwards would be a food-safety
# bug rather than a rounding difference. ``LEAST`` cannot extend anything, which
# is why it is used instead of a hand-written ``CASE``.
#
# *A missing input removes the clamp; it never invents one.* No ``opened_at``,
# no ``product.food_family``, no guideline row, or a family whose ``opened_days``
# is NULL: in every one of those the printed date is the whole answer. This is
# the same register as :func:`family_for_categories` returning ``None``.
#
# **The rule is computed in SQL, not in Python.** The listing sorts by it, the
# ``expiring_within_days`` filter compares against it, and the calendar feed
# windows on it -- all three over a household's whole active inventory. Computing
# in Python would mean loading every lot on every one of those calls to discard
# most of them, which is the shape of query this codebase pays an index to avoid.
# :func:`effective_expiry` below is the same rule for callers that already hold
# the three values in memory, and ``tests/domain/test_effective_expiry.py``
# asserts the two agree rather than trusting that they do.
#
# **This relies on PostgreSQL's ``LEAST``, which ignores NULL arguments** and
# returns NULL only when every argument is NULL. That is not the standard
# behaviour everywhere (MySQL propagates NULL), and it is what makes the two
# interesting cases fall out on their own: an unopened lot keeps its printed
# date, and an opened lot with no printed date is governed by the opening alone.
# ADR-0003 makes PostgreSQL the only engine, including in tests, so depending on
# it is a decision rather than an accident.


def effective_expiry(
    best_before: date | None,
    opened_at: date | None,
    opened_days: int | None,
) -> date | None:
    """When this lot becomes unfit, or ``None`` when nothing says.

    The Python twin of :func:`effective_expiry_sql`, for callers that already
    hold the three values. ``None`` means the application knows of no date at
    all -- not "it keeps forever", and not a date to invent.
    """
    after_opening = (
        None
        if opened_at is None or opened_days is None
        else opened_at + timedelta(days=opened_days)
    )
    known = [value for value in (best_before, after_opening) if value is not None]
    return min(known) if known else None


def effective_expiry_sql() -> ColumnElement[date]:
    """The same rule as a SQL expression, for filtering and ordering in the database.

    Requires ``inventory_lot`` and ``shelf_life_guideline`` in the statement's
    ``FROM``; :func:`join_shelf_life_guideline` is how the second one gets there.
    """
    return func.least(
        InventoryLot.best_before,
        InventoryLot.opened_at + ShelfLifeGuideline.opened_days,
        type_=Date(),
    )


def join_shelf_life_guideline(statement: Select[Any]) -> Select[Any]:
    """Attach the guideline row of each lot's product, or no row at all.

    A ``LEFT`` join, and that is the whole of the "no guideline, no clamp" rule:
    an unresolved ``food_family`` and a family this table has no row for both
    leave ``opened_days`` NULL, which leaves :func:`effective_expiry_sql`
    reading the printed date alone.

    ``product`` must already be joined -- every caller lists it among the columns
    it reads anyway.
    """
    return statement.outerjoin(ShelfLifeGuideline, ShelfLifeGuideline.family == Product.food_family)
