"""Sign-up, sign-in, sign-out, CSRF, and the household a caller may not open.

The census in ``test_route_authentication.py`` proves every route *asks* for a
session. This file proves the session mechanism itself does what it claims:
that the cookie is the credential, that revoking it is immediate, that a
state-changing request without a CSRF token is refused, and that a signed-in
account cannot reach a household it is not a member of.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE
from chaudron.api.throttling import ConcurrencyLimiter, Throttles
from chaudron.domain.models import MembershipRole, UserAccount, UserSession
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from chaudron.services.auth import hash_token
from tests.conftest import (
    TEST_PASSWORD,
    MakeHousehold,
    MakeUser,
    SignedIn,
    household_headers,
)

# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


async def test_registration_creates_an_account_a_household_and_an_owner(
    anonymous_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await anonymous_client.post(
        "/v1/auth/register",
        json={
            "email": "Nouvelle@Example.test",
            "password": "un-mot-de-passe-assez-long",
            "display_name": "Nouvelle",
            "household_name": "Chez Nouvelle",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["email"] == "nouvelle@example.test", "the address is normalised on the way in"
    assert len(body["households"]) == 1
    assert body["households"][0]["name"] == "Chez Nouvelle"
    assert body["households"][0]["role"] == "owner"
    assert body["csrf_token"]

    # And the session is usable straight away, from the cookie the response set.
    assert SESSION_COOKIE in response.cookies
    listing = await anonymous_client.get("/v1/locations", headers={CSRF_HEADER: body["csrf_token"]})
    assert listing.status_code == 200

    stored = await db_session.scalar(
        select(UserAccount).where(UserAccount.email == "nouvelle@example.test")
    )
    assert stored is not None
    assert stored.password_hash is not None
    assert stored.password_hash.startswith("$argon2id$"), "Argon2id, not bcrypt and not PBKDF2"
    assert TEST_PASSWORD not in stored.password_hash


async def test_the_session_cookie_is_host_prefixed_httponly_secure_and_lax(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """Every attribute that makes the cookie safe, asserted on the wire.

    Read off the raw ``Set-Cookie`` header rather than a parsed jar, because the
    jar drops exactly the attributes under test.
    """
    response = await anonymous_client.post(
        "/v1/auth/register",
        json={"email": "cookie@example.test", "password": "un-mot-de-passe-assez-long"},
    )
    assert response.status_code == 201
    raw = response.headers["set-cookie"]

    assert raw.startswith(f"{SESSION_COOKIE}="), "the __Host- prefix is what forbids a Domain"
    lowered = raw.lower()
    assert "httponly" in lowered, "script must not be able to read the session"
    assert "secure" in lowered, "required by the __Host- prefix, and by not shipping it in clear"
    assert "samesite=lax" in lowered, "a cross-site POST must not carry it"
    assert "path=/" in lowered
    assert "domain=" not in lowered, "__Host- forbids Domain; a sibling subdomain must not write it"


async def test_the_stored_token_is_a_digest_not_the_cookie(
    anonymous_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A dump of ``user_session`` must not yield a single usable session."""
    response = await anonymous_client.post(
        "/v1/auth/register",
        json={"email": "digest@example.test", "password": "un-mot-de-passe-assez-long"},
    )
    token = response.cookies[SESSION_COOKIE]

    rows = list(await db_session.scalars(select(UserSession)))
    assert rows, "the session was not written"
    assert all(row.token_hash != token for row in rows), "the plaintext token reached the database"
    assert any(row.token_hash == hash_token(token) for row in rows)


async def test_a_second_account_cannot_take_the_same_address(
    anonymous_client: httpx.AsyncClient,
) -> None:
    payload = {"email": "twice@example.test", "password": "un-mot-de-passe-assez-long"}
    assert (await anonymous_client.post("/v1/auth/register", json=payload)).status_code == 201

    again = await anonymous_client.post(
        "/v1/auth/register", json={**payload, "email": "TWICE@example.test"}
    )
    assert again.status_code == 409
    assert again.json()["type"].endswith("/email-already-registered")


