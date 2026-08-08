"""Shopping lists: reading one in, and noticing when something runs out.

Two services, one write path, and that is why they share a module. Both end at
``shopping_list_item``, both go through the same unit resolution, and both are
built on the same refusal to act on the household's behalf: the import returns a
proposal (contract 7.2), the depletion detector returns a proposal (contract
6bis), and in each case a separate, explicit call is what writes.

The parser
----------

A shopping list is already half structured -- one line, one item, sometimes a
quantity -- so a deterministic parser does the work and a model is optional
(contract 7.1). That ordering is not an optimisation. ADR-0007 makes "no provider
configured" a normal state, and a household that has not pasted an API key must
still be able to import its list. The model is a *second* pass over the lines the
parser could not split; its absence lowers quality and blocks nothing, which is
the "emulation with documented loss" case of ADR-0005 rather than the
"explicitly unavailable" one.

Units resolve against the ``unit`` table, never against a list invented here.
What this module adds is *spelling*: the table stores ``tbsp`` with the symbol
``c. à s.``, and a French shopping list writes "cuillère à soupe", "c.à.s" or
"CàS". The synonym map below maps spellings onto codes, and a synonym whose code
is not in the table is dropped when the lexicon is built -- so the vocabulary of
units is the database's, and only the vocabulary of *words* is here.

What the parser cannot do
-------------------------

Matching a free-text label to a catalogue product is the hardest problem in this
domain and it is not solved (``docs/technical-notes-ingestion.md``, decision 9:
"PDT NOUV 1KG" and "Pommes de terre nouvelles" share no trigram, and the real
answer is an abbreviation lexicon per retailer that does not exist yet).
This module therefore matches on **exact normalised equality and nothing else**.
No fuzzy matching, no trigram similarity, no Open Food Facts search. A miss
costs the user one tap in a screen they are already reviewing; a wrong match
silently puts the wrong product on the list, and there is no screen for that.
The failure mode was chosen, not inherited.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Product,
    QuantityDimension,
    ShoppingItemOrigin,
    ShoppingList,
    ShoppingListItem,
    Unit,
)
from chaudron.domain.ports import (
    InvalidQuantityError,
    ProductNotFoundError,
    UnknownUnitError,
)
from chaudron.domain.shopping import (
    AllergenScreen,
    ConfirmedImport,
    ConfirmLine,
    DeclinedRepurchaseStore,
    DepletionEvent,
    DepletionProposal,
    DepletionReason,
    ImportProposal,
    ImportSource,
    ParseConfidence,
    ParsedBy,
    ParsedQuantity,
    ProposedLine,
    ShoppingLineSplitter,
    ShoppingListNotFoundError,
    SplitCandidate,
    is_depletion_reason,
)
from chaudron.infra.untrusted_text import sanitize

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LIST_NAME",
    "MAX_CONFIRM_LINES",
    "MAX_LABEL_LENGTH",
    "DepletionService",
    "ImportBounds",
    "ShoppingImportService",
    "UnitLexicon",
    "parse_shopping_line",
    "resolve_current_list",
]

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Ceiling on one raw line before anything looks at it. Long enough for any real
#: shopping-list entry, short enough that a document made of one 200 kB "line"
#: cannot be a product label. Also the bound that keeps a single line from
#: outweighing the instructions around it once it reaches a prompt (AUD-006).
MAX_LINE_LENGTH: Final = 200

#: Ceiling on a product label written to the database, and on one sent to a
#: model. Shorter than the raw line: a label is what is left after the quantity
#: and the bullet are removed.
MAX_LABEL_LENGTH: Final = 120

#: Words a label may have and still read as a product rather than as a sentence.
#: "pommes de terre" is three; "qqch pour le dessert" is four and is exactly the
#: line the contract shows with ``confidence: none``. Deliberately one blunt rule
#: instead of a list of French function words: a blunt rule can be described in
#: one sentence to a user, and its failures ("filet de poulet fermier" is called
#: unreadable) all fall towards *more* review rather than less.
MAX_LABEL_WORDS: Final = 3

#: Lines handed to the splitter in one import. A model pass is billed to the
#: household, and a 500-line document must not become a 500-line prompt.
MAX_MODEL_LINES: Final = 60

#: Lines accepted by the confirm endpoint. Larger than the import ceiling would
#: be pointless -- the client is sending back a reviewed proposal.
MAX_CONFIRM_LINES: Final = 500

#: Largest amount accepted from any source. Rejects a decimal that is really an
#: EAN caught by the number pattern, and bounds ``numeric(12,3)``.
MAX_AMOUNT: Final = Decimal("100000")

#: numeric(12,3): the precision the schema stores quantities at.
_QUANTUM: Final = Decimal("0.001")

#: Name given to the list created when a household first needs one.
DEFAULT_LIST_NAME: Final = "Courses"

#: ``shopping_list.name`` is ``varchar(120)``.
_LIST_NAME_LENGTH: Final = 120


@dataclass(frozen=True, slots=True)
class ImportBounds:
    """The configured ceilings, passed in rather than read from the environment."""

    max_bytes: int
    max_pdf_pages: int
    max_text_chars: int
    max_lines: int


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_COMBINING: Final = "Mn"


def _fold(value: str) -> str:
    """Lowercase, accent-free, punctuation-free: the key both sides compare on.

    "c. à s.", "C.A.S" and "cas" all fold to ``cas``, which is why the synonym map
    needs one spelling per unit rather than one per typography. Compatibility
    decomposition (NFKD) rather than NFC here because this value is a lookup key
    and never displayed -- the concern is that "é" and "é" agree, not that the
    author's choice of character survives.
    """
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        char for char in decomposed if char.isalnum() and unicodedata.category(char) != _COMBINING
    )


def _fold_words(value: str) -> str:
    """:func:`_fold` per word, keeping single spaces. The key for product names."""
    return " ".join(folded for word in value.split() if (folded := _fold(word)))


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #

#: French spellings, keyed by the ``unit.code`` they mean. Every key is checked
#: against the table when the lexicon is built, so this map cannot introduce a
#: unit the database does not have -- it can only fail to be useful.
_UNIT_SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "g": ("g", "gr", "gramme", "grammes"),
    "kg": ("kg", "kilo", "kilos", "kilogramme", "kilogrammes"),
    "mg": ("mg", "milligramme", "milligrammes"),
    "ml": ("ml", "millilitre", "millilitres"),
    "cl": ("cl", "centilitre", "centilitres"),
    "dl": ("dl", "décilitre", "décilitres"),
    "l": ("l", "litre", "litres"),
    "tbsp": ("cuillère à soupe", "cuillères à soupe", "c à s", "cas", "cs"),
    "tsp": ("cuillère à café", "cuillères à café", "c à c", "cac", "cc"),
    "piece": ("pièce", "pièces", "pc", "pcs", "unité", "unités"),
}

#: The unit a bare count resolves to. Its dimension is read from the table like
#: any other; only the code is assumed, and its absence disables count parsing
#: rather than inventing a dimension.
_COUNT_UNIT_CODE: Final = "piece"

#: How many whitespace-separated tokens a unit spelling may span. Three covers
#: "c. à s." and "cuillère à soupe", which are the longest in the map.
_MAX_UNIT_TOKENS: Final = 3


@dataclass(frozen=True, slots=True)
class _UnitEntry:
    code: str
    dimension: QuantityDimension


class UnitLexicon:
    """Spellings to ``unit`` rows. Built from the table, once per request.

    Precedence when two spellings fold to the same key: the code wins, then the
    symbol, then a synonym. A synonym can therefore never shadow a real unit code.
    """

    def __init__(self, units: Iterable[Unit]) -> None:
        # Materialised: the argument is read three times below and a caller
        # passing a generator would otherwise build a lexicon with codes and no
        # symbols, which fails only on the lines that spell a unit unusually.
        table = list(units)
        rows = {unit.code: _UnitEntry(unit.code, unit.dimension) for unit in table}
        self._by_spelling: dict[str, _UnitEntry] = {}
        for unit in table:
            self._by_spelling.setdefault(_fold(unit.code), rows[unit.code])
        for unit in table:
            self._by_spelling.setdefault(_fold(unit.symbol), rows[unit.code])
        for code, spellings in _UNIT_SYNONYMS.items():
            entry = rows.get(code)
            if entry is None:
                # A synonym for a unit this instance does not have. Dropping it is
                # the point: the table decides which units exist.
                continue
            for spelling in spellings:
                self._by_spelling.setdefault(_fold(spelling), entry)
        self._count = rows.get(_COUNT_UNIT_CODE)

    @classmethod
    async def load(cls, session: AsyncSession) -> UnitLexicon:
        rows = (await session.scalars(select(Unit))).all()
        return cls(rows)

    def get(self, code: str) -> _UnitEntry | None:
        """Resolve a unit *code* exactly, for values a client submitted."""
        entry = self._by_spelling.get(_fold(code))
        return entry if entry is not None and entry.code == code else None

    def resolve_spelling(self, spelling: str) -> _UnitEntry | None:
        return self._by_spelling.get(_fold(spelling))

    @property
    def count(self) -> _UnitEntry | None:
        """The unit a bare count uses, or ``None`` if this instance has none."""
        return self._count


# --------------------------------------------------------------------------- #
# The deterministic parser
# --------------------------------------------------------------------------- #
#
# Several patterns below suppress the ambiguous-character rules. Those
# characters are the point: a French shopping list really is written with EN DASH
# bullets, a MULTIPLICATION SIGN for "3 x yaourt" and a typographic apostrophe in
# "d'huile". Replacing them with their ASCII look-alikes would mean not
# recognising the lines this parser exists to read.

#: Bullets, dashes, box-drawing checkboxes and enumeration glyphs. A leading
#: *digit* is deliberately absent: "2. pommes" is indistinguishable from "2
#: pommes" and reading it as a quantity is the more useful of the two mistakes.
_BULLET_RE: Final = re.compile(r"^[\s\-–—*•·+>»▪◦□☐☑✓✔]+")  # noqa: RUF001
_CHECKBOX_RE: Final = re.compile(r"^\[\s*[xX✓✔]?\s*\]\s*")

#: A trailing price, with or without dot leaders: "Pain .......... 2,50 €".
#: Removed before parsing, because a price is not a quantity and a line ending in
#: one otherwise resolves its amount from the wrong end.
_TRAILING_PRICE_RE: Final = re.compile(
    r"(?:[\s.·…_]{2,}|\s+)\d{1,4}[.,]\d{2}\s*(?:€|eur|euros?)?\s*$", re.IGNORECASE
)

_NUMBER: Final = r"\d{1,6}(?:[.,]\d{1,3})?"
_FRACTION: Final = r"\d{1,3}\s*/\s*\d{1,3}"

_PREFIX_MULTIPLIER_RE: Final = re.compile(
    rf"^({_NUMBER})\s*[x×*]\s*(?=\S)",  # noqa: RUF001
    re.IGNORECASE,
)
_SUFFIX_MULTIPLIER_RE: Final = re.compile(
    rf"[x×]\s*({_NUMBER})\s*$",  # noqa: RUF001
    re.IGNORECASE,
)
_PREFIX_NUMBER_RE: Final = re.compile(rf"^(?:({_FRACTION})|({_NUMBER}))\s*")
_SUFFIX_NUMBER_RE: Final = re.compile(
    rf"(?:^|[\s:;,\-–—])\s*({_NUMBER})\s*([^\s\d]{{0,12}})\.?$"  # noqa: RUF001
)

#: Leading partitive articles, removed only from what follows a quantity: "2 kg
#: **de** pommes de terre". A line that is nothing but "du pain" keeps its
#: article, because there the word is part of how the user named the thing.
_PARTITIVE_RE: Final = re.compile(
    r"^(?:d['’]|de\s+l['’]|de\s+la\s+|des\s+|du\s+|de\s+)",  # noqa: RUF001
    re.IGNORECASE,
)

_WORD_NUMBERS: Final[Mapping[str, int]] = {
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "onze": 11,
    "douze": 12,
}

#: "une douzaine d'œufs" is a real shopping-list line and a bare "12" is what it
#: means. "demi-douzaine" folds to ``demidouzaine``.
_DOZENS: Final[Mapping[str, int]] = {"douzaine": 12, "demidouzaine": 6}


@dataclass(frozen=True, slots=True)
class _Reading:
    """What the parser made of one line, before the catalogue is consulted."""

    label: str
    amount: Decimal | None = None
    unit: _UnitEntry | None = None


def _to_decimal(raw: str) -> Decimal | None:
    """A French or English decimal, or a simple fraction, bounded and positive."""
    text = raw.strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            value = Decimal(numerator.strip()) / Decimal(denominator.strip())
        except InvalidOperation, ZeroDivisionError, ArithmeticError:
            return None
    else:
        try:
            value = Decimal(text.replace(",", "."))
        except InvalidOperation:
            return None
    if value <= 0 or value > MAX_AMOUNT:
        return None
    # numeric(12,3) in the schema; quantising here means a value that survives
    # parsing is a value the database will accept unchanged. The integral case is
    # re-quantised rather than normalised because ``Decimal("500").normalize()``
    # is ``5E+2`` -- numerically right, and a surprise in any log or assertion.
    quantised = value.quantize(_QUANTUM)
    if quantised == quantised.to_integral_value():
        return quantised.quantize(Decimal(1))
    return quantised.normalize()


def _match_unit(tokens: Sequence[str], lexicon: UnitLexicon) -> tuple[_UnitEntry, int] | None:
    """Longest unit spelling at the head of ``tokens``, and how many it consumed."""
    for span in range(min(_MAX_UNIT_TOKENS, len(tokens)), 0, -1):
        entry = lexicon.resolve_spelling("".join(tokens[:span]))
        if entry is not None:
            return entry, span
    return None


def _clean_label(value: str, *, strip_partitive: bool) -> str:
    text = value.strip(" \t.,;:-–—…·*/\\|()[]{}\"'")  # noqa: RUF001
    if strip_partitive:
        text = _PARTITIVE_RE.sub("", text, count=1)
    return " ".join(text.split())[:MAX_LABEL_LENGTH].strip()


def parse_shopping_line(raw: str, lexicon: UnitLexicon) -> _Reading:
    """Read one line. Pure: no I/O, no catalogue, no model.

    Exposed for the tests, which measure how many lines of a French corpus come
    out correctly split -- a number that has to be measurable to be defended.
    """
    text = _CHECKBOX_RE.sub("", _BULLET_RE.sub("", raw)).strip()
    text = _TRAILING_PRICE_RE.sub("", text).strip()
    if not text:
        return _Reading(label="")

    # ``_read_dozen`` before ``_read_prefix_quantity``, and the order is not
    # cosmetic: "une douzaine d'œufs" starts with a spelled number, so the general
    # rule would claim it first and read one *douzaine* instead of twelve eggs.
    for rule in (_read_prefix_multiplier, _read_dozen, _read_prefix_quantity):
        reading = rule(text, lexicon)
        if reading is not None:
            return reading

    # Suffix forms are tried after every prefix form: "2 kg de pommes" must not be
    # read from its right-hand side, and no legitimate line carries both.
    for suffix_rule in (_read_suffix_multiplier, _read_suffix_quantity):
        reading = suffix_rule(text, lexicon)
        if reading is not None:
            return reading

    return _Reading(label=_clean_label(text, strip_partitive=False))


def _read_prefix_multiplier(text: str, lexicon: UnitLexicon) -> _Reading | None:
    """``3 × yaourt nature``, ``2x pain``."""  # noqa: RUF002
    match = _PREFIX_MULTIPLIER_RE.match(text)
    if match is None or lexicon.count is None:
        return None
    amount = _to_decimal(match.group(1))
    label = _clean_label(text[match.end() :], strip_partitive=True)
    if amount is None or not label:
        return None
    return _Reading(label=label, amount=amount, unit=lexicon.count)


def _read_prefix_quantity(text: str, lexicon: UnitLexicon) -> _Reading | None:
    """``2 kg de pommes de terre``, ``1,5 L de lait``, ``3 pommes``, ``un kilo de riz``."""
    match = _PREFIX_NUMBER_RE.match(text)
    if match is not None:
        amount = _to_decimal(match.group(1) or match.group(2))
        rest = text[match.end() :]
    else:
        first, _, remainder = text.partition(" ")
        spelled = _WORD_NUMBERS.get(_fold(first))
        if spelled is None:
            return None
        amount, rest = Decimal(spelled), remainder
    if amount is None:
        return None

    tokens = rest.split()
    if tokens:
        matched = _match_unit(tokens, lexicon)
        if matched is not None:
            unit, consumed = matched
            label = _clean_label(" ".join(tokens[consumed:]), strip_partitive=True)
            if label:
                return _Reading(label=label, amount=amount, unit=unit)
            # "2 kg" with nothing after it is a quantity without a product; the
            # whole line is the label and the quantity is discarded rather than
            # attached to an empty name.
            return None

    # A number with no unit is a count of pieces -- if this instance has that unit.
    label = _clean_label(rest, strip_partitive=True)
    if not label or lexicon.count is None:
        return None
    return _Reading(label=label, amount=amount, unit=lexicon.count)


def _read_dozen(text: str, lexicon: UnitLexicon) -> _Reading | None:
    """``une douzaine d'œufs``, ``demi-douzaine de citrons``."""
    if lexicon.count is None:
        return None
    tokens = text.replace("-", " ").split()
    if not tokens:
        return None
    head = _fold(tokens[0])
    rest_index = 1
    if head in {"un", "une"} and len(tokens) > 1:
        head = _fold(tokens[1])
        rest_index = 2
    count = _DOZENS.get(head)
    if count is None:
        return None
    label = _clean_label(" ".join(tokens[rest_index:]), strip_partitive=True)
    if not label:
        return None
    return _Reading(label=label, amount=Decimal(count), unit=lexicon.count)


