"""The inventory endpoints, along the paths a client actually walks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import SessionDep, get_depletion_service
from chaudron.domain.models import (
    InventoryLot,
    StockMovement,
    StockMovementKind,
    StorageKind,
)
from chaudron.domain.shopping import DepletionEvent, DepletionProposal
from chaudron.services.shopping_import import DepletionService
from tests.api.conftest import MakeLocation, MakeProduct
from tests.conftest import MakeHousehold, household_headers


async def test_create_read_and_page_through_the_inventory(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    milk = await make_product(name="Lait demi-écrémé", gtin="03033490004743")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(milk.id),
            "location_id": str(fridge.id),
            "amount": "1.5",
            "unit": "l",
            "expires_on": "2099-01-31",
            "expiry_kind": "use_by",
            "source": "barcode_scan",
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()
    # A string, not a number: the contract is explicit, and a float here loses
    # decimals on a quantity of food.
    assert item["quantity"] == {"amount": "1.500", "unit": "l"}
    assert item["product"]["gtin"] == "3033490004743", "the padding is a storage detail"
    assert item["location"]["kind"] == "fridge"
    assert item["source"] == "barcode_scan"
    assert item["created_at"].endswith("Z")

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert [row["id"] for row in body["items"]] == [item["id"]]

    page = await api_client.get("/v1/inventory?limit=1&offset=1", headers=headers)
    assert page.json() == {"total": 1, "items": []}


async def test_filters_narrow_the_listing(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    pantry = await make_location(household, name="Placard", kind=StorageKind.PANTRY)
    milk = await make_product(name="Lait demi-écrémé")
    rice = await make_product(name="Riz basmati", brand="Taureau Ailé")

    soon = (date.today() + timedelta(days=2)).isoformat()  # noqa: DTZ011 - calendar date
    late = (date.today() + timedelta(days=200)).isoformat()  # noqa: DTZ011 - calendar date
    for product, location, expires in ((milk, fridge, soon), (rice, pantry, late)):
        response = await api_client.post(
            "/v1/inventory",
            headers=headers,
            json={
                "product_id": str(product.id),
                "location_id": str(location.id),
                "amount": "1",
                "unit": "kg",
                "expires_on": expires,
                "expiry_kind": "best_before",
            },
        )
        assert response.status_code == 201, response.text

    by_location = await api_client.get(f"/v1/inventory?location_id={pantry.id}", headers=headers)
    assert [row["product"]["name"] for row in by_location.json()["items"]] == ["Riz basmati"]

    by_query = await api_client.get("/v1/inventory?q=lait", headers=headers)
    assert by_query.json()["total"] == 1

    by_brand = await api_client.get("/v1/inventory?q=Taureau", headers=headers)
    assert by_brand.json()["total"] == 1

    expiring = await api_client.get("/v1/inventory?expiring_within_days=7", headers=headers)
    assert [row["product"]["name"] for row in expiring.json()["items"]] == ["Lait demi-écrémé"]


async def test_scanning_the_same_pack_twice_merges_into_one_lot(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """500 g then 1 kg of the same flour is one lot of 1.5 kg, not two lines."""
    household = await make_household()
    headers = household_headers(household)
    pantry = await make_location(household, name="Placard", kind=StorageKind.PANTRY)
    flour = await make_product(name="Farine T55", brand="Francine")

    first = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(flour.id),
            "location_id": str(pantry.id),
            "amount": "1",
            "unit": "kg",
        },
    )
    assert first.status_code == 201, first.text

    second = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(flour.id),
            "location_id": str(pantry.id),
            "amount": "500",
            "unit": "g",
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    # Re-expressed in the unit already on the lot, never contradicting the
    # canonical total.
    assert second.json()["quantity"] == {"amount": "1.500", "unit": "kg"}

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert listing.json()["total"] == 1

    movements = (
        await db_session.scalars(
            select(StockMovement).where(StockMovement.household_id == household.id)
        )
    ).all()
    assert [movement.kind for movement in movements] == [
        StockMovementKind.INTAKE,
        StockMovementKind.INTAKE,
    ]


async def test_patch_changes_only_what_it_names(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    freezer = await make_location(household, name="Congélateur", kind=StorageKind.FREEZER)
    peas = await make_product(name="Petits pois", brand="Bonduelle")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(peas.id),
            "location_id": str(fridge.id),
            "amount": "750",
            "unit": "g",
            "expires_on": "2099-03-01",
            "expiry_kind": "best_before",
        },
    )
    item_id = created.json()["id"]

    patched = await api_client.patch(
        f"/v1/inventory/{item_id}",
        headers=headers,
        json={"amount": "500", "location_id": str(freezer.id)},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["quantity"] == {"amount": "500.000", "unit": "g"}
    assert body["location"]["id"] == str(freezer.id)
    assert body["expires_on"] == "2099-03-01", "an unsent field is not a cleared field"

    cleared = await api_client.patch(
        f"/v1/inventory/{item_id}", headers=headers, json={"expires_on": None}
    )
    assert cleared.json()["expires_on"] is None
    assert cleared.json()["expiry_kind"] == "unknown"

    adjustments = (
        await db_session.scalars(
            select(StockMovement).where(
                StockMovement.household_id == household.id,
                StockMovement.kind == StockMovementKind.ADJUSTMENT,
            )
        )
    ).all()
    assert len(adjustments) == 1
    assert adjustments[0].delta_canonical == -250


async def test_delete_records_the_reason_in_the_ledger(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """Waste and consumption are different facts; the ledger must keep them apart."""
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household)
    yoghurt = await make_product(name="Yaourt nature", brand="Danone")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(yoghurt.id),
            "location_id": str(fridge.id),
            "amount": "4",
            "unit": "piece",
        },
    )
    item_id = uuid.UUID(created.json()["id"])

    deleted = await api_client.delete(f"/v1/inventory/{item_id}?reason=wasted", headers=headers)
    # 200, not the 204 contract v1 froze: the body carries `depleted` (contract
    # v1.1 section 6bis), and a 204 has no body to carry it in.
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["depleted"]["reason"] == "wasted"

    assert (await api_client.get("/v1/inventory", headers=headers)).json()["total"] == 0

    lot = await db_session.get(InventoryLot, item_id)
    assert lot is not None, "a removal is a state change, never a DELETE"
    assert lot.depleted_at is not None

    movement = (
        await db_session.scalars(
            select(StockMovement).where(
                StockMovement.inventory_lot_id == item_id,
                StockMovement.kind == StockMovementKind.WASTE,
            )
        )
    ).one()
    assert movement.delta_canonical == -4
    assert movement.reason == "wasted"


async def test_delete_defaults_to_consumed(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    bread = await make_product(name="Pain de campagne", brand=None)

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={"product_id": str(bread.id), "amount": "1", "unit": "piece"},
    )
    item_id = uuid.UUID(created.json()["id"])

    assert (await api_client.delete(f"/v1/inventory/{item_id}", headers=headers)).status_code == 200
    movement = (
        await db_session.scalars(
            select(StockMovement).where(StockMovement.inventory_lot_id == item_id)
        )
    ).all()
    assert {entry.kind for entry in movement} == {
        StockMovementKind.INTAKE,
        StockMovementKind.CONSUMPTION,
    }


async def test_inline_product_creation(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Manual entry is a first-class path: no barcode, no pre-existing product."""
    household = await make_household()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product": {"name": "Carottes du marché", "default_unit": "g"},
            "amount": "800",
            "unit": "g",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["product"]["name"] == "Carottes du marché"


