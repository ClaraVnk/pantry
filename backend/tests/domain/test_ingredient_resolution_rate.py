"""How often the post-call check can name an ingredient -- measured, not assumed.

Matching a label a model wrote against a catalogue is an open problem
(``docs/technical-notes-ingestion.md`` section 7), and ADR-0009 turns it into a
**safety control**: an ingredient that cannot be named discards the suggestion.
Two numbers therefore decide whether the feature works, and they pull in opposite
directions.

*Recall* is the share of honest ingredient labels that resolve. Every miss costs
a suggestion, so a low recall makes the feature a denial of service for exactly
the households it exists to protect.

*Refusal* is the share of dangerous labels that do **not** resolve. Every miss
here serves somebody a peanut butter labelled as butter. It has to be total, and
the assertion below says so: not a rate, a count of zero.

The corpus is written by hand from French pantry labels and the way a model
paraphrases them. It is **not** production data, and the figure it produces is a
property of this corpus rather than a measurement of the real world -- the honest
reading is "the resolver behaves like this on the cases we could think of", and
the first real false match is a reason to reopen the design (ADR-0009, Révision).
"""

from __future__ import annotations

import uuid

from chaudron.domain.constraints import (
    PANTRY_STAPLES,
    ProductFacts,
    StockIndex,
    StockLine,
)
from chaudron.domain.models import AllergenDataState

#: A plausible screened inventory: what a French household actually has, spelled
#: the way Open Food Facts spells it.
PANTRY: tuple[str, ...] = (
    "Crème fraîche épaisse",
    "Lait demi-écrémé UHT",
    "Beurre doux",
    "Yaourt nature brassé",
    "Emmental râpé",
    "Œufs frais de poule élevée en plein air",
    "Pommes de terre",
    "Carottes",
    "Oignons jaunes",
    "Courgettes",
    "Tomates pelées",
    "Ail",
    "Riz basmati",
    "Pâtes penne",
    "Farine de blé T55",
    "Lentilles vertes",
    "Filet de cabillaud",
    "Steak haché 5%",
    "Blanc de poulet",
    "Champignons de Paris",
)

#: Labels a model writes for something in that pantry. Each is a suggestion that
#: is lost -- not made unsafe -- if it fails to resolve.
HONEST: tuple[str, ...] = (
    "Crème fraîche épaisse",
    "crème fraîche",
    "Crème",
    "200 g de crème fraîche",
    "lait demi-écrémé",
    "Lait",
    "beurre",
    "Beurre doux",
    "yaourt nature",
    "emmental râpé",
    "Emmental",
    "œufs",
    "oeufs",
    "2 œufs",
    "pommes de terre",
    "500 g de pommes de terre",
    "carottes",
    "oignons jaunes",
    "Oignons",
    "courgettes",
    "tomates pelées",
    "ail",
    "riz basmati",
    "Riz",
    "pâtes penne",
    "farine de blé",
    "Farine",
    "lentilles vertes",
    "filet de cabillaud",
    "cabillaud",
    "steak haché",
    "blanc de poulet",
    "champignons de Paris",
    "Champignons",
    "sel",
    "poivre",
    "eau",
    "huile d'olive",
    "vinaigre",
    "gros sel",
)

#: Labels that must **not** resolve against that pantry. Every one of them is a
#: real way the permissive version of this matcher gets somebody hurt, or a way
#: it silently invents an ingredient the household does not own.
DANGEROUS: tuple[str, ...] = (
    # The headline: a substring matcher answers "Beurre doux" here.
    "Beurre de cacahuète",
    "beurre de cacahuètes",
    "purée d'amandes",
    "beurre de sésame",
    "lait de soja",
    "lait d'amande",
    "crème de coco",
    "farine de sarrasin",
    "riz au lait",
    # Bare "huile" is ambiguous between olive, peanut and sesame, and two of
    # those are on the regulated list.
    "huile",
    "sauce soja",
    "pâte de curry",
    # Not in the pantry at all: resolving these would be inventing stock.
    "saumon fumé",
    "crevettes",
    "moutarde de Dijon",
    "câpres",
    "parmesan",
    "quelques herbes fraîches",
)

#: The floor this corpus has to clear. Set below the measured figure with room to
#: spare, so an unrelated change moves it before it fails, and high enough that a
#: resolver which stopped matching anything would be caught.
MIN_RECALL = 0.90


def _index() -> StockIndex:
    lines = [
        StockLine(
            inventory_item_id=uuid.uuid7(),
            product=ProductFacts(
                product_id=uuid.uuid7(),
                name=name,
                allergen_state=AllergenDataState.DECLARED,
                allergens_risk=frozenset(),
                pnns_markers=frozenset(),
                category_tags=(),
            ),
            quantity="1",
            unit="piece",
            expires_on=None,
        )
        for name in PANTRY
    ]
    return StockIndex(lines, PANTRY_STAPLES)


def test_honest_ingredient_labels_resolve_often_enough_to_be_usable() -> None:
    """A resolver that refuses everything is safe and useless.

    The failure this guards against is not a wrong match, it is a feature that
    returns 502 to every household with an allergy -- which would be read as the
    application being broken, and worked around by turning the constraints off.
    """
    index = _index()

    resolved = [label for label in HONEST if index.resolve(label).resolved]

    recall = len(resolved) / len(HONEST)
    missed = sorted(set(HONEST) - set(resolved))
    assert recall >= MIN_RECALL, f"recall {recall:.0%} on {len(HONEST)} labels; missed {missed}"


def test_no_dangerous_label_resolves_to_anything() -> None:
    """Zero, not a rate. One match here is a peanut butter served as butter.

    The list mixes two failure modes on purpose. "Beurre de cacahuète" against
    "Beurre doux" is the safety one. "Parmesan" against a pantry that has none is
    the honesty one -- resolving it would let a suggestion claim an ingredient
    the household does not own, which is the same defect ``in_stock`` was
    recomputed to prevent.
    """
    index = _index()

    matched = {label: index.resolve(label) for label in DANGEROUS if index.resolve(label).resolved}

    assert matched == {}, f"{len(matched)} unsafe labels resolved: {sorted(matched)}"
