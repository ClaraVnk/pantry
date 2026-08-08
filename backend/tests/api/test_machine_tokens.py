"""Machine access tokens: what they open, what they refuse, and what kills them.

Contract v1.1 section 10. ``tests/api/test_route_authentication.py`` proves
*statically* which routes a token may reach; this file proves the mechanism
behind that census actually behaves as the census assumes -- that a declared
scope is checked rather than merely declared, that an undeclared route refuses a
token rather than ignoring it, and that revocation is a write whose effect is
immediate.

Every test here drives the real application over HTTP, with a real row in
``machine_token`` and a real ``Authorization`` header. Nothing is stubbed: the
resolver, the ``SECURITY DEFINER`` function, the row-level security policy and
the scope check are the ones that ship.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import SESSION_COOKIE
from chaudron.api.main import build_throttles
from chaudron.api.throttling import Throttles
from chaudron.domain.models import HouseholdMember, MachineToken, MachineTokenScope
from chaudron.infra.db import current_transaction_household
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from chaudron.services.tokens import (
    TOKEN_PREFIX,
    MachineTokenService,
    new_machine_token,
)
from tests.conftest import SignedIn, household_headers

pytestmark = pytest.mark.anyio

ALL_SCOPES: Final = [scope.value for scope in MachineTokenScope]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Issued:
    """A token as the creation response handed it back."""

    value: str
    body: Mapping[str, Any]

    @property
    def id(self) -> str:
        return str(self.body["id"])

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.value}"}


async def issue(
    client: httpx.AsyncClient,
    *,
    scopes: list[str],
    name: str = "Home Assistant",
    expires_in_days: int | None = None,
    household: Any = None,
) -> Issued:
    """Create a token the way the interface does: from a browser session."""
    response = await client.post(
        "/v1/tokens",
        json={"name": name, "scopes": scopes, "expires_in_days": expires_in_days},
        headers=household_headers(household) if household is not None else None,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Issued(value=body["token"], body=body)


@pytest.fixture
async def bearer(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no cookie and no CSRF token: exactly what a program is.

    Deliberately *not* ``api_client`` with an extra header. A test that kept the
    cookie around would pass even if the bearer path did nothing, because the
    cookie alone would have authorised the request.
    """
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


# --------------------------------------------------------------------------- #
# Creation: shown once, and only from a browser
# --------------------------------------------------------------------------- #


async def test_a_token_is_shown_once_and_never_again(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: Any
) -> None:
    household = await make_household()
    issued = await issue(
        api_client, scopes=["inventory:read"], household=household, name="Domotique"
    )

    assert issued.value.startswith(TOKEN_PREFIX)
    assert issued.body["prefix"] == TOKEN_PREFIX
    assert issued.body["last4"] == issued.value[-4:]
    assert issued.body["scopes"] == ["inventory:read"]
    assert issued.body["last_used_at"] is None
    assert issued.body["expires_at"] is None

    listed = await api_client.get("/v1/tokens", headers=household_headers(household))
    assert listed.status_code == 200
    assert issued.value not in listed.text, "the value must not survive the creation response"
    (row,) = listed.json()
    assert row["id"] == issued.id
    assert row["name"] == "Domotique"
    assert set(row) == set(issued.body) - {"token"}

    stored = await db_session.scalar(
        select(MachineToken).where(MachineToken.id == uuid.UUID(issued.id))
    )
    assert stored is not None
    assert stored.token_hash == hashlib.sha256(issued.value.encode()).hexdigest()
    assert issued.value not in repr(stored.__dict__), "no column may hold the plaintext"


