"""Scoring the expander against a hand-annotated corpus of held-out receipt lines.

Kept out of ``test_*.py`` on purpose: this is the measuring instrument, and it is
imported both by the regression test and by anyone re-measuring after enriching the
lexicon. It reports precision and recall separately because they answer different
questions, and only one of them is a safety question.

**Precision** -- of the expansions the module emitted, how many were right. A miss
here is a confident wrong reading, which flows silently into the allergen check that
ADR-0009 puts after the model call. This is the number that must stay high.

**Recall** -- of the abbreviations a human could read, how many the module read. A
miss here sends a line to human review. Annoying, never dangerous.

The corpus deliberately annotates abbreviations the lexicon does *not* know, so recall
measures the lexicon's real coverage rather than its self-consistency.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chaudron.domain.labels import expand_label, to_comparison_form
from chaudron.domain.labels.lexicon import NON_DESIGNATION_KINDS, Lexicon

CORPUS_PATH: Final = Path(__file__).parent / "eval_corpus.toml"

#: Gold value meaning "this token is a maker or own-brand name and must be lifted out
#: of the designation", as opposed to expanded into words.
BRAND_MARKER: Final = "@brand"


@dataclass(frozen=True, slots=True)
class Case:
    raw: str
    retailer: str
    gold: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Miss:
    """One scoring event worth naming in a failure message."""

    raw: str
    token: str
    expected: str
    got: str


@dataclass(frozen=True, slots=True)
class Score:
    cases: int
    gold_total: int
    true_positives: int
    false_positives: int
    false_negatives: int
    #: Lines on which nothing was expanded wrongly, whatever the recall. This is the
    #: rate at which the module is *safe*, which is not the rate at which it is useful.
    safe_lines: int
    #: Lines on which every annotated abbreviation was read correctly and nothing else
    #: was touched.
    perfect_lines: int
    wrong: tuple[Miss, ...]
    missed: tuple[Miss, ...]

    @property
    def precision(self) -> float:
        emitted = self.true_positives + self.false_positives
        return self.true_positives / emitted if emitted else 1.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.gold_total if self.gold_total else 1.0

    @property
    def safe_line_rate(self) -> float:
        return self.safe_lines / self.cases if self.cases else 1.0

    @property
    def perfect_line_rate(self) -> float:
        return self.perfect_lines / self.cases if self.cases else 1.0


def load_cases(path: Path = CORPUS_PATH) -> tuple[Case, ...]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Case(
            raw=case["raw"],
            retailer=case["retailer"],
            gold={to_comparison_form(key): value for key, value in case["gold"].items()},
        )
        for case in document["case"]
    )


def score(cases: Sequence[Case], *, lexicon: Lexicon | None = None) -> Score:
    """Run the expander over ``cases`` and count what it got right."""
    true_positives = false_positives = false_negatives = 0
    safe_lines = perfect_lines = 0
    gold_total = 0
    wrong: list[Miss] = []
    missed: list[Miss] = []

    for case in cases:
        gold_total += len(case.gold)
        result = expand_label(case.raw, retailer=case.retailer, lexicon=lexicon)
        line_wrong = 0
        line_right = 0
        seen: set[str] = set()

        for token in result.tokens:
            expected = case.gold.get(token.source)
            is_brand = token.kind in NON_DESIGNATION_KINDS
            emitted = token.kind is not None and (is_brand or token.readings != (token.source,))
            if expected is not None:
                seen.add(token.source)
                correct = is_brand if expected == BRAND_MARKER else expected in token.readings
                if correct:
                    true_positives += 1
                    line_right += 1
                elif emitted:
                    false_positives += 1
                    line_wrong += 1
                    wrong.append(Miss(case.raw, token.source, expected, " | ".join(token.readings)))
                else:
                    false_negatives += 1
                    missed.append(Miss(case.raw, token.source, expected, "-"))
            elif emitted:
                false_positives += 1
                line_wrong += 1
                wrong.append(
                    Miss(case.raw, token.source, "(leave alone)", " | ".join(token.readings))
                )

        # A gold token the tokeniser never produced -- glued to a digit, split on a
        # separator we do not know -- is a miss all the same.
        for form, expected in case.gold.items():
            if form not in seen:
                false_negatives += 1
                missed.append(Miss(case.raw, form, expected, "(token not produced)"))

        if line_wrong == 0:
            safe_lines += 1
            if line_right == len(case.gold):
                perfect_lines += 1

    return Score(
        cases=len(cases),
        gold_total=gold_total,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        safe_lines=safe_lines,
        perfect_lines=perfect_lines,
        wrong=tuple(wrong),
        missed=tuple(missed),
    )


def report(result: Score) -> str:
    """A human-readable summary, used as the assertion message of the regression test."""
    lines = [
        f"cases            : {result.cases}",
        f"gold annotations : {result.gold_total}",
        f"true positives   : {result.true_positives}",
        f"false positives  : {result.false_positives}",
        f"false negatives  : {result.false_negatives}",
        f"precision        : {result.precision:.3f}",
        f"recall           : {result.recall:.3f}",
        f"safe lines       : {result.safe_lines}/{result.cases} ({result.safe_line_rate:.3f})",
        f"perfect lines    : {result.perfect_lines}/{result.cases} "
        f"({result.perfect_line_rate:.3f})",
    ]
    if result.wrong:
        lines.append("wrong expansions:")
        lines += [
            f"  {miss.raw!r}: {miss.token!r} expected {miss.expected!r}, got {miss.got!r}"
            for miss in result.wrong
        ]
    return "\n".join(lines)
