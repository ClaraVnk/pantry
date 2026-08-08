"""Reading a drive order recap out of the text a PDF already contains.

A photographed till receipt has to go through a vision model. **A drive order
recap does not**, and that difference is worth a module.

The PDF a drive sends carries selectable text. Extracting it costs one call to
``pypdf`` -- no provider, no token, no bill for the household -- and it is
*more* accurate than any transcription, because nothing generated it: the
characters are the ones the retailer wrote. ``docs/technical-notes-ingestion.md``
section 3.4 measures the alternative on 10 656 receipts: a vision model peaks at
0.49 F1 on line items and, worse, "fabricates or alters lines to force their sum
onto the printed total". A parser cannot do that. It can only fail to read a
line, which is a failure the review screen shows.

So the ordering is not an optimisation, it is the accuracy decision: text first,
and a model only for what has no text.

What this parser refuses to do
------------------------------

Every rule below is conservative in one direction: **it would rather return
nothing than return a number it is not sure of.** A missing quantity costs the
reviewer one field to type. A wrong quantity is a wrong stock, and the whole
point of contract 6ter is that a silently wrong inventory is worse than an empty
one.

Concretely: a trailing figure with no decimal part is never read as a price, a
figure with no recognised unit is never read as a mass, the merchant is ``None``
unless it matches a known chain rather than guessed from the first line, and the
currency is ``None`` unless a symbol or an ISO code is actually printed.

What is *not* covered, and is therefore left to the human
--------------------------------------------------------

``docs/technical-notes-ingestion.md`` section 3.3 lists the French specifics no
public source documents: variable-weight items, discount lines printed negative,
"2+1 free" bundles, loyalty deductions, multi-rate VAT summaries, packaging
deposits. A negative amount is read as a line like any other and left for review;
none of the others is modelled. The total is taken from the printed total and
never recomputed, so a promotion this parser did not understand changes what the
lines say and never what the budget counts.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Final

from chaudron.domain.llm_ports import ParsedReceipt, ReceiptLine

__all__ = [
    "RETAILER_SLUGS",
    "looks_like_receipt_text",
    "parse_receipt_text",
    "retailer_slug",
]

#: Money, French or English decimal, always with two decimal places. The two
#: places are the whole of what separates a price from a quantity, a weight, an
#: EAN and a page number, so the rule is not relaxed anywhere below.
_AMOUNT: Final = r"-?\d{1,6}[.,]\d{2}"

#: A price at the end of a line, with the currency and the VAT letter French
#: chains print after it ("12,40 € A", "3,20 B").
_TRAILING_TOTAL: Final = re.compile(
    rf"(?P<amount>{_AMOUNT})\s*(?:€|EUR)?\s*(?:[A-Z]\b)?\s*$", re.IGNORECASE
)

#: A quantity with its unit. A bare number is never one: on a receipt the same
#: digits are as likely to be a pack size, a till number or a queue position.
_MEASURE: Final = r"(?P<qty>\d{1,5}(?:[.,]\d{1,3})?)\s*(?P<unit>kg|g|cl|ml|l)\b"

#: The units a per-unit price can be quoted in, after the slash.
_PER: Final = r"kg|g|l|cl|ml|pce|u|pi[eè]ce"

# The four price-column forms, in the order they are tried. Order is the whole
# design: each one is *more specific* than the next, and trying the loose ones
# first is how a count ends up read as a weight.

#: ``2 x 1,15`` -- the ordinary multiple. A count and what one costs.
_COUNT_TIMES_PRICE: Final = re.compile(
    rf"(?<![\d.,])(?P<count>\d{{1,3}})\s*[x*]\s*(?P<price>{_AMOUNT})\s*$", re.IGNORECASE
)

#: ``0,864 kg x 1,89 €/kg`` -- the variable-weight line, and the only form that
#: gives both a weight and its rate. ADR-0008 already established that these
#: carry store-internal barcodes that no public catalogue holds, so the label is
#: all there will ever be to go on.
_MEASURE_TIMES_PRICE: Final = re.compile(
    rf"{_MEASURE}\s*(?:[x*]\s*)?(?P<price>{_AMOUNT})\s*(?:€|EUR)?\s*(?:/\s*(?:{_PER}))?\s*$",
    re.IGNORECASE,
)

#: A per-unit price with no quantity beside it. Recognised only when the line
#: says what it is *per* -- ``x 3,20`` or ``1,89 €/kg``. Without one of those
#: markers a second figure is just a figure, and reading it as a unit price is
#: exactly the guess this module refuses to make.
_UNIT_PRICE_ALONE: Final = re.compile(
    rf"(?:[x*]\s*(?P<x>{_AMOUNT})|(?P<per>{_AMOUNT})\s*(?:€|EUR)?\s*/\s*(?:{_PER}))\s*$",
    re.IGNORECASE,
)

#: ``PDT NOUV 1KG`` -- a size printed as part of the designation. Read as the
#: quantity when nothing else supplied one, and **left in the label**: it is what
#: the chain printed, and ``domain/labels`` extracts quantities from designations
#: itself. Removing it here would take the size off "LAIT 1L" and hand the
#: catalogue a label the receipt never carried.
_MEASURE_ALONE: Final = re.compile(rf"{_MEASURE}\s*$", re.IGNORECASE)

#: ``CRQ MONSIEUR X4`` -- a pack count, also part of the designation and also left
#: in the label.
_TRAILING_COUNT: Final = re.compile(r"[x*]\s*(?P<count>\d{1,3})\s*$", re.IGNORECASE)

#: ``2 COCA COLA 1,5L`` -- a count *column*, printed to the left of the
#: designation rather than inside it, so this one is removed. Tried **before**
#: the designation size: two bottles of 1,5 L is three litres, and reading the
#: size instead would put a third of the purchase in the cupboard.
#:
#: Bounded to three digits, required to be followed by a letter so a date or a
#: barcode cannot become a quantity, and refused when that letter is a unit --
#: ``500 G FARINE`` is a weight column, not five hundred of anything.
#:
#: The residual false positive is a name that starts with a number: ``3 FROMAGES``
#: reads as three of something called "fromages". Accepted, because the reviewer
#: sees it and because refusing the rule would lose the common case to protect
#: the rare one.
_LEADING_COUNT: Final = re.compile(
    rf"^(?P<count>\d{{1,3}})\s+(?!(?:{_PER})\b)(?=[^\d\s])", re.IGNORECASE
)

#: The printed total. ``TOTAL`` alone is not enough -- a receipt also prints
#: ``TOTAL TVA``, ``SOUS-TOTAL`` and ``TOTAL ARTICLES``, and taking the wrong one
#: puts the VAT in the budget. Each alternative below is anchored on the words
#: that mean *what the customer paid*.
_TOTAL_LINE: Final = re.compile(
    r"^(?:"
    r"total(?:\s+(?:ttc|a\s+payer|à\s+payer|de\s+la\s+commande|commande|panier))?"
    r"|net\s+a\s+payer|net\s+à\s+payer"
    r"|montant(?:\s+(?:total|du|dû|a\s+payer|à\s+payer))?"
    r"|somme\s+due"
    r")\b",
    re.IGNORECASE,
)

#: Words that disqualify a line from being the total even when it starts with one
#: of the forms above.
_NOT_A_TOTAL: Final = re.compile(r"\b(?:tva|sous[-\s]?total|articles?|points?|remise)\b", re.I)

#: Lines that are receipt furniture rather than purchases. Matched on the *start*
#: of the line, folded and accent-free, so "T.V.A. 5,5%" and "tva 5,5 %" are one
#: rule.
_NOT_A_PURCHASE: Final = tuple(
    sorted(
        {
            "total",
            "soustotal",
            "sous total",
            "net a payer",
            "montant",
            "somme due",
            "tva",
            "dont tva",
            "taux",
            "cb",
            "carte bancaire",
            "carte bleue",
            "especes",
            "cheque",
            "ticket restaurant",
            "titre restaurant",
            "rendu",
            "monnaie",
            "reglement",
            "paiement",
            "remise",
            "avantage",
            "fidelite",
            "carte fidelite",
            "points",
            "cagnotte",
            "nombre d articles",
            "nb articles",
            "articles",
            "siret",
            "siren",
            "tel",
            "telephone",
            "merci",
            "a bientot",
            "au revoir",
            "facture",
            "commande",
            "n commande",
            "retrait",
            "creneau",
            "date",
            "heure",
            "caisse",
            "vendeur",
            "magasin",
            "adresse",
            "sac",
            "frais de service",
            "frais de preparation",
        }
    )
)

#: ``dd/mm/yyyy``, ``dd-mm-yy``, ``dd.mm.yyyy``. Day first, because every French
#: receipt is, and month-first would silently move a purchase by up to eleven
#: months rather than fail.
_DATE: Final = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})\b")

_TIME: Final = re.compile(r"\b([01]?\d|2[0-3])[:hH]([0-5]\d)\b")

#: Chains whose printed name maps onto a ``domain/labels`` retailer slug. Only
#: these produce a retailer: the lexicon is scoped per chain, and attaching the
#: wrong chain to a label applies the wrong abbreviations -- which is the one
#: failure mode ``domain/labels`` is built to avoid.
RETAILER_SLUGS: Final[tuple[tuple[str, str], ...]] = (
    ("carrefour market", "carrefour-market"),
    ("carrefour contact", "carrefour-market"),
    ("carrefour", "carrefour"),
    ("e leclerc", "leclerc"),
    ("eleclerc", "leclerc"),
    ("leclerc", "leclerc"),
    ("intermarche", "intermarche"),
    ("netto", "intermarche"),
    ("auchan", "auchan"),
    ("hyper u", "hyper-u"),
    ("super u", "super-u"),
    ("u express", "u-express"),
    ("utile", "utile"),
    ("monoprix", "monoprix"),
)

#: Below this many characters, "the PDF has text" is a font descriptor and a page
#: number rather than a receipt, and the vision path is the honest answer.
_MIN_USEFUL_CHARS: Final = 40

#: How long a label may be. ``receipt_line.raw_label`` is ``text``, so this is a
#: readability bound rather than a schema one -- and the bound that keeps a
#: single line from outweighing everything around it (AUD-006).
MAX_RAW_LABEL: Final = 120

#: A line shorter than this, once stripped, is a stray character from a table
#: border rather than a product.
_MIN_LABEL_CHARS: Final = 2

#: Largest amount accepted from any source. Rejects a decimal that is really a
#: card number caught by the price pattern, and bounds ``numeric(12,2)``.
_MAX_AMOUNT: Final = Decimal("100000")

_CENTS: Final = Decimal("0.01")
_QUANTUM: Final = Decimal("0.001")

#: ``unit.code`` values the measures above map onto. Only units the reference
#: table really has; the service re-resolves each one against ``unit`` anyway, so
#: a code missing from a given instance drops the quantity rather than the line.
_MEASURE_UNITS: Final[dict[str, str]] = {
    "kg": "kg",
    "g": "g",
    "l": "l",
    "cl": "cl",
    "ml": "ml",
}

_COUNT_UNIT: Final = "piece"

# The two character sets below suppress the ambiguous-character rule, and the
# characters are the point. A French receipt really does print a NO-BREAK SPACE
# before the currency sign -- it is the typographic rule, not a stray byte -- and
# a PDF exporter really does emit MIDDLE DOT and HORIZONTAL ELLIPSIS as dot
# leaders and EN DASH as a separator. Replacing them with their ASCII look-alikes
# would mean not stripping the characters this parser exists to strip.

#: Trailing filler between a label and its price: dot leaders and the space that
#: precedes a currency sign.
_TRAILING_FILLER: Final = " .·… "  # noqa: RUF001

#: Everything a label may be wrapped in once the numbers are off it.
_LABEL_EDGES: Final = " .·…-–—:;  "  # noqa: RUF001


def _fold(value: str) -> str:
    """Lowercase, accent-free, punctuation collapsed to single spaces."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    kept = [
        char if char.isalnum() else " " for char in decomposed if unicodedata.category(char) != "Mn"
    ]
    return " ".join("".join(kept).split())