async def test_creating_a_token_needs_a_browser_session(
    anonymous_client: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    response = await anonymous_client.post(
        "/v1/tokens",
        json={"name": "x", "scopes": ["inventory:read"], "expires_in_days": None},
        headers=household_headers(household),
    )
    assert response.status_code == 401


async def test_creating_a_token_needs_a_csrf_token(
    api_app: FastAPI, signed_in: SignedIn, make_household: Any
) -> None:
    """A cookie alone is ambient authority; the token endpoint is not exempt."""
    household = await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as client:
        response = await client.post(
            "/v1/tokens",
            json={"name": "x", "scopes": ["inventory:read"], "expires_in_days": None},
            headers=household_headers(household),
        )
    assert response.status_code == 403
    assert response.json()["type"].endswith("/csrf-token-invalid")


async def test_a_token_without_a_scope_is_refused_at_creation(
    api_client: httpx.AsyncClient, make_household: Any
) -> None:
    """ "A token without a scope can do nothing" is enforced by never storing one."""
    household = await make_household()
    response = await api_client.post(
        "/v1/tokens",
        json={"name": "x", "scopes": [], "expires_in_days": None},
        headers=household_headers(household),
    )
    assert response.status_code == 422


async def test_an_absurd_expiry_is_refused(
    api_client: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    for days in (0, -1, 100_000):
        response = await api_client.post(
            "/v1/tokens",
            json={"name": "x", "scopes": ["budget:read"], "expires_in_days": days},
            headers=household_headers(household),
        )
        assert response.status_code == 422, days


# --------------------------------------------------------------------------- #
# What a token opens
# --------------------------------------------------------------------------- #


async def test_a_token_reaches_an_endpoint_inside_its_scope(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)

    response = await bearer.get("/v1/inventory", headers=issued.headers)

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


async def test_a_token_carries_no_csrf_requirement(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    """An ``Authorization`` header is never attached by a browser to a cross-site
    request, so there is no forgery to defend against and no token to echo."""
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:write"], household=household)

    response = await bearer.post(
        "/v1/locations",
        json={"name": "Frigo", "kind": "fridge"},
        headers=issued.headers,
    )
    # `POST /v1/locations` declares no scope, so this is a 401 -- but a 401 from
    # the *scope* rule, never a 403 from a missing CSRF header.
    assert response.status_code == 401
    assert response.json()["type"].endswith("/token-not-accepted")

    added = await bearer.post(
        "/v1/inventory",
        json={
            "product": {"name": "Lait", "default_unit": "l"},
            "amount": "1",
            "unit": "l",
        },
        headers=issued.headers,
    )
    assert added.status_code == 201, added.text


async def test_scopes_are_additive_and_never_implicit(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    """``inventory:write`` does not grant ``inventory:read``, in either direction."""
    household = await make_household()
    writer = await issue(api_client, scopes=["inventory:write"], household=household)

    refused = await bearer.get("/v1/inventory", headers=writer.headers)

    assert refused.status_code == 403
    problem = refused.json()
    assert problem["type"].endswith("/insufficient-scope")
    assert problem["required_scopes"] == ["inventory:read"]
    assert problem["granted_scopes"] == ["inventory:write"]
    assert "insufficient_scope" in refused.headers["WWW-Authenticate"]


async def test_a_token_is_refused_outside_its_scope(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)

    response = await bearer.get("/v1/budget", headers=issued.headers)

    assert response.status_code == 403
    assert response.json()["required_scopes"] == ["budget:read"]


# --------------------------------------------------------------------------- #
# What no token opens, whatever it holds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/recipes/suggest", {"servings": 2}),
        ("GET", "/v1/members", None),
        (
            "POST",
            "/v1/tokens",
            {"name": "second", "scopes": ["budget:read"], "expires_in_days": None},
        ),
        ("GET", "/v1/providers/capabilities", None),
        ("GET", "/v1/calendar/subscription", None),
        ("POST", "/v1/locations", {"name": "Cave", "kind": "cupboard"}),
    ],
)
async def test_a_token_holding_every_scope_still_reaches_none_of_these(
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    make_household: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """The five scopes are the whole surface, and holding all five is not "admin".

    Recipe suggestions spend money; members carry health data; ``/v1/tokens``
    would let a leaked credential outrun its own revocation. None of the three is
    a *scope* a token could be issued without -- there is no scope that reaches
    them at all, which is why holding every one of them changes nothing.
    """
    household = await make_household()
    issued = await issue(api_client, scopes=ALL_SCOPES, household=household)

    response = await bearer.request(method, path, json=body, headers=issued.headers)

    assert response.status_code == 401, response.text
    assert response.json()["type"].endswith("/token-not-accepted")
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


async def test_a_token_cannot_revoke_a_token(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    """The other half of "revocation must not become whack-a-mole"."""
    household = await make_household()
    issued = await issue(api_client, scopes=ALL_SCOPES, household=household)

    response = await bearer.delete(f"/v1/tokens/{issued.id}", headers=issued.headers)

    assert response.status_code == 401
    still_live = await bearer.get("/v1/inventory", headers=issued.headers)
    assert still_live.status_code == 200


async def test_a_token_cannot_list_the_households_other_tokens(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    issued = await issue(api_client, scopes=ALL_SCOPES, household=household)

    response = await bearer.get("/v1/tokens", headers=issued.headers)

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# One household, resolved at issuing time
# --------------------------------------------------------------------------- #


async def test_a_token_names_its_household_and_a_header_cannot_move_it(
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    make_household: Any,
) -> None:
    household = await make_household()
    other = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)

    matching = await bearer.get(
        "/v1/inventory", headers={**issued.headers, **household_headers(household)}
    )
    assert matching.status_code == 200, "repeating the token's own household is harmless"

    moved = await bearer.get(
        "/v1/inventory", headers={**issued.headers, **household_headers(other)}
    )
    assert moved.status_code == 403
    assert moved.json()["type"].endswith("/household-forbidden")


async def test_resolution_arms_the_tenant_all_the_way_to_set_local(
    db_session: AsyncSession, make_household: Any, signed_in: SignedIn
) -> None:
    """The path the row-level security policies depend on, checked at the source.

    Reads the parameter back **from PostgreSQL** rather than from what Python
    believes it posted: the whole point of ``current_transaction_household`` is to
    catch the case where the two disagree.
    """
    household = await make_household()
    service = MachineTokenService(db_session, max_per_household=20)
    issued = await service.issue(
        household_id=household.id,
        user_id=signed_in.user.id,
        name="probe",
        scopes=[MachineTokenScope.INVENTORY_READ],
        expires_in_days=None,
    )
    await db_session.flush()

    assert await current_transaction_household(db_session) is None

    grant = await service.resolve(issued.token)

    assert grant is not None
    assert grant.household_id == household.id
    assert await current_transaction_household(db_session) == household.id


# --------------------------------------------------------------------------- #
# Refusals: one answer, one shape
# --------------------------------------------------------------------------- #


async def test_an_expired_token_is_refused_exactly_like_an_unknown_one(
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: Any,
) -> None:
    """Same status, same body, same headers -- because it is the same branch.

    The expiry lives in the resolver's ``WHERE`` clause (migration ``0011``), so
    there is no code path on which "found, then rejected" could be distinguished
    from "not found". A post-filter in Python would have been correct and still
    observably different.
    """
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)
    stored = await db_session.scalar(
        select(MachineToken).where(MachineToken.id == uuid.UUID(issued.id))
    )
    assert stored is not None
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    expired = await bearer.get("/v1/inventory", headers=issued.headers)
    unknown = await bearer.get(
        "/v1/inventory", headers={"Authorization": f"Bearer {new_machine_token()}"}
    )

    assert expired.status_code == unknown.status_code == 401
    assert _without_request_id(expired) == _without_request_id(unknown)
    assert expired.headers["WWW-Authenticate"] == unknown.headers["WWW-Authenticate"]


async def test_revocation_takes_effect_on_the_next_request(
    api_client: httpx.AsyncClient, bearer: httpx.AsyncClient, make_household: Any
) -> None:
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)
    assert (await bearer.get("/v1/inventory", headers=issued.headers)).status_code == 200

    revoked = await api_client.delete(
        f"/v1/tokens/{issued.id}", headers=household_headers(household)
    )
    assert revoked.status_code == 204

    assert (await bearer.get("/v1/inventory", headers=issued.headers)).status_code == 401
    listed = await api_client.get("/v1/tokens", headers=household_headers(household))
    assert listed.json() == []


async def test_revoking_an_unknown_identifier_says_nothing(
    api_client: httpx.AsyncClient, make_household: Any
) -> None:
    """``204`` either way: a ``404`` would answer "does this token exist?"."""
    household = await make_household()
    response = await api_client.delete(
        f"/v1/tokens/{uuid.uuid7()}", headers=household_headers(household)
    )
    assert response.status_code == 204


async def test_a_token_dies_with_the_membership_that_issued_it(
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: Any,
    signed_in: SignedIn,
) -> None:
    """This is what makes "a member may issue one" safe.

    The resolver joins back to ``household_member`` on every request, so a token
    grants no more than the person behind it still has. Without that join, an
    owner removing somebody from the household would leave that person's
    integration reading the pantry indefinitely -- and nothing in the interface
    would say so.
    """
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)
    assert (await bearer.get("/v1/inventory", headers=issued.headers)).status_code == 200

    await db_session.execute(
        delete(HouseholdMember).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == signed_in.user.id,
        )
    )
    await db_session.flush()

    assert (await bearer.get("/v1/inventory", headers=issued.headers)).status_code == 401


