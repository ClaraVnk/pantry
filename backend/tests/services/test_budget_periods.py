"""Calendar arithmetic of the budget, with no database in sight.

The period boundaries are the part of contract 6ter that a user notices
immediately when it is wrong -- "my month restarted on the 3rd" -- and they are
pure functions, so they are tested as pure functions. Everything that needs
PostgreSQL lives in ``tests/api/test_budget.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from chaudron.domain.models import BudgetPeriod
from chaudron.services.budget import period_bounds, previous_period_start, resolve_zone


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        # First day, last day and a day in between all resolve to the same month.
        (date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 8, 17), date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 8, 31), date(2026, 8, 1), date(2026, 8, 31)),
        # 30-day month, and a leap February -- monthrange rather than a table.
        (date(2026, 4, 12), date(2026, 4, 1), date(2026, 4, 30)),
        (date(2028, 2, 29), date(2028, 2, 1), date(2028, 2, 29)),
    ],
)
def test_month_is_calendar(day: date, start: date, end: date) -> None:
    assert period_bounds(BudgetPeriod.MONTH, day) == (start, end)


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        # 2026-08-03 is a Monday, 2026-08-09 the Sunday that closes its week.
        (date(2026, 8, 3), date(2026, 8, 3), date(2026, 8, 9)),
        (date(2026, 8, 9), date(2026, 8, 3), date(2026, 8, 9)),
        # A Sunday belongs to the week that started six days earlier, not to the
        # one about to start. This is the assertion that breaks the day someone
        # reaches for a US-style week.
        (date(2026, 8, 2), date(2026, 7, 27), date(2026, 8, 2)),
        # A week that straddles a month change is still one week.
        (date(2026, 9, 1), date(2026, 8, 31), date(2026, 9, 6)),
    ],
)
def test_week_starts_on_monday(day: date, start: date, end: date) -> None:
    assert period_bounds(BudgetPeriod.WEEK, day) == (start, end)


def test_previous_month_crosses_the_year() -> None:
    assert previous_period_start(BudgetPeriod.MONTH, date(2026, 1, 1)) == date(2025, 12, 1)


def test_previous_week_is_seven_days_back() -> None:
    assert previous_period_start(BudgetPeriod.WEEK, date(2026, 8, 3)) == date(2026, 7, 27)


def test_periods_tile_without_gap_or_overlap() -> None:
    """Every day belongs to exactly one period, and the periods are contiguous.

    Month arithmetic that loses 31 August, or counts it twice, produces a
    spending figure that is wrong once a year and looks right the rest of the
    time -- the worst possible failure mode for a number a household trusts. So
    the property is checked over sixteen months of real days rather than on a
    handful of hand-picked ones.
    """
    for period in BudgetPeriod:
        spans: dict[date, tuple[date, date]] = {}
        day = date(2025, 11, 1)
        while day < date(2027, 3, 1):
            start, end = period_bounds(period, day)
            assert start <= day <= end
            # The same start must always yield the same end, or two days of one
            # month would be aggregated into two different buckets.
            assert spans.setdefault(start, (start, end)) == (start, end)
            day += timedelta(days=1)

        ordered = [spans[key] for key in sorted(spans)]
        for earlier, later in pairwise(ordered):
            assert later[0] - earlier[1] == timedelta(days=1)
            assert previous_period_start(period, later[0]) == earlier[0]


def test_unknown_timezone_degrades_to_utc() -> None:
    """A budget screen must not 500 because a tzdata release is missing a name."""
    assert resolve_zone("Mars/Olympus_Mons").key == "UTC"
    assert resolve_zone(None).key == "UTC"
    assert resolve_zone("Europe/Zurich").key == "Europe/Zurich"
