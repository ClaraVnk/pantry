"""``/v1/locations`` -- the endpoint a household with nothing has to reach first.

A freshly registered household owns no storage location, and until this endpoint
existed there was no way to obtain one: the first screen after sign-up was a dead
end. Two things are worth proving beyond the happy path -- that the empty list is
a *state* and not a failure, and that the creation is scoped to the caller's
household like everything else.
"""

from __future__ import annotations

import httpx

from chaudron.domain.models import StorageKind
from tests.api.conftest import MakeLocation, MakeProduct
from tests.conftest import MakeHousehold, household_headers


async def test_a_new_household_lists_no_location_and_can_create_one(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The path a brand-new account walks, in the order it walks it."""
    household = await make_household()
    headers = household_headers(household)

    empty = await api_client.get("/v1/locations", headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json() == [], "no location is an ordinary state, not an error"

    created = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "Frigo", "kind": "fridge"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Frigo"
    assert body["kind"] == "fridge"
    assert body["item_count"] == 0, "a location nothing has been put in holds nothing"

    listing = await api_client.get("/v1/locations", headers=headers)
    assert [row["id"] for row in listing.json()] == [body["id"]]


async def test_locations_are_listed_in_alphabetical_order(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``sort_order`` keeps its default, so the tie is broken by name."""
    household = await make_household()
    headers = household_headers(household)

    for name, kind in (("Placard", "pantry"), ("Cave", "cellar"), ("Frigo", "fridge")):
        response = await api_client.post(
            "/v1/locations", headers=headers, json={"name": name, "kind": kind}
        )
        assert response.status_code == 201, response.text

    listing = await api_client.get("/v1/locations", headers=headers)
    assert [row["name"] for row in listing.json()] == ["Cave", "Frigo", "Placard"]


async def test_a_duplicate_name_is_refused_without_regard_to_case(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``uq_storage_location_name`` is case-insensitive; the answer says so.

    The second half matters as much as the 409: the request that follows a
    rejected one must still work. The insert runs in a savepoint precisely so the
    ``IntegrityError`` does not abort the whole request transaction.
    """
    household = await make_household()
    headers = household_headers(household)

    first = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "Frigo", "kind": "fridge"}
    )
    assert first.status_code == 201, first.text

    clash = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "  frigo  ", "kind": "freezer"}
    )
    assert clash.status_code == 409, clash.text
    problem = clash.json()
    assert problem["type"].endswith("location-name-taken")
    assert "frigo" not in problem["detail"].lower(), "the problem body echoes no user text"

    recovered = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "Congélateur", "kind": "freezer"}
    )
    assert recovered.status_code == 201, recovered.text


async def test_a_blank_name_is_refused_before_it_reaches_the_column(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    headers = household_headers(household)

    for name in ("", "   "):
        response = await api_client.post(
            "/v1/locations", headers=headers, json={"name": name, "kind": "pantry"}
        )
        assert response.status_code == 422, response.text

    too_long = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "x" * 81, "kind": "pantry"}
    )
    assert too_long.status_code == 422, too_long.text


async def test_the_name_is_stored_trimmed(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    headers = household_headers(household)

    created = await api_client.post(
        "/v1/locations", headers=headers, json={"name": "  Cellier  ", "kind": "pantry"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Cellier"


async def test_an_unknown_kind_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``kind`` decides whether expiry is suspended; an invented one cannot pass."""
    household = await make_household()
    response = await api_client.post(
        "/v1/locations",
        headers=household_headers(household),
        json={"name": "Garage", "kind": "garage"},
    )
    assert response.status_code == 422, response.text


async def test_an_unknown_field_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``StrictModel``: a client that meant ``kind`` and wrote ``type`` is told."""
    household = await make_household()
    response = await api_client.post(
        "/v1/locations",
        headers=household_headers(household),
        json={"name": "Frigo", "kind": "fridge", "type": "fridge"},
    )
    assert response.status_code == 422, response.text


async def test_a_location_created_in_one_household_is_invisible_to_another(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The tenant comes from the resolved household, never from the body."""
    first = await make_household()
    second = await make_household()

    created = await api_client.post(
        "/v1/locations", headers=household_headers(first), json={"name": "Frigo", "kind": "fridge"}
    )
    assert created.status_code == 201, created.text

    assert (await api_client.get("/v1/locations", headers=household_headers(second))).json() == []

    # And the same name is free in the other household: uniqueness is per tenant.
    twin = await api_client.post(
        "/v1/locations", headers=household_headers(second), json={"name": "Frigo", "kind": "fridge"}
    )
    assert twin.status_code == 201, twin.text


async def test_creating_a_location_requires_a_session(
    anonymous_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await anonymous_client.post(
        "/v1/locations",
        headers=household_headers(household),
        json={"name": "Frigo", "kind": "fridge"},
    )
    assert response.status_code == 401, response.text


async def test_the_item_count_follows_the_stock(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """A location created empty starts reporting once something is put in it."""
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo", kind=StorageKind.FRIDGE)
    milk = await make_product(name="Lait")

    added = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(milk.id),
            "location_id": str(fridge.id),
            "amount": "1",
            "unit": "l",
            "expires_on": None,
            "expiry_kind": None,
            "source": "manual",
        },
    )
    assert added.status_code == 201, added.text

    listing = await api_client.get("/v1/locations", headers=headers)
    assert [row["item_count"] for row in listing.json()] == [1]
