"""What the deterministic parser reads, and what it measurably does not.

No database and no model: the parser is pure, so its quality can be a *number*
rather than an impression. :func:`test_corpus_split_rate` asserts a floor on that
number over a French corpus, and :data:`KNOWN_FAILURES` names the lines it gets
wrong on purpose -- a parser whose limitations are written down is one whose
next author knows what they are changing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chaudron.domain.models import QuantityDimension, Unit
from chaudron.services.shopping_import import UnitLexicon, parse_shopping_line

#: The rows migration ``0002`` seeds, rebuilt in memory. Built from the same
#: shape as the table on purpose: the point of the lexicon is that it cannot
#: invent a unit, so a test that hard-coded the spellings would test nothing.
_SEEDED: tuple[tuple[str, QuantityDimension, str], ...] = (
    ("g", QuantityDimension.MASS, "g"),
    ("kg", QuantityDimension.MASS, "kg"),
    ("mg", QuantityDimension.MASS, "mg"),
    ("ml", QuantityDimension.VOLUME, "ml"),
    ("cl", QuantityDimension.VOLUME, "cl"),
    ("dl", QuantityDimension.VOLUME, "dl"),
    ("l", QuantityDimension.VOLUME, "L"),
    ("tbsp", QuantityDimension.VOLUME, "c. à s."),
    ("tsp", QuantityDimension.VOLUME, "c. à c."),
    ("piece", QuantityDimension.COUNT, "pc"),
)


@pytest.fixture(scope="module")
def lexicon() -> UnitLexicon:
    return UnitLexicon(
        Unit(code=code, dimension=dimension, symbol=symbol) for code, dimension, symbol in _SEEDED
    )


#: raw line -> (amount, unit code, label). ``None`` for the amount means the line
#: is expected to carry no quantity at all.
# ``noqa`` on the table: a French shopping list is written with a MULTIPLICATION
# SIGN, an EN DASH and typographic apostrophes. A corpus that replaced them with
# ASCII look-alikes would stop testing what actually arrives.
CORPUS: tuple[tuple[str, str | None, str | None, str], ...] = (
    ("2 kg de pommes de terre", "2", "kg", "pommes de terre"),
    ("3 × yaourt nature", "3", "piece", "yaourt nature"),  # noqa: RUF001
    ("pain", None, None, "pain"),
    ("1,5 L de lait", "1.5", "l", "lait"),
    ("500 g de farine", "500", "g", "farine"),
    ("- 6 œufs", "6", "piece", "œufs"),
    ("• Café moulu", None, None, "Café moulu"),
    ("[ ] papier toilette", None, None, "papier toilette"),
    ("2 boîtes de tomates pelées", "2", "piece", "boîtes de tomates pelées"),
    ("Lait demi-écrémé : 1 L", "1", "l", "Lait demi-écrémé"),
    ("Farine 500g", "500", "g", "Farine"),
    ("yaourt nature x4", "4", "piece", "yaourt nature"),
    ("une douzaine d'œufs", "12", "piece", "œufs"),
    ("1 kilo de carottes", "1", "kg", "carottes"),
    ("2 c. à s. d'huile d'olive", "2", "tbsp", "huile d'olive"),
    ("3 cuillères à soupe de miel", "3", "tbsp", "miel"),
    ("250 ml de crème fraîche", "250", "ml", "crème fraîche"),
    ("Pain .......... 2,50 €", None, None, "Pain"),
    ("du beurre", None, None, "du beurre"),
    ("☐ Sucre en poudre", None, None, "Sucre en poudre"),
    ("4 pommes", "4", "piece", "pommes"),
    ("1/2 kg de courgettes", "0.5", "kg", "courgettes"),
    ("Riz basmati - 1 kg", "1", "kg", "Riz basmati"),
    ("2 cl de vinaigre", "2", "cl", "vinaigre"),
    ("qqch pour le dessert", None, None, "qqch pour le dessert"),
)


@pytest.mark.parametrize(("raw", "amount", "unit", "label"), CORPUS)
def test_corpus_line(
    raw: str, amount: str | None, unit: str | None, label: str, lexicon: UnitLexicon
) -> None:
    reading = parse_shopping_line(raw, lexicon)

    assert reading.label == label, f"{raw!r} produced label {reading.label!r}"
    if amount is None:
        assert reading.amount is None, f"{raw!r} invented a quantity"
        assert reading.unit is None
    else:
        assert reading.amount == Decimal(amount), f"{raw!r} produced {reading.amount}"
        assert reading.unit is not None and reading.unit.code == unit


def test_corpus_split_rate(lexicon: UnitLexicon) -> None:
    """The headline number: how much of a real list comes out right.

    Asserted as a floor rather than an exact value so that improving the parser
    is not a test failure, and so the number in the change report is one this
    suite actually measures.
    """
    correct = 0
    for raw, amount, unit, label in CORPUS:
        reading = parse_shopping_line(raw, lexicon)
        quantity_ok = (
            reading.amount is None and reading.unit is None
            if amount is None
            else reading.amount == Decimal(amount)
            and reading.unit is not None
            and reading.unit.code == unit
        )
        if quantity_ok and reading.label == label:
            correct += 1

    rate = correct / len(CORPUS)
    assert rate >= 0.95, f"only {correct}/{len(CORPUS)} lines parsed as expected"


# --------------------------------------------------------------------------- #
# The limits, written down
# --------------------------------------------------------------------------- #

#: Lines the parser gets wrong, and what it does instead. Each is a deliberate
#: trade rather than an oversight, and each is a starting point for whoever
#: improves it. They are asserted so a future change that fixes one has to say so
#: here rather than leave the documentation stale.
KNOWN_FAILURES: tuple[tuple[str, str], ...] = (
    # An enumerated list reads its numbering as quantities. Distinguishing "2."
    # from "2" needs the shape of the whole document, which this parser, working
    # one line at a time, does not have.
    ("2. tomates", "tomates"),
    # A container is not a unit, so its count lands on the label. Keeping the word
    # beats dropping it, and "boîte" belongs in a unit table nobody has written.
    ("3 paquets de pâtes", "paquets de pâtes"),
    # Two items on one line stay one item. Splitting on "et" would break "sel et
    # poivre" and every product whose name contains it.
    ("pain et lait", "pain et lait"),
)


@pytest.mark.parametrize(("raw", "label"), KNOWN_FAILURES)
def test_known_failures_are_still_what_we_think_they_are(
    raw: str, label: str, lexicon: UnitLexicon
) -> None:
    assert parse_shopping_line(raw, lexicon).label == label


def test_a_long_line_is_not_claimed_as_a_product(lexicon: UnitLexicon) -> None:
    """Prose keeps its whole text as the label, for the caller to mark unparsed."""
    raw = "ne pas oublier de demander à Marie ce qu'elle veut pour samedi"

    reading = parse_shopping_line(raw, lexicon)

    assert reading.amount is None
    assert len(reading.label.split()) > 3, "this line must stay out of the parsed set"


def test_a_unit_absent_from_the_table_is_not_resolved() -> None:
    """The table decides which units exist; the synonym map only spells them.

    Built without ``tbsp``, "cuillère à soupe" resolves to nothing rather than to
    an invented unit -- which is what keeps the composite foreign key on
    ``(code, dimension)`` satisfiable by construction.
    """
    without_tbsp = UnitLexicon(
        Unit(code=code, dimension=dimension, symbol=symbol)
        for code, dimension, symbol in _SEEDED
        if code != "tbsp"
    )

    assert without_tbsp.resolve_spelling("cuillère à soupe") is None
    assert without_tbsp.resolve_spelling("kg") is not None


def test_a_quantity_with_no_product_is_not_turned_into_an_item(lexicon: UnitLexicon) -> None:
    reading = parse_shopping_line("2 kg", lexicon)

    assert reading.amount is None, "a bare quantity has no product to attach to"
    assert reading.label == "2 kg"