def _read_suffix_multiplier(text: str, lexicon: UnitLexicon) -> _Reading | None:
    """``yaourt nature x4``."""
    match = _SUFFIX_MULTIPLIER_RE.search(text)
    if match is None or lexicon.count is None:
        return None
    amount = _to_decimal(match.group(1))
    label = _clean_label(text[: match.start()], strip_partitive=False)
    if amount is None or not label:
        return None
    return _Reading(label=label, amount=amount, unit=lexicon.count)


def _read_suffix_quantity(text: str, lexicon: UnitLexicon) -> _Reading | None:
    """``Lait : 1 L``, ``Farine 500g``, ``Pommes de terre - 2 kg``."""
    match = _SUFFIX_NUMBER_RE.search(text)
    if match is None:
        return None
    unit_spelling = match.group(2)
    if not unit_spelling:
        # A bare trailing number is far more often a size printed on the packaging
        # ("Lait 1,5") or a leftover page number than a quantity. Refused, so the
        # line keeps its whole text as a label.
        return None
    unit = lexicon.resolve_spelling(unit_spelling)
    amount = _to_decimal(match.group(1))
    label = _clean_label(text[: match.start()], strip_partitive=False)
    if unit is None or amount is None or not label:
        return None
    return _Reading(label=label, amount=amount, unit=unit)


