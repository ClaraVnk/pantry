"""The feed over HTTP: authentication, isolation, bounds, and read-only-ness.

These drive the real application through an ASGI transport. What they assert is
what an operator has to be able to promise: that a credential opens exactly one
household, that no credential opens two, that a write is refused rather than
swallowed, and that the response has a size somebody can reason about.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from icalendar import Calendar
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.types import ASGIApp
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE, get_session
from chaudron.api.main import create_app
from chaudron.api.routers.calendar import _FAILED_AUTH_PER_HOUR, authenticate
from chaudron.config import Settings
from chaudron.domain.models import MembershipRole, QuantityDimension
from chaudron.infra.calendar.credentials import (
    FEED_ID_LENGTH,
    FEED_SECRET_LENGTH,
    FeedHousehold,
    FeedKeyring,
)
from chaudron.infra.logging import household_id_var
from chaudron.services.calendar import (
    FUTURE_WINDOW_DAYS,
    MAX_TASKS,
    PAST_WINDOW_DAYS,
    CalendarFeedService,
)
from tests.calendar.conftest import (
    MakeFridge,
    MakeLot,
    basic_auth,
    basic_header,
    calendar_settings,
    feed_id_of,
)
from tests.conftest import MakeHousehold, SignedIn, TenantPair

PROPFIND_ALL = b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
PROPFIND_ETAGS = (
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:getcontenttype/></d:prop></d:propfind>'
)
CALENDAR_QUERY_TODOS = (
    b'<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    b"<D:prop><D:getetag/><C:calendar-data/></D:prop>"
    b'<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VTODO"/>'
    b"</C:comp-filter></C:filter></C:calendar-query>"
)


def today() -> date:
    return datetime.now(UTC).date()


def calendar_path(feed_id: str) -> str:
    return f"/caldav/p/{feed_id}/cal/expiry/"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


async def test_the_feed_challenges_an_anonymous_client(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    response = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    )
    assert response.status_code == 401
    # Without this header a CalDAV client never offers to ask for a password.
    assert response.headers["WWW-Authenticate"].startswith("Basic ")


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer something",
        "Basic not-base64!!",
        f"Basic {base64.b64encode(b'no-separator').decode()}",
        f"Basic {base64.b64encode(b'TOOSHORT:TOOSHORT').decode()}",
    ],
)
async def test_a_malformed_credential_gets_the_same_answer_as_a_wrong_one(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    header: str,
) -> None:
    """Absent, malformed and wrong must be indistinguishable (audit AUD-013)."""
    path = calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    headers = {"Authorization": header} if header else {}
    response = await calendar_client.request("PROPFIND", path, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == (
        "This feed requires the user name and password shown in Chaudron."
    )


async def test_the_identifier_alone_does_not_open_the_feed(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """A URL in a proxy log is not a credential. This is the property."""
    feed_id = feed_id_of(keyring, tenant_pair.household_a)
    guessed = base64.b64encode(f"{feed_id}:{'A' * 32}".encode()).decode()
    response = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id), headers={"Authorization": f"Basic {guessed}"}
    )
    assert response.status_code == 401


async def test_the_feed_is_absent_when_the_instance_has_not_enabled_it(
    initialised_database: str,
    db_session: AsyncSession,
    tenant_pair: TenantPair,
    signed_in: SignedIn,
) -> None:
    """A disabled feed answers 404, not 403: it must look like no such route.

    Valid credentials, and still nothing -- which is what makes an instance that
    never turned the feature on indistinguishable from one that has no such code.
    """
    app = create_app(calendar_settings(initialised_database, enabled=False))

    async def _use_test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    disabled_keyring = FeedKeyring.from_settings(app.state.settings)
    transport = httpx.ASGITransport(app=app)
    # Signed in as the owner of household A, so the subscription route reaches the
    # feature flag rather than stopping at the session check -- which is what makes
    # the 404 below evidence about the *feature* and not about the caller.
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as client:
        response = await client.request(
            "PROPFIND",
            calendar_path(feed_id_of(disabled_keyring, tenant_pair.household_a)),
            headers=basic_auth(disabled_keyring, tenant_pair.household_a),
        )
        subscription = await client.get(
            "/v1/calendar/subscription",
            headers={"X-Household-Id": str(tenant_pair.household_a.id)},
        )
    await app.state.catalog.aclose()
    await app.state.database.dispose()
    assert response.status_code == 404
    assert subscription.status_code == 404


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


async def test_one_credential_never_reaches_another_household(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """The isolation test that matters: B's password on A's path."""
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=3), name="Secret")
    response = await calendar_client.request(
        "PROPFIND",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers={**basic_auth(keyring, tenant_pair.household_b), "Depth": "1"},
        content=PROPFIND_ALL,
    )
    assert response.status_code == 404
    assert "Secret" not in response.text


async def test_a_feed_shows_only_its_own_stock(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=2), name="Chez A")
    await make_lot(tenant_pair.household_b, best_before=today() + timedelta(days=2), name="Chez B")

    response = await calendar_client.request(
        "REPORT",
        calendar_path(feed_id_of(keyring, tenant_pair.household_b)),
        headers=basic_auth(keyring, tenant_pair.household_b),
        content=CALENDAR_QUERY_TODOS,
    )
    assert response.status_code == 207
    assert "Chez B" in response.text
    assert "Chez A" not in response.text


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_well_known_redirects_to_the_context_path(
    calendar_client: httpx.AsyncClient,
) -> None:
    """RFC 6764 §6: this is how a client that only knows the host finds us."""
    response = await calendar_client.request("PROPFIND", "/.well-known/caldav")
    assert response.status_code == 301
    assert response.headers["Location"] == "/caldav/"


async def test_options_advertises_calendar_access_but_not_locking(
    calendar_client: httpx.AsyncClient,
) -> None:
    response = await calendar_client.request("OPTIONS", "/caldav/")
    assert response.status_code == 204
    compliance = {part.strip() for part in response.headers["DAV"].split(",")}
    assert "calendar-access" in compliance
    # Class 2 is locking. Claiming it on a read-only server would be a lie a
    # client could act on.
    assert "2" not in compliance


async def test_options_answers_on_the_collection_too(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """Some clients probe the collection rather than the root before syncing."""
    response = await calendar_client.request(
        "OPTIONS", calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    )
    assert response.status_code == 204
    assert "PROPFIND" in response.headers["Allow"]
    assert "REPORT" in response.headers["Allow"]


async def test_the_openapi_document_survives_the_non_standard_methods(
    initialised_database: str,
) -> None:
    """``PROPFIND`` has no OpenAPI spelling; the schema must still build.

    The CalDAV routes are excluded from the schema, and this asserts that the
    exclusion holds -- a generation failure here would take ``/docs`` down for
    every other endpoint.
    """
    settings = calendar_settings(initialised_database).model_copy(update={"enable_docs": True})
    app = create_app(settings)
    schema = app.openapi()
    assert "/caldav/" not in schema["paths"]
    assert "/v1/calendar/subscription" in schema["paths"]
    await app.state.catalog.aclose()
    await app.state.database.dispose()


async def test_the_root_names_the_current_user_principal(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    response = await calendar_client.request(
        "PROPFIND",
        "/caldav/",
        headers=basic_auth(keyring, tenant_pair.household_a),
        content=PROPFIND_ALL,
    )
    assert response.status_code == 207
    assert f"/caldav/p/{feed_id_of(keyring, tenant_pair.household_a)}/" in response.text


async def test_the_home_lists_a_vtodo_collection(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """``supported-calendar-component-set`` is what files this under Reminders."""
    feed_id = feed_id_of(keyring, tenant_pair.household_a)
    response = await calendar_client.request(
        "PROPFIND",
        f"/caldav/p/{feed_id}/cal/",
        headers={**basic_auth(keyring, tenant_pair.household_a), "Depth": "1"},
        content=PROPFIND_ALL,
    )
    assert response.status_code == 207
    assert 'name="VTODO"' in response.text
    assert 'name="VEVENT"' not in response.text
    assert calendar_path(feed_id) in response.text


async def test_depth_infinity_is_refused(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    response = await calendar_client.request(
        "PROPFIND",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers={**basic_auth(keyring, tenant_pair.household_a), "Depth": "infinity"},
        content=PROPFIND_ALL,
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #


async def test_a_task_carries_product_quantity_location_and_date(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
    make_fridge: MakeFridge,
) -> None:
    fridge = await make_fridge(tenant_pair.household_a)
    due = today() + timedelta(days=4)
    await make_lot(
        tenant_pair.household_a,
        best_before=due,
        name="Yaourt nature",
        quantity="4",
        unit="piece",
        dimension=QuantityDimension.COUNT,
        location=fridge,
    )
    response = await calendar_client.request(
        "GET",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    assert response.status_code == 200
    (todo,) = Calendar.from_ical(response.text).walk("VTODO")
    assert str(todo["SUMMARY"]) == "Yaourt nature — 4 pc"
    assert str(todo["LOCATION"]) == "Frigo"
    assert todo["DUE"].dt == due


async def test_a_task_carries_nothing_the_feed_was_not_asked_for(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """Data minimisation, asserted rather than intended.

    The brand is in the database and deliberately not in the feed: the product
    name identifies the item on a lock screen, and every further field is one
    more thing leaving the home on every poll.
    """
    lot = await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=2))
    response = await calendar_client.request(
        "GET",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    body = response.text
    # No internal identifier reaches the phone, so none reaches a proxy log.
    assert str(lot.id) not in body
    assert str(tenant_pair.household_a.id) not in body

    (todo,) = Calendar.from_ical(body).walk("VTODO")
    emitted = set(todo.keys())
    assert emitted == {
        "UID",
        "DTSTAMP",
        "CREATED",
        "LAST-MODIFIED",
        "SUMMARY",
        "DUE",
        "STATUS",
    }
    # The alarm repeats the summary rather than adding a second string; nothing
    # in the component says more about the household than those four facts do.
    (alarm,) = todo.walk("VALARM")
    assert str(alarm["DESCRIPTION"]) == str(todo["SUMMARY"])


async def test_lots_without_a_date_and_depleted_lots_are_not_published(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    await make_lot(tenant_pair.household_a, best_before=None, name="Sans date")
    await make_lot(
        tenant_pair.household_a,
        best_before=today() + timedelta(days=2),
        name="Consommé",
        depleted=True,
    )
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=2), name="Actif")
    response = await calendar_client.request(
        "GET",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    assert "Actif" in response.text
    assert "Sans date" not in response.text
    assert "Consommé" not in response.text


async def test_the_window_bounds_the_feed_at_both_ends(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """A lower bound too: without it, ancient lots fill the cap before tomorrow's."""
    await make_lot(
        tenant_pair.household_a,
        best_before=today() - timedelta(days=PAST_WINDOW_DAYS + 5),
        name="Oublié depuis longtemps",
    )
    await make_lot(
        tenant_pair.household_a,
        best_before=today() + timedelta(days=FUTURE_WINDOW_DAYS + 5),
        name="Conserve lointaine",
    )
    await make_lot(
        tenant_pair.household_a, best_before=today() - timedelta(days=1), name="Périmé hier"
    )
    response = await calendar_client.request(
        "GET",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    assert "Périmé hier" in response.text
    assert "Oublié depuis longtemps" not in response.text
    assert "Conserve lointaine" not in response.text


async def test_the_number_of_tasks_is_capped(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """The cap is what makes the largest possible response a knowable size."""
    for index in range(MAX_TASKS + 5):
        await make_lot(
            tenant_pair.household_a,
            best_before=today() + timedelta(days=1 + index % FUTURE_WINDOW_DAYS),
            name=f"Produit {index}",
        )
    response = await calendar_client.request(
        "GET",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    assert len(Calendar.from_ical(response.text).walk("VTODO")) == MAX_TASKS


# --------------------------------------------------------------------------- #
# Synchronisation
# --------------------------------------------------------------------------- #


async def test_etags_are_stable_while_the_data_is(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    """A tag that moved on every poll would make every poll a full download."""
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=3))
    feed_id = feed_id_of(keyring, tenant_pair.household_a)
    headers = {**basic_auth(keyring, tenant_pair.household_a), "Depth": "1"}

    first = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id), headers=headers, content=PROPFIND_ETAGS
    )
    second = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id), headers=headers, content=PROPFIND_ETAGS
    )
    assert first.text == second.text


