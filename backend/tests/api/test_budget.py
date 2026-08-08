"""The budget endpoints, against a real PostgreSQL.

Receipt import does not exist yet: no code path in the application writes a
``receipt`` row today. Everything here therefore seeds receipts directly, which
is the honest way to test a read model whose writer is still to be built -- and
which is also why the assertions below are about *arithmetic and coverage*
rather than about extraction quality.

The assertions that matter most are the ones nobody would write by accident:

* the spend is the printed total even when the lines say something else;
* two currencies stay two numbers;
* a receipt with no total is counted as missing, not as zero;
* an empty period is empty, not "0.00".
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    Receipt,
    ReceiptLine,
    ReceiptLineMatchStatus,
    ReceiptStatus,
)
from tests.conftest import MakeHousehold, TenantPair, household_headers

#: A month with a 31st, and one whose week boundaries were checked by hand:
#: 2026-08-03 is a Monday.
AUGUST = date(2026, 8, 15)


class MakeReceipt(Protocol):
    async def __call__(
        self,
        household: Household,
        *,
        purchased_at: datetime | None = ...,
        total: Decimal | None = ...,
        currency: str | None = ...,
        status: ReceiptStatus = ...,
        line_prices: tuple[Decimal | None, ...] = ...,
    ) -> Receipt: ...


@pytest.fixture
def make_receipt(db_session: AsyncSession) -> MakeReceipt:
    """Seed a receipt and its lines. No application code writes these yet."""

    async def _make_receipt(
        household: Household,
        *,
        purchased_at: datetime | None = None,
        total: Decimal | None = None,
        currency: str | None = "EUR",
        status: ReceiptStatus = ReceiptStatus.PARSED,
        line_prices: tuple[Decimal | None, ...] = (),
    ) -> Receipt:
        marker = uuid.uuid7().bytes
        receipt = Receipt(
            household_id=household.id,
            image_object_key=f"households/{household.id}/{uuid.uuid7()}",
            image_sha256=hashlib.sha256(marker).hexdigest(),
            status=status,
            merchant_name="Supermarché",
            purchased_at=purchased_at,
            total_amount=total,
            currency=currency,
        )
        db_session.add(receipt)
        await db_session.flush()

        for index, price in enumerate(line_prices):
            db_session.add(
                ReceiptLine(
                    household_id=household.id,
                    receipt_id=receipt.id,
                    line_no=index + 1,
                    raw_label=f"ARTICLE {index + 1}",
                    total_price=price,
                    match_status=ReceiptLineMatchStatus.PENDING,
                )
            )
        await db_session.flush()
        return receipt

    return _make_receipt


async def _budget(
    client: httpx.AsyncClient,
    household: Household,
    *,
    period: str = "month",
    at: date | None = None,
) -> dict[str, Any]:
    query = {"period": period}
    if at is not None:
        query["at"] = at.isoformat()
    response = await client.get("/v1/budget", headers=household_headers(household), params=query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _only(body: dict[str, Any]) -> dict[str, Any]:
    """The single currency of a single-currency period."""
    currencies = body["currencies"]
    assert len(currencies) == 1, currencies
    entry: dict[str, Any] = currencies[0]
    return entry


# --------------------------------------------------------------------------- #
# The rule the whole feature rests on
# --------------------------------------------------------------------------- #


async def test_spend_is_the_printed_total_not_the_sum_of_lines(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """The lines say 40.00, the till says 31.20. The budget says 31.20.

    ``docs/technical-notes-ingestion.md`` section 3.4: models fabricate lines to
    make the arithmetic land on the printed total, so the lines are the least
    trustworthy field on the ticket and the total the most.
    """
    await make_receipt(
        tenant_pair.household_a,
        purchased_at=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        total=Decimal("31.20"),
        line_prices=(Decimal("20.00"), Decimal("20.00")),
    )

    entry = _only(await _budget(api_client, tenant_pair.household_a, at=AUGUST))
    assert entry["spent"] == "31.20"
    assert entry["receipt_count"] == 1
    # And the disagreement is reported rather than repaired.
    assert entry["line_sum_mismatch_count"] == 1


async def test_matching_lines_are_not_flagged(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    await make_receipt(
        tenant_pair.household_a,
        purchased_at=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        total=Decimal("30.00"),
        line_prices=(Decimal("10.00"), Decimal("20.00")),
    )
    assert (
        _only(await _budget(api_client, tenant_pair.household_a, at=AUGUST))[
            "line_sum_mismatch_count"
        ]
        == 0
    )


async def test_rounding_slack_scales_with_the_number_of_lines(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """Three centimes over three lines is display rounding, not a fabricated line."""
    await make_receipt(
        tenant_pair.household_a,
        purchased_at=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        total=Decimal("30.00"),
        line_prices=(Decimal("10.01"), Decimal("10.01"), Decimal("10.01")),
    )
    assert (
        _only(await _budget(api_client, tenant_pair.household_a, at=AUGUST))[
            "line_sum_mismatch_count"
        ]
        == 0
    )


async def test_a_priceless_line_makes_the_comparison_impossible_not_false(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """A partial line sum can never disprove a total, so it is not allowed to try."""
    await make_receipt(
        tenant_pair.household_a,
        purchased_at=datetime(2026, 8, 12, 17, 0, tzinfo=UTC),
        total=Decimal("30.00"),
        line_prices=(Decimal("10.00"), None),
    )
    entry = _only(await _budget(api_client, tenant_pair.household_a, at=AUGUST))
    assert entry["spent"] == "30.00"
    assert entry["line_sum_mismatch_count"] == 0


# --------------------------------------------------------------------------- #
# Period boundaries
# --------------------------------------------------------------------------- #


async def test_month_boundaries_are_calendar_boundaries(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    for moment in (
        datetime(2026, 7, 31, 23, 59, tzinfo=UTC),  # out, by one minute
        datetime(2026, 8, 1, 0, 0, tzinfo=UTC),  # in, first instant
        datetime(2026, 8, 31, 23, 59, tzinfo=UTC),  # in, last instant
        datetime(2026, 9, 1, 0, 0, tzinfo=UTC),  # out, by one minute
    ):
        await make_receipt(household, purchased_at=moment, total=Decimal("10.00"))

    body = await _budget(api_client, household, at=AUGUST)
    assert body["period_start"] == "2026-08-01"
    assert body["period_end"] == "2026-08-31"
    assert _only(body)["spent"] == "20.00"
    assert body["coverage"]["receipts_with_total"] == 2


async def test_the_week_starts_on_monday(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    # 2026-08-02 is a Sunday, 2026-08-03 the Monday that opens the next week.
    await make_receipt(
        household, purchased_at=datetime(2026, 8, 2, 18, 0, tzinfo=UTC), total=Decimal("10.00")
    )
    await make_receipt(
        household, purchased_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC), total=Decimal("25.00")
    )

    monday_week = await _budget(api_client, household, period="week", at=date(2026, 8, 5))
    assert monday_week["period_start"] == "2026-08-03"
    assert monday_week["period_end"] == "2026-08-09"
    assert _only(monday_week)["spent"] == "25.00"

    sunday_week = await _budget(api_client, household, period="week", at=date(2026, 8, 2))
    assert sunday_week["period_start"] == "2026-07-27"
    assert _only(sunday_week)["spent"] == "10.00"


async def test_the_household_timezone_decides_the_day(
    api_client: httpx.AsyncClient,
    tenant_pair: TenantPair,
    make_receipt: MakeReceipt,
    db_session: AsyncSession,
) -> None:
    """A Sunday-evening shop in Zurich is not a Monday shop.

    22:30 local on 2026-08-02 is 20:30 UTC the same day, so this passes either
    way; 00:30 local on 2026-08-03 is 22:30 UTC on the 2nd, and *that* is the
    one a UTC-only implementation files under the wrong week.
    """
    household = tenant_pair.household_a
    household.timezone = "Europe/Zurich"
    await db_session.flush()

    await make_receipt(
        household, purchased_at=datetime(2026, 8, 2, 22, 30, tzinfo=UTC), total=Decimal("42.00")
    )

    week = await _budget(api_client, household, period="week", at=date(2026, 8, 5))
    assert week["period_start"] == "2026-08-03"
    assert _only(week)["spent"] == "42.00"


# --------------------------------------------------------------------------- #
# Currencies
# --------------------------------------------------------------------------- #


async def test_currencies_are_reported_separately_and_never_added(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("30.00"),
        currency="EUR",
    )
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        total=Decimal("50.00"),
        currency="CHF",
    )

    body = await _budget(api_client, household, at=AUGUST)
    by_code = {entry["currency"]: entry for entry in body["currencies"]}
    assert set(by_code) == {"CHF", "EUR"}
    assert by_code["EUR"]["spent"] == "30.00"
    assert by_code["CHF"]["spent"] == "50.00"
    # No total, no conversion, no third entry pretending to be the sum.
    assert len(body["currencies"]) == 2


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


async def test_a_receipt_without_a_total_is_missing_not_zero(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    await make_receipt(
        household, purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC), total=Decimal("18.40")
    )
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        total=None,
        status=ReceiptStatus.FAILED,
    )

    body = await _budget(api_client, household, at=AUGUST)
    assert _only(body)["spent"] == "18.40"
    assert body["coverage"]["receipts_with_total"] == 1
    assert body["coverage"]["receipts_missing_total"] == 1


async def test_a_total_without_a_currency_cannot_be_spent(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """An amount that belongs to no currency can be added to nothing."""
    household = tenant_pair.household_a
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("18.40"),
        currency=None,
    )
    body = await _budget(api_client, household, at=AUGUST)
    assert body["currencies"] == []
    assert body["coverage"]["receipts_missing_total"] == 1


async def test_stock_entered_without_a_receipt_is_counted(
    api_client: httpx.AsyncClient,
    tenant_pair: TenantPair,
    make_location: Any,
    make_product: Any,
) -> None:
    """The field that says what the displayed amount does not count.

    The lot is created through the real endpoint, because "entered by hand" is
    exactly the path that produces a priceless item.
    """
    household = tenant_pair.household_a
    product = await make_product(name="Farine T55")
    location = await make_location(household, name="Placard")

    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "location_id": str(location.id),
            "amount": "1",
            "unit": "kg",
        },
    )
    assert created.status_code == 201, created.text

    today = datetime.now(UTC).date()
    body = await _budget(api_client, household, at=today)
    assert body["coverage"]["stock_items_added_without_receipt"] == 1
    assert body["coverage"]["receipts_with_total"] == 0


# --------------------------------------------------------------------------- #
# Empty period, isolation, history
# --------------------------------------------------------------------------- #


async def test_an_empty_period_reports_no_currency_at_all(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    """No receipts means no amount -- not "0.00", which reads as "you spent nothing"."""
    body = await _budget(api_client, tenant_pair.household_a, at=AUGUST)
    assert body["currencies"] == []
    assert body["coverage"] == {
        "receipts_with_total": 0,
        "receipts_missing_total": 0,
        "stock_items_added_without_receipt": 0,
    }


async def test_one_household_never_sees_another_budget(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    await make_receipt(
        tenant_pair.household_b,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("99.99"),
    )
    body = await _budget(api_client, tenant_pair.household_a, at=AUGUST)
    assert body["currencies"] == []
    assert body["coverage"]["receipts_with_total"] == 0

    seen_by_owner = await _budget(api_client, tenant_pair.household_b, at=AUGUST)
    assert _only(seen_by_owner)["spent"] == "99.99"


async def test_history_returns_complete_periods_oldest_first(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    for month, amount in ((6, "10.00"), (7, "20.00"), (8, "30.00")):
        await make_receipt(
            household,
            purchased_at=datetime(2026, month, 10, 12, 0, tzinfo=UTC),
            total=Decimal(amount),
        )

    response = await api_client.get(
        "/v1/budget/history",
        headers=household_headers(household),
        params={"period": "month", "count": 2, "at": AUGUST.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert [span["period_start"] for span in body["periods"]] == ["2026-06-01", "2026-07-01"]
    assert [_only(span)["spent"] for span in body["periods"]] == ["10.00", "20.00"]
    # August is the period in progress and is deliberately absent: an incomplete
    # period plotted next to finished ones always reads as a collapse.


async def test_history_carries_no_target(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """Today's target is not evidence of what last month's target was.

    Nothing records the history of a target -- deliberately, since it would be a
    record of how a family's means moved -- so a past period reporting one would
    be asserting something nobody ever wrote down.
    """
    household = tenant_pair.household_a
    await api_client.put(
        "/v1/budget/target",
        headers=household_headers(household),
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )
    await make_receipt(
        household,
        purchased_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        total=Decimal("20.00"),
    )

    response = await api_client.get(
        "/v1/budget/history",
        headers=household_headers(household),
        params={"period": "month", "count": 1, "at": AUGUST.isoformat()},
    )
    assert _only(response.json()["periods"][0])["target"] is None
    # ...while the period in progress still carries it.
    assert _only(await _budget(api_client, household, at=AUGUST))["target"] == "400.00"


# --------------------------------------------------------------------------- #
# The optional target
# --------------------------------------------------------------------------- #


async def test_no_target_is_the_normal_state(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    await make_receipt(
        tenant_pair.household_a,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("12.00"),
    )
    assert _only(await _budget(api_client, tenant_pair.household_a, at=AUGUST))["target"] is None

    response = await api_client.get(
        "/v1/budget/target", headers=household_headers(tenant_pair.household_a)
    )
    assert response.status_code == 200
    assert response.json() is None


async def test_setting_a_target_twice_replaces_it(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    household = tenant_pair.household_a
    headers = household_headers(household)

    first = await api_client.put(
        "/v1/budget/target",
        headers=headers,
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"period": "month", "amount": "400.00", "currency": "EUR"}

    second = await api_client.put(
        "/v1/budget/target",
        headers=headers,
        json={"period": "month", "amount": "350", "currency": "CHF"},
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"period": "month", "amount": "350.00", "currency": "CHF"}

    # One row, not two: a household that raised its target does not now have a
    # screen showing both figures.
    body = await _budget(api_client, household, at=AUGUST)
    assert body["currencies"] == [
        {
            "currency": "CHF",
            "spent": "0.00",
            "receipt_count": 0,
            "line_sum_mismatch_count": 0,
            "target": "350.00",
        }
    ]


async def test_a_target_only_applies_to_its_own_currency(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    household = tenant_pair.household_a
    await api_client.put(
        "/v1/budget/target",
        headers=household_headers(household),
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("50.00"),
        currency="CHF",
    )

    body = await _budget(api_client, household, at=AUGUST)
    by_code = {entry["currency"]: entry for entry in body["currencies"]}
    assert by_code["EUR"]["target"] == "400.00"
    assert by_code["CHF"]["target"] is None


async def test_exceeding_a_target_changes_nothing_but_the_numbers(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_receipt: MakeReceipt
) -> None:
    """Overspending is a fact reported by two fields, not an error or a warning."""
    household = tenant_pair.household_a
    await api_client.put(
        "/v1/budget/target",
        headers=household_headers(household),
        json={"period": "month", "amount": "100.00", "currency": "EUR"},
    )
    await make_receipt(
        household,
        purchased_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        total=Decimal("250.00"),
    )

    response = await api_client.get(
        "/v1/budget", headers=household_headers(household), params={"at": AUGUST.isoformat()}
    )
    assert response.status_code == 200
    entry = _only(response.json())
    assert entry["spent"] == "250.00"
    assert entry["target"] == "100.00"


async def test_deleting_a_target_is_idempotent(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    headers = household_headers(tenant_pair.household_a)
    await api_client.put(
        "/v1/budget/target",
        headers=headers,
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )

    assert (await api_client.delete("/v1/budget/target", headers=headers)).status_code == 204
    # Deleting what is no longer there is a success, not a 404.
    assert (await api_client.delete("/v1/budget/target", headers=headers)).status_code == 204
    assert (await api_client.get("/v1/budget/target", headers=headers)).json() is None


async def test_a_target_belongs_to_one_household(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    await api_client.put(
        "/v1/budget/target",
        headers=household_headers(tenant_pair.household_a),
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )
    seen_by_b = await api_client.get(
        "/v1/budget/target", headers=household_headers(tenant_pair.household_b)
    )
    assert seen_by_b.json() is None


async def test_targets_are_per_period(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    headers = household_headers(tenant_pair.household_a)
    await api_client.put(
        "/v1/budget/target",
        headers=headers,
        json={"period": "month", "amount": "400.00", "currency": "EUR"},
    )
    await api_client.put(
        "/v1/budget/target",
        headers=headers,
        json={"period": "week", "amount": "90.00", "currency": "EUR"},
    )

    monthly = await api_client.get("/v1/budget/target", headers=headers, params={"period": "month"})
    weekly = await api_client.get("/v1/budget/target", headers=headers, params={"period": "week"})
    assert monthly.json()["amount"] == "400.00"
    assert weekly.json()["amount"] == "90.00"

    # The bare DELETE of the contract stops tracking, full stop.
    assert (await api_client.delete("/v1/budget/target", headers=headers)).status_code == 204
    assert (
        await api_client.get("/v1/budget/target", headers=headers, params={"period": "week"})
    ).json() is None


@pytest.mark.parametrize(
    "body",
    [
        {"period": "month", "amount": "0", "currency": "EUR"},
        {"period": "month", "amount": "-5.00", "currency": "EUR"},
        {"period": "month", "amount": "10.00", "currency": "eur"},
        {"period": "month", "amount": "10.00", "currency": "EURO"},
        {"period": "quarter", "amount": "10.00", "currency": "EUR"},
        {"period": "month", "amount": "10.00", "currency": "EUR", "rollover": True},
    ],
)
async def test_a_malformed_target_is_refused(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, body: dict[str, Any]
) -> None:
    response = await api_client.put(
        "/v1/budget/target", headers=household_headers(tenant_pair.household_a), json=body
    )
    assert response.status_code == 422, response.text


async def test_the_budget_needs_a_session(anonymous_client: httpx.AsyncClient) -> None:
    """Same 401 as everywhere else: spending is not readable without a caller."""
    assert (await anonymous_client.get("/v1/budget")).status_code == 401
    assert (await anonymous_client.get("/v1/budget/history")).status_code == 401
    assert (
        await anonymous_client.put(
            "/v1/budget/target", json={"period": "month", "amount": "1.00", "currency": "EUR"}
        )
    ).status_code == 401
    assert (await anonymous_client.delete("/v1/budget/target")).status_code == 401


async def test_the_budget_needs_a_household_the_caller_belongs_to(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Signed in is not entitled: the header selects, the membership authorises."""
    await make_household(name="Mine")
    theirs = await make_household(name="Theirs", member=False)
    assert (
        await api_client.get("/v1/budget", headers=household_headers(theirs))
    ).status_code == 403
