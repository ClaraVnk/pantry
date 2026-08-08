"""The measured baseline, turned into a floor nothing may fall below.

The numbers asserted here were measured on 2026-08-04 against a corpus of 83 lines
from three receipts that played no part in building the lexicon (protocol and full
figures in ``docs/label-lexicon.md``):

    precision      0.946   (35 correct out of 37 expansions emitted)
    recall         0.412   (35 of 85 annotated abbreviations read)
    safe lines     0.976   (81 of 83 lines carried no wrong expansion)
    perfect lines  0.554

The floors are set just under those values. **Precision is the one that matters**: an
enrichment that raises recall while dropping precision has made the module more
dangerous, and this test is what says so out loud.
"""

from __future__ import annotations

from typing import Final

import pytest

from tests.labels.evaluation import load_cases, report, score

#: Anything below this means a wrong reading now reaches the allergen check.
MIN_PRECISION: Final = 0.94
#: Coverage may only go up. This is the number enrichment is supposed to move.
MIN_RECALL: Final = 0.40
MIN_SAFE_LINE_RATE: Final = 0.97


@pytest.fixture(scope="module")
def measured() -> object:
    return score(load_cases())


def test_the_corpus_is_the_one_that_was_measured() -> None:
    cases = load_cases()
    assert len(cases) == 83
    assert {case.retailer for case in cases} == {"leclerc", "intermarche", "carrefour"}


def test_precision_has_not_regressed() -> None:
    result = score(load_cases())
    assert result.precision >= MIN_PRECISION, report(result)


def test_recall_has_not_regressed() -> None:
    result = score(load_cases())
    assert result.recall >= MIN_RECALL, report(result)


def test_almost_every_line_is_safe() -> None:
    result = score(load_cases())
    assert result.safe_line_rate >= MIN_SAFE_LINE_RATE, report(result)


def test_the_known_wrong_expansions_are_still_only_these_two() -> None:
    """The two known errors, named so that a third one cannot appear unnoticed.

    ``CROUT`` is *croûton* on one line and *croûte* on another, and the lexicon only
    knows the first. ``LT`` is read as *lait* on a line where it is part of a brand.
    Both are documented in ``docs/label-lexicon.md``; both are the reason the module
    exposes ``requires_review`` rather than pretending to be right.
    """
    result = score(load_cases())
    assert {miss.token for miss in result.wrong} == {"crout", "lt"}, report(result)
