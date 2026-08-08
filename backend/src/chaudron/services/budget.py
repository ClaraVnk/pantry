"""What a household spent on groceries, and what it optionally aims to spend.

Contract 6ter. Three decisions here are the feature, and every one of them is
counter-intuitive enough to be undone by a well-meaning refactor.

**The spend is ``receipt.total_amount``. Never ``SUM(receipt_line.total_price)``.**
``docs/technical-notes-ingestion.md`` section 3.4 reports, over 10 656 real
receipts, that vision models *fabricate or alter lines to force their sum onto
the printed total* -- the line-item field scores 0.49 to 0.64 F1 while every
other field clears 0.85. A receipt whose arithmetic adds up is therefore not
evidence of correctness; it is sometimes the symptom of the opposite. The total
is one number, printed large, that a human checks at a glance on the review
screen. The lines are thirty numbers nobody checks. So the budget rests on the
one, and the inventory -- which has no choice -- rests on the thirty.

The line sum *is* computed here, for exactly one purpose: to be compared with
the total and counted when the two disagree
(:attr:`CurrencySpend.line_sum_mismatch_count`). A disagreement is never
silently corrected. It is the best signal available that a line was invented,
and correcting it would destroy the signal to make a screen look tidier.

**Currencies are never added together.** ``receipt.currency`` is per receipt; a
period holding two currencies yields two amounts, never one converted amount.
Converting would need a rate, a rate date, and a decision that belongs to a
bank rather than to a pantry application.

**Coverage travels with every figure.** The budget only knows what came in on a
receipt. An item typed in by hand or scanned from a barcode has no price at
all, so "you spent 240 EUR this month" with half the shopping unscanned is a
lie by omission -- the same defect §8 of the contract fixed with
``uncategorised_product_count``. :class:`BudgetCoverage` says out loud what the
displayed amount does not count.

Two smaller rules, recorded because they are judgement calls rather than
deductions:

*A receipt counts as soon as it carries a total and a currency* -- it does not
have to be ``confirmed``. ``confirmed`` means the *lines* were accepted into
stock, and making the money wait on an inventory chore would hide spending
behind the very step §6ter separates it from ("on the same photo, the budget is
more reliable than the inventory"). A receipt with no total lands in
``receipts_missing_total`` whatever its status -- ``failed`` included, which is
precisely the case that field exists to make visible.

*A receipt with no purchase date is dated by its import.* The money was spent;
dropping the receipt because the date could not be read would understate the
spend, which is the one failure mode this whole module exists to avoid.
"""

from __future__ import annotations

import calendar
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    BudgetPeriod,
    BudgetTarget,
    Household,
    InventoryLot,
    Receipt,
    ReceiptLine,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_HISTORY_PERIODS",
    "BudgetCoverage",
    "BudgetService",
    "CurrencySpend",
    "PeriodSpend",
    "SetTargetCommand",
    "TargetView",
    "period_bounds",
    "previous_period_start",
]

#: Rounding slack allowed between the printed total and the sum of the lines,
#: **per line of the receipt**.
#:
#: Two centimes of display rounding on a forty-line receipt is not a fabricated
#: line, and flagging it would train the reader to ignore the flag. A fabricated
#: line, on the other hand, moves the sum by the price of a product -- one to two
#: orders of magnitude above this bound. The tolerance therefore scales with the
#: number of lines, because that is what rounding does; it deliberately does not
#: scale with the amount, because a fabricated line on a large receipt is no less
#: fabricated.
#:
#: ``docs/technical-notes-ingestion.md`` section 3.5 tolerates 1 to 3 centimes of
#: VAT rounding on a whole receipt; one centime per line is the same order of
#: magnitude expressed per unit, which is the unit rounding actually happens in.
LINE_SUM_TOLERANCE_PER_LINE: Final = Decimal("0.01")

#: Periods one history call may return. Two years of months, or roughly half a
#: year of weeks -- past that the answer is a data export, not a trend.
MAX_HISTORY_PERIODS: Final = 24

#: Money is ``numeric(12,2)``.
_MONEY_QUANTUM: Final = Decimal("0.01")