async def test_a_malformed_authorization_header_never_falls_back_to_a_cookie(
    api_client: httpx.AsyncClient, make_household: Any
) -> None:
    """A bearer header, when present, *is* the credential.

    Falling back would make the effective identity of a request depend on which of
    two credentials the server happened to prefer -- and a request whose
    authorisation is ambiguous is one nobody can reason about afterwards.
    """
    household = await make_household()
    response = await api_client.get(
        "/v1/inventory",
        headers={**household_headers(household), "Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["type"].endswith("/token-not-accepted")


# --------------------------------------------------------------------------- #
# last_used_at, and the write it is not allowed to become
# --------------------------------------------------------------------------- #


async def test_last_used_at_is_written_at_most_once_an_hour(
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: Any,
) -> None:
    """Otherwise every authenticated read becomes a write, on exactly the traffic a
    polling integration generates."""
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)
    token_id = uuid.UUID(issued.id)

    async def stored_last_used() -> datetime | None:
        db_session.expire_all()
        return await db_session.scalar(
            select(MachineToken.last_used_at).where(MachineToken.id == token_id)
        )

    assert await stored_last_used() is None

    await bearer.get("/v1/inventory", headers=issued.headers)
    first = await stored_last_used()
    assert first is not None, "the first use has to be recorded, or the column is useless"

    for _ in range(3):
        await bearer.get("/v1/inventory", headers=issued.headers)
    assert await stored_last_used() == first

    # Age it past the granularity and the next request refreshes it.
    stored = await db_session.get(MachineToken, token_id)
    assert stored is not None
    stored.last_used_at = first - timedelta(hours=2)
    await db_session.flush()

    await bearer.get("/v1/inventory", headers=issued.headers)
    assert (await stored_last_used()) != first - timedelta(hours=2)


# --------------------------------------------------------------------------- #
# Guessing
# --------------------------------------------------------------------------- #


async def test_rejected_tokens_are_rate_limited(
    api_app: FastAPI, bearer: httpx.AsyncClient
) -> None:
    """A stranger presenting values is bounded; a running integration is not.

    Only refusals are charged, which the second half of this test shows: the
    valid token keeps working after the bucket for invalid ones is empty.
    """
    api_app.state.throttles = _throttles_guessing_at(api_app, limit=2)

    statuses = [
        (
            await bearer.get(
                "/v1/inventory", headers={"Authorization": f"Bearer {new_machine_token()}"}
            )
        ).status_code
        for _ in range(4)
    ]

    assert statuses[:2] == [401, 401]
    assert statuses[2:] == [429, 429]


async def test_a_valid_token_is_not_charged_to_the_guessing_budget(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    bearer: httpx.AsyncClient,
    make_household: Any,
) -> None:
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)

    api_app.state.throttles = _throttles_guessing_at(api_app, limit=1)

    for _ in range(5):
        response = await bearer.get("/v1/inventory", headers=issued.headers)
        assert response.status_code == 200


