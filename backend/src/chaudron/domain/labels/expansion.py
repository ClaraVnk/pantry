"""Expanding a receipt label into the French designations it might stand for.

The design is shaped by one asymmetry, stated in ADR-0009 and repeated here because
it is the reason this module returns so little so often:

    A wrong expansion is more dangerous than no expansion.

Downstream, an unidentified ingredient invalidates a recipe suggestion and asks a
human. A *confidently wrong* identification resolves an ingredient to the wrong
catalogue product, and the allergen check that runs after the model call then reads
the allergen list of a food nobody is about to eat. The first failure is visible and
annoying; the second is invisible and physical.

So: several readings are all returned rather than arbitrated (``SAV`` is *saveur* on
a crouton line and *savon* on a soap line, and only the caller has the shelf context
to decide), and anything below ``MEDIUM`` confidence is surfaced as needing review
rather than folded into an answer.

The expander never invents. It expands forms the lexicon knows and leaves everything
else verbatim; ``unresolved`` lists what it could not read so that a caller can tell
"this is plain French already" from "this line is full of abbreviations I do not
have".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import product
from typing import Final

from chaudron.domain.labels.lexicon import (
    NON_DESIGNATION_KINDS,
    Confidence,
    EntryKind,
    Lexicon,
    LexiconEntry,
    confidence_rank,
    default_lexicon,
    weakest,
)
from chaudron.domain.labels.normalization import (
    NormalizedLabel,
    TruncationHint,
    normalize_label,
)

__all__ = [
    "Expansion",
    "ExpansionResult",
    "TokenReading",
    "Verdict",
    "expand_label",
]


class Verdict(StrEnum):
    """What the caller is allowed to do with the result.

    ``RESOLVED``
        Exactly one reading. Usable as a catalogue query, subject to
        :attr:`ExpansionResult.requires_review`.
    ``AMBIGUOUS``
        Several readings, all returned. The caller must disambiguate with context it
        has and this module does not, or send the line to review.
    ``UNRESOLVED``
        No reading could be produced. Send to review. This is a normal, frequent and
        *safe* outcome, not a bug.
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class Expansion:
    """One complete reading of a label."""

    text: str
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class TokenReading:
    """What the expander made of one token, kept so a caller can audit the result."""

    source: str
    readings: tuple[str, ...]
    confidence: Confidence
    kind: EntryKind | None
    evidence: str


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    """The outcome of expanding one receipt line."""

    label: NormalizedLabel
    verdict: Verdict
    #: Every full reading judged possible, best confidence first. Empty when
    #: ``verdict`` is ``UNRESOLVED`` -- and also when the readings were too numerous
    #: to enumerate honestly, in which case ``tokens`` still carries the per-token
    #: alternatives.
    candidates: tuple[Expansion, ...]
    tokens: tuple[TokenReading, ...]
    #: Tokens that look like abbreviations and are not in the lexicon. These are the
    #: enrichment backlog: each one is a line someone can add to ``lexicon.toml``.
    unresolved: tuple[str, ...]
    #: The last word, when the line looks cut off. It is a word *fragment*, so a
    #: matcher should treat it as a prefix (``chocola%``) rather than as a word --
    #: no dictionary of abbreviations will ever contain it, because the cut is
    #: positional, not lexical.
    truncated_tail: str | None
    #: Maker and own-brand names lifted out of the designation, in comparison form.
    brands: tuple[str, ...]
    retailer: str | None
    #: False when nothing in the line was abbreviated: the label was already plain
    #: French and ``candidates`` holds it unchanged.
    expanded: bool

    @property
    def best(self) -> Expansion | None:
        """The single reading, or ``None`` when there is not exactly one."""
        if self.verdict is Verdict.RESOLVED and self.candidates:
            return self.candidates[0]
        return None

    @property
    def requires_review(self) -> bool:
        """Whether a human must look before this result is trusted.

        True unless there is exactly one reading at ``MEDIUM`` confidence or better.
        Ambiguity, low confidence and failure all land here on purpose: they are the
        three ways of saying "I do not know", and they must be indistinguishable to a
        caller that is about to make a safety decision.
        """
        best = self.best
        return best is None or confidence_rank(best.confidence) < confidence_rank(Confidence.MEDIUM)

    @property
    def is_complete(self) -> bool:
        """Whether the designation is believed whole.

        False when the printer cut the line or when a token could not be read. A
        complete-looking designation is still only a *belief*: a chain that truncates
        on a word boundary leaves no trace of having done so.
        """
        return (
            not self.unresolved
            and self.truncated_tail is None
            and self.label.truncation is TruncationHint.NONE
        )


