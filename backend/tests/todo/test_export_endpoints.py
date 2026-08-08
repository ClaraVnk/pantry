"""The two endpoints, against a real PostgreSQL and a scripted Todoist.

The database half matters because the read is the part that can be quietly wrong:
row-level security, the outer joins onto ``product`` and ``unit``, the
``checked_at IS NULL`` filter and the ordering are all things a unit test with a
hand-built list would assert about itself rather than about the schema.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    Product,
    ProductSource,
    QuantityDimension,
    ShoppingItemOrigin,
    ShoppingList,
    ShoppingListItem,
)
from chaudron.infra.todo.factory import ShoppingExportFactory
from chaudron.infra.todo.settings import TodoExportSettings
from tests.conftest import MakeHousehold, household_headers
from tests.todo.conftest import FAKE_TOKEN, TodoistDouble


async def _seed_list(session: AsyncSession, household: Household) -> ShoppingList:
    """A list with the four shapes an item can take, in a deliberate order."""
    shopping_list = ShoppingList(household_id=household.id, name="Courses", is_default=True)
    session.add(shopping_list)
    await session.flush()

    product = Product(household_id=household.id, name="Yaourt nature", source=ProductSource.MANUAL)
    session.add(product)
    await session.flush()

    session.add_all(
        [
            # Free text with a mass quantity.
            ShoppingListItem(
                household_id=household.id,
                shopping_list_id=shopping_list.id,
                label="Pommes de terre",
                quantity_value=Decimal("2"),
                quantity_unit_code="kg",
                quantity_dimension=QuantityDimension.MASS,
                origin=ShoppingItemOrigin.MANUAL,
                sort_order=10,
            ),
            # A catalogue product, counted.
            ShoppingListItem(
                household_id=household.id,
                shopping_list_id=shopping_list.id,
                product_id=product.id,
                quantity_value=Decimal("4"),
                quantity_unit_code="piece",
                quantity_dimension=QuantityDimension.COUNT,
                origin=ShoppingItemOrigin.MANUAL,
                sort_order=20,
            ),
            # No quantity at all -- the common case.
            ShoppingListItem(
                household_id=household.id,
                shopping_list_id=shopping_list.id,
                label="Du pain",
                origin=ShoppingItemOrigin.MANUAL,
                sort_order=30,
            ),
            # Already ticked: must not be exported.
            ShoppingListItem(
                household_id=household.id,
                shopping_list_id=shopping_list.id,
                label="Café",
                origin=ShoppingItemOrigin.MANUAL,
                sort_order=40,
                checked_at=dt.datetime.now(dt.UTC),
            ),
        ]
    )
    await session.flush()
    return shopping_list


async def test_the_text_export_is_the_list_one_item_per_line(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)

    response = await api_client.get(
        f"/v1/shopping-lists/{shopping_list.id}/export/text",
        headers=household_headers(household),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert "charset=utf-8" in response.headers["content-type"]
    assert response.text == "Pommes de terre 2 kg\nYaourt nature \u00d7 4\nDu pain"
    assert "Café" not in response.text, "a ticked item is work already done"


async def test_the_text_export_refuses_another_household_s_list(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Not "empty", not "403": the same 404 as a list that does not exist.

    Telling a caller that a list exists but is not theirs is an oracle for
    enumerating households (the reasoning of AUD-013, applied here).
    """
    owner = await make_household()
    stranger = await make_household()
    shopping_list = await _seed_list(db_session, owner)

    response = await api_client.get(
        f"/v1/shopping-lists/{shopping_list.id}/export/text",
        headers=household_headers(stranger),
    )

    assert response.status_code == 404


