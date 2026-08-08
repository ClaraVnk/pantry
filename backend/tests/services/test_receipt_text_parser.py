"""What the deterministic receipt-text reader gets right, and what it refuses.

These are unit tests over a pure function, and they exist because the *number*
matters: ``docs/technical-notes-ingestion.md`` section 3.4 measures vision models
at 0.49 F1 on line items, so the claim that a drive recap should be read from its
text rather than from a picture of itself is only worth making if the text reader
actually reads. Everything below runs with no database, no provider and no
network.

Two groups, and the second is the important one. The first checks the forms a
French receipt prints. The second checks the *refusals*: every case where the
parser could guess and does not. A missing quantity costs the reviewer one field;
a wrong one is a wrong cupboard.

**What these tests do not prove.** The fixtures are written here, from the layout
French chains use, not extracted from real receipts -- no public corpus of French
till receipts exists (section 3.2: "aucun dataset public de tickets français").
So this measures the parser against the format as documented, and says nothing
about the share of real drive recaps it reads correctly. That number is unknown
and is not claimed anywhere.
"""

from __future__ import annotations

from decimal import Decimal

from chaudron.services.receipt_text import (
    looks_like_receipt_text,
    parse_receipt_text,
    retailer_slug,
)

# A drive order recap, in the shape those PDFs extract to: a designation column,
# optional quantity and unit-price columns, a line total on the right.
RECAP = """SUPER U CHAMPTOCEAUX
12 rue du Marche
Commande drive n 4471829
Retrait le 02/08/2026 a 10h30

LT DEM 1/2 ECR 1L            2 x 1,15          2,30
PDT NOUV 1KG                                   2,49
BANANES                 0,864 kg x 1,89 /kg    1,63
CRQ MONSIEUR X4                                3,95
YAOURT NATURE X8                               2,10
SAC REUTILISABLE                               0,50

Nombre d articles : 6
SOUS-TOTAL                                    12,97
Remise fidelite                               -1,00
TOTAL A PAYER                                 11,97
Dont TVA 5,5%                                  0,62
CB EUR                                        11,97
Merci de votre visite
"""


def _lines(text: str) -> dict[str, tuple[object, object, object, object]]:
    parsed, _ = parse_receipt_text(text, max_lines=200)
    return {
        line.label: (line.quantity, line.unit, line.unit_price, line.total_price)
        for line in parsed.lines
    }


# --------------------------------------------------------------------------- #
# The forms a French receipt prints
# --------------------------------------------------------------------------- #


def test_a_multiple_is_read_as_a_count_and_a_unit_price() -> None:
    """``2 x 1,15`` is two bricks at 1,15, not one 1,15-litre anything.

    The order the price-column rules are tried in is what makes this true, and it
    is the one thing in the parser that a tidying refactor would silently break:
    reading ``1L`` first would stock one litre and drop the multiple.
    """
    assert _lines(RECAP)["LT DEM 1/2 ECR 1L"] == (
        Decimal(2),
        "piece",
        Decimal("1.15"),
        Decimal("2.30"),
    )


def test_a_variable_weight_line_keeps_its_weight_and_its_rate() -> None:
    assert _lines(RECAP)["BANANES"] == (
        Decimal("0.864"),
        "kg",
        Decimal("1.89"),
        Decimal("1.63"),
    )


def test_a_size_inside_the_designation_becomes_a_quantity_and_stays_in_the_label() -> None:
    """``PDT NOUV 1KG`` is one kilo *and* is still called ``PDT NOUV 1KG``.

    Removing the size from the label would hand ``domain/labels`` a designation
    the receipt never printed, and that module extracts quantities from
    designations itself.
    """
    assert _lines(RECAP)["PDT NOUV 1KG"] == (Decimal("1.000"), "kg", None, Decimal("2.49"))


def test_a_pack_count_inside_the_designation_becomes_a_quantity() -> None:
    assert _lines(RECAP)["CRQ MONSIEUR X4"] == (Decimal(4), "piece", None, Decimal("3.95"))


def test_a_leading_count_column_is_removed_from_the_label() -> None:
    """``2 COCA COLA`` is a count *column*, unlike ``X4`` which is part of the name."""
    assert _lines("2 COCA COLA 1,5L   3,80")["COCA COLA 1,5L"] == (
        Decimal(2),
        "piece",
        None,
        Decimal("3.80"),
    )


