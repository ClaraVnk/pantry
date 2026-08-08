"""Every failure answers in the shape the contract fixes, and leaks nothing."""

from __future__ import annotations

import uuid

import httpx

from tests.api.conftest import MakeProduct
from tests.conftest import MakeHousehold, household_headers


async def test_problem_documents_carry_the_contract_fields(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.patch(
        f"/v1/inventory/{uuid.uuid7()}",
        headers=household_headers(household),
        json={"amount": "1"},
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].startswith("https://chaudron.dev/problems/")
    assert set(body) >= {"type", "title", "status", "detail"}
    assert body["status"] == 404


async def test_validation_errors_do_not_echo_the_submitted_values(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A body echoed back is a body written to the client's logs."""
    household = await make_household()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={"amount": "not-a-number", "unit": "g", "product_id": str(uuid.uuid7())},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["type"].endswith("/validation-failed")
    assert "not-a-number" not in response.text
    assert all(set(error) == {"loc", "msg"} for error in body["errors"])


async def test_both_product_forms_at_once_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    household = await make_household()
    product = await make_product()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "product": {"name": "Doublon"},
            "amount": "1",
            "unit": "piece",
        },
    )
    assert response.status_code == 422


async def test_unknown_unit_is_a_422_not_a_500(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    household = await make_household()
    product = await make_product()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={"product_id": str(product.id), "amount": "1", "unit": "cuillère"},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/unknown-unit")


async def test_a_dated_lot_cannot_declare_an_unknown_kind(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    household = await make_household()
    product = await make_product()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "amount": "1",
            "unit": "piece",
            "expires_on": "2099-01-01",
            "expiry_kind": "unknown",
        },
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/expiry-date-inconsistent")


async def test_a_zero_quantity_is_refused_on_every_write_path(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """Refused by the *schema* now, on the create and the patch alike.

    It used to be refused only by ``services/inventory.py``, and the answer was
    ``/invalid-quantity`` rather than ``/validation-failed`` for that reason. The
    domain guard is still there and still the only thing that can express "0.0001 g
    rounds to zero at three decimals"; what changed is that it is no longer alone.
    ``PATCH`` carried no bound at all, so the only thing between ``{"amount": "0"}``
    and ``ck_inventory_lot_quantity_positive`` was that one guard -- and a violated
    CHECK is answered by ``handle_unexpected``, which logs the exception PostgreSQL
    composed by quoting the whole failing row back.
    """
    household = await make_household()
    product = await make_product()
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={"product_id": str(product.id), "amount": "1", "unit": "g"},
    )
    assert created.status_code == 201

    for response in (
        await api_client.post(
            "/v1/inventory",
            headers=household_headers(household),
            json={"product_id": str(product.id), "amount": "0", "unit": "g"},
        ),
        await api_client.patch(
            f"/v1/inventory/{created.json()['id']}",
            headers=household_headers(household),
            json={"amount": "0"},
        ),
    ):
        assert response.status_code == 422
        body = response.json()
        assert body["type"].endswith("/validation-failed")
        assert ["body", "amount"] in [error["loc"] for error in body["errors"]]


async def test_a_quantity_that_rounds_to_zero_is_still_a_domain_refusal(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """The control for the test above: the schema bound does not make the domain
    guard redundant, and ``/invalid-quantity`` is still reachable."""
    household = await make_household()
    product = await make_product()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={"product_id": str(product.id), "amount": "0.0001", "unit": "g"},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/invalid-quantity")


async def test_an_unknown_removal_reason_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    household = await make_household()
    product = await make_product()
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={"product_id": str(product.id), "amount": "1", "unit": "piece"},
    )
    response = await api_client.delete(
        f"/v1/inventory/{created.json()['id']}?reason=exploded",
        headers=household_headers(household),
    )
    assert response.status_code == 422
