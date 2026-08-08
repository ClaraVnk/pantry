"""Shopping budget: what was spent, what it does not cover, and an optional target.

The four operations of contract 6ter. The shape of the answer carries two
decisions that the frozen contract states in prose rather than in JSON, and both
are visible in :class:`BudgetOut`.

**One amount per currency, in a list.** The contract's example object shows
``currency``, ``spent``, ``receipt_count``, ``target`` and
``line_sum_mismatch_count`` side by side, and its prose forbids ever adding two
currencies together. A single flat object can only satisfy both when a household
shops in one currency. Every one of those fields therefore keeps its contract
name and moves, together, into ``currencies`` -- one entry for a household that
shops at home, several for one that crossed a border, never a conversion. The
period-level fields (``period``, ``period_start``, ``period_end``, ``coverage``)
stay where the example puts them, because none of them depends on a currency.

**Amounts are strings.** JSON numbers are IEEE 754 doubles in every mainstream
parser, and this module exists to be exact about money. Same rule as
``quantity.amount`` in ``api/schemas.py``.

The request and response models live here rather than in ``api/schemas.py``, the
way ``routers/calendar.py`` keeps its own: they are used by one router, and
nothing else in the application has a reason to import them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from chaudron.api.deps import BudgetReadDep, MemberDep, SessionDep
from chaudron.domain.models import BudgetPeriod
from chaudron.services.budget import (
    MAX_HISTORY_PERIODS,
    BudgetService,
    CurrencySpend,
    PeriodSpend,
    SetTargetCommand,
    TargetView,
)

router = APIRouter(prefix="/v1/budget", tags=["budget"])

#: ``numeric(12,2)``: two decimals, so ``400`` renders as ``"400.00"`` as in the
#: contract's example.
_MONEY_QUANTUM: Final = Decimal("0.01")

#: The contract's own default for the history call.
_DEFAULT_HISTORY_COUNT: Final = 6

#: What the contract's query parameter accepts, and nothing else.
PeriodParam = Literal["week", "month"]


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored -- as everywhere else at the boundary."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class CoverageOut(BaseModel):
    """What the displayed amount does not count.

    Every field is always present, including at zero. An absent field and a zero
    would say the same thing to a naive client -- "nothing is missing" -- when
    one of the two means "we do not know" (the rule §8 of the contract states for
    ``uncategorised_product_count``).
    """

    receipts_with_total: int
    receipts_missing_total: int
    stock_items_added_without_receipt: int


class CurrencySpendOut(BaseModel):
    currency: str
    spent: Decimal
    receipt_count: int
    #: Receipts whose printed total and line sum disagree. Never reconciled: it
    #: is the best available signal that a line was invented
    #: (``docs/technical-notes-ingestion.md`` section 3.4).
    line_sum_mismatch_count: int
    #: ``null`` unless the household set a target *in this currency*. Optional by
    #: design, and it triggers a display and nothing else.
    target: Decimal | None

    @field_serializer("spent")
    def _serialise_spent(self, value: Decimal) -> str:
        return str(value.quantize(_MONEY_QUANTUM))

    @field_serializer("target")
    def _serialise_target(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(_MONEY_QUANTUM))


class BudgetOut(BaseModel):
    period: PeriodParam
    period_start: date
    period_end: date
    currencies: list[CurrencySpendOut]
    coverage: CoverageOut


class BudgetHistoryOut(BaseModel):
    """Complete periods only, oldest first.

    The period in progress is excluded on purpose: it is incomplete by
    definition, and plotted next to finished ones it makes the last point look
    like a collapse every day of the month but the last. ``GET /v1/budget`` is
    where the current period lives.
    """

    period: PeriodParam
    periods: list[BudgetOut]


class TargetOut(BaseModel):
    period: PeriodParam
    amount: Decimal
    currency: str

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> str:
        return str(value.quantize(_MONEY_QUANTUM))


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class SetTargetIn(StrictModel):
    """``{"period": "month", "amount": "400.00", "currency": "EUR"}``.

    ``amount`` is accepted as a string as well as a number, because that is how
    it is rendered and how a client that round-trips the response will send it
    back. Pydantic parses both into :class:`~decimal.Decimal` without ever
    passing through ``float``.
    """

    period: PeriodParam
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("9999999999"), decimal_places=2)]
    #: ISO 4217, uppercase. Not validated against a list of live currencies: a
    #: closed list is a maintenance burden that would reject a legitimate code
    #: the day a country renames its money.
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=BudgetOut,
    summary="What this household spent over the calendar period containing a date",
)
async def read_budget(
    household_id: BudgetReadDep,
    session: SessionDep,
    period: PeriodParam = "month",
    at: Annotated[date | None, Query()] = None,
) -> BudgetOut:
    """The spend observed over the period containing ``at`` (default: today).

    Calendar month, week starting Monday, in the household's own timezone. The
    amount comes from ``receipt.total_amount`` and never from the sum of the
    receipt lines -- see ``services/budget.py`` for why that distinction decides
    whether any of this is trustworthy.
    """
    service = BudgetService(session)
    return _to_budget_out(await service.current(household_id, period=_as_period(period), at=at))


@router.get(
    "/history",
    response_model=BudgetHistoryOut,
    summary="The complete periods before the current one, for a trend",
)
async def read_budget_history(
    household_id: BudgetReadDep,
    session: SessionDep,
    period: PeriodParam = "month",
    count: Annotated[int, Query(ge=1, le=MAX_HISTORY_PERIODS)] = _DEFAULT_HISTORY_COUNT,
    at: Annotated[date | None, Query()] = None,
) -> BudgetHistoryOut:
    service = BudgetService(session)
    spans = await service.history(household_id, period=_as_period(period), count=count, at=at)
    return BudgetHistoryOut(period=period, periods=[_to_budget_out(span) for span in spans])


@router.get(
    "/target",
    response_model=TargetOut | None,
    summary="The household's target for a period, if it set one",
)
async def read_budget_target(
    household_id: BudgetReadDep,
    session: SessionDep,
    period: PeriodParam = "month",
) -> TargetOut | None:
    """``null`` when no target is set, which is the normal state.

    Not in the contract's table, and additive by construction: a client may
    ignore it entirely, since ``GET /v1/budget`` already carries the target
    alongside the spending it applies to. It exists so the settings form can be
    rendered without asking for a period's figures first.
    """
    service = BudgetService(session)
    found = await service.target(household_id, _as_period(period))
    return None if found is None else _to_target_out(found)


@router.put(
    "/target",
    response_model=TargetOut,
    summary="Set or replace an optional spending target",
)
async def set_budget_target(
    household_id: MemberDep,
    session: SessionDep,
    body: SetTargetIn,
) -> TargetOut:
    """At most one target per period; a second ``PUT`` replaces the first.

    Setting one changes what a screen displays and nothing else: no alert, no
    block, no nudge. Overspending on food is information, not a fault.
    """
    service = BudgetService(session)
    stored = await service.set_target(
        household_id,
        SetTargetCommand(
            period=_as_period(body.period), amount=body.amount, currency=body.currency
        ),
    )
    return _to_target_out(stored)


@router.delete(
    "/target",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop tracking a target",
)
async def clear_budget_target(
    household_id: MemberDep,
    session: SessionDep,
    period: Annotated[PeriodParam | None, Query()] = None,
) -> Response:
    """Idempotent: deleting a target nobody set is a success, not a 404.

    ``period`` omitted clears every target the household has, which is what the
    contract's bare ``DELETE /v1/budget/target`` asks for -- stop tracking, not
    stop tracking whichever of two the user may not remember setting.
    """
    service = BudgetService(session)
    await service.clear_target(household_id, None if period is None else _as_period(period))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


def _as_period(value: PeriodParam) -> BudgetPeriod:
    return BudgetPeriod(value)


def _to_currency_out(spend: CurrencySpend) -> CurrencySpendOut:
    return CurrencySpendOut(
        currency=spend.currency,
        spent=spend.spent,
        receipt_count=spend.receipt_count,
        line_sum_mismatch_count=spend.line_sum_mismatch_count,
        target=spend.target,
    )


def _to_budget_out(span: PeriodSpend) -> BudgetOut:
    return BudgetOut(
        period=_period_param(span.period),
        period_start=span.period_start,
        period_end=span.period_end,
        currencies=[_to_currency_out(entry) for entry in span.currencies],
        coverage=CoverageOut(
            receipts_with_total=span.coverage.receipts_with_total,
            receipts_missing_total=span.coverage.receipts_missing_total,
            stock_items_added_without_receipt=span.coverage.stock_items_added_without_receipt,
        ),
    )


def _to_target_out(target: TargetView) -> TargetOut:
    return TargetOut(
        period=_period_param(target.period),
        amount=target.amount,
        currency=target.currency,
    )


def _period_param(period: BudgetPeriod) -> PeriodParam:
    return "week" if period is BudgetPeriod.WEEK else "month"
