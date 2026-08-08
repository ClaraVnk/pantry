"""The two things a person whose cookie has leaked can do about it.

``AuthService.revoke_all`` was written when sessions were, described in its own
docstring as *the lever for a suspected compromise*, and then called by nothing:
no route, no script, no test. There was also no way to change a password at all.
So the honest answer to "I think somebody has my session" was *wait thirty days*
-- the absolute expiry -- or *find an operator with a psql prompt* (audit
AUD-028).

Both routes are asserted here against real session rows and a real cookie jar,
because the property that matters is not "the endpoint returns 200" but "the other
credential stops working and mine does not". Those are two different rows and a
test that checked one would pass while the feature did nothing.

The design decision worth restating, because it looks like a bug until it is
read: **both routes revoke the caller's own session too**, and hand back a new
one in the same response. A stolen cookie is not another session -- it is a copy
of *this* one -- so sparing the current row to "keep the user signed in" would
spare precisely the credential the user came here to kill. Rotating instead keeps
them signed in and kills the copy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE
from chaudron.domain.models import UserAccount, UserSession
from chaudron.services.auth import hash_token, new_token
from tests.conftest import TEST_PASSWORD, SignedIn

pytestmark = pytest.mark.integration

_REVOKE_ALL = "/v1/auth/sessions/revoke-all"
_PASSWORD = "/v1/auth/password"
_NEW_PASSWORD = "a-brand-new-passphrase-nobody-guesses"


async def _other_device(session: AsyncSession, user: UserAccount) -> str:
    """A second live session for the same account, as a second browser would leave.

    Written directly as a row for the reason ``tests/conftest.py`` gives about the
    ``signed_in`` fixture: going through ``POST /v1/auth/login`` would make this
    file fail whenever the login endpoint did, and report it as a failure of
    revocation.
    """
    token = new_token()
    now = datetime.now(UTC)
    session.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_token=new_token(),
            expires_at=now + timedelta(days=30),
            idle_expires_at=now + timedelta(days=7),
        )
    )
    await session.flush()
    return token


def _client_for(app: FastAPI, token: str) -> httpx.AsyncClient:
    """A second browser holding *token* and nothing else."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        cookies={SESSION_COOKIE: token},
    )


def _adopt(client: httpx.AsyncClient, body: dict[str, str]) -> None:
    """Take the rotated CSRF token, as the interface does from the same body.

    The cookie is handled by the jar; the CSRF token is not a cookie on purpose
    (``routers/auth.py``), so a client that ignored this would send the previous
    one and get a 403 on its next write -- which is exactly what the interface
    would do if ``SessionProvider`` did not adopt the response.
    """
    client.headers[CSRF_HEADER] = body["csrf_token"]


# --------------------------------------------------------------------------- #
# Sign me out everywhere
# --------------------------------------------------------------------------- #


async def test_revoking_every_session_cuts_the_other_devices(
    api_client: httpx.AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    signed_in: SignedIn,
) -> None:
    other = await _other_device(db_session, signed_in.user)
    async with _client_for(api_app, other) as second:
        assert (await second.get("/v1/auth/session")).status_code == 200

        response = await api_client.post(_REVOKE_ALL)
        assert response.status_code == 200, response.text

        assert (await second.get("/v1/auth/session")).status_code == 401


async def test_revoking_every_session_keeps_the_caller_signed_in(
    api_client: httpx.AsyncClient, signed_in: SignedIn
) -> None:
    """ "Keep the current session" means the person, not the row.

    The row goes -- a copy of it is what they are trying to kill -- and a new
    credential arrives in the same response, so the browser that asked never sees
    a sign-in screen.
    """
    response = await api_client.post(_REVOKE_ALL)
    assert response.status_code == 200, response.text
    _adopt(api_client, response.json())

    assert response.json()["user_id"] == str(signed_in.user.id)
    assert response.json()["csrf_token"] != signed_in.csrf_token
    assert (await api_client.get("/v1/auth/session")).status_code == 200


async def test_the_cookie_presented_to_revoke_all_stops_working(
    api_client: httpx.AsyncClient, api_app: FastAPI, signed_in: SignedIn
) -> None:
    """The half that makes this a remedy rather than hygiene.

    A thief holding a copy of the very cookie that made this call must be cut off
    by it. Checked from a *second* client still holding the old value, because the
    first one has already been handed the replacement.
    """
    async with _client_for(api_app, signed_in.token) as thief:
        assert (await thief.get("/v1/auth/session")).status_code == 200

        assert (await api_client.post(_REVOKE_ALL)).status_code == 200

        assert (await thief.get("/v1/auth/session")).status_code == 401


