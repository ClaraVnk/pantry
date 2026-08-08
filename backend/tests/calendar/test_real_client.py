"""Verification by a CalDAV client this project did not write.

Everything else in this directory asserts that the server produces what the
server intended. That proves nothing about interoperability, which is the only
property that matters here: a feed no phone can consume is not a feed. So this
module runs the application behind a real socket and points **python-caldav** at
it -- the reference client implementation, which performs the same conversation
an account setup performs:

``current-user-principal`` → ``calendar-home-set`` → enumerate collections →
``calendar-query`` for ``VTODO`` → parse the returned iCalendar.

If any property this server emits is malformed, mis-namespaced or missing from
that path, the client fails here rather than silently showing an empty list on
somebody's phone.

**What this does not prove.** It does not prove that *iOS Reminders* displays
the collection: that needs a device, and no test in this repository can stand in
for one. What it does prove is that a conforming RFC 4791 client completes
discovery and retrieves the tasks -- which is the part that was in this project's
hands. The remaining risk is documented in ``docs/calendar-feed.md`` rather than
papered over.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from chaudron.domain.models import QuantityDimension
from chaudron.infra.calendar.credentials import FeedHousehold, FeedKeyring
from tests.calendar.conftest import MakeFridge, MakeLot
from tests.conftest import TenantPair

caldav = pytest.importorskip("caldav", reason="python-caldav is a dev dependency")

#: Five seconds' worth of polling for a loopback listener that normally takes
#: milliseconds. Bounded so a broken server fails rather than hangs.
_STARTUP_POLL_SECONDS = 0.01
_STARTUP_POLLS = 500


@pytest.fixture
async def live_server(calendar_app: FastAPI) -> AsyncIterator[str]:
    """The application on a loopback port, on this test's event loop.

    Same loop on purpose: the session dependency is overridden with the test's
    transaction, and a server on a loop of its own could not use it. The client
    below therefore runs in a worker thread while this loop serves it.

    Port ``0`` lets the kernel choose, so the suite never collides with whatever
    else is running on this machine.
    """
    config = uvicorn.Config(
        calendar_app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    # `uvicorn.Server` exposes a boolean rather than an event, so polling it is
    # the only way to know it is listening. Bounded, so a server that never
    # starts fails the test instead of hanging the suite.
    for _ in range(_STARTUP_POLLS):
        if server.started:
            break
        await asyncio.sleep(_STARTUP_POLL_SECONDS)
    assert server.started, "the test server never began listening"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving
        await calendar_app.state.catalog.aclose()
        await calendar_app.state.database.dispose()


def _collect(url: str, feed_id: str, secret: str) -> list[dict[str, Any]]:
    """Run the whole client conversation, synchronously, in a worker thread."""
    with caldav.DAVClient(url=f"{url}/caldav/", username=feed_id, password=secret) as client:
        principal = client.principal()
        calendars = principal.calendars()
        assert calendars, "the client found no calendar collection"
        found = []
        for todo in calendars[0].todos():
            component = todo.icalendar_component
            found.append(
                {
                    "summary": str(component.get("SUMMARY")),
                    "location": str(component.get("LOCATION") or ""),
                    "due": component.get("DUE").dt,
                    "uid": str(component.get("UID")),
                }
            )
        return found


async def test_a_real_caldav_client_discovers_and_reads_the_feed(
    live_server: str,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
    make_fridge: MakeFridge,
) -> None:
    fridge = await make_fridge(tenant_pair.household_a)
    due = datetime.now(UTC).date() + timedelta(days=3)
    await make_lot(
        tenant_pair.household_a,
        best_before=due,
        name="Yaourt nature",
        quantity="4",
        unit="piece",
        dimension=QuantityDimension.COUNT,
        location=fridge,
    )
    credentials = keyring.credentials_for(FeedHousehold(id=tenant_pair.household_a.id))

    todos = await asyncio.to_thread(_collect, live_server, credentials.feed_id, credentials.secret)

    assert len(todos) == 1
    assert todos[0]["summary"] == "Yaourt nature — 4 pc"
    assert todos[0]["location"] == "Frigo"
    assert todos[0]["due"] == due


async def test_a_real_client_sees_only_its_own_household(
    live_server: str,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """The isolation property, checked through the client rather than the wire."""
    await make_lot(tenant_pair.household_a, best_before=None, name="Chez A")
    await make_lot(
        tenant_pair.household_a,
        best_before=datetime.now(UTC).date() + timedelta(days=2),
        name="Chez A daté",
    )
    await make_lot(
        tenant_pair.household_b,
        best_before=datetime.now(UTC).date() + timedelta(days=2),
        name="Chez B daté",
    )
    credentials = keyring.credentials_for(FeedHousehold(id=tenant_pair.household_b.id))

    todos = await asyncio.to_thread(_collect, live_server, credentials.feed_id, credentials.secret)

    summaries = {todo["summary"] for todo in todos}
    assert summaries == {"Chez B daté — 1 L"}


async def test_a_real_client_is_refused_with_a_wrong_password(
    live_server: str, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    credentials = keyring.credentials_for(FeedHousehold(id=tenant_pair.household_a.id))
    with pytest.raises(caldav.lib.error.AuthorizationError):
        await asyncio.to_thread(_collect, live_server, credentials.feed_id, "A" * 32)


def _sync_twice(url: str, feed_id: str, secret: str) -> tuple[int, int]:
    """Initial ``sync-collection``, then a second one with the token it returned.

    This is the loop a subscribed client runs forever, and the one place where a
    server without change history can get it wrong. What is verified is the
    common path: nothing changed between the two calls, so the second must be
    cheap and must not disturb the client.
    """
    with caldav.DAVClient(url=f"{url}/caldav/", username=feed_id, password=secret) as client:
        calendar = client.principal().calendars()[0]
        collection = calendar.objects_by_sync_token(load_objects=True)
        first = len(list(collection))
        collection.sync()
        return first, len(list(collection))


async def test_a_real_client_can_run_the_synchronisation_loop(
    live_server: str,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    await make_lot(
        tenant_pair.household_a,
        best_before=datetime.now(UTC).date() + timedelta(days=5),
        name="Crème fraîche",
    )
    credentials = keyring.credentials_for(FeedHousehold(id=tenant_pair.household_a.id))

    first, second = await asyncio.to_thread(
        _sync_twice, live_server, credentials.feed_id, credentials.secret
    )

    assert first == 1
    assert second == 1