def _throttles_guessing_at(app: FastAPI, *, limit: int) -> Throttles:
    """The application's own limiters, with the token-guessing budget narrowed.

    Rebuilt from the settings rather than mutated in place: :class:`Throttles` is
    frozen, and the ``object.__setattr__`` this used to reach for was a way round
    that rather than a reason to.

    The narrowed limiter gets a scope of its own, under the prefix the harness
    made unique for this test (``tests/conftest.py``). Its bucket is a row
    committed on the limiter's own connection, so it outlives the test; a fixed
    scope would hand the next test a budget already spent.
    """
    throttles = build_throttles(
        app.state.settings,
        app.state.database.engine,
        scope_prefix=app.state.rate_limit_scope_prefix,
    )
    guessing = SharedRateLimiter(
        app.state.database.engine,
        BucketPolicy(
            scope=f"{app.state.rate_limit_scope_prefix}guessing:machine_token_attempts",
            limit=limit,
            window_seconds=3600.0,
        ),
    )
    return replace(throttles, machine_token_attempts=guessing)


def _without_request_id(response: httpx.Response) -> dict[str, object]:
    body = dict(response.json())
    body.pop("request_id", None)
    return body


# --------------------------------------------------------------------------- #
# The shape of the Authorization header
# --------------------------------------------------------------------------- #
#
# Two header shapes used to fall through to the cookie instead of being refused,
# which is "fails closed" in the sense that nothing extra was granted -- and is
# still wrong, because the request was then authenticated as *a person* while the
# client believed it was acting as its integration. Nothing in the response says
# which, and the audit trail records the wrong one (audit AUD-030).


