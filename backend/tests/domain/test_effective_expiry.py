"""The clamp of ``docs/data-model.md`` 7.4, and the two implementations of it.

``min(best_before, opened_at + shelf_life_guideline.opened_days)`` exists twice:
once in Python for callers holding the three values, once in SQL because the
listing sorts on it and the calendar feed windows on it. Two implementations of a
food-safety rule is one more than anybody wants, so the second half of this file
runs the same table of cases through PostgreSQL and asserts the answers match.

The SQL side leans on PostgreSQL's ``LEAST`` ignoring NULL arguments. That is the
assumption the whole design rests on -- it is what makes an unopened lot keep its
printed date and an opened one with no printed date acquire a date at all -- so
it is asserted against a live server rather than quoted from the manual.
"""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.shelf_life import effective_expiry

#: ``(best_before, opened_at, opened_days, expected)``, named for what each one
#: decides rather than for the values it carries.
CASES: list[tuple[str, date | None, date | None, int | None, date | None]] = [
    (
        "nothing known at all yields no date, not a default",
        None,
        None,
        None,
        None,
    ),
    (
        "a sealed lot keeps the date printed on it",
        date(2026, 9, 1),
        None,
        3,
        date(2026, 9, 1),
    ),
    (
        "opening shortens the printed date",
        date(2026, 9, 1),
        date(2026, 8, 1),
        3,
        date(2026, 8, 4),
    ),
    (
        "opening never extends it: the earlier of the two wins",
        date(2026, 8, 2),
        date(2026, 8, 1),
        180,
        date(2026, 8, 2),
    ),
    (
        "an unresolved family clamps nothing rather than inventing a duration",
        date(2026, 9, 1),
        date(2026, 8, 1),
        None,
        date(2026, 9, 1),
    ),
    (
        "an opened lot with no printed date is governed by the opening alone",
        None,
        date(2026, 8, 1),
        3,
        date(2026, 8, 4),
    ),
    (
        "opened, no printed date, no duration: still nothing known",
        None,
        date(2026, 8, 1),
        None,
        None,
    ),
    (
        "the same day counts as day zero, so a one-day family expires tomorrow",
        None,
        date(2026, 8, 1),
        1,
        date(2026, 8, 2),
    ),
]


@pytest.mark.parametrize(
    ("best_before", "opened_at", "opened_days", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_the_rule_in_python(
    best_before: date | None,
    opened_at: date | None,
    opened_days: int | None,
    expected: date | None,
) -> None:
    assert effective_expiry(best_before, opened_at, opened_days) == expected


@pytest.mark.parametrize(
    ("best_before", "opened_at", "opened_days", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_postgresql_agrees_with_python(
    db_session: AsyncSession,
    best_before: date | None,
    opened_at: date | None,
    opened_days: int | None,
    expected: date | None,
) -> None:
    """``LEAST`` over the same three values, evaluated by the server.

    Spelled out here rather than driven through :func:`effective_expiry_sql`
    because what is under test is the *operator*: the helper only wires the two
    columns into it, and a test that went through the helper would prove nothing
    about the NULL handling it depends on.
    """
    answer = await db_session.scalar(
        sa.select(
            sa.func.least(
                sa.literal(best_before, sa.Date()),
                sa.literal(opened_at, sa.Date()) + sa.literal(opened_days, sa.SmallInteger()),
                type_=sa.Date(),
            )
        )
    )
    assert answer == expected
    assert answer == effective_expiry(best_before, opened_at, opened_days)