async def test_the_collection_tag_moves_when_the_stock_does(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    feed_id = feed_id_of(keyring, tenant_pair.household_a)
    headers = basic_auth(keyring, tenant_pair.household_a)

    before = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id), headers=headers, content=PROPFIND_ALL
    )
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=3))
    after = await calendar_client.request(
        "PROPFIND", calendar_path(feed_id), headers=headers, content=PROPFIND_ALL
    )
    assert before.text != after.text


async def test_an_unknown_sync_token_is_refused_rather_than_answered_wrongly(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """RFC 6578: no history means the client must be told to resynchronise.

    Answering with the current collection instead would leave a deleted task on
    the phone for ever, which is the failure this report exists to prevent.
    """
    body = (
        b'<d:sync-collection xmlns:d="DAV:">'
        b"<d:sync-token>https://chaudron.dev/ns/sync/stale</d:sync-token>"
        b"<d:prop><d:getetag/></d:prop></d:sync-collection>"
    )
    response = await calendar_client.request(
        "REPORT",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
        content=body,
    )
    assert response.status_code == 403
    assert "valid-sync-token" in response.text


async def test_an_initial_sync_returns_everything_and_a_token(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    make_lot: MakeLot,
) -> None:
    await make_lot(tenant_pair.household_a, best_before=today() + timedelta(days=3), name="Beurre")
    body = (
        b'<d:sync-collection xmlns:d="DAV:"><d:sync-token/>'
        b"<d:prop><d:getetag/></d:prop></d:sync-collection>"
    )
    response = await calendar_client.request(
        "REPORT",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
        content=body,
    )
    assert response.status_code == 207
    assert "sync-token" in response.text
    assert "getetag" in response.text


# --------------------------------------------------------------------------- #
# Read-only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PROPPATCH", "MKCALENDAR"])
async def test_a_write_is_refused_rather_than_swallowed(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    method: str,
) -> None:
    response = await calendar_client.request(
        method,
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_auth(keyring, tenant_pair.household_a),
    )
    assert response.status_code == 403


async def test_an_anonymous_write_is_still_only_a_challenge(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """The refusal must not be usable to learn that a path exists."""
    response = await calendar_client.request(
        "PUT", calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Handing the credentials to the household
# --------------------------------------------------------------------------- #


async def test_a_household_can_read_its_own_subscription_details(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    response = await calendar_client.get(
        "/v1/calendar/subscription",
        headers={"X-Household-Id": str(tenant_pair.household_a.id)},
    )
    assert response.status_code == 200
    payload = response.json()
    expected = keyring.credentials_for(FeedHousehold(id=tenant_pair.household_a.id))
    assert payload["username"] == expected.feed_id
    assert payload["password"] == expected.secret
    assert payload["server_url"] == "https://chaudron.test/caldav/"
    # The one endpoint that returns a bearer credential must never be cached.
    assert response.headers["cache-control"] == "no-store"


async def test_subscription_details_need_a_session(
    anonymous_calendar_client: httpx.AsyncClient,
) -> None:
    """It hands out a bearer credential, so a stranger gets nothing at all."""
    assert (await anonymous_calendar_client.get("/v1/calendar/subscription")).status_code == 401


# --------------------------------------------------------------------------- #
# The tenancy hook
# --------------------------------------------------------------------------- #


async def test_authentication_posts_the_tenant_for_the_transaction(
    calendar_app: FastAPI,
    db_session: AsyncSession,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
) -> None:
    """The feed must read through the same path every other read uses.

    ``infra/db.py`` emits ``SET LOCAL chaudron.household_id`` on the first
    statement issued once ``household_id_var`` holds a value, and every
    row-level security policy reads that setting. A feed that resolved a
    household without writing it there would run *outside* the policies -- and
    would see either nothing or, with the ``WHERE`` clause present, the right
    rows for the wrong reason. This asserts the hook is set, and set to the
    household the credential named rather than to anything a client sent.
    """
    settings: Settings = calendar_app.state.settings
    request = Request(
        {
            "type": "http",
            "method": "PROPFIND",
            "path": "/caldav/",
            "raw_path": b"/caldav/",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 51234),
            "app": calendar_app,
            "headers": [
                (
                    b"authorization",
                    basic_auth(keyring, tenant_pair.household_b)["Authorization"].encode(),
                )
            ],
        }
    )
    token = household_id_var.set(None)
    try:
        context = await authenticate(request, db_session, settings)
        assert context.household_id == tenant_pair.household_b.id
        assert household_id_var.get() == str(tenant_pair.household_b.id)
    finally:
        household_id_var.reset(token)


# --------------------------------------------------------------------------- #
# The cost of checking a credential
# --------------------------------------------------------------------------- #
#
# Everything below is a regression on one finding: the endpoint's own docstring
# named the threat -- "the cost of *checking* an attempt, not brute force" -- and
# the two controls meant to bound it sat behind the cost they were bounding.


@pytest.mark.parametrize(
    ("feed_id", "secret"),
    [
        ("é" * FEED_ID_LENGTH, "é" * FEED_SECRET_LENGTH),
        ("A" * FEED_ID_LENGTH, "é" * FEED_SECRET_LENGTH),
        ("é" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH),
        ("Ünïcödé" + "A" * (FEED_ID_LENGTH - 7), "B" * FEED_SECRET_LENGTH),
    ],
    ids=["both", "secret-only", "identifier-only", "mixed"],
)
async def test_a_non_ascii_credential_is_refused_rather_than_crashing(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    feed_id: str,
    secret: str,
) -> None:
    """The crash that had no upper bound on how often it could be asked for.

    ``hmac.compare_digest`` raises ``TypeError`` rather than returning ``False``
    when either operand is a ``str`` holding a character outside ASCII, and the
    check that stood in front of it counted characters without looking at them:
    ``"é" * 26`` is twenty-six of them. Every attempt was a ``500``, a full stack
    trace in the journal and a read of the household table -- and the limiter
    meant to bound that ran after the exception, so nothing bounded it at all.

    A household must exist for this to mean anything: with an empty candidate
    list the loop never runs and the comparison is never reached, which is how
    the first attempt at reproducing it came back green.
    """
    assert tenant_pair.household_a
    response = await calendar_client.request(
        "PROPFIND",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers=basic_header(feed_id, secret),
        content=PROPFIND_ALL,
    )
    assert response.status_code == 401
    # Indistinguishable from every other refusal: a caller must not learn that
    # this particular guess reached further into the code than another.
    assert response.json()["detail"] == (
        "This feed requires the user name and password shown in Chaudron."
    )


async def test_the_failure_budget_is_spent_before_the_scan_it_bounds(
    calendar_client: httpx.AsyncClient,
    calendar_app: FastAPI,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the budget is gone, a refused attempt must not reach the database.

    This is the whole of the finding. A ``429`` issued *after* the household scan
    changes the answer and not the work: the attempt has already read the table
    and derived a MAC per row by the time it is told to go away. The count below
    is of calls to :meth:`CalendarFeedService.resolve_household`, so it is the
    work being asserted, not the status code.
    """
    scans = 0
    original = CalendarFeedService.resolve_household

    async def counting(self: CalendarFeedService, feed_id: str, secret: str) -> uuid.UUID | None:
        nonlocal scans
        scans += 1
        return await original(self, feed_id, secret)

    monkeypatch.setattr(CalendarFeedService, "resolve_household", counting)

    path = calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    wrong = basic_header("A" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH)
    statuses = [
        (
            await calendar_client.request("PROPFIND", path, headers=wrong, content=PROPFIND_ALL)
        ).status_code
        for _ in range(_FAILED_AUTH_PER_HOUR + 5)
    ]

    assert statuses[0] == 401, "a first wrong guess is refused, not throttled"
    assert statuses[-1] == 429, "the budget must run out"
    assert scans == _FAILED_AUTH_PER_HOUR, (
        f"{scans} scans for {len(statuses)} attempts: the limiter is charged before the "
        f"scan, so a throttled attempt must cost no read at all"
    )
    assert calendar_app.state.calendar_throttles.failures is not None


async def test_a_subscriber_polling_normally_never_spends_the_failure_budget(
    calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
) -> None:
    """The property the reordering must not break, asserted rather than argued.

    The token is taken before the scan and given back once the request has been
    admitted, so a valid poll costs nothing. Without the refund, the thirty-first
    poll of a perfectly ordinary client would be a ``429`` on a budget named
    "failed authentications".
    """
    path = calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    headers = {**basic_auth(keyring, tenant_pair.household_a), "Depth": "0"}
    for _ in range(_FAILED_AUTH_PER_HOUR + 5):
        response = await calendar_client.request(
            "PROPFIND", path, headers=headers, content=PROPFIND_ALL
        )
        assert response.status_code == 207

    # And the budget is still there for the failures it is meant to count.
    refused = await calendar_client.request(
        "PROPFIND",
        path,
        headers=basic_header("A" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH),
        content=PROPFIND_ALL,
    )
    assert refused.status_code == 401


# --------------------------------------------------------------------------- #
# Revocation
# --------------------------------------------------------------------------- #


async def test_revoking_ends_the_credential_that_survived_the_membership(
    calendar_client: httpx.AsyncClient,
    anonymous_calendar_client: httpx.AsyncClient,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
) -> None:
    """The sequence the pentest replayed, now ending where it should.

    Somebody who read the subscription page once keeps a working credential for
    as long as the instance key lives: the CalDAV tree authenticates with the
    credential rather than with a membership, so withdrawing the membership --
    which cuts the session immediately -- left the feed answering ``207``. One
    ``POST`` now ends it, for this household and no other.
    """
    household = tenant_pair.household_a
    tenant = {"X-Household-Id": str(household.id)}

    before = (await calendar_client.get("/v1/calendar/subscription", headers=tenant)).json()
    old = basic_header(before["username"], before["password"])
    path = calendar_path(before["username"])
    assert (
        await anonymous_calendar_client.request(
            "PROPFIND", path, headers={**old, "Depth": "0"}, content=PROPFIND_ALL
        )
    ).status_code == 207

    revoked = await calendar_client.post("/v1/calendar/subscription/revoke", headers=tenant)
    assert revoked.status_code == 200
    after = revoked.json()
    assert after["username"] != before["username"], "the identifier moves, not just the secret"
    assert after["password"] != before["password"]

    # The withdrawn pair no longer names anything, on its own path or on the new one.
    for target in (path, calendar_path(after["username"])):
        stale = await anonymous_calendar_client.request(
            "PROPFIND", target, headers={**old, "Depth": "0"}, content=PROPFIND_ALL
        )
        assert stale.status_code == 401, target

    fresh = await anonymous_calendar_client.request(
        "PROPFIND",
        calendar_path(after["username"]),
        headers={**basic_header(after["username"], after["password"]), "Depth": "0"},
        content=PROPFIND_ALL,
    )
    assert fresh.status_code == 207

    # And the page now shows the pair that works, not the one that was revoked.
    reread = (await calendar_client.get("/v1/calendar/subscription", headers=tenant)).json()
    assert reread["username"] == after["username"]
    assert reread["password"] == after["password"]


async def test_revoking_one_household_leaves_every_other_subscribed(
    calendar_client: httpx.AsyncClient,
    anonymous_calendar_client: httpx.AsyncClient,
    db_session: AsyncSession,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
) -> None:
    """What the instance-wide epoch could not do, and why this is a column."""
    neighbour = tenant_pair.household_b
    neighbour_auth = basic_auth(keyring, neighbour)
    neighbour_path = calendar_path(feed_id_of(keyring, neighbour))

    revoked = await calendar_client.post(
        "/v1/calendar/subscription/revoke",
        headers={"X-Household-Id": str(tenant_pair.household_a.id)},
    )
    assert revoked.status_code == 200

    still_working = await anonymous_calendar_client.request(
        "PROPFIND", neighbour_path, headers={**neighbour_auth, "Depth": "0"}, content=PROPFIND_ALL
    )
    assert still_working.status_code == 207


async def test_only_an_owner_may_revoke(
    calendar_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A member can read the stock; locking every device out is not their call."""
    household = await make_household(role=MembershipRole.MEMBER)
    response = await calendar_client.post(
        "/v1/calendar/subscription/revoke",
        headers={"X-Household-Id": str(household.id)},
    )
    assert response.status_code == 403


async def test_revocation_needs_a_session(
    anonymous_calendar_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    response = await anonymous_calendar_client.post(
        "/v1/calendar/subscription/revoke",
        headers={"X-Household-Id": str(tenant_pair.household_a.id)},
    )
    assert response.status_code == 401


async def test_revocation_is_absent_when_the_feed_is(
    initialised_database: str,
    db_session: AsyncSession,
    tenant_pair: TenantPair,
    signed_in: SignedIn,
) -> None:
    """An instance that publishes no feed has nothing to revoke, and says 404."""
    app = create_app(calendar_settings(initialised_database, enabled=False))

    async def _use_test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: signed_in.csrf_token},
    ) as client:
        response = await client.post(
            "/v1/calendar/subscription/revoke",
            headers={"X-Household-Id": str(tenant_pair.household_a.id)},
        )
    await app.state.catalog.aclose()
    await app.state.database.dispose()
    assert response.status_code == 404


async def test_a_utf16_document_type_declaration_is_refused_over_http(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """The bypass, replayed where it was found: an authenticated ``PROPFIND``."""
    document = (
        "<!DOCTYPE d:propfind [<!ENTITY chaudron 'resourcetype'>]>"
        '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    )
    response = await calendar_client.request(
        "PROPFIND",
        calendar_path(feed_id_of(keyring, tenant_pair.household_a)),
        headers={**basic_auth(keyring, tenant_pair.household_a), "Depth": "0"},
        content=b"\xff\xfe" + document.encode("utf-16-le"),
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# What the failure budget is keyed on
# --------------------------------------------------------------------------- #
#
# `_client_key` reads `request.client.host`, which in the deployed shape has
# already been rewritten by uvicorn's proxy-headers middleware. That rewriting is
# the difference between "each subscriber has a bucket" and "everybody behind the
# proxy shares one", and the docstring on `_client_key` asserts the first. These
# two tests are what keep that assertion from going stale: they wire the
# middleware exactly as `scripts/entrypoint.sh` and `ops/chaudron.container` do --
# trusting the proxy's pinned address on chaudron-net, and nothing else.

_PROXY_ADDRESS = "10.89.7.10"


def _behind_the_proxy(app: FastAPI) -> ASGIApp:
    """The application behind uvicorn's proxy-header middleware, wired as deployed.

    ``trusted_hosts`` is the single pinned address, exactly as
    ``scripts/entrypoint.sh`` passes ``--forwarded-allow-ips`` from
    ``ops/chaudron.container``. Two casts, and neither hides anything: uvicorn
    types an ASGI application with per-message ``TypedDict`` scopes and
    starlette types it with ``MutableMapping[str, Any]``. They describe the same
    three-argument callable, and mypy cannot see that through the generics.
    """
    middleware = ProxyHeadersMiddleware(cast("Any", app), trusted_hosts=_PROXY_ADDRESS)
    return cast("ASGIApp", middleware)


async def _refuse_from(client: httpx.AsyncClient, path: str, forwarded_for: str) -> int:
    response = await client.request(
        "PROPFIND",
        path,
        headers={
            **basic_header("A" * FEED_ID_LENGTH, "B" * FEED_SECRET_LENGTH),
            "X-Forwarded-For": forwarded_for,
        },
        content=PROPFIND_ALL,
    )
    return response.status_code


async def test_one_callers_failures_do_not_spend_anothers_budget(
    calendar_app: FastAPI, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """The bucket is the caller, not the proxy in front of them.

    Caddy overwrites ``X-Forwarded-For`` with the peer it accepted, so what
    arrives is one address: the client's own. Were the key the proxy's address
    instead, the second caller below would meet a ``429`` it did nothing to earn
    -- which is the collective lockout this test exists to say does not happen.
    """
    transport = httpx.ASGITransport(
        app=_behind_the_proxy(calendar_app), client=(_PROXY_ADDRESS, 44300)
    )
    path = calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        for _ in range(_FAILED_AUTH_PER_HOUR + 1):
            await _refuse_from(client, path, "198.51.100.99")
        assert await _refuse_from(client, path, "198.51.100.99") == 429, "the guilty address pays"
        assert await _refuse_from(client, path, "203.0.113.7") == 401, (
            "a second caller behind the same proxy must have a budget of its own"
        )
    await calendar_app.state.catalog.aclose()
    await calendar_app.state.database.dispose()


async def test_a_prepended_forwarded_address_cannot_choose_the_bucket(
    calendar_app: FastAPI, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """A limiter keyed on a value the caller picks is not a limiter.

    uvicorn walks ``X-Forwarded-For`` from the right and stops at the first
    address it does not trust, so the entry the proxy appended wins and anything
    a client wrote in front of it is skipped. Here the budget is exhausted while
    rotating the *prepended* half through thirty-odd distinct addresses: if that
    half decided the key, every attempt would land in a fresh bucket and the
    ``429`` would never come.
    """
    transport = httpx.ASGITransport(
        app=_behind_the_proxy(calendar_app), client=(_PROXY_ADDRESS, 44301)
    )
    path = calendar_path(feed_id_of(keyring, tenant_pair.household_a))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        statuses = [
            await _refuse_from(client, path, f"203.0.113.{n}, 198.51.100.42")
            for n in range(1, _FAILED_AUTH_PER_HOUR + 3)
        ]
    await calendar_app.state.catalog.aclose()
    await calendar_app.state.database.dispose()

    assert statuses[0] == 401
    assert statuses[-1] == 429, (
        "rotating the client-supplied half of X-Forwarded-For must not mint a new budget"
    )