async def test_an_unknown_list_is_a_problem_document(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.get(
        f"/v1/shopping-lists/{uuid.uuid4()}/export/text",
        headers=household_headers(household),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_sending_to_todoist_reports_what_was_accepted(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    todoist: TodoistDouble,
) -> None:
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    api_app.state.shopping_export_factory = ShoppingExportFactory(
        TodoExportSettings(todoist_household_id=household.id, todoist_token=FAKE_TOKEN),
        transport=todoist.transport(),
    )

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == "todoist"
    assert body["exported_item_count"] == 3
    contents = [command["args"]["content"] for command in todoist.calls[0].commands]
    assert contents == ["Pommes de terre 2 kg", "Yaourt nature \u00d7 4", "Du pain"]


async def test_an_export_to_an_unconfigured_destination_explains_itself(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    api_app.state.shopping_export_factory = ShoppingExportFactory(TodoExportSettings())

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["type"].endswith("export-target-not-configured")
    assert body["supported"] == ["todoist"]


async def test_bring_is_refused_by_name(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """ADR-0010 declines Bring! for the reason ADR-0002 declined retailer drives.

    Asserted rather than merely written down: "we decided not to" is the kind of
    decision that gets quietly undone by a later pull request, and a test is the
    only form of a decision that argues back.
    """
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    api_app.state.shopping_export_factory = ShoppingExportFactory(TodoExportSettings())

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/bring",
        headers=household_headers(household),
    )

    assert response.status_code == 409
    assert "bring" not in response.json()["supported"]


async def test_an_export_requires_a_resolved_household(
    api_client: httpx.AsyncClient,
) -> None:
    """No household resolves, so nothing is exported.

    ``403`` rather than the ``401`` this asserted before authentication landed,
    and the change of code is the change of meaning: the caller *is* signed in --
    ``api_client`` carries a session -- and simply belongs to no household yet, so
    there is nothing for ``X-Household-Id`` to select (``api/deps.py``). ``401``
    would now say "log in", which is advice that cannot help.
    """
    response = await api_client.get(f"/v1/shopping-lists/{uuid.uuid4()}/export/text")
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/v1/shopping-lists/{shopping_list_id}/export/text",
        "/v1/shopping-lists/{shopping_list_id}/export/{target}",
    ],
)
def test_the_routes_are_registered_on_the_application(api_app: FastAPI, path: str) -> None:
    """The one line this change adds to ``api/main.py`` is load-bearing.

    Read off the OpenAPI document rather than ``app.routes``: FastAPI wraps an
    included router in an opaque object with no ``path``, so walking the route
    table would assert nothing while appearing to assert something.
    """
    assert path in api_app.openapi()["paths"]


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_slug"),
    [
        (401, 409, "export-target-rejected"),
        (429, 429, "export-rate-limited"),
        (503, 502, "export-target-unavailable"),
    ],
)
async def test_a_failing_destination_becomes_an_actionable_problem_document(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    todoist: TodoistDouble,
    status: int,
    expected_status: int,
    expected_slug: str,
) -> None:
    """Three failures, three answers. "Export failed" cannot be told apart from a
    revoked token, a throttle or an outage -- and the three have three remedies."""
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    todoist.responder = lambda _uuids: httpx.Response(status, text="denied")
    api_app.state.shopping_export_factory = ShoppingExportFactory(
        TodoExportSettings(todoist_household_id=household.id, todoist_token=FAKE_TOKEN),
        transport=todoist.transport(),
    )

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == expected_status
    assert response.json()["type"].endswith(expected_slug)


async def test_a_partial_export_is_reported_as_a_failure_with_its_counts(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    todoist: TodoistDouble,
) -> None:
    """Not 200 with a warning. A client that only checks the status code must not
    tell the user their list was sent."""
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    todoist.responder = lambda uuids: httpx.Response(
        200, json={"sync_status": {uuids[0]: "ok"}, "temp_id_mapping": {}}
    )
    api_app.state.shopping_export_factory = ShoppingExportFactory(
        TodoExportSettings(todoist_household_id=household.id, todoist_token=FAKE_TOKEN),
        transport=todoist.transport(),
    )

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == 502
    body = response.json()
    assert body["type"].endswith("export-partially-applied")
    assert (body["exported_item_count"], body["rejected_item_count"]) == (1, 2)


async def test_no_error_response_carries_the_token(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    todoist: TodoistDouble,
) -> None:
    """End to end, through the real error handlers: a destination that echoes the
    credential it rejected must not have it reflected to the browser."""
    household = await make_household()
    shopping_list = await _seed_list(db_session, household)
    todoist.responder = lambda _uuids: httpx.Response(401, text=f"bad Bearer {FAKE_TOKEN}")
    api_app.state.shopping_export_factory = ShoppingExportFactory(
        TodoExportSettings(todoist_household_id=household.id, todoist_token=FAKE_TOKEN),
        transport=todoist.transport(),
    )

    response = await api_client.post(
        f"/v1/shopping-lists/{shopping_list.id}/export/todoist",
        headers=household_headers(household),
    )

    assert FAKE_TOKEN not in response.text
