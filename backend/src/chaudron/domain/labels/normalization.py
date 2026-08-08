"""Turning a till-receipt line into something two forms can be compared on.

A receipt line is not a product name. ``BEUR.PLAQ.PRESID.DX 82%MG 500G`` carries a
designation, a brand, a fat content and a net weight in one 30-character field, and
it does so differently at every chain. Everything downstream -- expansion, trigram
blocking, full-text search -- needs those parts separated first.

Two forms come out of here and they are deliberately not the same string:

``comparison``
    Lower-cased, accents stripped, whitespace collapsed. This is what the lexicon is
    keyed on and what a matcher should hand to ``pg_trgm``. It is lossy on purpose.

``display``
    The line as printed, minus the noise a cashier's printer added (leading ``*``,
    Auchan's ``..`` truncation marker, run-together spaces). Accents and case are
    left alone, because we cannot restore what an uppercase unaccented printer threw
    away and inventing them produces ``PRESID`` -> ``preside``, not ``Président``.

Mixing the two is the classic way to show a user a mangled name. They are separate
fields on :class:`NormalizedLabel` and neither is derived from the other at call
sites.

Pure functions, no I/O, no state.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "NormalizedLabel",
    "Quantity",
    "QuantityKind",
    "TruncationHint",
    "normalize_label",
    "to_comparison_form",
    "to_display_form",
]


class QuantityKind(StrEnum):
    """What a quantity token measures."""

    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"
    PERCENTAGE = "percentage"


class TruncationHint(StrEnum):
    """How confident we are that the printer cut the designation short.

    ``MARKER`` is a fact -- the chain printed an explicit ellipsis. ``WIDTH`` is an
    inference from the line reaching the chain's known column count, which is a good
    but not certain signal. ``NONE`` means we saw no reason to suspect truncation,
    not that the line is complete: an 18-character label that happens to end on a
    word boundary is indistinguishable from a whole one.
    """

    NONE = "none"
    MARKER = "marker"
    WIDTH = "width"


@dataclass(frozen=True, slots=True)
class Quantity:
    """A quantity lifted out of a label.

    ``value`` is expressed in ``unit`` (grams, millilitres, or ``None`` for counts and
    percentages) so that ``1KG`` and ``1000G`` compare equal. ``pack_count`` carries
    the multiplier of a multipack: ``6X25CL`` is six units of 250 ml, not 1.5 litres,
    and collapsing it to a total would lose the shape of the pack.
    """

    kind: QuantityKind
    value: Decimal
    unit: str | None
    pack_count: int | None
    source: str


@dataclass(frozen=True, slots=True)
class NormalizedLabel:
    """A receipt line split into the pieces the rest of the pipeline needs."""

    raw: str
    display: str
    comparison: str
    designation: str
    words: tuple[str, ...]
    quantities: tuple[Quantity, ...]
    truncation: TruncationHint


# Chains that print a fixed-width designation column. The value is the width at which
# a line is considered cut; a designation reaching it is flagged ``WIDTH``.
#
# These come from counting characters on real receipts (see docs/label-lexicon.md for
# the provenance of each). They are a heuristic, not a published specification -- no
# chain documents its truncation policy.
_RETAILER_WIDTHS: Final[dict[str, int]] = {
    "carrefour": 18,
    "carrefour-market": 18,
    "monoprix": 18,
    "auchan": 19,
    "intermarche": 20,
    "lidl": 20,
    "leclerc": 31,
    "super-u": 30,
    "u-express": 30,
}

# Markers a chain appends when it cut the designation. Auchan prints "..", and OCR of
# a thermal receipt readily turns that into a single-character ellipsis.
_TRUNCATION_MARKERS: Final[tuple[str, ...]] = ("..", "…")

# Leading glyphs that mean something to the cashier and nothing to us: Auchan marks
# food-rate VAT lines with "*", some chains use ">" or ">>>" for department headers.
_LEADING_NOISE: Final = re.compile(r"^[*>\s\-]+")

_WHITESPACE: Final = re.compile(r"\s+")

# U+2019 RIGHT SINGLE QUOTATION MARK, what a phone keyboard produces and a till never
# does. Spelled by codepoint so that no editor silently normalises it away.
_TYPOGRAPHIC_APOSTROPHE: Final = "\u2019"

# Punctuation that carries no meaning at the edge of a word. The slash is absent on
# purpose: a trailing slash is how Intermarché prints a cut compound (``BISC.CITR/``)
# and the expander uses it.
_WORD_EDGE: Final = ".,&+-'!?:;()"

# ``1/2`` (demi) and ``3/4`` are read as a unit, never as a division or a date. Giving
# them their own token keeps ``1/2ECREM`` from tokenising as ``1`` + ``2ecrem``.
_FRACTIONS: Final = re.compile(r"(?<![\d/])([13])/([24])(?![\d/])")

_NUMBER: Final = r"\d+(?:[.,]\d+)?"
_MASS_UNITS: Final[dict[str, Decimal]] = {
    "mg": Decimal("0.001"),
    "g": Decimal(1),
    "gr": Decimal(1),
    "grs": Decimal(1),
    "kg": Decimal(1000),
}
_VOLUME_UNITS: Final[dict[str, Decimal]] = {
    "ml": Decimal(1),
    "cl": Decimal(10),
    "dl": Decimal(100),
    "l": Decimal(1000),
}
_UNIT_ALTERNATION: Final = "kg|grs|gr|g|mg|ml|cl|dl|l"

# Order matters: the multipack forms must be tried before the plain one, or ``6X25CL``
# is read as a bare ``25CL`` and the pack is lost.
_PACK_THEN_SIZE: Final = re.compile(
    rf"\b(?P<pack>\d+)\s*x\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT_ALTERNATION})\b"
)
_SIZE_THEN_PACK: Final = re.compile(
    rf"\b(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT_ALTERNATION})\s*x\s*(?P<pack>\d+)\b"
)
_PLAIN_SIZE: Final = re.compile(
    rf"(?<![a-z0-9])(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT_ALTERNATION})(?![a-z0-9])"
)
# ``X6``, ``X20ST``: a leading x binds the count, and the optional ``ST`` says the
# units are sachets or sticks.
_LEADING_COUNT: Final = re.compile(r"(?<![a-z0-9])x\s*(?P<count>\d+)\s*(?P<sachet>st)?(?![a-z0-9])")
# ``6ST``: sachets/sticks, attested at Carrefour and Super U.
_SACHET_COUNT: Final = re.compile(r"(?<![a-z0-9])(?P<count>\d+)\s*st(?![a-z0-9])")
_PERCENTAGE: Final = re.compile(rf"(?<![a-z0-9])(?P<value>{_NUMBER})\s*%")

# ``4SACH``, ``20SACHETS``, ``18DELICE``: a count glued to a word. Applied only to what
# survives quantity extraction, so ``500G`` and ``6X25CL`` are long gone by then and
# no unit is ever torn off its number.
_DIGIT_LETTER_BOUNDARY: Final = re.compile(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)")


def to_display_form(raw: str) -> str:
    """The label as it should be shown back to a human.

    Case and accents survive untouched. Only printer artefacts go: the leading VAT
    star, the trailing truncation marker, doubled spaces.
    """
    text = _LEADING_NOISE.sub("", raw)
    for marker in _TRUNCATION_MARKERS:
        if text.rstrip().endswith(marker):
            text = text.rstrip()[: -len(marker)]
    return _WHITESPACE.sub(" ", text).strip()


def to_comparison_form(raw: str) -> str:
    """The lossy form the lexicon and any similarity engine are keyed on.

    Accents are removed because a thermal printer already removed them at most chains
    -- ``MANGUES SÉCHÉES`` and ``MANGUES SECHEES`` are the same line printed by two
    different tills, and a lexicon that distinguishes them is a lexicon that misses
    half its hits.
    """
    decomposed = unicodedata.normalize("NFD", raw)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = unicodedata.normalize("NFC", stripped).lower()
    # Typographic apostrophes and the quote character Monoprix uses as a separator
    # (BIO"ENDIVE) both behave as word boundaries.
    lowered = lowered.replace(_TYPOGRAPHIC_APOSTROPHE, "'").replace('"', " ")
    return _WHITESPACE.sub(" ", lowered).strip()


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "."))


def _extract(
    text: str,
    pattern: re.Pattern[str],
    build: Callable[[re.Match[str]], Quantity | None],
) -> tuple[str, list[Quantity]]:
    """Replace every match of ``pattern`` by a space, collecting what it described."""
    found: list[Quantity] = []

    def _replace(match: re.Match[str]) -> str:
        quantity = build(match)
        if quantity is not None:
            found.append(quantity)
        return " "

    return pattern.sub(_replace, text), found


def _mass_or_volume(value: Decimal, unit: str, pack: int | None, source: str) -> Quantity | None:
    if unit in _MASS_UNITS:
        return Quantity(
            kind=QuantityKind.MASS,
            value=value * _MASS_UNITS[unit],
            unit="g",
            pack_count=pack,
            source=source,
        )
    if unit in _VOLUME_UNITS:
        return Quantity(
            kind=QuantityKind.VOLUME,
            value=value * _VOLUME_UNITS[unit],
            unit="ml",
            pack_count=pack,
            source=source,
        )
    return None


def _extract_quantities(text: str) -> tuple[str, tuple[Quantity, ...]]:
    quantities: list[Quantity] = []

    text, found = _extract(
        text,
        _PACK_THEN_SIZE,
        lambda m: _mass_or_volume(_decimal(m["value"]), m["unit"], int(m["pack"]), m.group(0)),
    )
    quantities += found

    text, found = _extract(
        text,
        _SIZE_THEN_PACK,
        lambda m: _mass_or_volume(_decimal(m["value"]), m["unit"], int(m["pack"]), m.group(0)),
    )
    quantities += found

    text, found = _extract(
        text,
        _PLAIN_SIZE,
        lambda m: _mass_or_volume(_decimal(m["value"]), m["unit"], None, m.group(0)),
    )
    quantities += found

    text, found = _extract(
        text,
        _PERCENTAGE,
        lambda m: Quantity(
            kind=QuantityKind.PERCENTAGE,
            value=_decimal(m["value"]),
            unit=None,
            pack_count=None,
            source=m.group(0),
        ),
    )
    quantities += found

    text, found = _extract(
        text,
        _SACHET_COUNT,
        lambda m: Quantity(
            kind=QuantityKind.COUNT,
            value=Decimal(m["count"]),
            unit="sachet",
            pack_count=None,
            source=m.group(0),
        ),
    )
    quantities += found

    text, found = _extract(
        text,
        _LEADING_COUNT,
        lambda m: Quantity(
            kind=QuantityKind.COUNT,
            value=Decimal(m["count"]),
            unit="sachet" if m["sachet"] else None,
            pack_count=None,
            source=m.group(0),
        ),
    )
    quantities += found

    return _WHITESPACE.sub(" ", text).strip(), tuple(quantities)


def _detect_truncation(raw: str, display: str, retailer: str | None) -> TruncationHint:
    stripped = raw.rstrip()
    if any(stripped.endswith(marker) for marker in _TRUNCATION_MARKERS):
        return TruncationHint.MARKER
    width = _RETAILER_WIDTHS.get(retailer or "")
    if width is not None and len(display) >= width:
        return TruncationHint.WIDTH
    return TruncationHint.NONE


def normalize_label(raw: str, *, retailer: str | None = None) -> NormalizedLabel:
    """Split a raw receipt line into display form, comparison form and quantities.

    ``retailer`` is a slug (``"carrefour"``, ``"super-u"``...). It is only used to
    decide whether the line looks cut off; passing ``None`` costs nothing but the
    truncation signal.
    """
    display = to_display_form(raw)
    comparison = to_comparison_form(display)
    spaced = _FRACTIONS.sub(r" \1/\2 ", comparison)
    designation, quantities = _extract_quantities(spaced)
    designation = _DIGIT_LETTER_BOUNDARY.sub(" ", designation)
    # A bare number left after quantity extraction is a grade, a lot size or an OCR
    # artefact; either way it is not part of the designation. Edge punctuation goes
    # too: ``BOND.`` and ``BOND`` are one form, and keeping the printer's abbreviation
    # dot on the outside of a word would make the lexicon carry both spellings.
    words = tuple(
        stripped
        for stripped in (word.strip(_WORD_EDGE) for word in designation.split(" "))
        if stripped and not stripped.isdigit()
    )
    return NormalizedLabel(
        raw=raw,
        display=display,
        comparison=comparison,
        designation=" ".join(words),
        words=words,
        quantities=quantities,
        truncation=_detect_truncation(raw, display, retailer),
    )
