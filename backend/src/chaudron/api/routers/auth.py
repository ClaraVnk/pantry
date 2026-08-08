"""Sign up, sign in, sign out, "who am I?", and the two levers over a session.

Six routes and one cookie. The cookie is the only credential this API accepts,
and everything that makes it safe is set here in one place, so there is a single
line to read when somebody asks what protects it:

``__Host-`` (browser-enforced: ``Secure``, ``Path=/``, no ``Domain``, so no
sibling subdomain can write it), ``HttpOnly`` (script cannot read it, which is
what keeps an XSS from becoming a session export), ``SameSite=Lax`` (a
cross-site POST does not carry it) and ``Max-Age`` matching the row's absolute
expiry so the browser forgets it at roughly the moment the server does.

**There is no development mode and no way to weaken any of that.** The one that
would be tempting -- dropping ``Secure`` so a plain-HTTP deployment works -- is
absent on purpose: browsers already accept ``Secure`` cookies from ``localhost``
and ``127.0.0.1``, so the local loop does not need it, and a flag that exists is
a flag that eventually ships. ``Settings`` refuses to start a production instance
whose ``base_url`` is not ``https://`` instead (``config.py``).

**What this application cannot do, and does not pretend to.** There is no
outbound email anywhere in Chaudron. So there is no address verification, and --
the one that matters -- **no password reset**. A forgotten password is a
forgotten account, and the honest answer is to say so rather than to build an
unauthenticated recovery path, which is a back door with a friendly name. What it
would take is written down in ``docs/security-model.md``: SMTP configuration
validated at startup, single-use tokens with a short expiry stored hashed like
these ones, and rate limiting per address. Until that exists, an owner
re-inviting the person is the recovery path.

**What it can now do, and could not before.** ``POST /v1/auth/sessions/revoke-all``
and ``POST /v1/auth/password`` are the two things a person who suspects their
cookie has leaked needs, and until this change neither existed: the service method
behind the first had no caller anywhere in the repository, and there was no way to
change a password at all (audit AUD-028). The only bound on a stolen session was
the 30-day absolute expiry, and the only remedy was an operator with a psql
prompt. Both routes revoke **every** session including the one making the request
-- a copied cookie is a copy of *this* row, so sparing it spares the thief -- and
both mint a replacement before answering, so the remedy does not sign out the
person applying it.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from chaudron.api.deps import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthServiceDep,
    PrincipalDep,
    ThrottlesDep,
    get_settings_dep,
)
from chaudron.api.errors import (
    ProblemError,
    email_already_registered,
    invalid_credentials,
    rate_limited,
)
from chaudron.api.throttling import AtCapacityError
from chaudron.config import Settings
from chaudron.infra.passwords import MIN_PASSWORD_LENGTH
from chaudron.services.auth import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCurrentPasswordError,
    InvalidEmailError,
    IssuedSession,
    Principal,
    WeakPasswordError,
    normalise_email,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

#: Cookie lifetime is quoted to the browser in seconds. Kept equal to the row's
#: absolute expiry: a browser holding a cookie the server has already forgotten
#: produces a 401 the user cannot explain, which is worse than a login screen.
_SECONDS_PER_HOUR: Final = 3600


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class RegisterIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    display_name: str = Field(default="", max_length=120)
    household_name: str = Field(default="", max_length=120)


class ChangePasswordIn(BaseModel):
    """The old password and the new one. Both :class:`~pydantic.SecretStr`.

    So that a model dumped into a log line, a traceback frame or a debugger prints
    a mask rather than the credential. ``api/errors.py`` already strips pydantic's
    ``input`` echo from a 422; this is the second lock, and the same one
    ``routers/export_targets.py`` puts on a third party's token.

    ``min_length`` is declared on the new password and **not** on the current one,
    for the reason ``LoginIn`` gives: a short value offered as the current password
    is a *wrong* password, and a 422 naming the policy would answer a question the
    endpoint refuses to. ``max_length`` stays on both, because that one is a
    denial-of-service bound (``infra/passwords.py``).
    """

    current_password: SecretStr = Field(max_length=1024)
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    # No `min_length` here, deliberately: a short password is a *wrong* password
    # on this endpoint, and a 422 naming the rule would tell a caller that the
    # policy exists without telling them anything useful. `max_length` stays,
    # because that one is a denial-of-service bound (`infra/passwords.py`).
    password: str = Field(max_length=1024)


class HouseholdOut(BaseModel):
    id: str
    name: str
    role: str


class SessionOut(BaseModel):
    """Everything the interface needs to render a signed-in state.

    ``csrf_token`` is here because it has to reach the client somehow, and a
    response body is the one channel a cross-origin attacker cannot read: the
    browser refuses to hand them the body of a CORS request their origin is not
    allowed to make. Putting it in a second cookie instead would work only while
    the interface and the API share a host, which is not a property worth
    depending on.
    """

    user_id: str
    email: str
    display_name: str
    csrf_token: str
    households: list[HouseholdOut]


def _session_out(principal: Principal) -> SessionOut:
    return SessionOut(
        user_id=str(principal.user_id),
        email=principal.email,
        display_name=principal.display_name,
        csrf_token=principal.csrf_token,
        households=[
            HouseholdOut(id=str(m.household_id), name=m.household_name, role=str(m.role))
            for m in principal.memberships
        ],
    )


# --------------------------------------------------------------------------- #
# The cookie
# --------------------------------------------------------------------------- #


def _set_session_cookie(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=settings.session_absolute_ttl_hours * _SECONDS_PER_HOUR,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        # No `domain`: `__Host-` forbids it, and that prohibition is the whole
        # value of the prefix.
    )


def _clear_session_cookie(response: Response) -> None:
    # Same attributes as when it was set, or the browser deletes nothing: a
    # cookie is identified by name, domain and path together.
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")


def _client_key(request: Request) -> str:
    """Best available identity for a caller who has not signed in yet.

    Behind a reverse proxy this is the proxy, which collapses every caller into
    one bucket -- the same limitation ``routers/calendar.py`` documents. It still
    bounds a single misbehaving client on a direct deployment, and the per-account
    limiter below covers the case this one cannot see.
    """
    return request.client.host if request.client is not None else "unknown"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post(
    "/register",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account, its first household, and sign in",
)
async def register(
    request: Request,
    response: Response,
    payload: RegisterIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """Create the account and the household it owns, then start a session.

    Answering ``409`` for an address that already exists makes this endpoint an
    account enumerator, and that is accepted knowingly rather than overlooked:
    the alternative is to answer identically and send a message to the address,
    which needs outbound email this instance does not have. It is written down in
    ``services/auth.py`` and in ``docs/security-model.md`` so that adding SMTP
    later has a checklist item pointing here.
    """
    try:
        await throttles.registrations.acquire(_client_key(request))
    except AtCapacityError as exc:
        raise rate_limited(
            detail="Too many accounts have been created from this address. Try again later.",
            retry_after=exc.retry_after,
        ) from None

    try:
        issued = await auth.register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            household_name=payload.household_name,
        )
    except EmailAlreadyRegisteredError:
        raise email_already_registered() from None
    except WeakPasswordError as exc:
        raise ProblemError(
            slug="password-too-weak",
            title="Password too weak",
            status=422,
            detail=exc.detail,
        ) from None
    except InvalidEmailError as exc:
        raise ProblemError(
            slug="invalid-email", title="Invalid email address", status=422, detail=str(exc)
        ) from None

    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


@router.post("/login", response_model=SessionOut, summary="Start a session")
async def login(
    request: Request,
    response: Response,
    payload: LoginIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """Verify the credentials and mint a **new** session.

    "New" is the anti-fixation property: whatever session identifier the browser
    arrived holding, the one it leaves with was generated here, after the
    password was checked (``AuthService.issue_session``). Any session the request
    presented is revoked on the way through, so a credential planted in the
    victim's browser before sign-in is dead rather than merely superseded.

    Two limiters, because neither sees what the other does. Per source address
    bounds one host spraying many accounts; per address bounds a botnet guessing
    at one account. The per-address counter is incremented for addresses that do
    not exist too -- otherwise its own behaviour would answer the question the
    endpoint refuses to.

    There is no CSRF token to check here (there is no session yet), and none is
    needed: the body is JSON, and a cross-site HTML form cannot send
    ``application/json``. A form-encoded body reaches the validation layer and is
    refused with ``422`` before any credential is read.
    """
    address = normalise_email(payload.email)
    for key, limiter in (
        (_client_key(request), throttles.login_attempts_by_ip),
        (address, throttles.login_attempts_by_account),
    ):
        try:
            await limiter.acquire(key)
        except AtCapacityError as exc:
            raise rate_limited(
                detail="Too many sign-in attempts. Wait before trying again.",
                retry_after=exc.retry_after,
            ) from None

    user = await auth.authenticate(email=payload.email, password=payload.password)
    if user is None:
        raise invalid_credentials()

    await _revoke_presented_session(request, auth)
    issued = await auth.issue_session(user)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


async def _revoke_presented_session(request: Request, auth: AuthService) -> None:
    """Kill whatever session the browser arrived with, before minting the next one."""
    existing = request.cookies.get(SESSION_COOKIE)
    if not existing:
        return
    principal = await auth.resolve(existing)
    if principal is not None:
        await auth.revoke(principal.session_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(response: Response, principal: PrincipalDep, auth: AuthServiceDep) -> None:
    """Revoke the session server-side, then clear the cookie.

    In that order, and both halves matter. Clearing the cookie alone would leave
    a live row that anyone holding a copy of the token could keep using; revoking
    alone would leave the browser sending a dead credential on every request.

    Behind ``PrincipalDep``, so it also requires a valid ``X-CSRF-Token``: without
    that, any page on the internet could sign a user out of Chaudron.
    """
    await auth.revoke(principal.session_id)
    _clear_session_cookie(response)


@router.post(
    "/sessions/revoke-all",
    response_model=SessionOut,
    summary="End every session of this account, and start a fresh one here",
)
async def revoke_all_sessions(
    response: Response,
    principal: PrincipalDep,
    auth: AuthServiceDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """ "Sign me out everywhere." The lever for a cookie somebody thinks has leaked.

    ``AuthService.revoke_all`` has existed since sessions did, described as *the
    lever for a suspected compromise*, and was reachable from nothing: no route,
    no script, no test (audit AUD-028). A user who believed their cookie had been
    copied had **nothing** -- the only bound was the 30-day absolute expiry, and
    the only cure was an operator with a psql prompt.

    **Every row goes, this request's included, and a new one is minted before the
    response leaves.** That combination is the whole design and neither half is
    negotiable.

    Sparing the current row would be the obvious reading of "keep me signed in",
    and it is precisely wrong for the situation that brings someone here: a stolen
    cookie is not *another* session, it is a **copy of this one**. Sparing the row
    would spare the thief. So the row dies, and the browser that asked is handed a
    freshly generated credential in the same response -- which is session rotation,
    the same gesture ``POST /v1/auth/login`` performs, for the same reason.

    Behind :data:`PrincipalDep`, so a valid ``X-CSRF-Token`` is required and a
    machine token cannot reach it. Without the first, any page on the internet
    could sign a Chaudron user out of every device they own.

    What this does **not** touch: machine tokens. They are a separate credential
    with their own list and their own revocation (``routers/tokens.py``), and
    silently killing a household's integrations because one person's laptop was
    stolen would be a surprise nobody asked for. The interface says so next to the
    button.
    """
    await auth.revoke_all(principal.user_id)
    issued = await auth.issue_session_for(principal.user_id)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


@router.post(
    "/password",
    response_model=SessionOut,
    summary="Change this account's password, ending every other session",
)
async def change_password(
    response: Response,
    payload: ChangePasswordIn,
    principal: PrincipalDep,
    auth: AuthServiceDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """Replace the password, having proved the old one, and rotate the session.

    **The current password is required**, and this is the endpoint where the
    absence of outbound mail stops being a documentation note and becomes a
    constraint. There is no reset link, no proof of control over an address,
    nothing that can stand in for the secret the caller already holds. An endpoint
    that skipped the check would turn a stolen cookie into a permanent takeover --
    the exact failure the route above it exists to answer.

    **Changing a password ends every session.** That is what makes it useful as a
    remedy rather than merely as hygiene: a password change that left the other
    browsers signed in would leave the intruder signed in. And, as above, the
    caller does not get logged out by their own remedy -- the response carries a
    new cookie and a new CSRF token, minted after the revocation.

    Not rate limited by a counter of its own, deliberately. Reaching it costs a
    live session **and** a matching CSRF token, so it is not a door a stranger can
    knock on; and each call pays one Argon2 verification, which is the same cost
    ``POST /v1/auth/login`` pays and is itself the bound. A limiter keyed on the
    account would additionally let anyone holding the cookie lock the owner out of
    signing back in, which is a worse trade.
    """
    try:
        user = await auth.change_password(
            user_id=principal.user_id,
            current_password=payload.current_password.get_secret_value(),
            new_password=payload.new_password.get_secret_value(),
        )
    except InvalidCurrentPasswordError:
        raise ProblemError(
            slug="current-password-invalid",
            title="Current password does not match",
            status=403,
            detail=(
                "The current password sent with this request does not match this "
                "account. The password was not changed."
            ),
        ) from None
    except WeakPasswordError as exc:
        raise ProblemError(
            slug="password-too-weak",
            title="Password too weak",
            status=422,
            detail=exc.detail,
        ) from None

    await auth.revoke_all(user.id)
    issued = await auth.issue_session(user)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


@router.get("/session", response_model=SessionOut, summary="The current session")
async def read_session(principal: PrincipalDep) -> SessionOut:
    """Who is signed in, which households they may open, and the CSRF token.

    Called by the interface on every load: the cookie survives a refresh, the
    in-memory CSRF token does not, and this is where it is fetched back. A ``401``
    here is the signal to show the sign-in screen.
    """
    return _session_out(principal)


#: The header the interface must echo. Exported so ``api/main.py`` can add it to
#: the CORS allow-list without repeating the string.
__all__ = ["CSRF_HEADER", "router"]