_UTC_NAME: Final = "UTC"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BudgetCoverage:
    """What the displayed spend does not count.

    Always rendered next to the amount, never behind a disclosure control. A
    coverage figure a user has to click to discover is a coverage figure that
    does not exist.
    """

    receipts_with_total: int
    receipts_missing_total: int
    #: Lots entered over the period with no receipt line behind them: manual
    #: entries and barcode scans. They have no price and can never acquire one.
    stock_items_added_without_receipt: int


@dataclass(frozen=True, slots=True)
class CurrencySpend:
    """One currency's worth of a period. Never merged with another one."""

    currency: str
    spent: Decimal
    receipt_count: int
    #: Receipts whose printed total and line sum disagree beyond the tolerance.
    #: Reported, never reconciled -- see the module docstring.
    line_sum_mismatch_count: int
    #: The household's target for this period, when it is denominated in this
    #: currency. ``None`` is the normal state: a target is optional.
    target: Decimal | None


@dataclass(frozen=True, slots=True)
class PeriodSpend:
    """A calendar period, its coverage, and one amount per currency."""

    period: BudgetPeriod
    period_start: date
    period_end: date
    currencies: tuple[CurrencySpend, ...]
    coverage: BudgetCoverage


@dataclass(frozen=True, slots=True)
class TargetView:
    period: BudgetPeriod
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class SetTargetCommand:
    period: BudgetPeriod
    amount: Decimal
    currency: str


# --------------------------------------------------------------------------- #
# Calendar arithmetic
# --------------------------------------------------------------------------- #


def period_bounds(period: BudgetPeriod, at: date) -> tuple[date, date]:
    """First and last day, both inclusive, of the period containing *at*.

    Calendar, not rolling, and the week starts on Monday (contract 6ter). A
    rolling window would make "what have I spent this month" a different
    question every day, so two readings a day apart could not be compared -- and
    comparing them is the entire point of the screen.
    """
    if period is BudgetPeriod.WEEK:
        start = at - timedelta(days=at.weekday())
        return start, start + timedelta(days=6)
    start = at.replace(day=1)
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


def previous_period_start(period: BudgetPeriod, start: date) -> date:
    """The first day of the period immediately before the one starting at *start*."""
    if period is BudgetPeriod.WEEK:
        return start - timedelta(days=7)
    return (start - timedelta(days=1)).replace(day=1)


def _bucket_start(period: BudgetPeriod, day: date) -> date:
    return period_bounds(period, day)[0]


def resolve_zone(name: str | None) -> ZoneInfo:
    """The household's zone, or UTC when it names one this machine does not have.

    A period boundary is a local midnight: computing it in UTC would move a
    Sunday-evening shop into the following week for anybody east of Greenwich.
    An unknown zone is logged and degrades to UTC rather than failing the read --
    a budget screen that 500s because a timezone database is a version behind
    helps nobody.
    """
    if name is None:
        return ZoneInfo(_UTC_NAME)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        logger.warning("budget_unknown_timezone", extra={"timezone": name})
        return ZoneInfo(_UTC_NAME)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _ReceiptFacts:
    """One receipt, reduced to what the budget needs to know about it."""

    local_date: date
    currency: str | None
    total_amount: Decimal | None
    line_count: int
    priceless_line_count: int
    line_sum: Decimal | None