def _amount(raw: str, *, quantum: Decimal) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    if abs(value) > _MAX_AMOUNT:
        return None
    return value.quantize(quantum)


def retailer_slug(merchant: str | None) -> str | None:
    """The ``domain/labels`` slug for a printed merchant name, or ``None``.

    ``None`` is a normal answer and costs only the chain-specific half of the
    lexicon. Guessing a chain would cost precision on every label of the receipt.
    """
    if not merchant:
        return None
    folded = _fold(merchant)
    for needle, slug in RETAILER_SLUGS:
        if needle in folded:
            return slug
    return None


def looks_like_receipt_text(text: str) -> bool:
    """Whether extracted text is worth parsing at all.

    The gate between "this PDF carries its own text" and "this PDF is a scan
    wrapped around an image". A scan extracts to a handful of characters or to
    nothing, and handing that to the parser would produce an empty proposal that
    blames the user for a document the parser simply could not read.
    """
    stripped = "".join(text.split())
    if len(stripped) < _MIN_USEFUL_CHARS:
        return False
    return any(char.isalpha() for char in stripped)


def _find_total(lines: list[str]) -> Decimal | None:
    """The printed total, from the last line that claims to be one.

    The *last*, because a drive recap prints an order summary at the top and the
    amount actually charged at the bottom, and the bottom one is the one the card
    was debited for.
    """
    found: Decimal | None = None
    for line in lines:
        folded = _fold(line)
        if not _TOTAL_LINE.match(folded) or _NOT_A_TOTAL.search(folded):
            continue
        match = _TRAILING_TOTAL.search(line)
        if match is None:
            continue
        value = _amount(match.group("amount"), quantum=_CENTS)
        if value is not None and value > 0:
            found = value
    return found