def test_the_printed_total_is_the_one_the_customer_paid() -> None:
    """Not the subtotal, not the VAT line, not the article count.

    The budget rests on this single number (contract 6ter), so picking the wrong
    line here would put the VAT in somebody's monthly spend.
    """
    parsed, _ = parse_receipt_text(RECAP, max_lines=200)
    assert parsed.total == Decimal("11.97")


def test_the_date_the_merchant_and_the_currency_are_read() -> None:
    parsed, _ = parse_receipt_text(RECAP, max_lines=200)
    assert parsed.purchased_on is not None
    assert parsed.purchased_on.isoformat() == "2026-08-02"
    assert parsed.merchant is not None
    assert "SUPER U" in parsed.merchant
    assert parsed.currency == "EUR"


def test_the_merchant_maps_onto_a_lexicon_retailer() -> None:
    """The slug is what scopes ``domain/labels``; a wrong one applies wrong
    abbreviations to every line of the receipt."""
    assert retailer_slug("SUPER U CHAMPTOCEAUX") == "super-u"
    assert retailer_slug("E.LECLERC SAINT-HERBLAIN") == "leclerc"
    assert retailer_slug("CARREFOUR MARKET") == "carrefour-market"
    assert retailer_slug("Épicerie du coin") is None


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #


def test_payment_tax_and_summary_rows_are_not_purchases() -> None:
    """Nothing that is not an article reaches the proposal.

    A ``TOTAL`` row read as a line would put the whole shop in the cupboard once
    more; a ``CB`` row would put the card payment there.
    """
    labels = set(_lines(RECAP))
    assert labels == {
        "LT DEM 1/2 ECR 1L",
        "PDT NOUV 1KG",
        "BANANES",
        "CRQ MONSIEUR X4",
        "YAOURT NATURE X8",
    }


def test_a_line_with_no_price_is_not_a_purchase() -> None:
    assert _lines("PAIN DE CAMPAGNE") == {}


def test_a_bare_trailing_number_is_never_a_price() -> None:
    """Two decimal places are what separate a price from a pack size.

    ``LAIT 1,5`` is a bottle size printed without its unit, and reading it as a
    1,5 EUR line would invent both a purchase and its amount.
    """
    assert _lines("LAIT 1,5") == {}


def test_a_bare_trailing_number_is_never_a_quantity() -> None:
    """``CAFE 250 3,90``: the 250 has no unit, so it stays in the label.

    A gramme would be a guess, and a lot of "250 pieces of coffee" is the kind of
    wrong that survives to the day somebody cooks with it.
    """
    quantity, unit, _, total = _lines("CAFE 250 3,90")["CAFE 250"]
    assert quantity is None
    assert unit is None
    assert total == Decimal("3.90")


def test_a_price_with_no_designation_is_dropped() -> None:
    assert _lines("   12,40") == {}


def test_the_currency_is_never_assumed() -> None:
    """A total with no currency counts as *missing* in contract 6ter, which is
    strictly better than counting it against a currency nobody printed."""
    parsed, _ = parse_receipt_text("MAGASIN\nPAIN  2,00\nTOTAL A PAYER  2,00\n", max_lines=200)
    assert parsed.total == Decimal("2.00")
    assert parsed.currency is None


def test_an_unreadable_date_leaves_the_receipt_undated() -> None:
    parsed, _ = parse_receipt_text("PAIN 2,00\nRetrait le 32/13/2026\n", max_lines=200)
    assert parsed.purchased_on is None


def test_a_negative_line_is_kept_as_printed() -> None:
    """A discount printed negative is a line like any other.

    Dropping it would make the line sum disagree with the total for a reason that
    is nobody's mistake, and the gap between those two numbers is the signal the
    review screen exists to show.
    """
    assert _lines("BON DE REDUCTION  -1,50")["BON DE REDUCTION"][3] == Decimal("-1.50")


def test_more_lines_than_the_ceiling_are_dropped_and_declared() -> None:
    text = "\n".join(f"ARTICLE {index}  1,00" for index in range(10))
    parsed, truncated = parse_receipt_text(text, max_lines=3)
    assert len(parsed.lines) == 3
    assert truncated is True


def test_a_scan_with_no_text_is_recognised_before_it_is_parsed() -> None:
    """The gate between "this PDF carries text" and "this PDF is a picture".

    Without it a scanned recap yields an empty proposal, which blames the user
    for a document the parser could not read rather than saying so.
    """
    assert looks_like_receipt_text("") is False
    assert looks_like_receipt_text("1\n2\n") is False
    assert looks_like_receipt_text(RECAP) is True