class BudgetService:
    """Reads receipts and stock, reads and writes the optional target.

    Takes the session directly, like :class:`ShoppingListService`: one reader,
    four tables, no second consumer. Never commits -- the request's transaction
    does that (``infra/db.py``).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- reads -------------------------------------------------------------- #

    async def current(
        self, household_id: uuid.UUID, *, period: BudgetPeriod, at: date | None = None
    ) -> PeriodSpend:
        """The period containing *at*, defaulting to today in the household's zone."""
        zone = resolve_zone(await self._timezone(household_id))
        day = at if at is not None else datetime.now(zone).date()
        start, end = period_bounds(period, day)
        spans = await self._collect(household_id, period, zone, start, end)
        return spans[start]

    async def history(
        self, household_id: uuid.UUID, *, period: BudgetPeriod, count: int, at: date | None = None
    ) -> tuple[PeriodSpend, ...]:
        """The *count* complete periods before the one containing *at*, oldest first.

        The current period is deliberately excluded. It is incomplete by
        definition, so plotting it beside finished ones makes the last point
        always look like a drop -- a trend line that lies every day of the month
        except the last. ``GET /v1/budget`` is where the period in progress
        lives.

        Past periods carry **no target**. A target is a current intention with no
        history behind it (there is no ``budget_target`` archive, on purpose), so
        stamping today's figure onto last May would assert something nobody
        recorded -- and a reader comparing May against "its" target would be
        comparing it against a number that did not exist then.
        """
        zone = resolve_zone(await self._timezone(household_id))
        day = at if at is not None else datetime.now(zone).date()
        bounded = max(0, min(count, MAX_HISTORY_PERIODS))

        starts: list[date] = []
        cursor = period_bounds(period, day)[0]
        for _ in range(bounded):
            cursor = previous_period_start(period, cursor)
            starts.append(cursor)
        starts.reverse()
        if not starts:
            return ()

        spans = await self._collect(
            household_id,
            period,
            zone,
            starts[0],
            period_bounds(period, starts[-1])[1],
            with_target=False,
        )
        return tuple(spans[start] for start in starts)

    async def target(self, household_id: uuid.UUID, period: BudgetPeriod) -> TargetView | None:
        row = await self._target_row(household_id, period)
        return None if row is None else _to_target_view(row)

    # -- writes ------------------------------------------------------------- #

    async def set_target(self, household_id: uuid.UUID, command: SetTargetCommand) -> TargetView:
        """Create or replace this household's target for one period.

        A replacement, not a second row: the unique constraint on
        ``(household_id, period)`` means there is never a set of targets the
        interface would have to pick a winner from.
        """
        row = await self._target_row(household_id, command.period)
        if row is None:
            row = BudgetTarget(
                household_id=household_id,
                period=command.period,
                amount=command.amount,
                currency=command.currency,
            )
            self._session.add(row)
        else:
            row.amount = command.amount
            row.currency = command.currency
        await self._session.flush()
        logger.info(
            "budget_target_set",
            extra={"period": command.period.value, "currency": command.currency},
            # The amount is not logged. It is a household's means, which
            # `docs/security-model.md` classifies alongside the inventory as an
            # A3 asset, and a log file is where such a value quietly outlives
            # every retention promise made about the database.
        )
        return _to_target_view(row)

    async def clear_target(
        self, household_id: uuid.UUID, period: BudgetPeriod | None = None
    ) -> int:
        """Stop tracking a target. Idempotent; returns how many rows went away.

        ``period`` absent means every period, which is what the bare
        ``DELETE /v1/budget/target`` of the contract asks for: stop tracking, not
        stop tracking one of two things the user may not remember setting.
        """
        statement = (
            sa.delete(BudgetTarget)
            .where(BudgetTarget.household_id == household_id)
            # ``RETURNING`` rather than ``rowcount``: the count is then a typed
            # sequence of the identifiers actually removed, which is also what
            # tells a caller "nothing was there" apart from "the driver did not
            # say".
            .returning(BudgetTarget.id)
        )
        if period is not None:
            statement = statement.where(BudgetTarget.period == period)
        removed = len((await self._session.execute(statement)).all())
        await self._session.flush()
        if removed:
            logger.info(
                "budget_target_cleared", extra={"period": None if period is None else period.value}
            )
        return int(removed)

    # -- internals ---------------------------------------------------------- #

    async def _collect(
        self,
        household_id: uuid.UUID,
        period: BudgetPeriod,
        zone: ZoneInfo,
        since: date,
        until: date,
        with_target: bool = True,
    ) -> dict[date, PeriodSpend]:
        """Every period between *since* and *until*, in two queries and no more.

        Bucketing happens in Python from one local date per row rather than in
        SQL: the span is a handful of periods and a few dozen receipts, and a
        ``date_trunc``-per-period query would put the week-starts-on-Monday rule
        in two places written in two languages.
        """
        facts = await self._receipt_facts(household_id, zone, since, until)
        stock = await self._stock_without_receipt(household_id, zone, since, until)
        target = await self._target_row(household_id, period) if with_target else None

        by_bucket: dict[date, list[_ReceiptFacts]] = defaultdict(list)
        for fact in facts:
            by_bucket[_bucket_start(period, fact.local_date)].append(fact)

        stock_by_bucket: dict[date, int] = defaultdict(int)
        for day, count in stock:
            stock_by_bucket[_bucket_start(period, day)] += count

        spans: dict[date, PeriodSpend] = {}
        cursor = since
        while cursor <= until:
            start, end = period_bounds(period, cursor)
            spans[start] = _assemble(
                period=period,
                start=start,
                end=end,
                facts=by_bucket.get(start, ()),
                stock_items_without_receipt=stock_by_bucket.get(start, 0),
                target=None if target is None else _to_target_view(target),
            )
            cursor = end + timedelta(days=1)
        return spans

    async def _timezone(self, household_id: uuid.UUID) -> str | None:
        name = await self._session.scalar(
            sa.select(Household.timezone).where(Household.id == household_id)
        )
        return None if name is None else str(name)

    async def _target_row(
        self, household_id: uuid.UUID, period: BudgetPeriod
    ) -> BudgetTarget | None:
        row: BudgetTarget | None = await self._session.scalar(
            sa.select(BudgetTarget).where(
                # Belt to the row-level policy's braces, as everywhere else: RLS
                # already scopes the statement, and naming the household means a
                # misconfigured session finds nothing rather than somebody else's
                # budget.
                BudgetTarget.household_id == household_id,
                BudgetTarget.period == period,
            )
        )
        return row

    async def _receipt_facts(
        self, household_id: uuid.UUID, zone: ZoneInfo, since: date, until: date
    ) -> tuple[_ReceiptFacts, ...]:
        """One row per receipt, with the line aggregate the mismatch check needs.

        A ``LEFT JOIN``: a receipt with no lines at all is still a receipt, and
        its total is still money spent.
        """
        local_date = self._local_date(
            sa.func.coalesce(Receipt.purchased_at, Receipt.created_at), zone
        )
        statement = (
            sa.select(
                local_date.label("local_date"),
                Receipt.currency,
                Receipt.total_amount,
                sa.func.count(ReceiptLine.id).label("line_count"),
                sa.func.count(ReceiptLine.id)
                .filter(ReceiptLine.total_price.is_(None))
                .label("priceless_line_count"),
                sa.func.sum(ReceiptLine.total_price).label("line_sum"),
            )
            .select_from(Receipt)
            .outerjoin(
                ReceiptLine,
                sa.and_(
                    ReceiptLine.receipt_id == Receipt.id,
                    ReceiptLine.household_id == Receipt.household_id,
                ),
            )
            .where(
                Receipt.household_id == household_id,
                local_date.between(since, until),
            )
            .group_by(Receipt.id, local_date, Receipt.currency, Receipt.total_amount)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple(
            _ReceiptFacts(
                local_date=row.local_date,
                currency=None if row.currency is None else str(row.currency),
                total_amount=_as_decimal(row.total_amount),
                line_count=int(row.line_count),
                priceless_line_count=int(row.priceless_line_count),
                line_sum=_as_decimal(row.line_sum),
            )
            for row in rows
        )

    async def _stock_without_receipt(
        self, household_id: uuid.UUID, zone: ZoneInfo, since: date, until: date
    ) -> tuple[tuple[date, int], ...]:
        """Lots entered over the span that no receipt line produced.

        ``acquired_on`` when the user gave one, the creation instant otherwise:
        the question is "what went into the cupboard this month", and a lot
        entered on Tuesday for Saturday's shop belongs to Saturday.
        """
        local_date = self._local_date(
            sa.func.coalesce(
                sa.cast(InventoryLot.acquired_on, sa.DateTime(timezone=True)),
                InventoryLot.created_at,
            ),
            zone,
        )
        statement = (
            sa.select(local_date.label("local_date"), sa.func.count().label("lot_count"))
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.source_receipt_line_id.is_(None),
                local_date.between(since, until),
            )
            .group_by(local_date)
        )
        rows = (await self._session.execute(statement)).all()
        return tuple((row.local_date, int(row.lot_count)) for row in rows)

    @staticmethod
    def _local_date(moment: sa.ColumnElement[datetime], zone: ZoneInfo) -> sa.ColumnElement[date]:
        """A ``timestamptz`` read as a calendar day in the household's zone.

        ``timezone(name, tstz)`` is PostgreSQL's ``AT TIME ZONE``; the name is a
        bound parameter, never interpolated, and :func:`resolve_zone` has already
        rejected anything the machine's database does not know.
        """
        return sa.cast(sa.func.timezone(zone.key, moment), sa.Date)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _line_sum_disagrees(fact: _ReceiptFacts) -> bool:
    """Whether this receipt's lines contradict its printed total.

    Three cases answer "cannot tell", and all three answer ``False`` rather than
    guessing:

    * no total -- there is nothing to compare against;
    * no lines -- an empty sum is not a contradiction, it is an absence;
    * at least one line with no price -- a partial sum can never disprove a
      total, and reporting a mismatch on the strength of it would cry wolf on
      every half-parsed receipt.

    The third case is a real hole: a model that omits the price of the line it
    invented escapes this check. Nothing here can close it -- the honest answer
    is that the line sum is unknown, and the review screen shows the lines
    themselves.
    """
    if fact.total_amount is None or fact.line_count == 0:
        return False
    if fact.priceless_line_count > 0 or fact.line_sum is None:
        return False
    tolerance = LINE_SUM_TOLERANCE_PER_LINE * fact.line_count
    return abs(fact.line_sum - fact.total_amount) > tolerance