def _confidence_for(reading: _Reading, *, matched: bool) -> ParseConfidence:
    if reading.amount is not None and matched:
        return "high"
    if reading.amount is not None or matched:
        return "medium"
    words = reading.label.split()
    if not words or len(words) > MAX_LABEL_WORDS:
        return "none"
    return "low"


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


class ShoppingImportService:
    """Turn a document into a proposal, and a reviewed proposal into rows.

    Takes the session directly rather than a repository, on the same grounds as
    :class:`chaudron.services.recipes.RecipeService`: one writer, two reads of
    reference tables, and no second consumer that would justify the indirection.
    Never commits -- the request's transaction is opened and closed in
    ``infra/db.py``.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        bounds: ImportBounds,
        splitter: ShoppingLineSplitter | None = None,
        allergens: AllergenScreen | None = None,
    ) -> None:
        self._session = session
        self._bounds = bounds
        self._splitter = splitter
        self._allergens = allergens

    async def propose(
        self, household_id: uuid.UUID, *, text: str, source: ImportSource
    ) -> ImportProposal:
        """Read ``text`` into a proposal. Writes nothing, stores nothing."""
        lexicon = await UnitLexicon.load(self._session)
        raw_lines = _candidate_lines(text)
        truncated = len(raw_lines) > self._bounds.max_lines
        raw_lines = raw_lines[: self._bounds.max_lines]

        readings = [parse_shopping_line(line, lexicon) for line in raw_lines]
        parsed_by: ParsedBy = "deterministic"

        unresolved = [
            index
            for index, reading in enumerate(readings)
            if _confidence_for(reading, matched=False) == "none"
        ]
        model_touched: set[int] = set()
        if self._splitter is not None and unresolved:
            model_touched = await self._apply_splitter(raw_lines, readings, unresolved, lexicon)
            parsed_by = "deterministic+model"

        matches = await self._match_products(household_id, [reading.label for reading in readings])
        flags = await self._allergen_flags(household_id, [r.label for r in readings])

        lines: list[ProposedLine] = []
        for index, (raw, reading) in enumerate(zip(raw_lines, readings, strict=True)):
            matched_id = matches.get(index)
            confidence = _confidence_for(reading, matched=matched_id is not None)
            if index in model_touched:
                # A model read this line. It is never presented as understood: the
                # text came from a file a stranger supplied and went through a
                # prompt (AUD-006), so the user confirms it or it does not happen.
                confidence = "medium" if confidence == "high" else confidence
            line_flags = flags[index]
            lines.append(
                ProposedLine(
                    raw=raw,
                    quantity=(
                        None
                        if reading.amount is None or reading.unit is None
                        else ParsedQuantity(
                            amount=reading.amount,
                            unit_code=reading.unit.code,
                            dimension=reading.unit.dimension,
                        )
                    ),
                    product_name=reading.label or raw,
                    matched_product_id=matched_id,
                    confidence=confidence,
                    needs_review=(
                        confidence == "none" or index in model_touched or bool(line_flags)
                    ),
                    flags=line_flags,
                )
            )

        return ImportProposal(
            import_id=uuid.uuid7(),
            source=source,
            parsed_by=parsed_by,
            lines=tuple(lines),
            unparsed_line_count=sum(1 for line in lines if line.confidence == "none"),
            truncated=truncated,
        )

    async def _apply_splitter(
        self,
        raw_lines: Sequence[str],
        readings: list[_Reading],
        unresolved: Sequence[int],
        lexicon: UnitLexicon,
    ) -> set[int]:
        """Ask the model about the lines the parser could not split, and verify it.

        Everything that comes back is treated as hostile, because everything that
        went in was: the text is a file a user uploaded, and it reached a prompt.
        Three checks, each of which drops a candidate rather than the whole batch:
        the line must be one that was actually submitted, the amount must be a
        positive bounded decimal, and the unit must be a code the ``unit`` table
        has. Returns the indices a candidate was applied to.
        """
        assert self._splitter is not None
        selected = list(unresolved)[:MAX_MODEL_LINES]
        # Sanitised before it leaves this process: one bounded single line each, no
        # control characters, no tag-block smuggling (AUD-006, untrusted_text).
        payload = [sanitize(raw_lines[index], limit=MAX_LINE_LENGTH) for index in selected]
        by_sanitised = dict(zip(payload, selected, strict=True))

        try:
            candidates = await self._splitter.split(payload)
        except Exception:
            # A second pass that fails must not fail the import: the deterministic
            # reading is a complete answer on its own (contract 7.1).
            logger.warning("shopping_import_splitter_failed", exc_info=True)
            return set()

        touched: set[int] = set()
        for candidate in list(candidates)[: len(payload)]:
            applied = self._apply_candidate(candidate, by_sanitised, readings, lexicon)
            if applied is not None:
                touched.add(applied)
        return touched

    def _apply_candidate(
        self,
        candidate: SplitCandidate,
        by_sanitised: Mapping[str, int],
        readings: list[_Reading],
        lexicon: UnitLexicon,
    ) -> int | None:
        index = by_sanitised.get(candidate.raw)
        if index is None:
            # A candidate for a line that was never sent. A splitter cannot add
            # items to somebody's shopping list, so this is dropped and logged.
            logger.warning("shopping_import_splitter_unsolicited_line")
            return None
        label = sanitize(candidate.product_name, limit=MAX_LABEL_LENGTH)
        if not label:
            return None

        amount = _to_decimal(candidate.amount) if candidate.amount else None
        unit = lexicon.get(candidate.unit_code) if candidate.unit_code else None
        if (amount is None) != (unit is None):
            # A half-quantity is no quantity: the schema stores value, code and
            # dimension together or not at all.
            amount, unit = None, None
        readings[index] = _Reading(label=label, amount=amount, unit=unit)
        return index

    async def _match_products(
        self, household_id: uuid.UUID, labels: Sequence[str]
    ) -> dict[int, uuid.UUID]:
        """Exact normalised equality against the visible catalogue, and no more.

        One query for the whole document rather than one per line. Deliberately
        blunt -- see the module docstring on why fuzzy matching is absent. A
        household's own product wins over a public one with the same name: it is
        the row that household curated.
        """
        wanted = {label.casefold() for label in labels if label}
        if not wanted:
            return {}
        rows = (
            await self._session.scalars(
                select(Product).where(
                    func.lower(Product.name).in_(wanted),
                    Product.archived_at.is_(None),
                    # Not `in_([household_id, None])`: SQL never matches NULL with
                    # IN, so the public catalogue would silently become invisible.
                    (Product.household_id == household_id) | Product.household_id.is_(None),
                )
            )
        ).all()

        by_name: dict[str, Product] = {}
        for row in rows:
            key = _fold_words(row.name)
            current = by_name.get(key)
            if current is None or (current.household_id is None and row.household_id is not None):
                by_name[key] = row

        matched: dict[int, uuid.UUID] = {}
        for index, label in enumerate(labels):
            if not label:
                continue
            found = by_name.get(_fold_words(label))
            if found is not None:
                matched[index] = found.id
        return matched

    async def _allergen_flags(
        self, household_id: uuid.UUID, labels: Sequence[str]
    ) -> list[tuple[str, ...]]:
        """Flags to attach, per line. Never a reason to drop one (contract 7.4).

        A household is allowed to buy what one of its members cannot eat. Nothing
        in this method removes a line, and the port it calls cannot express it --
        the rule is in the types, not in a comment asking for care.
        """
        if self._allergens is None:
            # No dietary model registered on this instance yet. Reporting no flags
            # is the honest answer; silently filtering would be the harmful one.
            return [() for _ in labels]
        try:
            result = list(await self._allergens.flags_for(household_id, list(labels)))
        except Exception:
            logger.warning("shopping_import_allergen_screen_failed", exc_info=True)
            return [() for _ in labels]
        result.extend([()] * (len(labels) - len(result)))
        return [tuple(flags) for flags in result[: len(labels)]]

    # -- the only call that writes ------------------------------------------ #

    async def confirm(
        self,
        household_id: uuid.UUID,
        *,
        lines: Sequence[ConfirmLine],
        shopping_list_id: uuid.UUID | None = None,
        list_name: str | None = None,
    ) -> ConfirmedImport:
        """Write the reviewed lines. The single write path of the import.

        The proposal itself was never stored (see ``domain/shopping.py``), so this
        call is authoritative on its own body and validates everything again: the
        unit against the table, the product against what this household may see,
        the label through the same neutralisation as any untrusted text.
        """
        lexicon = await UnitLexicon.load(self._session)
        target = await self._resolve_list(household_id, shopping_list_id, list_name)
        next_order = await self._next_sort_order(household_id, target.id)

        created = 0
        for offset, line in enumerate(lines):
            item = await self._build_item(household_id, target.id, line, lexicon)
            item.sort_order = next_order + offset
            self._session.add(item)
            created += 1
        await self._session.flush()

        logger.info(
            "shopping_import_confirmed",
            extra={"shopping_list_id": str(target.id), "item_count": created},
        )
        return ConfirmedImport(
            shopping_list_id=target.id,
            shopping_list_name=target.name,
            created_item_count=created,
        )

    async def _build_item(
        self,
        household_id: uuid.UUID,
        list_id: uuid.UUID,
        line: ConfirmLine,
        lexicon: UnitLexicon,
    ) -> ShoppingListItem:
        label = sanitize(line.label, limit=MAX_LABEL_LENGTH) if line.label else None
        product_id: uuid.UUID | None = None
        if line.product_id is not None:
            product = await self._session.scalar(
                select(Product).where(
                    Product.id == line.product_id,
                    Product.archived_at.is_(None),
                    (Product.household_id == household_id) | Product.household_id.is_(None),
                )
            )
            if product is None:
                raise ProductNotFoundError(line.product_id)
            product_id = product.id
        if product_id is None and not label:
            # ``ck_shopping_list_item_target_present``, caught before the database
            # does: a label that sanitised down to nothing leaves a line with no
            # target at all, and the schema violation would surface as a 500.
            raise InvalidQuantityError("a line needs either a product or a label")

        unit = None
        if line.unit_code is not None:
            unit = lexicon.get(line.unit_code)
            if unit is None:
                raise UnknownUnitError(line.unit_code)

        return ShoppingListItem(
            household_id=household_id,
            shopping_list_id=list_id,
            product_id=product_id,
            label=label,
            quantity_value=line.amount if unit is not None else None,
            quantity_unit_code=unit.code if unit is not None else None,
            quantity_dimension=unit.dimension if unit is not None else None,
            # The enum has no ``import`` member and adding one is a migration this
            # change does not own. ``manual`` is the truthful nearest value: a human
            # reviewed every line before it got here.
            origin=ShoppingItemOrigin.MANUAL,
        )

    async def _resolve_list(
        self, household_id: uuid.UUID, shopping_list_id: uuid.UUID | None, name: str | None
    ) -> ShoppingList:
        if shopping_list_id is None:
            return await resolve_current_list(self._session, household_id, name=name)
        found = await self._session.scalar(
            select(ShoppingList).where(
                ShoppingList.id == shopping_list_id,
                ShoppingList.household_id == household_id,
                ShoppingList.archived_at.is_(None),
            )
        )
        if found is None:
            raise ShoppingListNotFoundError(f"no shopping list {shopping_list_id}")
        return found

    async def _next_sort_order(self, household_id: uuid.UUID, list_id: uuid.UUID) -> int:
        highest = await self._session.scalar(
            select(func.max(ShoppingListItem.sort_order)).where(
                ShoppingListItem.household_id == household_id,
                ShoppingListItem.shopping_list_id == list_id,
            )
        )
        return 0 if highest is None else int(highest) + 1


async def resolve_current_list(
    session: AsyncSession, household_id: uuid.UUID, *, name: str | None = None
) -> ShoppingList:
    """The household's live default list, created on first use.

    One function for both write paths -- the import's confirmation and the
    ordinary item endpoints of contract 6bis -- because two of them would
    eventually disagree about what "the current list" is, and the partial unique
    index ``uq_shopping_list_default`` turns that disagreement into an integrity
    error rather than a merge.

    The creation branch is only reached when the household has no live default,
    so it cannot violate that index.
    """
    default = await session.scalar(
        select(ShoppingList).where(
            ShoppingList.household_id == household_id,
            ShoppingList.is_default.is_(True),
            ShoppingList.archived_at.is_(None),
        )
    )
    if default is not None:
        return default

    created = ShoppingList(
        household_id=household_id,
        name=sanitize(name, limit=_LIST_NAME_LENGTH) if name else DEFAULT_LIST_NAME,
        is_default=True,
    )
    session.add(created)
    await session.flush()
    return created


def _candidate_lines(text: str) -> list[str]:
    """Split extracted text into the lines worth parsing.

    Each is sanitised here, at the boundary, rather than at the point it reaches a
    prompt: after this the whole pipeline holds bounded single-line values, which
    is the structural property ``untrusted_text`` exists to establish.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        cleaned = sanitize(raw, limit=MAX_LINE_LENGTH)
        # A line with no letter or digit is decoration -- a rule of dashes, a page
        # number's underline -- and never an item.
        if cleaned and any(char.isalnum() for char in cleaned):
            lines.append(cleaned)
    return lines