async def test_a_short_password_is_refused(anonymous_client: httpx.AsyncClient) -> None:
    response = await anonymous_client.post(
        "/v1/auth/register", json={"email": "short@example.test", "password": "court"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


async def test_login_accepts_the_password_and_returns_a_working_session(
    anonymous_client: httpx.AsyncClient,
    signed_in_user: UserAccount,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()

    response = await anonymous_client.post(
        "/v1/auth/login", json={"email": signed_in_user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]

    listing = await anonymous_client.get(
        "/v1/locations", headers={**household_headers(household), CSRF_HEADER: csrf}
    )
    assert listing.status_code == 200


async def test_login_is_case_insensitive_on_the_address(
    anonymous_client: httpx.AsyncClient, signed_in_user: UserAccount
) -> None:
    response = await anonymous_client.post(
        "/v1/auth/login",
        json={"email": signed_in_user.email.upper(), "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


async def test_an_unknown_address_and_a_wrong_password_are_indistinguishable(
    anonymous_client: httpx.AsyncClient, signed_in_user: UserAccount
) -> None:
    """No account enumeration: same status, same body, same problem type.

    The *timing* is equalised in ``infra/passwords.py`` -- an unknown address
    still burns one full Argon2 verification against a placeholder digest -- which
    cannot be asserted reliably on a shared CI runner. What is asserted here is
    the half that can be: the responses are identical.
    """
    unknown = await anonymous_client.post(
        "/v1/auth/login",
        json={"email": "nobody-at-all@example.test", "password": "un-mot-de-passe-assez-long"},
    )
    wrong = await anonymous_client.post(
        "/v1/auth/login",
        json={"email": signed_in_user.email, "password": "un-mot-de-passe-assez-faux"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert _without_request_id(unknown) == _without_request_id(wrong)
    assert unknown.json()["type"].endswith("/invalid-credentials")


async def test_a_disabled_account_cannot_sign_in(
    anonymous_client: httpx.AsyncClient, signed_in_user: UserAccount, db_session: AsyncSession
) -> None:
    signed_in_user.disabled_at = datetime.now(UTC)
    await db_session.flush()

    response = await anonymous_client.post(
        "/v1/auth/login", json={"email": signed_in_user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 401


async def test_an_account_with_no_password_cannot_sign_in(
    anonymous_client: httpx.AsyncClient, make_user: MakeUser
) -> None:
    """``password_hash`` is nullable for external identity providers.

    A ``NULL`` there must never be a password that matches anything -- including
    the empty string, which is the shape this bug always takes.
    """
    user = await make_user()
    for candidate in ("", "un-mot-de-passe-assez-long"):
        response = await anonymous_client.post(
            "/v1/auth/login", json={"email": user.email, "password": candidate}
        )
        assert response.status_code == 401


async def test_login_rotates_the_session_identifier(
    anonymous_client: httpx.AsyncClient, signed_in: SignedIn
) -> None:
    """Session fixation: the identifier the browser leaves with was minted here.

    The client arrives holding a valid session. After signing in it holds a
    *different* token, and the one it arrived with is dead -- so a credential
    planted in a victim's browser before sign-in cannot be used afterwards.
    """
    anonymous_client.cookies.set(SESSION_COOKIE, signed_in.token)

    response = await anonymous_client.post(
        "/v1/auth/login", json={"email": signed_in.user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    minted = response.cookies[SESSION_COOKIE]
    assert minted != signed_in.token, "the pre-existing session identifier survived sign-in"

    # The planted one no longer resolves.
    stale = httpx.AsyncClient(
        transport=anonymous_client._transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    )
    async with stale:
        assert (await stale.get("/v1/locations")).status_code == 401


async def test_sign_in_is_rate_limited_per_account_and_per_address(
    api_app: FastAPI, anonymous_client: httpx.AsyncClient, signed_in_user: UserAccount
) -> None:
    """Two attempts allowed, the third refused -- and refused with ``Retry-After``."""
    api_app.state.throttles = _throttles(api_app, logins=2)

    for _ in range(2):
        attempt = await anonymous_client.post(
            "/v1/auth/login",
            json={"email": signed_in_user.email, "password": "un-mot-de-passe-assez-faux"},
        )
        assert attempt.status_code == 401

    refused = await anonymous_client.post(
        "/v1/auth/login", json={"email": signed_in_user.email, "password": TEST_PASSWORD}
    )
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1


async def test_the_per_account_limiter_counts_addresses_that_do_not_exist(
    api_app: FastAPI, anonymous_client: httpx.AsyncClient
) -> None:
    """Otherwise the limiter itself answers the question the endpoint refuses to.

    If misses were not counted, a caller could tell a real address from a fake
    one by whether the fourth attempt came back ``429`` or ``401``.
    """
    api_app.state.throttles = _throttles(api_app, logins=2)
    body = {"email": "ghost@example.test", "password": "un-mot-de-passe-assez-long"}

    assert (await anonymous_client.post("/v1/auth/login", json=body)).status_code == 401
    assert (await anonymous_client.post("/v1/auth/login", json=body)).status_code == 401
    assert (await anonymous_client.post("/v1/auth/login", json=body)).status_code == 429


async def test_a_form_encoded_login_is_refused(anonymous_client: httpx.AsyncClient) -> None:
    """Login CSRF: a cross-site ``<form>`` cannot send ``application/json``.

    That is what protects the one unsafe route that has no session yet, and
    therefore no CSRF token to check. A form-encoded body reaches validation and
    is refused before any credential is read.
    """
    response = await anonymous_client.post(
        "/v1/auth/login", data={"email": "a@example.test", "password": "un-mot-de-passe"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Sign-out
# --------------------------------------------------------------------------- #


async def test_logout_revokes_the_session_server_side(
    api_client: httpx.AsyncClient, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    """Immediate revocation, which is the whole reason sessions are rows.

    The token is deliberately re-presented after the response asked the browser
    to drop it. Clearing the client's copy is a courtesy; a sign-out that relied
    on it would go on working for anybody who had kept the value -- which is the
    entire class of problem server-side sessions exist to remove.
    """
    await make_household()
    assert (await api_client.get("/v1/locations")).status_code == 200

    assert (await api_client.post("/v1/auth/logout")).status_code == 204

    api_client.cookies.set(SESSION_COOKIE, signed_in.token)
    assert (await api_client.get("/v1/locations")).status_code == 401


async def test_logout_clears_the_cookie_in_the_browser_too(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    await make_household()
    response = await api_client.post("/v1/auth/logout")
    assert response.status_code == 204
    assert SESSION_COOKIE in response.headers.get("set-cookie", "")


async def test_logout_needs_a_csrf_token(
    api_app: FastAPI, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    """Otherwise any page on the internet could sign a user out of Chaudron."""
    await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as forged:
        assert (await forged.post("/v1/auth/logout")).status_code == 403


async def test_the_session_endpoint_reports_the_caller(
    api_client: httpx.AsyncClient, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    household = await make_household(name="Foyer principal")
    response = await api_client.get("/v1/auth/session")
    assert response.status_code == 200

    body = response.json()
    assert body["email"] == signed_in.user.email
    assert body["csrf_token"] == signed_in.csrf_token
    assert [row["id"] for row in body["households"]] == [str(household.id)]


async def test_the_session_endpoint_is_401_without_a_cookie(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """The signal the interface uses to decide between the app and the sign-in screen."""
    assert (await anonymous_client.get("/v1/auth/session")).status_code == 401


# --------------------------------------------------------------------------- #
# Cross-site request forgery
# --------------------------------------------------------------------------- #


async def test_a_write_without_a_csrf_token_is_refused(
    api_app: FastAPI, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    """**The regression this whole change could have introduced.**

    While the API was authorised by ``X-Household-Id``, forgery was impossible by
    construction: a third-party form cannot set a header. A cookie is ambient
    authority -- the browser attaches it to a request the user never made -- so
    the protection has to be put back explicitly, and proved.

    The client below is exactly what a forged request looks like: the session
    cookie is present, because the browser sends it, and the CSRF header is not,
    because a cross-site form cannot produce one.
    """
    household = await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as forged:
        response = await forged.post(
            "/v1/members",
            headers=household_headers(household),
            json={"display_name": "Injecté", "age_band": "adult", "diet": "omnivore"},
        )

    assert response.status_code == 403
    assert response.json()["type"].endswith("/csrf-token-invalid")


async def test_a_write_with_the_wrong_csrf_token_is_refused(
    api_app: FastAPI, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    """A token from somewhere else is no better than none."""
    household = await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: "not-the-token-for-this-session"},
    ) as forged:
        response = await forged.delete(
            f"/v1/inventory/{uuid.uuid7()}", headers=household_headers(household)
        )
    assert response.status_code == 403
    assert response.json()["type"].endswith("/csrf-token-invalid")


#: One real route per unsafe verb. A path that does not serve the method answers
#: ``405`` from the router before any dependency runs, which would make this test
#: pass without proving anything.
_UNSAFE_ROUTES = [
    ("POST", "/v1/members"),
    ("PUT", "/v1/budget/target"),
    ("PATCH", f"/v1/members/{uuid.uuid7()}"),
    ("DELETE", "/v1/budget/target"),
]


@pytest.mark.parametrize(("method", "path"), _UNSAFE_ROUTES, ids=[m for m, _ in _UNSAFE_ROUTES])
async def test_every_unsafe_method_needs_a_token(
    api_app: FastAPI,
    signed_in: SignedIn,
    make_household: MakeHousehold,
    method: str,
    path: str,
) -> None:
    """The rule is the method, not the route: no unsafe verb is exempt."""
    household = await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as forged:
        response = await forged.request(method, path, headers=household_headers(household), json={})
    assert response.status_code != 405, f"{method} {path} is not routed; the test proves nothing"
    assert response.status_code == 403, f"{method} was accepted without a CSRF token"
    assert response.json()["type"].endswith("/csrf-token-invalid")


async def test_reads_do_not_need_a_csrf_token(
    api_app: FastAPI, signed_in: SignedIn, make_household: MakeHousehold
) -> None:
    """A ``GET`` changes nothing, so requiring a token there would only break clients."""
    household = await make_household()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
    ) as reader:
        response = await reader.get("/v1/locations", headers=household_headers(household))
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Household selection
# --------------------------------------------------------------------------- #


async def test_a_household_the_caller_does_not_belong_to_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Authenticated, and still not entitled. ``403``, and no data.

    The header is a selector; membership is the authorisation. This is the case
    the audit's attacker had: a valid identifier for somebody else's household.
    """
    await make_household(name="Mine")
    theirs = await make_household(name="Theirs", member=False)

    for method, path in (
        ("GET", "/v1/locations"),
        ("GET", "/v1/inventory"),
        ("GET", "/v1/members"),
        ("GET", "/v1/shopping-lists/current"),
    ):
        response = await api_client.request(method, path, headers=household_headers(theirs))
        assert response.status_code == 403, f"{path} let a non-member in"
        assert response.json()["type"].endswith("/household-forbidden")


async def test_a_write_into_another_households_scope_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    await make_household(name="Mine")
    theirs = await make_household(name="Theirs", member=False)

    response = await api_client.post(
        "/v1/members",
        headers=household_headers(theirs),
        json={"display_name": "Intrus", "age_band": "adult", "diet": "omnivore"},
    )
    assert response.status_code == 403


async def test_several_memberships_require_an_explicit_choice(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Two households and no header: guessing would be wrong half the time."""
    first = await make_household(name="Aaa maison")
    second = await make_household(name="Bbb colocation")

    response = await api_client.get("/v1/locations")
    assert response.status_code == 409
    body = response.json()
    assert body["type"].endswith("/household-selection-required")
    assert {row["id"] for row in body["households"]} == {str(first.id), str(second.id)}

    # And naming either of them works.
    for household in (first, second):
        chosen = await api_client.get("/v1/locations", headers=household_headers(household))
        assert chosen.status_code == 200


async def test_an_account_with_no_household_is_told_so(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/v1/locations")
    assert response.status_code == 403
    assert response.json()["type"].endswith("/no-household")


async def test_an_archived_household_disappears_from_the_memberships(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """Archiving is how a household leaves the instance; it must also leave the session."""
    household = await make_household()
    household.archived_at = datetime.now(UTC)
    await db_session.flush()

    assert (await api_client.get("/v1/auth/session")).json()["households"] == []
    assert (
        await api_client.get("/v1/locations", headers=household_headers(household))
    ).status_code == 403


# --------------------------------------------------------------------------- #
# Session lifetime and revocation
# --------------------------------------------------------------------------- #


async def test_an_expired_session_is_not_accepted(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    signed_in: SignedIn,
    db_session: AsyncSession,
) -> None:
    await make_household()
    record = await db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(signed_in.token))
    )
    assert record is not None
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    assert (await api_client.get("/v1/locations")).status_code == 401


async def test_an_idle_session_is_not_accepted(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    signed_in: SignedIn,
    db_session: AsyncSession,
) -> None:
    """The absolute deadline is still far away; the idle one has passed."""
    await make_household()
    record = await db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(signed_in.token))
    )
    assert record is not None
    record.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    assert (await api_client.get("/v1/locations")).status_code == 401


async def test_disabling_an_account_cuts_its_live_sessions_immediately(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    signed_in: SignedIn,
    db_session: AsyncSession,
) -> None:
    """Not at expiry -- now. This is what a stateless token cannot do."""
    await make_household()
    assert (await api_client.get("/v1/locations")).status_code == 200

    signed_in.user.disabled_at = datetime.now(UTC)
    await db_session.flush()

    assert (await api_client.get("/v1/locations")).status_code == 401


async def test_an_invented_cookie_resolves_to_nobody(
    anonymous_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    await make_household()
    anonymous_client.cookies.set(SESSION_COOKIE, "a" * 43)
    assert (await anonymous_client.get("/v1/locations")).status_code == 401


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


async def test_the_calendar_subscription_is_owner_only(
    api_client: httpx.AsyncClient,
    api_app: FastAPI,
    make_household: MakeHousehold,
) -> None:
    """It hands out a bearer secret that only an instance-wide rotation revokes.

    A ``member`` may read the stock; giving them a credential nobody can withdraw
    for them alone is a different decision (``routers/calendar.py``).
    """
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"calendar_feed_enabled": True}
    )
    household = await make_household(role=MembershipRole.MEMBER)

    response = await api_client.get(
        "/v1/calendar/subscription", headers=household_headers(household)
    )
    assert response.status_code == 403
    assert response.json()["type"].endswith("/household-forbidden")


async def test_the_calendar_subscription_is_served_to_an_owner(
    api_client: httpx.AsyncClient, api_app: FastAPI, make_household: MakeHousehold
) -> None:
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"calendar_feed_enabled": True}
    )
    household = await make_household(role=MembershipRole.OWNER)

    response = await api_client.get(
        "/v1/calendar/subscription", headers=household_headers(household)
    )
    assert response.status_code == 200
    assert response.json()["password"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _without_request_id(response: httpx.Response) -> dict[str, object]:
    body = dict(response.json())
    body.pop("request_id", None)
    return body


def _throttles(app: FastAPI, *, logins: int) -> Throttles:
    """Sign-in limiters tight enough to reach, in scopes no other test can reach.

    Takes the application for the engine the buckets live in and for the scope
    prefix the harness made unique for this test (``tests/conftest.py``). The
    prefix is what keeps this honest: these rows are committed on a connection of
    the limiter's own, so they survive the test that wrote them, and two tests
    sharing a scope would share one budget -- the second would then get its
    ``429`` from the first test's spending rather than from its own.
    """
    engine = app.state.database.engine
    prefix = f"{app.state.rate_limit_scope_prefix}auth:"

    def rate(name: str, limit: int, window_seconds: float) -> SharedRateLimiter:
        return SharedRateLimiter(
            engine,
            BucketPolicy(scope=f"{prefix}{name}", limit=limit, window_seconds=window_seconds),
        )

    return Throttles(
        recipe_suggestions=rate("recipe_suggestions", 100, 3600.0),
        recipe_inferences=ConcurrencyLimiter(per_key=4, total=8),
        product_lookups=rate("product_lookups", 100, 60.0),
        receipt_imports=rate("receipt_imports", 100, 3600.0),
        receipt_inferences=ConcurrencyLimiter(per_key=4, total=8),
        shopping_imports=rate("shopping_imports", 100, 3600.0),
        shopping_import_documents=ConcurrencyLimiter(per_key=4, total=8),
        login_attempts_by_ip=rate("login_attempts_by_ip", logins, 3600.0),
        login_attempts_by_account=rate("login_attempts_by_account", logins, 3600.0),
        registrations=rate("registrations", logins, 3600.0),
        machine_token_attempts=rate("machine_token_attempts", logins, 3600.0),
    )