async def test_a_tab_after_bearer_is_the_credential_it_looks_like(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: Any
) -> None:
    """RFC 9110 allows a horizontal tab between the scheme and its parameter.

    ``partition(" ")`` made the whole string the scheme, which matched nothing,
    which meant the token was ignored. Driven through a client that *also* holds a
    valid cookie, because that is the only configuration in which the old
    behaviour was silent: the request succeeded, as somebody else.
    """
    household = await make_household()
    issued = await issue(api_client, scopes=["inventory:read"], household=household)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="https://testserver"
    ) as client:
        # A tab-separated header naming a scope this token does hold: it resolves.
        readable = await client.get(
            "/v1/inventory", headers={"Authorization": f"Bearer\t{issued.value}"}
        )
        assert readable.status_code == 200, readable.text

        # And a route the token may not reach is refused as a token, not answered
        # as whatever cookie happened to be lying around.
        closed = await client.get(
            "/v1/members", headers={"Authorization": f"Bearer\t{issued.value}"}
        )
        assert closed.status_code == 401
        assert closed.json()["type"].endswith("/token-not-accepted")


async def test_bearer_with_no_value_is_refused_rather_than_ignored(
    api_app: FastAPI, signed_in: SignedIn
) -> None:
    """A client whose token variable expanded to nothing must be told so.

    Falling back to the cookie here authenticates the request as the person whose
    browser session the client happens to be reusing -- the failure above, with a
    more likely cause.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as client:
        for value in ("Bearer", "Bearer ", "Bearer\t", "bearer   "):
            response = await client.get("/v1/auth/session", headers={"Authorization": value})
            assert response.status_code == 401, f"{value!r} was answered from the cookie"
            assert response.json()["type"].endswith("/token-not-accepted")


async def test_an_empty_authorization_header_still_falls_back_to_the_cookie(
    api_app: FastAPI, signed_in: SignedIn
) -> None:
    """It names no scheme, some proxies emit one, and refusing it helps nobody."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as client:
        response = await client.get("/v1/auth/session", headers={"Authorization": "   "})

    assert response.status_code == 200, response.text
