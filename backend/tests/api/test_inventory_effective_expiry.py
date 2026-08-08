"""``GET /v1/inventory`` once the after-opening rule is applied.

The case worth the whole feature is the last one here: a pot opened three weeks
ago whose packaging still says next month. Nothing about that lot changes on its
own -- no column moves, no job runs -- so if the listing does not shorten the
date at read time, the household is told the product is fine until the day they
open the fridge and smell it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import FoodFamily, Product, ProductSource
from tests.api.conftest import MakeLocation
from tests.conftest import MakeHousehold, household_headers


def today() -> date:
    return date.today()  # noqa: DTZ011 - a calendar date, as the column is


async def make_family_product(
    session: AsyncSession, *, name: str, family: FoodFamily | None
) -> Product:
    """A public catalogue product, resolved to a keeping family or to none.

    Written here rather than added to ``make_product`` in the shared conftest:
    ``food_family`` is only interesting to this file, and a fixture parameter
    every other test ignores is a fixture parameter nobody maintains.
    """
    product = Product(
        household_id=None,
        name=name,
        source=ProductSource.MANUAL,
        food_family=family,
    )
    session.add(product)
    await session.flush()
    return product


async def add_lot(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    product: Product,
    *,
    expires_on: date | None,
    opened_at: date | None,
    location_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": str(product.id),
        "amount": "1",
        "unit": "kg",
        "expires_on": None if expires_on is None else expires_on.isoformat(),
        "opened_at": None if opened_at is None else opened_at.isoformat(),
    }
    if expires_on is not None:
        payload["expiry_kind"] = "use_by"
    if location_id is not None:
        payload["location_id"] = location_id
    response = await client.post("/v1/inventory", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_an_unopened_lot_is_governed_by_the_date_on_the_pack(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    yoghurt = await make_family_product(
        db_session, name="Yaourt nature", family=FoodFamily.FRESH_DAIRY
    )
    printed = today() + timedelta(days=20)

    created = await add_lot(api_client, headers, yoghurt, expires_on=printed, opened_at=None)

    assert created["expires_on"] == printed.isoformat()
    assert created["effective_expires_on"] == printed.isoformat(), (
        "an unopened lot has nothing to clamp, and the two fields must say the same thing"
    )


async def test_opening_shortens_the_date_without_touching_the_printed_one(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    yoghurt = await make_family_product(
        db_session, name="Yaourt nature", family=FoodFamily.FRESH_DAIRY
    )
    printed = today() + timedelta(days=20)
    opened = today() - timedelta(days=1)

    created = await add_lot(api_client, headers, yoghurt, expires_on=printed, opened_at=opened)

    assert created["expires_on"] == printed.isoformat(), "the label is reported as it was read"
    # Fresh dairy: the three-day ANSES figure, from the seeded guideline row.
    assert created["effective_expires_on"] == (opened + timedelta(days=3)).isoformat()


async def test_a_lot_opened_three_weeks_ago_is_already_expired(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The reason the feature exists, and the one case that cannot be faked.

    Nothing moved: the printed date is still weeks away and the row has not been
    written since. The listing has to reach the same conclusion a nose would.
    """
    household = await make_household()
    headers = household_headers(household)
    cream = await make_family_product(
        db_session, name="Crème fraîche", family=FoodFamily.FRESH_DAIRY
    )
    printed = today() + timedelta(days=30)
    opened = today() - timedelta(days=21)

    created = await add_lot(api_client, headers, cream, expires_on=printed, opened_at=opened)
    assert created["effective_expires_on"] < today().isoformat()

    expiring = await api_client.get("/v1/inventory?expiring_within_days=0", headers=headers)
    assert expiring.status_code == 200
    body = expiring.json()
    assert body["total"] == 1, "an opened lot past its after-opening limit is expiring stock"
    assert [row["id"] for row in body["items"]] == [created["id"]]


async def test_an_opened_lot_with_no_printed_date_acquires_one(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The opening date plus the family's figure is a real answer, not an invention.

    Both inputs are facts: the household said when they opened it, and the
    guideline row cites its source. What would be an invention is a date for a
    lot that was never opened, or one whose product resolved to no family -- and
    the two tests below cover exactly that.
    """
    household = await make_household()
    headers = household_headers(household)
    tin = await make_family_product(db_session, name="Tomates pelées", family=FoodFamily.CANNED)
    opened = today() - timedelta(days=1)

    created = await add_lot(api_client, headers, tin, expires_on=None, opened_at=opened)

    assert created["expires_on"] is None
    assert created["expiry_kind"] == "unknown"
    # Canned: three days once opened and decanted.
    assert created["effective_expires_on"] == (opened + timedelta(days=3)).isoformat()


@pytest.mark.parametrize(
    ("family", "why"),
    [
        (None, "the catalogue resolved no keeping family for this product"),
        (FoodFamily.EGGS, "the family carries no after-opening figure"),
    ],
)
async def test_without_a_duration_the_printed_date_stands_alone(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    family: FoodFamily | None,
    why: str,
) -> None:
    """No guideline, no clamp -- and no invented number to stand in for one."""
    household = await make_household()
    headers = household_headers(household)
    product = await make_family_product(db_session, name="Produit", family=family)
    printed = today() + timedelta(days=30)
    opened = today() - timedelta(days=21)

    created = await add_lot(api_client, headers, product, expires_on=printed, opened_at=opened)

    assert created["effective_expires_on"] == printed.isoformat(), why


async def test_a_lot_that_is_neither_dated_nor_opened_stays_dateless(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    flour = await make_family_product(db_session, name="Farine", family=FoodFamily.DRY_GOODS)

    created = await add_lot(api_client, headers, flour, expires_on=None, opened_at=None)

    assert created["effective_expires_on"] is None

    expiring = await api_client.get("/v1/inventory?expiring_within_days=3650", headers=headers)
    assert expiring.json()["total"] == 0, "a lot with no date at all never enters an expiry filter"


async def test_the_listing_sorts_on_the_effective_date(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """An opened pot outranks a sealed one its printed date puts weeks behind."""
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    opened_cream = await make_family_product(
        db_session, name="Crème entamée", family=FoodFamily.FRESH_DAIRY
    )
    sealed_milk = await make_family_product(
        db_session, name="Lait scellé", family=FoodFamily.FRESH_DAIRY
    )

    await add_lot(
        api_client,
        headers,
        opened_cream,
        expires_on=today() + timedelta(days=30),
        opened_at=today(),
        location_id=str(fridge.id),
    )
    await add_lot(
        api_client,
        headers,
        sealed_milk,
        expires_on=today() + timedelta(days=10),
        opened_at=None,
        location_id=str(fridge.id),
    )

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert [row["product"]["name"] for row in listing.json()["items"]] == [
        "Crème entamée",
        "Lait scellé",
    ], "ordering on the printed date would put the sealed carton first"
