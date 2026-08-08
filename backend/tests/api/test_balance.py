"""``GET /v1/balance`` -- the week, computed from the ledger and from nothing else.

The movements are written directly rather than through ``DELETE /v1/inventory``.
That is deliberate: what is under test is the aggregation and the kinds it counts,
and driving it through the removal endpoint would make this file fail whenever
that endpoint's response shape changes for reasons of its own.

The three claims:

* only ``consumption`` counts -- waste is food that left without being eaten and
  an adjustment is a typing error being fixed (contract 6);
* a product resolving to no marker counts for no marker *and is counted*;
* the reference version travels with the answer, so a revised benchmark cannot
  rewrite what a household was told last spring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    ExpiryDateKind,
    Household,
    InventoryLot,
    PnnsMarker,
    Product,
    QuantityDimension,
    StockEntrySource,
    StockMovement,
    StockMovementKind,
    StorageLocation,
)
from tests.api.conftest import MakeLocation
from tests.api.test_dietary_suggestions import add_product
from tests.conftest import MakeHousehold, household_headers

BALANCE_URL = "/v1/balance"


async def consume(
    session: AsyncSession,
    household: Household,
    location: StorageLocation,
    product: Product,
    *,
    grams: int = 200,
    kind: StockMovementKind = StockMovementKind.CONSUMPTION,
    days_ago: int = 1,
) -> None:
    """One lot, emptied, and the movement that emptied it.

    ``depleted_at`` is set for two reasons, and both are the schema talking: an
    active lot at zero is an inconsistency the check constraint refuses, and the
    merge key is partial on ``depleted_at IS NULL``, so two live dateless lots of
    the same product in the same place cannot coexist -- which is the whole point
    of that index and not something to work around.
    """
    lot = InventoryLot(
        household_id=household.id,
        product_id=product.id,
        storage_location_id=location.id,
        quantity_value=Decimal(grams),
        quantity_unit_code="g",
        quantity_dimension=QuantityDimension.MASS,
        quantity_canonical=Decimal(0),
        initial_quantity_canonical=Decimal(grams),
        best_before=None,
        date_kind=ExpiryDateKind.UNKNOWN,
        entry_source=StockEntrySource.MANUAL,
        depleted_at=datetime.now(UTC),
    )
    session.add(lot)
    await session.flush()
    session.add(
        StockMovement(
            household_id=household.id,
            inventory_lot_id=lot.id,
            kind=kind,
            delta_canonical=Decimal(-grams),
            quantity_dimension=QuantityDimension.MASS,
            occurred_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
    )
    await session.flush()


async def test_the_week_is_returned_with_its_reference_and_its_blind_spot(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.get(BALANCE_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "reference",
        "window_days",
        "uncategorised_product_count",
        "gaps",
        "excesses",
        "satisfiable_from_stock",
        "note",
    }
    assert body["reference"] == "pnns-2019"
    assert body["window_days"] == 7
    # Always present, even for a household that has consumed nothing at all.
    assert body["uncategorised_product_count"] == 0


async def test_a_shortfall_carries_the_wording_and_the_url_it_comes_from(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A household must be able to open the page this application is quoting.

    That is the entire argument for frequency benchmarks over a score (ADR-0009):
    an opaque number can only be accepted, a published one can be checked.
    """
    household = await make_household()

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()

    fish = next(gap for gap in body["gaps"] if gap["marker"] == "fish")
    assert fish["target"] == "2 par semaine"
    assert fish["observed"] == 0
    assert fish["statement"].startswith("Poisson 2 fois par semaine")
    assert fish["source_url"].startswith("https://www.mangerbouger.fr/")


