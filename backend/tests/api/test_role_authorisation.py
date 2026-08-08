"""What each role may actually do, driven over HTTP against a real database.

``tests/api/test_route_authentication.py`` proves *statically* which routes carry
a role guard. This file proves the guards behave as the census assumes: that a
``viewer`` is refused where the table says a member is needed, that a ``member``
is refused where it says owner, and -- the half a census cannot see -- that the
refusal reaches the caller as a ``403`` with a slug they can act on rather than as
a 500 or a silent success.

The situation being closed is not hypothetical. It was replayed end to end
(audit AUD-027): an account holding ``viewer`` in a household registered **its
own** Todoist token against ``PUT /v1/shopping-lists/export/targets/todoist``,
ticked the consent box on the household's behalf, and received the household's
shopping list -- ``200``, ``exported_item_count: 1``. Every route below was
reachable by that same account, because ``household_member.role`` was read by
exactly one line in the whole of ``src/``.

Two properties are asserted for every case, not one. A viewer is refused **and** a
member is not, because a guard that refuses everybody passes the first half of
this file and breaks the application.
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import HouseholdMember, MembershipRole
from tests.conftest import MakeHousehold, SignedIn, household_headers

pytestmark = pytest.mark.integration

_TODOIST_TARGET: Final = "/v1/shopping-lists/export/targets/todoist"

#: Long enough for ``MIN_API_KEY_LENGTH``, and obviously synthetic.
_FAKE_TOKEN: Final = "0123456789abcdef0123456789abcdef01234567"

#: One representative write per router, with a body that would be accepted if the
#: role check were not there. The point is the ``403``, so each body is the
#: smallest valid one -- a payload that would fail validation would produce a
#: ``422`` and prove nothing about authorisation.
_WRITES: Final[tuple[tuple[str, str, dict[str, object] | None], ...]] = (
    ("POST", "/v1/locations", {"name": "Congélateur", "kind": "freezer"}),
    ("POST", "/v1/members", {"display_name": "Alex", "age_band": "adult", "diet": "omnivore"}),
    ("POST", "/v1/shopping-lists/current/items", {"items": [{"free_text": "Pain"}]}),
    ("PUT", "/v1/budget/target", {"period": "month", "amount": "400.00", "currency": "CHF"}),
    ("PUT", _TODOIST_TARGET, {"token": _FAKE_TOKEN, "consent_granted": True}),
    (
        "POST",
        "/v1/tokens",
        {"name": "Home Assistant", "scopes": ["inventory:read"], "expires_in_days": None},
    ),
)

#: Reads a viewer must keep. Without these, "viewer" would be a role that can do
#: nothing at all, which is a different bug wearing the same 403.
_READS: Final = (
    "/v1/locations",
    "/v1/inventory",
    "/v1/members",
    "/v1/shopping-lists/current",
    "/v1/budget",
)


async def _demote(session: AsyncSession, household_id: object, user_id: object, role: str) -> None:
    """Change the caller's role on a household they already belong to.

    Written as an ``UPDATE`` rather than by re-creating the membership because the
    row is what several tests below are *about*: the point is a role that changed
    under a credential which did not.
    """
    await session.execute(
        update(HouseholdMember)
        .where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.user_id == user_id,
        )
        .values(role=role)
    )


# --------------------------------------------------------------------------- #
# A viewer reads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path", "body"), _WRITES, ids=[w[1] for w in _WRITES])
async def test_a_viewer_may_not_write(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    household = await make_household(role=MembershipRole.VIEWER)

    response = await api_client.request(
        method, path, json=body, headers=household_headers(household)
    )

    assert response.status_code == 403, f"{method} {path} let a viewer through: {response.text}"
    # Either refusal is correct and the two are different sentences on purpose:
    # a member-guarded route says which role is needed, an owner-guarded one keeps
    # the answer identical to "no such household" (`api/errors.py`).
    assert response.json()["type"].endswith(("/insufficient-role", "/household-forbidden"))


@pytest.mark.parametrize("path", _READS)
async def test_a_viewer_still_reads(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, path: str
) -> None:
    """The other half. A guard that refused everything would pass the test above."""
    household = await make_household(role=MembershipRole.VIEWER)

    response = await api_client.get(path, headers=household_headers(household))

    assert response.status_code == 200, f"{path} refused a viewer a read: {response.text}"


@pytest.mark.parametrize(("method", "path", "body"), _WRITES, ids=[w[1] for w in _WRITES])
async def test_an_owner_may_write(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    """And the guards let the household get on with its life."""
    household = await make_household(role=MembershipRole.OWNER)

    response = await api_client.request(
        method, path, json=body, headers=household_headers(household)
    )

    assert response.status_code < 400, f"{method} {path} refused an owner: {response.text}"


# --------------------------------------------------------------------------- #
# A member is not an owner
# --------------------------------------------------------------------------- #


async def test_a_member_may_not_register_an_export_destination(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Accepting a third party's token is a decision, and it belongs to the owner.

    The consent it dates is *the household's*, and what it authorises leaving the
    instance is what identifiable people eat.
    """
    household = await make_household(role=MembershipRole.MEMBER)

    response = await api_client.put(
        _TODOIST_TARGET,
        json={"token": _FAKE_TOKEN, "consent_granted": True},
        headers=household_headers(household),
    )

    assert response.status_code == 403, response.text