def _find_currency(text: str) -> str | None:
    """The currency, only when it is printed. Never defaulted to EUR.

    A receipt in a currency we assumed would be added to a total in another one,
    and contract 6ter forbids adding two currencies for reasons no default can
    repair.
    """
    if "€" in text:
        return "EUR"
    if re.search(r"\bEUR\b", text):
        return "EUR"
    if re.search(r"\bCHF\b", text):
        return "CHF"
    return None


def _find_purchased_at(text: str) -> dt.datetime | None:
    """The first plausible date on the document, with its time when one follows.

    Naive on purpose: the receipt prints a wall clock, and the household's zone
    is applied by the caller. Pretending it is UTC would move an evening shop to
    the next day for a fifth of Europe.
    """
    match = _DATE.search(text)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        day_value = dt.date(year, month, day)
    except ValueError:
        return None
    tail = text[match.end() : match.end() + 20]
    clock = _TIME.search(tail)
    if clock is None:
        return dt.datetime.combine(day_value, dt.time())
    return dt.datetime.combine(day_value, dt.time(int(clock.group(1)), int(clock.group(2))))


def _find_merchant(lines: list[str]) -> str | None:
    """The chain, when its name is printed anywhere. Otherwise nothing.

    Deliberately not "the first line of the document": on a drive recap the first
    line is as often the customer's own address, and putting a household's street
    in a merchant column is both wrong and a small privacy leak of its own.
    """
    for line in lines:
        folded = _fold(line)
        for needle, _ in RETAILER_SLUGS:
            if needle in folded:
                return line.strip()[:80]
    return None