async def test_consumption_counts_and_waste_does_not(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Counting waste as a meal would tell a household it ate what it binned."""
    household = await make_household()
    location = await make_location(household)
    fish = await add_product(db_session, "Filet de cabillaud", markers=(PnnsMarker.FISH,))
    await consume(db_session, household, location, fish)
    await consume(db_session, household, location, fish, kind=StockMovementKind.WASTE)
    await consume(db_session, household, location, fish, kind=StockMovementKind.ADJUSTMENT)

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()

    gap = next(item for item in body["gaps"] if item["marker"] == "fish")
    assert gap["observed"] == 1, "only the consumption movement is a meal"
    assert gap["shortfall"] == 1


async def test_a_movement_older_than_the_window_is_outside_the_week(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    household = await make_household()
    location = await make_location(household)
    fish = await add_product(db_session, "Filet de cabillaud", markers=(PnnsMarker.FISH,))
    await consume(db_session, household, location, fish, days_ago=9)

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()

    assert next(item for item in body["gaps"] if item["marker"] == "fish")["observed"] == 0


async def test_a_ceiling_in_grams_is_weighed_rather_than_counted(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The two PNNS ceilings are masses, and ``delta_canonical`` is already grams.

    Counting occasions here instead of weighing would report "three servings"
    against a benchmark of five hundred grams.
    """
    household = await make_household()
    location = await make_location(household)
    beef = await add_product(db_session, "Steak haché", markers=(PnnsMarker.RED_MEAT,))
    await consume(db_session, household, location, beef, grams=400)
    await consume(db_session, household, location, beef, grams=380)

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()

    excess = next(item for item in body["excesses"] if item["marker"] == "red_meat")
    assert excess["observed_grams"] == 780
    assert excess["unit"] == "gram"
    assert excess["target"] == "500 g par semaine"


async def test_a_product_that_resolves_to_no_marker_is_counted_as_such(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """A badly catalogued pantry has to look like missing evidence.

    Without this count, the same answer -- "you are one fish short" -- is
    produced by a household that ate no fish and by one whose fish nobody has
    categorised, and only one of the two can argue with it.
    """
    household = await make_household()
    location = await make_location(household)
    mystery = await add_product(db_session, "Truc du marché")
    await consume(db_session, household, location, mystery)

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()

    assert body["uncategorised_product_count"] == 1


async def test_one_household_s_meals_are_invisible_to_another(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """What a home eats is as tenant-scoped as its inventory."""
    household = await make_household()
    stranger = await make_household()
    location = await make_location(household)
    fish = await add_product(db_session, "Filet de cabillaud", markers=(PnnsMarker.FISH,))
    await consume(db_session, household, location, fish)

    body = (await api_client.get(BALANCE_URL, headers=household_headers(stranger))).json()

    assert next(item for item in body["gaps"] if item["marker"] == "fish")["observed"] == 0


async def test_the_ledger_written_by_the_removal_endpoint_is_the_one_that_is_read(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The two halves of ADR-0009's "no separate journal" decision, joined up.

    The balance reads ``stock_movement``; the application writes it when stock
    leaves. If those two ever stop meeting, the weekly figures go quietly to zero
    and nothing else fails.
    """
    household = await make_household()
    location = await make_location(household)
    fish = await add_product(db_session, "Filet de cabillaud", markers=(PnnsMarker.FISH,))
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(fish.id),
            "location_id": str(location.id),
            "amount": "200",
            "unit": "g",
            "expires_on": None,
            "expiry_kind": None,
            "source": "manual",
        },
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    removed = await api_client.delete(
        f"/v1/inventory/{item_id}",
        headers=household_headers(household),
        params={"reason": "consumed"},
    )
    assert removed.status_code in (200, 204), removed.text

    kinds = (
        await db_session.scalars(
            select(StockMovement.kind).where(
                StockMovement.household_id == household.id,
                StockMovement.inventory_lot_id == uuid.UUID(item_id),
            )
        )
    ).all()
    assert StockMovementKind.CONSUMPTION in kinds

    body = (await api_client.get(BALANCE_URL, headers=household_headers(household))).json()
    assert next(item for item in body["gaps"] if item["marker"] == "fish")["observed"] == 1