# --------------------------------------------------------------------------- #
# The repurchase proposal, where the inventory endpoints meet contract 6bis
# --------------------------------------------------------------------------- #
#
# What the service decides is tested in `tests/api/test_shopping_lists.py`. What
# is tested here is the wiring: that the proposal reaches the response of the two
# endpoints that can empty a lot, that it is shaped as the contract says, and --
# the point of the whole section -- that it can never cost the user the mutation
# they actually asked for.


async def _stocked(
    api_client: httpx.AsyncClient,
    headers: dict[str, str],
    product_id: uuid.UUID,
    *,
    amount: str = "1",
    unit: str = "piece",
) -> uuid.UUID:
    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={"product_id": str(product_id), "amount": amount, "unit": unit},
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["id"])


async def test_finishing_a_lot_proposes_buying_it_again(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """The whole point of section 6bis: it offers, and it does not add."""
    household = await make_household()
    headers = household_headers(household)
    milk = await make_product(name="Lait demi-écrémé UHT")
    item_id = await _stocked(api_client, headers, milk.id, amount="1", unit="l")

    removed = await api_client.delete(f"/v1/inventory/{item_id}", headers=headers)

    assert removed.status_code == 200, removed.text
    assert removed.json()["depleted"] == {
        "product_id": str(milk.id),
        "product_name": "Lait demi-écrémé UHT",
        "reason": "consumed",
        "already_on_list": False,
        "previously_declined": False,
    }
    listed = await api_client.get("/v1/shopping-lists/current", headers=headers)
    assert listed.json()["items"] == [], "a proposal that adds by itself is not a proposal"


async def test_a_correction_removes_the_lot_and_proposes_nothing(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """The rule that must never be inverted (contract 6bis).

    Fixing a mistyped 500 g into nothing is not finishing the product. The removal
    still happens -- only the offer to buy it again does not.
    """
    household = await make_household()
    headers = household_headers(household)
    flour = await make_product(name="Farine T55")
    item_id = await _stocked(api_client, headers, flour.id, amount="500", unit="g")

    removed = await api_client.delete(f"/v1/inventory/{item_id}?reason=correction", headers=headers)

    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert "depleted" in body, "the field is always present; a client must not have to guess"
    assert body["depleted"] is None
    lot = await db_session.get(InventoryLot, item_id)
    assert lot is not None and lot.depleted_at is not None


async def test_a_declined_product_is_reported_as_declined_on_removal(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """The refusal outlives the request that made it (contract 6bis, migration 0007)."""
    household = await make_household()
    headers = household_headers(household)
    sprouts = await make_product(name="Chou de Bruxelles")
    declined = await api_client.post(
        "/v1/shopping-lists/declined", headers=headers, json={"product_ids": [str(sprouts.id)]}
    )
    assert declined.status_code == 204, declined.text
    item_id = await _stocked(api_client, headers, sprouts.id)

    removed = await api_client.delete(f"/v1/inventory/{item_id}", headers=headers)

    assert removed.json()["depleted"]["previously_declined"] is True


async def test_the_field_is_on_the_mutations_and_not_on_the_listing(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """A page of 200 rows must not carry 200 nulls.

    ``PATCH`` answers the item plus ``depleted`` flat, as contract 6bis writes it;
    ``GET`` answers the item and nothing else. Same item, two shapes, on purpose.
    """
    household = await make_household()
    headers = household_headers(household)
    rice = await make_product(name="Riz basmati")
    item_id = await _stocked(api_client, headers, rice.id, amount="1", unit="kg")

    patched = await api_client.patch(
        f"/v1/inventory/{item_id}", headers=headers, json={"amount": "0.5"}
    )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["quantity"] == {"amount": "0.500", "unit": "kg"}
    # A correction is not a depletion, and an adjustment cannot reach zero anyway:
    # a non-positive amount is refused, so emptying a lot goes through DELETE.
    assert body["depleted"] is None

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert "depleted" not in listing.json()["items"][0]


async def test_a_broken_proposal_never_costs_the_user_the_removal(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """The courtesy fails; the deletion stands.

    The failure injected is a *database* error, because that is the one that has
    teeth: a statement that raises poisons the whole transaction, so a bare
    ``except`` around the proposal would let the request die at ``COMMIT`` and roll
    back the removal the user watched succeed. The savepoint is what makes the
    ``except`` honest, and this test is the only thing that proves it.
    """
    household = await make_household()
    headers = household_headers(household)
    eggs = await make_product(name="Œufs plein air")
    item_id = await _stocked(api_client, headers, eggs.id, amount="6")

    def broken(session: SessionDep) -> DepletionService:
        return _BrokenDepletionService(session)

    api_app.dependency_overrides[get_depletion_service] = broken
    try:
        removed = await api_client.delete(f"/v1/inventory/{item_id}", headers=headers)
    finally:
        del api_app.dependency_overrides[get_depletion_service]

    assert removed.status_code == 200, removed.text
    assert removed.json()["depleted"] is None
    lot = await db_session.get(InventoryLot, item_id)
    assert lot is not None and lot.depleted_at is not None, "the removal was rolled back"
    movements = (
        await db_session.scalars(
            select(StockMovement).where(
                StockMovement.inventory_lot_id == item_id,
                StockMovement.kind == StockMovementKind.CONSUMPTION,
            )
        )
    ).all()
    assert len(movements) == 1, "the ledger entry went with it"


class _BrokenDepletionService(DepletionService):
    """A depletion service that fails where a real one would: in the database."""

    async def propose(
        self, household_id: uuid.UUID, events: Sequence[DepletionEvent]
    ) -> tuple[DepletionProposal, ...]:
        await self._session.execute(text("SELECT 1 FROM a_table_that_does_not_exist"))
        raise AssertionError("unreachable: the statement above raises")