# --------------------------------------------------------------------------- #
# Depletion -> repurchase proposal (contract 6bis)
# --------------------------------------------------------------------------- #


class DepletionService:
    """Notice that something ran out, and say so. Never act on it.

    The reason of the movement decides on its own (contract 6bis): ``consumed``
    and ``wasted`` mean there is none left, ``correction`` means the number was
    wrong. Reading a correction as a depletion would fill a household's shopping
    list with its own typing mistakes, which is the same distinction that keeps a
    manual adjustment from counting as consumption.

    Batch by construction. Emptying the fridge after a meal depletes several
    lines at once, and the contract asks for one grouped proposal rather than
    five interruptions -- so the entry point takes a sequence and issues two
    queries for the whole batch, whatever its size.
    """

    def __init__(
        self, session: AsyncSession, *, declined: DeclinedRepurchaseStore | None = None
    ) -> None:
        self._session = session
        self._declined = declined

    async def propose(
        self, household_id: uuid.UUID, events: Sequence[DepletionEvent]
    ) -> tuple[DepletionProposal, ...]:
        """One proposal per depleted product, in submission order, deduplicated."""
        eligible: dict[uuid.UUID, DepletionReason] = {}
        for event in events:
            # The one gate. Narrowing is the point: past this line the reason is
            # typed as a depletion reason, so no caller downstream can put
            # ``correction`` into a proposal even by accident.
            if not is_depletion_reason(event.reason):
                continue
            eligible.setdefault(event.product_id, event.reason)
        if not eligible:
            return ()

        product_ids = list(eligible)
        names = await self._product_names(household_id, product_ids)
        on_list = await self._already_on_list(household_id, product_ids)
        declined = await self._previously_declined(household_id, product_ids)

        return tuple(
            DepletionProposal(
                product_id=product_id,
                product_name=names[product_id],
                reason=reason,
                already_on_list=product_id in on_list,
                previously_declined=product_id in declined,
            )
            for product_id, reason in eligible.items()
            if product_id in names
        )

    async def _product_names(
        self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        rows = (
            await self._session.scalars(
                select(Product).where(
                    Product.id.in_(product_ids),
                    (Product.household_id == household_id) | Product.household_id.is_(None),
                )
            )
        ).all()
        return {row.id: row.name for row in rows}

    async def _already_on_list(
        self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """Products sitting unchecked on a live list of this household.

        A checked-off item is history, not a pending purchase, so it does not
        suppress the proposal -- finishing the milk a week after buying it should
        offer to buy milk again.
        """
        rows = (
            await self._session.scalars(
                select(ShoppingListItem.product_id)
                .join(
                    ShoppingList,
                    (ShoppingList.id == ShoppingListItem.shopping_list_id)
                    & (ShoppingList.household_id == ShoppingListItem.household_id),
                )
                .where(
                    ShoppingListItem.household_id == household_id,
                    ShoppingListItem.product_id.in_(product_ids),
                    ShoppingListItem.checked_at.is_(None),
                    ShoppingList.archived_at.is_(None),
                )
            )
        ).all()
        return frozenset(row for row in rows if row is not None)

    async def _previously_declined(
        self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        if self._declined is None:
            # No store on this instance: nothing was ever remembered, so nothing
            # was ever declined. Reporting `false` is accurate for the state that
            # exists. The API always wires one (``api/deps.py``); the tests that
            # leave it out do so to isolate the reason rule from the memory.
            return frozenset()
        return await self._declined.declined_products(household_id, product_ids)