def _read_price_columns(rest: str) -> tuple[str, Decimal | None, str | None, Decimal | None]:
    """Peel the price columns off the right of a line.

    Returns what is left, the quantity, its unit code and the unit price. Only
    the *columns* are removed. A size or a pack count printed inside the
    designation -- ``PDT NOUV 1KG``, ``CRQ MONSIEUR X4`` -- yields a quantity and
    stays in the label, because it is what the chain printed and because
    ``domain/labels`` reads quantities out of designations itself.

    The four forms are tried most specific first. Trying the loose ones first is
    how ``LT DEM 1L 2 x 1,15`` ends up stocked as one litre instead of two
    bricks.
    """
    match = _COUNT_TIMES_PRICE.search(rest)
    if match is not None:
        return (
            rest[: match.start()].rstrip(_TRAILING_FILLER),
            Decimal(match.group("count")),
            _COUNT_UNIT,
            _amount(match.group("price"), quantum=_CENTS),
        )

    match = _MEASURE_TIMES_PRICE.search(rest)
    if match is not None:
        quantity = _amount(match.group("qty"), quantum=_QUANTUM)
        unit = _MEASURE_UNITS.get(match.group("unit").lower())
        if quantity is not None and unit is not None:
            return (
                rest[: match.start()].rstrip(_TRAILING_FILLER),
                quantity,
                unit,
                _amount(match.group("price"), quantum=_CENTS),
            )

    match = _UNIT_PRICE_ALONE.search(rest)
    if match is not None:
        unit_price = _amount(match.group("x") or match.group("per"), quantum=_CENTS)
        rest = rest[: match.start()].rstrip(_TRAILING_FILLER)
        # A lone multiplication sign can survive the cut ("BANANES 0,864 kg x"),
        # and it would otherwise block the measure rule below.
        rest = rest.rstrip("xX*").rstrip(_TRAILING_FILLER)
        measure = _MEASURE_ALONE.search(rest)
        if measure is not None:
            quantity = _amount(measure.group("qty"), quantum=_QUANTUM)
            unit = _MEASURE_UNITS.get(measure.group("unit").lower())
            if quantity is not None and unit is not None:
                return rest[: measure.start()].rstrip(_TRAILING_FILLER), quantity, unit, unit_price
        return rest, None, None, unit_price

    # Before the designation size, and the order is load-bearing: "2 COCA COLA
    # 1,5L" is two bottles, and reading the 1,5L instead would stock a third of
    # what was bought.
    leading = _LEADING_COUNT.match(rest)
    if leading is not None:
        return rest[leading.end() :], Decimal(leading.group("count")), _COUNT_UNIT, None

    measure = _MEASURE_ALONE.search(rest)
    if measure is not None:
        quantity = _amount(measure.group("qty"), quantum=_QUANTUM)
        unit = _MEASURE_UNITS.get(measure.group("unit").lower())
        if quantity is not None and unit is not None:
            # Kept in the label. See the docstring.
            return rest, quantity, unit, None

    count = _TRAILING_COUNT.search(rest)
    if count is not None:
        return rest, Decimal(count.group("count")), _COUNT_UNIT, None

    return rest, None, None, None