@dataclass(slots=True)
class _CurrencyTally:
    spent: Decimal = Decimal("0")
    receipt_count: int = 0
    line_sum_mismatch_count: int = 0


def _assemble(
    *,
    period: BudgetPeriod,
    start: date,
    end: date,
    facts: Iterable[_ReceiptFacts],
    stock_items_without_receipt: int,
    target: TargetView | None,
) -> PeriodSpend:
    tallies: dict[str, _CurrencyTally] = {}
    with_total = 0
    missing_total = 0

    for fact in facts:
        # A total without a currency is an amount that cannot be added to
        # anything, so it counts as missing rather than as spend in an
        # invented default currency.
        if fact.total_amount is None or fact.currency is None:
            missing_total += 1
            continue
        with_total += 1
        tally = tallies.setdefault(fact.currency, _CurrencyTally())
        tally.spent += fact.total_amount
        tally.receipt_count += 1
        if _line_sum_disagrees(fact):
            tally.line_sum_mismatch_count += 1

    # A target in a currency nothing was spent in still has to appear, or a
    # household that set one and then shopped nowhere would see no target at all
    # and conclude it had been lost.
    if target is not None and target.currency not in tallies:
        tallies[target.currency] = _CurrencyTally()

    currencies = tuple(
        CurrencySpend(
            currency=code,
            spent=tally.spent.quantize(_MONEY_QUANTUM),
            receipt_count=tally.receipt_count,
            line_sum_mismatch_count=tally.line_sum_mismatch_count,
            target=(
                target.amount.quantize(_MONEY_QUANTUM)
                if target is not None and target.currency == code
                else None
            ),
        )
        for code, tally in sorted(tallies.items())
    )

    return PeriodSpend(
        period=period,
        period_start=start,
        period_end=end,
        currencies=currencies,
        coverage=BudgetCoverage(
            receipts_with_total=with_total,
            receipts_missing_total=missing_total,
            stock_items_added_without_receipt=stock_items_without_receipt,
        ),
    )


def _to_target_view(row: BudgetTarget) -> TargetView:
    return TargetView(period=row.period, amount=row.amount, currency=row.currency)


def _as_decimal(value: object) -> Decimal | None:
    """Coerce a numeric column back to :class:`Decimal`, never to ``float``.

    asyncpg already returns ``Decimal`` for ``numeric``; the conversion is here
    so that a driver that ever stopped doing so would fail loudly at this line
    rather than silently introduce binary floating point into a money total.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    raise TypeError(f"expected a numeric column to yield Decimal, got {type(value).__name__}")