async def test_revoke_all_needs_a_csrf_token(
    api_client: httpx.AsyncClient, api_app: FastAPI, signed_in: SignedIn
) -> None:
    """Or any page on the internet could sign a Chaudron user out of every device."""
    async with _client_for(api_app, signed_in.token) as without_csrf:
        response = await without_csrf.post(_REVOKE_ALL)

    assert response.status_code == 403
    assert response.json()["type"].endswith("/csrf-token-invalid")


async def test_revoke_all_refuses_an_anonymous_caller(
    anonymous_client: httpx.AsyncClient,
) -> None:
    assert (await anonymous_client.post(_REVOKE_ALL)).status_code == 401


# --------------------------------------------------------------------------- #
# Changing a password
# --------------------------------------------------------------------------- #


async def test_changing_the_password_needs_the_current_one(
    api_client: httpx.AsyncClient, db_session: AsyncSession, signed_in: SignedIn
) -> None:
    """There is no mail, so the old secret is the only proof of identity there is.

    Without this check a stolen cookie would be a permanent takeover, which is the
    failure the route above exists to answer.
    """
    response = await api_client.post(
        _PASSWORD,
        json={"current_password": "not-the-password", "new_password": _NEW_PASSWORD},
    )

    assert response.status_code == 403, response.text
    assert response.json()["type"].endswith("/current-password-invalid")

    await db_session.refresh(signed_in.user)
    reread = await db_session.scalar(
        select(UserAccount.password_hash).where(UserAccount.id == signed_in.user.id)
    )
    assert reread == signed_in.user.password_hash, "a refused attempt still wrote a digest"


async def test_changing_the_password_signs_the_other_devices_out(
    api_client: httpx.AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    signed_in: SignedIn,
) -> None:
    """A password change that left the intruder signed in would not be a remedy."""
    other = await _other_device(db_session, signed_in.user)
    async with _client_for(api_app, other) as second:
        assert (await second.get("/v1/auth/session")).status_code == 200

        response = await api_client.post(
            _PASSWORD,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
        )
        assert response.status_code == 200, response.text

        assert (await second.get("/v1/auth/session")).status_code == 401


async def test_changing_the_password_does_not_sign_out_the_person_who_changed_it(
    api_client: httpx.AsyncClient, signed_in: SignedIn
) -> None:
    response = await api_client.post(
        _PASSWORD,
        json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert response.status_code == 200, response.text
    _adopt(api_client, response.json())

    assert (await api_client.get("/v1/auth/session")).status_code == 200


async def test_the_new_password_is_the_one_that_signs_in(
    api_client: httpx.AsyncClient, anonymous_client: httpx.AsyncClient, signed_in: SignedIn
) -> None:
    """The end-to-end claim, through the real login endpoint.

    Both directions: the new one works and the old one does not. Asserting only the
    first would pass on an implementation that stored *both*.
    """
    changed = await api_client.post(
        _PASSWORD,
        json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert changed.status_code == 200, changed.text

    with_new = await anonymous_client.post(
        "/v1/auth/login",
        json={"email": signed_in.user.email, "password": _NEW_PASSWORD},
    )
    assert with_new.status_code == 200, with_new.text

    with_old = await anonymous_client.post(
        "/v1/auth/login",
        json={"email": signed_in.user.email, "password": TEST_PASSWORD},
    )
    assert with_old.status_code == 401


async def test_a_weak_new_password_is_refused_by_the_schema(
    api_client: httpx.AsyncClient,
) -> None:
    """And says so, unlike the current-password field, which must stay silent."""
    response = await api_client.post(
        _PASSWORD,
        json={"current_password": TEST_PASSWORD, "new_password": "short"},
    )
    assert response.status_code == 422


async def test_changing_the_password_needs_a_csrf_token(
    api_client: httpx.AsyncClient, api_app: FastAPI, signed_in: SignedIn
) -> None:
    async with _client_for(api_app, signed_in.token) as without_csrf:
        response = await without_csrf.post(
            _PASSWORD,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
        )

    assert response.status_code == 403
    assert response.json()["type"].endswith("/csrf-token-invalid")


async def test_neither_lever_is_reachable_with_a_machine_token(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """A bearer credential must not be able to rotate the account behind it.

    Falls out of ``PrincipalDep`` -- only a cookie produces a
    :class:`~chaudron.services.auth.Principal` -- and is asserted because the day
    somebody gives these routes a scope, the property stops being structural.
    """
    for path in (_REVOKE_ALL, _PASSWORD):
        response = await anonymous_client.post(
            path,
            json={"current_password": TEST_PASSWORD, "new_password": _NEW_PASSWORD},
            headers={"Authorization": "Bearer chdr_whatever"},
        )
        assert response.status_code == 401, path