def _read_line(raw: str) -> ReceiptLine | None:
    """One printed line, or ``None`` when it is not a purchase.

    The trailing price is what makes a line a purchase: a receipt prints one on
    every article and on nothing else that matters here.
    """
    text = " ".join(raw.split())
    if not text:
        return None

    folded = _fold(text)
    if not folded or not any(char.isalpha() for char in folded):
        return None
    if any(folded.startswith(prefix) for prefix in _NOT_A_PURCHASE):
        return None

    total_match = _TRAILING_TOTAL.search(text)
    if total_match is None:
        return None
    total_price = _amount(total_match.group("amount"), quantum=_CENTS)
    if total_price is None:
        return None

    rest, quantity, unit, unit_price = _read_price_columns(
        text[: total_match.start()].rstrip(_TRAILING_FILLER)
    )

    label = rest.strip(_LABEL_EDGES)[:MAX_RAW_LABEL].strip()
    if len(label) < _MIN_LABEL_CHARS or not any(char.isalpha() for char in label):
        # A price with no name in front of it. It is a subtotal, a VAT row or a
        # column header that survived extraction, and inventing a label for it
        # would put an unnamed lot in somebody's cupboard.
        return None
    if quantity is not None and quantity <= 0:
        quantity, unit = None, None

    return ReceiptLine(
        label=label,
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        total_price=total_price,
    )


def parse_receipt_text(text: str, *, max_lines: int) -> tuple[ParsedReceipt, bool]:
    """Read extracted PDF text into the same shape a vision model would return.

    Returns the reading and whether the document held more candidate lines than
    ``max_lines``. Both paths land on :class:`ParsedReceipt` so that everything
    downstream -- label expansion, the arithmetic comparison, the review screen --
    exists once rather than twice.
    """
    raw_lines = [line for line in text.splitlines() if line.strip()]
    read: list[ReceiptLine] = []
    truncated = False
    for raw in raw_lines:
        line = _read_line(raw)
        if line is None:
            continue
        if len(read) >= max_lines:
            truncated = True
            break
        read.append(line)

    purchased = _find_purchased_at(text)
    merchant = _find_merchant(raw_lines)
    return (
        ParsedReceipt(
            lines=tuple(read),
            merchant=merchant,
            purchased_on=None if purchased is None else purchased.date(),
            currency=_find_currency(text),
            total=_find_total(raw_lines),
        ),
        truncated,
    )
