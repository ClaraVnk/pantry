"""The weekly balance: arithmetic over what actually left the stock.

Computed by the application and never by the model. A language model writes
recipes; it does not do dietetics, and an answer it invents cannot be checked
against a published source (ADR-0009).

Three properties are carried here rather than in the service, because they are
what makes the figure arguable rather than authoritative.

*The benchmark travels with its wording and its URL.* Every gap this module
produces carries the sentence Santé publique France published and the page it is
on. "You are one fish short this week" is a claim a household must be able to
open and check; an opaque score is one they can only accept.

*A benchmark with no official figure produces nothing.* Added fats and sugary
foods are covered by advice that carries no number anywhere -- "limit", "in
small quantities". They resolve to a marker so that a bar of chocolate is not
counted as unidentified, and they generate neither a gap nor an excess. Inventing
a threshold to make the table look complete is the failure this file is written
against.

*``around`` is not a floor.* The dairy benchmark is "sufficient but limited". It
can produce an excess and never a shortfall, because reading it as a floor would
have this application push more cheese onto somebody already above the mark.

What a *serving* is, and the honest limit of it
-----------------------------------------------

The source of consumption is ``stock_movement`` with kind ``consumption``: what
left the stock was cooked. That ledger records quantities and dates, not meals,
so one serving is read as **one consumption event**. Two spoonfuls of the same
yoghurt taken twice in a day count twice; a single lot split across a family of
four counts once. It is a proxy, it is stated as one, and it is the only reading
available from data the household does not have to type in -- a separate meal
journal is a journal nobody fills in (ADR-0009).

Masses are exact: ``delta_canonical`` is in grams for anything measured by mass,
so the two ceilings the PNNS expresses in grams are counted and not estimated.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from chaudron.domain.models import PnnsDirection, PnnsMarker, PnnsUnit

__all__ = [
    "WINDOW_DAYS",
    "Excess",
    "Gap",
    "Guideline",
    "Observation",
    "WeeklyBalance",
    "evaluate",
    "shortfall_sentence",
]

#: The rolling window every figure in this module is expressed over. Seven days
#: because that is the unit the PNNS benchmarks themselves use for the ones with
#: a weekly figure, and because "this week" is a period a household recognises.
WINDOW_DAYS: Final = 7

#: French cardinals, up to the largest shortfall a weekly benchmark can produce.
#: Written out because "il manque une portion de poisson" is the register
#: ADR-0009 asks for and "shortfall: 1" is not. Feminine, because the noun they
#: agree with below is "portion".
_CARDINALS: Final[Mapping[int, str]] = {
    1: "une",
    2: "deux",
    3: "trois",
    4: "quatre",
    5: "cinq",
    6: "six",
    7: "sept",
    8: "huit",
    9: "neuf",
    10: "dix",
}


@dataclass(frozen=True, slots=True)
class Guideline:
    """One row of ``pnns_guideline``, as the arithmetic needs it."""

    marker: PnnsMarker
    label: str
    direction: PnnsDirection
    amount: Decimal
    unit: PnnsUnit
    window_days: int
    statement: str
    source_url: str
    sort_order: int = 0

    def target_over(self, window_days: int) -> Decimal:
        """The benchmark rescaled to the response window.

        A daily benchmark of five portions is thirty-five over a week. Rescaling
        rather than reporting "5 per day" against a seven-day count is what keeps
        ``observed`` and ``target`` in the same unit -- the alternative reads as
        a household eating twelve portions against a target of five.
        """
        return self.amount * Decimal(window_days) / Decimal(self.window_days)


@dataclass(frozen=True, slots=True)
class Observation:
    """What was consumed against one marker over the window."""

    servings: int = 0
    grams: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Gap:
    """A benchmark the household is short of. Never produced for ``around``."""

    marker: PnnsMarker
    label: str
    target: str
    observed: int
    shortfall: int
    statement: str
    source_url: str


@dataclass(frozen=True, slots=True)
class Excess:
    """A ceiling the household is over."""

    marker: PnnsMarker
    label: str
    target: str
    observed: int
    unit: PnnsUnit
    statement: str
    source_url: str

    @property
    def observed_grams(self) -> int:
        """The contract's field name, which only ever meant a mass.

        Kept as the wire name for the two gram ceilings; a serving-based ceiling
        reports ``observed`` and ``unit`` beside it rather than pretending a
        count of sugary drinks is a weight.
        """
        return self.observed if self.unit is PnnsUnit.GRAM else 0


@dataclass(frozen=True, slots=True)
class WeeklyBalance:
    """The whole answer, including what it could not see.

    ``uncategorised_product_count`` is always present, including at zero. An
    absent field and a zero say the same thing to a naive client -- "everything
    is categorised" -- and one of the two means "we do not know" (contract 8).
    """

    reference: str
    window_days: int
    uncategorised_product_count: int
    gaps: tuple[Gap, ...]
    excesses: tuple[Excess, ...]
    satisfiable_from_stock: bool
    note: str | None


def _target_label(guideline: Guideline, window_days: int) -> str:
    """How the benchmark reads once rescaled: "2 par semaine", "500 g par semaine"."""
    target = guideline.target_over(window_days)
    quantised = target.quantize(Decimal(1)) if target == target.to_integral_value() else target
    period = "par jour" if window_days == 1 else f"par {window_days} jours"
    if window_days == WINDOW_DAYS:
        period = "par semaine"
    if guideline.unit is PnnsUnit.GRAM:
        return f"{quantised} g {period}"
    return f"{quantised} {period}"


def evaluate(
    guidelines: Sequence[Guideline],
    observations: Mapping[PnnsMarker, Observation],
    *,
    reference: str,
    window_days: int = WINDOW_DAYS,
    uncategorised_product_count: int,
    markers_in_stock: frozenset[PnnsMarker],
    note: str | None = None,
) -> WeeklyBalance:
    """Turn counted consumption into gaps and excesses against the benchmarks.

    ``markers_in_stock`` decides ``satisfiable_from_stock``, and it is read from
    the inventory *after* the dietary screen: a shortfall of fish that only the
    withheld half of the stock could have filled is not satisfiable, and saying
    so is the difference between an explanation and a reproach.
    """
    gaps: list[Gap] = []
    excesses: list[Excess] = []
    for guideline in sorted(guidelines, key=lambda item: (item.sort_order, item.marker)):
        observation = observations.get(guideline.marker, Observation())
        observed = (
            int(observation.grams) if guideline.unit is PnnsUnit.GRAM else observation.servings
        )
        target = guideline.target_over(window_days)
        label = _target_label(guideline, window_days)
        if guideline.direction is PnnsDirection.AT_LEAST and observed < target:
            gaps.append(
                Gap(
                    marker=guideline.marker,
                    label=guideline.label,
                    target=label,
                    observed=observed,
                    shortfall=math.ceil(target - observed),
                    statement=guideline.statement,
                    source_url=guideline.source_url,
                )
            )
        elif guideline.direction is not PnnsDirection.AT_LEAST and observed > target:
            # ``at_most`` and ``around`` share this branch: both are ceilings.
            # ``around`` never reaches the one above it, which is the whole
            # difference between "sufficient but limited" and a floor.
            excesses.append(
                Excess(
                    marker=guideline.marker,
                    label=guideline.label,
                    target=label,
                    observed=observed,
                    unit=guideline.unit,
                    statement=guideline.statement,
                    source_url=guideline.source_url,
                )
            )
    satisfiable = all(gap.marker in markers_in_stock for gap in gaps)
    return WeeklyBalance(
        reference=reference,
        window_days=window_days,
        uncategorised_product_count=uncategorised_product_count,
        gaps=tuple(gaps),
        excesses=tuple(excesses),
        satisfiable_from_stock=satisfiable,
        note=note,
    )


def shortfall_sentence(gaps: Sequence[Gap]) -> str | None:
    """The gaps in plain French, for the prompt and for the screen.

    "il manque un poisson, deux légumineuses" rather than a vector of numbers:
    the model has to act on it, and the household has to be able to disagree with
    it. Neither can do anything with a score (ADR-0009).
    """
    if not gaps:
        return None
    parts = [
        f"{_CARDINALS.get(gap.shortfall, str(gap.shortfall))} "
        f"portion{'s' if gap.shortfall > 1 else ''} de {gap.label.lower()}"
        for gap in gaps
    ]
    return "il manque " + ", ".join(parts)