# Splitting a word into its abbreviation atoms. The dot is the French receipt's own
# abbreviation mark (BEUR.PLAQ.PRESID.DX), the slash joins alternatives or a
# preposition (AIL/FROM, S/GLUTEN), the ampersand and plus sign join components, and
# the comma separates fields on the chains that print several (E.Leclerc prints
# ``designation,marque,contenance`` on one line). Decimal commas never reach here:
# quantity extraction has already consumed them.
_ATOM_SPLIT: Final = re.compile(r"[./&+,]")

# Beyond this many combinations, enumerating readings stops being informative and
# starts being a way to bury the ambiguity in a long list. The result then carries no
# candidates and the caller is told, honestly, that the line is ambiguous.
_MAX_CANDIDATES: Final = 12

# What makes a leftover token look like an abbreviation rather than an ordinary short
# French word. A vowel-free run of letters is the reliable signal (``SSN``, ``FQC``,
# ``P``); length alone is not, because ``ail``, ``riz`` and ``nuit`` are words.
_VOWELS: Final = frozenset("aeiouy")


@dataclass(frozen=True, slots=True)
class _Segment:
    """One position in the designation and the readings it can take."""

    source: str
    readings: tuple[str, ...]
    confidence: Confidence
    kind: EntryKind | None
    evidence: str
    unresolved: bool


def _looks_abbreviated(token: str) -> bool:
    """Whether an unreadable token is an abbreviation rather than a plain short word."""
    return token.isalpha() and not (_VOWELS & set(token))


def _verbatim(word: str) -> _Segment:
    """Keep a token as it stands, flagging it when it is plainly an abbreviation."""
    return _Segment(
        source=word,
        readings=(word,),
        confidence=Confidence.HIGH,
        kind=None,
        evidence="",
        unresolved=_looks_abbreviated(word),
    )


def _from_entries(source: str, entries: tuple[LexiconEntry, ...]) -> _Segment:
    """Fold every in-scope entry for a form into one segment.

    Two entries for the same form (one per retailer scope, say) contribute their
    readings to the same position, and the segment inherits the weakest confidence of
    the entries used. Losing the strong entry's confidence when a weak one also
    matches is deliberate: the ambiguity is real and the caller should feel it.
    """
    readings: list[str] = []
    for entry in entries:
        for reading in entry.expansions:
            if reading not in readings:
                readings.append(reading)
    kind = entries[0].kind
    if kind in NON_DESIGNATION_KINDS:
        readings = []
    return _Segment(
        source=source,
        readings=tuple(readings),
        confidence=weakest(entry.confidence for entry in entries),
        kind=kind,
        evidence="; ".join(dict.fromkeys(entry.evidence for entry in entries)),
        unresolved=False,
    )


def _expand_word(word: str, lexicon: Lexicon, retailer: str | None) -> list[_Segment]:
    """Read one whitespace-delimited word that no n-gram matched."""
    entries = lexicon.lookup(word, retailer=retailer)
    if entries:
        return [_from_entries(word, entries)]

    if "/" in word:
        head, _, tail = word.partition("/")
        rule = lexicon.slash_prefix(head)
        if rule is not None and tail:
            prefix = _Segment(
                source=f"{head}/",
                readings=rule.expansions,
                confidence=rule.confidence,
                kind=rule.kind,
                evidence=rule.evidence,
                unresolved=False,
            )
            return [prefix, *_expand_word(tail, lexicon, retailer)]

    atoms = [atom for atom in _ATOM_SPLIT.split(word) if atom]
    if len(atoms) > 1:
        segments: list[_Segment] = []
        for atom in atoms:
            atom_entries = lexicon.lookup(atom, retailer=retailer)
            segments.append(_from_entries(atom, atom_entries) if atom_entries else _verbatim(atom))
        return segments

    return [_verbatim(word)]