async def test_a_member_may_not_withdraw_an_export_destination(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    signed_in: SignedIn,
    make_household: MakeHousehold,
) -> None:
    """Symmetrical with registering: whoever may grant may withdraw, and nobody else.

    Registered as an owner first, because a withdrawal of nothing would be refused
    for the wrong reason and prove nothing about the role.
    """
    household = await make_household(role=MembershipRole.OWNER)
    registered = await api_client.put(
        _TODOIST_TARGET,
        json={"token": _FAKE_TOKEN, "consent_granted": True},
        headers=household_headers(household),
    )
    assert registered.status_code == 200, registered.text

    await _demote(db_session, household.id, signed_in.user.id, MembershipRole.MEMBER)

    response = await api_client.delete(_TODOIST_TARGET, headers=household_headers(household))
    assert response.status_code == 403, response.text


async def test_a_member_may_send_the_list_to_a_registered_destination(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Pressing send is an ordinary household operation; only registering is not.

    A ``409``/``403`` naming the *destination* is the pass condition here: it means
    the request reached the export path rather than being stopped by the role
    guard, which is the only thing this test is about.
    """
    household = await make_household(role=MembershipRole.MEMBER)

    response = await api_client.post(
        f"/v1/shopping-lists/{household.id}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code != 403 or not response.json()["type"].endswith(
        "/insufficient-role"
    ), response.text


# --------------------------------------------------------------------------- #
# A machine token is never more than the person behind it
# --------------------------------------------------------------------------- #


async def test_a_token_stops_writing_when_its_issuer_becomes_a_viewer(
    api_client: httpx.AsyncClient,
    anonymous_client: httpx.AsyncClient,
    db_session: AsyncSession,
    signed_in: SignedIn,
    make_household: MakeHousehold,
) -> None:
    """The guard would be theatre if minting a token walked around it.

    ``chaudron_resolve_machine_token`` has joined ``household_member`` since
    migration ``0011`` -- that join is what makes a token die with its issuer's
    membership -- and threw the role away, so a viewer could have written through a
    credential they were refused through a cookie. Migration ``0014`` carries the
    role out of the same join, and this is what proves the API reads it: the token
    is unchanged, the row it was minted from is unchanged, and only the role moved.
    """
    household = await make_household(role=MembershipRole.OWNER)
    minted = await api_client.post(
        "/v1/tokens",
        json={
            "name": "Home Assistant",
            "scopes": ["inventory:read", "inventory:write"],
            "expires_in_days": None,
        },
        headers=household_headers(household),
    )
    assert minted.status_code == 201, minted.text
    bearer = {"Authorization": f"Bearer {minted.json()['token']}"}

    before = await anonymous_client.get("/v1/inventory", headers=bearer)
    assert before.status_code == 200, before.text

    await _demote(db_session, household.id, signed_in.user.id, MembershipRole.VIEWER)

    # The read scope still works: a viewer reads, and so does their token.
    still_reading = await anonymous_client.get("/v1/inventory", headers=bearer)
    assert still_reading.status_code == 200, still_reading.text

    writing = await anonymous_client.post(
        "/v1/inventory",
        json={"product": {"name": "Riz"}, "quantity": {"value": "1", "unit": "kg"}},
        headers=bearer,
    )
    assert writing.status_code == 403, writing.text
    assert writing.json()["type"].endswith("/insufficient-role")
