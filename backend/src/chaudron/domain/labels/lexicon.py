"""The abbreviation lexicon: its shape, its invariants, and how it is loaded.

The lexicon itself lives in ``lexicon.toml`` next to this module, not in Python. It
is a business asset that grows by correction -- a shop manager, a support person or a
contributor with a receipt in hand must be able to add a line without touching code,
and a reviewer must be able to read the diff. TOML was chosen over the alternatives
for one property none of them have together: it parses with the standard library
(``tomllib``), and it carries **comments**, which is where an entry's provenance
lives. An entry no one can trace back to a receipt is an entry no one dares delete.

The only thing this module enforces is that the file cannot lie about itself:
duplicate forms in the same retailer scope, unknown confidence levels, and
designation entries with no expansion are load-time errors. A lexicon that fails to
load is a deployment that fails loudly, which is the correct failure mode for data
that feeds an allergen check.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from types import MappingProxyType
from typing import Any, Final

from chaudron.domain.labels.normalization import to_comparison_form

__all__ = [
    "Confidence",
    "EntryKind",
    "Lexicon",
    "LexiconEntry",
    "LexiconError",
    "default_lexicon",
    "load_lexicon",
]

LEXICON_FILENAME: Final = "lexicon.toml"


class LexiconError(RuntimeError):
    """The lexicon file is malformed. Raised at load time, never swallowed."""


class Confidence(StrEnum):
    """How much an expansion may be trusted.

    ``HIGH``
        The abbreviated form was read on a real receipt **and** the expansion is
        forced by the rest of that line (``CHIPS PDT SEL ET POIVRE`` leaves ``PDT``
        no second reading).
    ``MEDIUM``
        The form was read on a real receipt, but the expansion is an inference from
        context or from general knowledge of French retail rather than something the
        receipt spelled out.
    ``LOW``
        Plausible and useful for recall, not usable on its own. Callers on the
        allergen path must treat a ``LOW`` expansion as no expansion.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RANK: Final[Mapping[Confidence, int]] = MappingProxyType(
    {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
)


def confidence_rank(value: Confidence) -> int:
    """Total order on :class:`Confidence`, lowest first."""
    return _RANK[value]


def weakest(values: Iterable[Confidence]) -> Confidence:
    """The confidence of a chain is the confidence of its weakest link."""
    return min(values, key=confidence_rank, default=Confidence.HIGH)


class EntryKind(StrEnum):
    """Which class of abbreviation an entry belongs to.

    The classes are descriptive -- they exist so that a reader of the data file can
    see *why* a form was abbreviated, and so that coverage can be reported per class
    rather than as one opaque number.
    """

    #: The word was cut and the tail dropped: ``LEGU`` for légume, ``ROUG`` for rouge.
    TRUNCATION = "truncation"
    #: Vowels removed, consonants kept: ``PDT``, ``YRT``, ``ENTRMONT``.
    SKELETON = "skeleton"
    #: Initials of a phrase: ``VPF``, ``SSA``, ``MG``, ``DD``.
    INITIALISM = "initialism"
    #: A property of the product rather than its name: ``BIO``, ``S/GLUTEN``, ``NAT``.
    QUALIFIER = "qualifier"
    #: How it is packed or presented: ``BARQ``, ``SACH``, ``BTE``, ``ETUI``.
    PACKAGING = "packaging"
    #: A manufacturer's name, not part of the designation.
    BRAND = "brand"
    #: A chain's own range: ``MR`` (Leclerc), ``TOP BUDGET`` (Intermarché), ``AUC``.
    PRIVATE_LABEL = "private_label"
    #: A department header printed between item groups, never an item itself.
    DEPARTMENT = "department"


#: Kinds that name a maker or a shelf, not the food. They are recognised so they can
#: be lifted *out* of the designation -- matching "coleslaw" against a catalogue works,
#: matching "ranou coleslaw" does not -- but they never contribute expanded words.
NON_DESIGNATION_KINDS: Final[frozenset[EntryKind]] = frozenset(
    {EntryKind.BRAND, EntryKind.PRIVATE_LABEL, EntryKind.DEPARTMENT}
)


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    """One abbreviated form and everything known about it."""

    #: Comparison form of the abbreviation. May contain spaces (``"top budget"``),
    #: in which case it is matched as an n-gram over consecutive words.
    form: str
    #: Every reading judged possible. More than one means genuine ambiguity, and the
    #: expander is required to surface all of them rather than pick.
    expansions: tuple[str, ...]
    confidence: Confidence
    kind: EntryKind
    #: Where the form was seen. ``"assumed"`` marks an entry with no receipt behind
    #: it; those are the first candidates for deletion when a mismatch is reported.
    evidence: str
    #: Chains the entry applies to. Empty means all. A scoped entry is skipped when
    #: the caller does not say which chain the receipt came from -- ``U`` means the
    #: Système U own brand at Super U and means nothing anywhere else.
    retailers: frozenset[str]
    notes: str = ""

    @property
    def is_designation(self) -> bool:
        return self.kind not in NON_DESIGNATION_KINDS

    @property
    def word_count(self) -> int:
        return len(self.form.split(" "))


@dataclass(frozen=True, slots=True)
class Lexicon:
    """An immutable, indexed view over the entries of one lexicon file."""

    version: str
    entries: tuple[LexiconEntry, ...]
    _by_form: Mapping[str, tuple[LexiconEntry, ...]] = field(repr=False)
    _slash_prefixes: Mapping[str, LexiconEntry] = field(repr=False)
    max_ngram: int = field(repr=False, default=1)

    def lookup(self, form: str, *, retailer: str | None) -> tuple[LexiconEntry, ...]:
        """Entries matching ``form`` that are in scope for ``retailer``.

        Retailer-scoped entries are dropped when the chain is unknown. That loses
        recall and it is the intended trade: a scoped abbreviation applied to the
        wrong chain is exactly the confident-and-wrong answer this module exists to
        avoid.
        """
        candidates = self._by_form.get(form, ())
        return tuple(
            entry
            for entry in candidates
            if not entry.retailers or (retailer is not None and retailer in entry.retailers)
        )

    def slash_prefix(self, prefix: str) -> LexiconEntry | None:
        """The reading of a ``X/`` prefix, as in ``S/GLUTEN`` -> sans gluten."""
        return self._slash_prefixes.get(prefix)


def _require(table: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise LexiconError(f"{where}: missing required key {key!r}")
    return table[key]


def _string_list(value: Any, where: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LexiconError(f"{where}: {key!r} must be a list of strings")
    return tuple(value)


def _parse_entry(table: Mapping[str, Any], index: int) -> LexiconEntry:
    where = f"[[token]] #{index}"
    raw_form = _require(table, "form", where)
    if not isinstance(raw_form, str) or not raw_form.strip():
        raise LexiconError(f"{where}: 'form' must be a non-empty string")
    form = to_comparison_form(raw_form)

    try:
        kind = EntryKind(_require(table, "kind", where))
        confidence = Confidence(_require(table, "confidence", where))
    except ValueError as exc:
        raise LexiconError(f"{where} ({raw_form!r}): {exc}") from exc

    expansions = _string_list(_require(table, "expansions", where), where, "expansions")
    if kind not in NON_DESIGNATION_KINDS and not expansions:
        raise LexiconError(f"{where} ({raw_form!r}): a designation entry needs an expansion")

    evidence = _require(table, "evidence", where)
    if not isinstance(evidence, str) or not evidence.strip():
        raise LexiconError(f"{where} ({raw_form!r}): 'evidence' must say where the form was seen")

    retailers = _string_list(table.get("retailers", []), where, "retailers")
    notes = table.get("notes", "")
    if not isinstance(notes, str):
        raise LexiconError(f"{where} ({raw_form!r}): 'notes' must be a string")

    return LexiconEntry(
        form=form,
        expansions=tuple(to_comparison_form(item) for item in expansions),
        confidence=confidence,
        kind=kind,
        evidence=evidence,
        retailers=frozenset(retailers),
        notes=notes,
    )


def _index(entries: Iterable[LexiconEntry]) -> Mapping[str, tuple[LexiconEntry, ...]]:
    buckets: dict[str, list[LexiconEntry]] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    for entry in entries:
        key = (entry.form, entry.retailers)
        if key in seen:
            raise LexiconError(
                f"duplicate entry for {entry.form!r} in the same retailer scope; "
                "merge the readings into one entry's 'expansions' instead"
            )
        seen.add(key)
        buckets.setdefault(entry.form, []).append(entry)
    return MappingProxyType({form: tuple(items) for form, items in buckets.items()})


def _parse_slash_prefixes(tables: Iterable[Mapping[str, Any]]) -> Mapping[str, LexiconEntry]:
    prefixes: dict[str, LexiconEntry] = {}
    for index, table in enumerate(tables):
        entry = _parse_entry(table, index)
        prefix = entry.form.rstrip("/")
        if prefix in prefixes:
            raise LexiconError(f"duplicate slash prefix rule for {prefix!r}")
        prefixes[prefix] = entry
    return MappingProxyType(prefixes)


def load_lexicon(text: str) -> Lexicon:
    """Parse and validate a lexicon document. Raises :class:`LexiconError` on any flaw."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LexiconError(f"lexicon is not valid TOML: {exc}") from exc

    meta = document.get("meta", {})
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        raise LexiconError("[meta].version is required and must be a non-empty string")

    raw_tokens = document.get("token", [])
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise LexiconError("the lexicon must declare at least one [[token]]")

    entries = tuple(_parse_entry(table, index) for index, table in enumerate(raw_tokens))
    by_form = _index(entries)
    slash_prefixes = _parse_slash_prefixes(document.get("slash_prefix", []))

    return Lexicon(
        version=version,
        entries=entries,
        _by_form=by_form,
        _slash_prefixes=slash_prefixes,
        max_ngram=max(entry.word_count for entry in entries),
    )


_DEFAULT: Lexicon | None = None


def default_lexicon() -> Lexicon:
    """The lexicon shipped with the package.

    Parsed once and memoised. The cache is the only state in this package; it holds
    an immutable value and exists so that expanding ten thousand receipt lines does
    not re-parse the file ten thousand times. Tests that need a different lexicon
    pass it explicitly to :func:`~chaudron.domain.labels.expansion.expand_label`
    rather than mutating this.
    """
    global _DEFAULT
    if _DEFAULT is None:
        source = resources.files("chaudron.domain.labels").joinpath(LEXICON_FILENAME)
        _DEFAULT = load_lexicon(source.read_text(encoding="utf-8"))
    return _DEFAULT