def _segments(label: NormalizedLabel, lexicon: Lexicon, retailer: str | None) -> list[_Segment]:
    """Read the whole designation, longest n-gram first."""
    words = label.words
    out: list[_Segment] = []
    index = 0
    while index < len(words):
        matched = False
        upper = min(lexicon.max_ngram, len(words) - index)
        for size in range(upper, 0, -1):
            form = " ".join(words[index : index + size])
            entries = lexicon.lookup(form, retailer=retailer)
            if entries:
                out.append(_from_entries(form, entries))
                index += size
                matched = True
                break
        if not matched:
            out.extend(_expand_word(words[index], lexicon, retailer))
            index += 1
    return out


def _truncated_tail(segments: list[_Segment], label: NormalizedLabel) -> str | None:
    """The last word, when the line looks cut and that word was not read anyway.

    A tail the lexicon *did* recognise (``CHOCOLA`` -> chocolat) is not reported: the
    fragment has already been repaired, and reporting it would tell a caller to go
    prefix-matching on a word that is now whole.
    """
    if label.truncation is TruncationHint.NONE or not segments:
        return None
    last = segments[-1]
    if last.kind is not None:
        return None
    return last.source


def expand_label(
    raw: str,
    *,
    retailer: str | None = None,
    lexicon: Lexicon | None = None,
) -> ExpansionResult:
    """Expand one raw receipt line.

    ``retailer`` is a chain slug. It unlocks chain-scoped entries and the truncation
    width, and omitting it costs recall but never correctness.
    """
    active = lexicon if lexicon is not None else default_lexicon()
    label = normalize_label(raw, retailer=retailer)
    segments = _segments(label, active, retailer)
    tail = _truncated_tail(segments, label)

    brands = tuple(
        dict.fromkeys(
            segment.source for segment in segments if segment.kind in NON_DESIGNATION_KINDS
        )
    )
    unresolved = tuple(dict.fromkeys(s.source for s in segments if s.unresolved))
    tokens = tuple(
        TokenReading(
            source=segment.source,
            readings=segment.readings,
            confidence=segment.confidence,
            kind=segment.kind,
            evidence=segment.evidence,
        )
        for segment in segments
    )

    variable = [segment for segment in segments if segment.readings]
    expanded = any(
        segment.kind is not None or segment.readings != (segment.source,) for segment in segments
    )

    combinations = 1
    for segment in variable:
        combinations *= len(segment.readings)

    if not variable:
        # Every word is a brand or an unreadable fragment: nothing is left to match on.
        return ExpansionResult(
            label=label,
            verdict=Verdict.UNRESOLVED,
            candidates=(),
            tokens=tokens,
            unresolved=unresolved,
            truncated_tail=tail,
            brands=brands,
            retailer=retailer,
            expanded=expanded,
        )

    if combinations > _MAX_CANDIDATES:
        return ExpansionResult(
            label=label,
            verdict=Verdict.AMBIGUOUS,
            candidates=(),
            tokens=tokens,
            unresolved=unresolved,
            truncated_tail=tail,
            brands=brands,
            retailer=retailer,
            expanded=expanded,
        )

    # A brand recognised and dropped contributes no word to the reading, so its own
    # confidence must not drag the reading's down.
    used = [segment for segment in segments if segment.kind is not None and segment.readings]
    floor = weakest(segment.confidence for segment in used)
    candidates = tuple(
        Expansion(text=" ".join(choice), confidence=floor)
        for choice in product(*(segment.readings for segment in variable))
    )
    verdict = Verdict.RESOLVED if len(candidates) == 1 else Verdict.AMBIGUOUS

    return ExpansionResult(
        label=label,
        verdict=verdict,
        candidates=candidates,
        tokens=tokens,
        unresolved=unresolved,
        truncated_tail=tail,
        brands=brands,
        retailer=retailer,
        expanded=expanded,
    )
