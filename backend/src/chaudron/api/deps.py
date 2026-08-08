"""Dependency wiring: the transaction, the tenant, and the services.

Everything a handler needs arrives through this module, and nothing a handler
needs is built inside a handler. That is what lets the test suite swap the
session for one that rolls back, without the routes knowing.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import Depends, Header, Request, Security
from fastapi.security import SecurityScopes
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.errors import (
    authentication_required,
    csrf_token_invalid,
    household_forbidden,
    household_selection_required,
    insufficient_role,
    insufficient_scope,
    no_household,
    rate_limited,
    token_not_accepted,
)
from chaudron.api.throttling import AtCapacityError, Throttles
from chaudron.config import Settings
from chaudron.domain.models import MachineTokenScope, MembershipRole
from chaudron.domain.ports import ProductCatalog
from chaudron.domain.shopping import (
    AllergenScreen,
    DeclinedRepurchaseStore,
    ShoppingLineSplitter,
)
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.db import Database
from chaudron.infra.logging import household_id_var
from chaudron.infra.passwords import Passwords
from chaudron.infra.repositories import (
    SqlDeclinedRepurchaseStore,
    SqlInventoryRepository,
    SqlLocationRepository,
    SqlProductRepository,
    SqlUnitRegistry,
)
from chaudron.services.auth import AuthService, Principal, SessionLifetimes
from chaudron.services.balance import BalanceService
from chaudron.services.dietary import DietaryService
from chaudron.services.inventory import InventoryService
from chaudron.services.locations import LocationService
from chaudron.services.members import MemberService
from chaudron.services.privacy import PrivacyService
from chaudron.services.products import ProductService
from chaudron.services.providers import (
    ProviderCredentialService,
    ProviderPortsBuilder,
    ProviderService,
    provider_ports_builder,
)
from chaudron.services.receipts import ReceiptBounds, ReceiptImportService
from chaudron.services.recipe_feedback import RecipeFeedbackService
from chaudron.services.recipes import RecipeService
from chaudron.services.shopping_import import (
    DepletionService,
    ImportBounds,
    ShoppingImportService,
)
from chaudron.services.shopping_lists import DeclinedRepurchaseService, ShoppingListService
from chaudron.services.tokens import MachineTokenGrant, MachineTokenService

HOUSEHOLD_HEADER = "X-Household-Id"

#: The canonical 36-character lowercase form, and nothing else. ``uuid.UUID``
#: also accepts ``urn:uuid:…``, ``{…}`` and the unhyphenated digest, so three
#: spellings of one household reach the application today (audit AUD-026). That
#: is harmless while the value is only ever used as a :class:`uuid.UUID` -- and
#: stops being harmless the moment anything keys on the *string*, which the rate
#: limiter added alongside this now does. Normalising after the fact would work
#: just as well; refusing is shorter and leaves nothing to forget.
_CANONICAL_UUID: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """One session per request, inside one transaction (see ``infra/db.py``)."""
    async with database.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #
#
# Declared before authentication rather than after it, because authentication is
# now one of the things that has to be rate limited: presenting a machine token
# that does not resolve is an unauthenticated guess, and the limiter that bounds
# it has to exist by the time :func:`resolve_caller` is written.


def get_throttles(request: Request) -> Throttles:
    throttles: Throttles = request.app.state.throttles
    return throttles


ThrottlesDep = Annotated[Throttles, Depends(get_throttles)]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

#: The session cookie. ``__Host-`` is not decoration: a browser only accepts a
#: cookie under that prefix when it is ``Secure``, has ``Path=/`` and carries **no
#: ``Domain``** -- which is precisely the set of properties that stops a
#: neighbouring subdomain, or a page that has taken one over, from writing a
#: cookie this API would then read as a session.
SESSION_COOKIE = "__Host-chaudron_session"

#: Echoed by the client on every unsafe method, and compared against the value on
#: the session row. See :func:`chaudron.api.errors.csrf_token_invalid` for why
#: this exists at all, and why ``SameSite=Lax`` alone was not judged enough.
CSRF_HEADER = "X-CSRF-Token"

#: Methods that do not change state, and therefore carry no CSRF requirement.
#: ``HEAD`` and ``OPTIONS`` are here for the same reason as ``GET``; every other
#: method, including any added later, needs a token because the default has to
#: fail closed.
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def get_passwords(request: Request) -> Passwords:
    """The process-wide hasher, built once by the factory.

    On ``app.state`` rather than constructed per request because
    :class:`Passwords` computes a placeholder digest at construction -- a full
    Argon2 hash -- and paying 64 MiB and three passes to *build a dependency* on
    every login would be a denial of service of our own making.
    """
    passwords: Passwords = request.app.state.passwords
    return passwords


def get_auth_service(
    session: SessionDep,
    passwords: Annotated[Passwords, Depends(get_passwords)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> AuthService:
    return AuthService(
        session,
        passwords,
        SessionLifetimes(
            absolute=timedelta(hours=settings.session_absolute_ttl_hours),
            idle=timedelta(hours=settings.session_idle_ttl_hours),
        ),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_machine_token_service(
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> MachineTokenService:
    return MachineTokenService(session, max_per_household=settings.machine_tokens_per_household)


MachineTokenServiceDep = Annotated[MachineTokenService, Depends(get_machine_token_service)]


def enforce_csrf(request: Request, principal: Principal) -> None:
    """Refuse an unsafe method that did not echo this session's CSRF token.

    Compared with :func:`secrets.compare_digest` rather than ``==``: the token is
    a secret held by the legitimate client, and a byte-by-byte comparison that
    returns early is measurable often enough to be worth not writing.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    presented = request.headers.get(CSRF_HEADER)
    if not presented or not secrets.compare_digest(presented, principal.csrf_token):
        raise csrf_token_invalid()


#: The only authentication scheme this API accepts in an ``Authorization``
#: header for its own routes. Compared case-insensitively, as RFC 9110 requires.
#: ``Basic`` is not handled here on purpose -- the CalDAV feed has its own scheme
#: and its own authenticator (``routers/calendar.py``), and a header this function
#: does not recognise falls through to the cookie, which is what keeps the two
#: mechanisms from interfering.
BEARER_SCHEME: Final = "bearer"


def bearer_credential(request: Request) -> str | None:
    """The value of ``Authorization: Bearer <value>``, or ``None``.

    Two shapes used to fall through to the cookie instead of being refused, and
    both are worth a sentence because "fails closed" was the wrong reading of
    them (audit AUD-030).

    **``Bearer\\t<token>``.** RFC 9110 lets one or more spaces *or horizontal
    tabs* separate the scheme from its parameter, so a tab is a well-formed
    header. Splitting on a single space made the whole string the "scheme", which
    matched nothing, which meant the token was ignored and the request was
    authenticated by whatever cookie the browser happened to be holding. Nothing
    was granted that the cookie did not already carry -- and a client convinced it
    is acting as its integration while the server sees a *person* is a client
    whose audit trail is wrong. :meth:`str.split` with no separator splits on any
    run of whitespace, which is exactly the grammar.

    **``Bearer`` with nothing after it.** A client whose token variable expanded
    to the empty string. Falling back to the cookie there is the same confusion
    with a more likely cause, so it is a ``401`` naming the credential that was
    presented rather than a silent success under another one.

    An ``Authorization`` header that is *entirely* blank is still treated as
    absent: some proxies emit one, it names no scheme, and refusing it would break
    the cookie path for a header nobody meant to send. A header naming another
    scheme also falls through, which is what keeps the CalDAV feed's own ``Basic``
    authenticator (``routers/calendar.py``) from being caught here.
    """
    header = request.headers.get("Authorization")
    if header is None:
        return None
    parts = header.split(maxsplit=1)
    if not parts or parts[0].lower() != BEARER_SCHEME:
        return None
    presented = parts[1].strip() if len(parts) > 1 else ""
    if not presented:
        # The scheme *was* named. Answering with the same 401 as an unknown token
        # keeps this from being a second, chattier refusal a prober could tell
        # apart from a wrong value.
        raise token_not_accepted()
    return presented


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling, and by which of the two doors.

    Exactly one of the two fields is set. A browser arrives with a ``__Host-``
    cookie and a CSRF token and becomes a :class:`Principal`; a program arrives
    with ``Authorization: Bearer`` and becomes a
    :class:`~chaudron.services.tokens.MachineTokenGrant`. Nothing downstream has
    to know which, except the handful of routes that must refuse one of them --
    and those say so by asking for the thing they need.
    """

    principal: Principal | None = None
    grant: MachineTokenGrant | None = None

    def require_browser_session(self) -> Principal:
        """The session behind this call, or ``401`` for a machine token.

        The contract's rule that creating and revoking a token needs a browser
        session is enforced here and nowhere else: those routes ask for a
        :class:`Principal`, and a token cannot produce one. Without it a stolen
        token could mint replacements faster than an owner could revoke them.
        """
        if self.principal is None:
            raise token_not_accepted()
        return self.principal


async def resolve_caller(
    security_scopes: SecurityScopes,
    request: Request,
    auth: AuthServiceDep,
    tokens: MachineTokenServiceDep,
    throttles: ThrottlesDep,
) -> Caller:
    """The authenticated caller, by cookie or by bearer token, or ``401``.

    This is the only door, and it now has two leaves.

    **An ``Authorization: Bearer`` header, when present, *is* the credential.**
    The cookie is not consulted, even if one was sent. Falling back would make the
    effective identity of a request depend on which of two credentials the server
    happened to prefer, and a request whose authorisation is ambiguous is a
    request nobody can reason about afterwards.

    **A token is refused unless the route asked for scopes.** ``security_scopes``
    is FastAPI's own accumulation of the ``Security(..., scopes=[...])``
    declarations on the path from the route down to here, and it is *empty* for
    every route that used a plain ``Depends``. So the default is closed by
    construction rather than by an allow-list somebody has to maintain: a route
    written tomorrow with ``HouseholdDep`` is unreachable by any token, and making
    it reachable is a visible edit at the route.
    ``tests/api/test_route_authentication.py`` proves both directions.

    **Refusals are shaped, not just worded.** An unknown token, a revoked one, an
    expired one, one whose issuer was disabled or removed from the household: all
    six are one ``WHERE`` clause in the resolver (migration ``0011``), so they are
    one answer here and take one amount of time. A token that resolves but was
    not issued for this route is the one case answered differently -- ``403``,
    naming the missing scope -- because the caller already holds the token and
    telling them nothing would only cost them an afternoon.
    """
    presented = bearer_credential(request)
    if presented is not None:
        return Caller(
            grant=await _resolve_token(presented, security_scopes, request, tokens, throttles)
        )

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise authentication_required()
    principal = await auth.resolve(token)
    if principal is None:
        raise authentication_required()
    enforce_csrf(request, principal)
    return Caller(principal=principal)


async def _resolve_token(
    presented: str,
    security_scopes: SecurityScopes,
    request: Request,
    tokens: MachineTokenService,
    throttles: Throttles,
) -> MachineTokenGrant:
    """Turn a presented bearer value into a grant, or refuse."""
    if not security_scopes.scopes:
        # Not "this token may not", but "no token may": the route declared no
        # scope, so it is closed to the whole mechanism. Same 401 as an unknown
        # value, so a holder learns nothing about which of the two they hit.
        raise token_not_accepted()
    required = [MachineTokenScope(scope) for scope in security_scopes.scopes]

    grant = await tokens.resolve(presented)
    if grant is None:
        await _spend_failed_token_attempt(request, throttles)
        raise token_not_accepted()
    if not grant.allows(required):
        raise insufficient_scope(
            sorted(scope.value for scope in required),
            sorted(scope.value for scope in grant.scopes),
        )
    return grant


async def _spend_failed_token_attempt(request: Request, throttles: Throttles) -> None:
    """Bound how fast a stranger may guess at token values.

    **Only failures are counted.** A running integration polls this API on a
    timer and every one of its calls presents the same valid token; charging
    those to a bucket would throttle the legitimate use and leave the guesser the
    same budget. A guess costs one indexed lookup on a digest -- cheap enough to
    pay before deciding, and the deciding is what this bounds.

    Keyed on the source address, with the limitation
    ``routers/auth.py`` already documents: behind a reverse proxy this is the
    proxy, and every caller collapses into one bucket. It is still what bounds a
    direct deployment, and 256 bits of entropy is what bounds the rest.
    """
    key = request.client.host if request.client is not None else "unknown"
    try:
        await throttles.machine_token_attempts.acquire(key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "Too many access tokens have been rejected from this address. "
                "Wait before trying again."
            ),
            retry_after=exc.retry_after,
        ) from None


CallerDep = Annotated[Caller, Depends(resolve_caller)]


async def require_session(caller: CallerDep) -> Principal:
    """The signed-in account behind this request, or ``401``.

    For the routes that are *about* the account or that hand out a credential --
    signing out, reading the session, minting a machine token. A machine token
    reaches this and is refused: see :meth:`Caller.require_browser_session`.
    """
    return caller.require_browser_session()


PrincipalDep = Annotated[Principal, Depends(require_session)]


async def get_household_id(
    caller: CallerDep,
    x_household_id: Annotated[str | None, Header(alias=HOUSEHOLD_HEADER)] = None,
) -> uuid.UUID:
    """Resolve the household this request may touch, from the credential.

    **A machine token names its household; it does not select one.** The tenant
    was decided when the token was issued, from the membership list of the person
    who issued it, and it is a column on the row. ``X-Household-Id`` is accepted
    only when it repeats that same value -- a client that sets it out of habit is
    not punished, and one that points a token at a second household is refused
    with the same ``403`` as any other household it may not open. There is no
    path by which a header widens what a token reaches.

    For a browser session, everything below is unchanged.

    ``X-Household-Id`` survives -- an account may belong to a family home *and* a
    flatshare (:class:`chaudron.domain.models.UserAccount`), so the active one has
    to be selectable -- but its role has changed completely. **It is a selector,
    never a proof.** The value is accepted only when the caller's own membership
    list contains it; the list is read from ``household_member`` through a
    ``SECURITY DEFINER`` function before any tenant is posted (migration
    ``0009``), so nothing the client sends takes part in deciding what the client
    may see.

    Which is the second half of the point. The ``SET LOCAL
    chaudron.household_id`` that arms every row-level security policy is fed from
    ``household_id_var``, set on the last line of this function -- after the
    membership check, from the validated identifier and never from the raw
    header. Before this line runs, ``household_id_var`` is ``None`` and the
    policies show nothing (``infra/db.py``, migration ``0004``).

    * No header, one membership -- that household. The common case, and the
      reason a single-household user never sends the header at all.
    * No header, several memberships -- ``409``, listing them, because guessing
      would be wrong half the time and picking "the first" is a guess.
    * No header, no membership -- ``403``.
    * A header naming a household the account is not a member of -- ``403``,
      *identical* to a header naming a household that does not exist. The check is
      a lookup in the caller's own list, so the two are not merely reported alike:
      they are the same branch (audit AUD-013 closed the equivalent oracle on the
      old code, and it stays closed here by construction).
    """
    if caller.grant is not None:
        household_id = caller.grant.household_id
        if x_household_id is not None and x_household_id != str(household_id):
            raise household_forbidden()
    else:
        principal = caller.require_browser_session()
        if x_household_id is None:
            household_id = _sole_membership(principal)
        else:
            # A malformed value is refused exactly like an unauthorised one:
            # telling a caller that their UUID was well-formed but not theirs is a
            # distinction with no legitimate use.
            if not _CANONICAL_UUID.match(x_household_id):
                raise household_forbidden()
            household_id = uuid.UUID(x_household_id)
            if principal.membership(household_id) is None:
                raise household_forbidden()

    household_id_var.set(str(household_id))
    return household_id


# --------------------------------------------------------------------------- #
# Scoped access: the household, plus what a machine token must hold to reach it
# --------------------------------------------------------------------------- #
#
# Each alias below is :data:`HouseholdDep` with one addition -- a declaration that
# a machine token holding the named scope may reach this route too. A browser
# session reaches it exactly as before; the declaration changes nothing for a
# cookie.
#
# **The absence of an alias is the security property.** A route annotated with
# plain ``HouseholdDep`` declares no scope, and :func:`resolve_caller` refuses
# every token on a route that declares none. So "which routes may a token reach?"
# is answered by grepping for these five names, and forgetting to add one fails
# closed rather than open.
#
# There is deliberately no alias for recipe suggestions and none for household
# members. Those two absences are argued on
# :class:`chaudron.domain.models.MachineTokenScope` and frozen by contract v1.1
# section 10; adding either is a contract change, not a convenience.


def _scoped(scope: MachineTokenScope) -> Any:
    """``Security(get_household_id, scopes=[scope])``, spelled once."""
    return Security(get_household_id, scopes=[scope.value])


#: Read the stock, the storage locations, the expiry dates.
InventoryReadDep = Annotated[uuid.UUID, _scoped(MachineTokenScope.INVENTORY_READ)]

#: Read the shopping list in progress.
ShoppingReadDep = Annotated[uuid.UUID, _scoped(MachineTokenScope.SHOPPING_READ)]

# The two *write* scopes are declared further down, on :func:`require_member`
# rather than here: a write is reachable by a token **and** requires the issuer to
# be more than a viewer, and those two conditions have to be one dependency for
# FastAPI to solve the chain once. See :func:`_member_scoped`.

#: Read the spend and the target. There is no ``budget:write``: changing what a
#: household means to spend is a decision, and decisions are made in a browser.
BudgetReadDep = Annotated[uuid.UUID, _scoped(MachineTokenScope.BUDGET_READ)]


def _sole_membership(principal: Principal) -> uuid.UUID:
    """The one household to use when the request named none."""
    if not principal.memberships:
        raise no_household()
    if len(principal.memberships) > 1:
        raise household_selection_required(
            [
                {"id": str(m.household_id), "name": m.household_name, "role": str(m.role)}
                for m in principal.memberships
            ]
        )
    return principal.memberships[0].household_id


HouseholdDep = Annotated[uuid.UUID, Depends(get_household_id)]


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
#
# ``household_member.role`` has had three values since revision ``0001`` and, until
# this change, exactly one line of ``src/`` read any of them: the ``is_owner``
# below, wired to one route out of sixty-seven (audit AUD-027). A ``viewer`` could
# add stock, delete an eater's allergen record, revoke the household's machine
# tokens and register a third party's export token *on the household's behalf* --
# every one of them a write, none of them guarded.
#
# The rule applied here is the one the role names already promise and that nobody
# has to arbitrate: **a viewer reads.** Every route that changes state now passes
# through :func:`require_member`, and the four that hand a credential to a third
# party -- or accept one -- through :func:`require_owner`. Anything beyond that
# (should a member be allowed to erase a person? to spend the household's model
# budget?) is a product decision and is deliberately *not* taken here.
#
# Two properties make this cheap to keep true rather than merely true today.
# ``MemberDep`` is spelled exactly like ``HouseholdDep`` at the call site, so a
# route that needs it is a one-word edit; and
# ``tests/api/test_route_authentication.py`` carries a census of which role guards
# which route, in both directions, so a write route added tomorrow without one
# fails CI.


def _caller_role(caller: Caller, household_id: uuid.UUID) -> MembershipRole:
    """What authority this call carries in *household_id*.

    A machine token answers with its **issuer's current role**, re-read from
    ``household_member`` by the resolver on every request (migration ``0011``,
    extended by ``0014``). Without that, a viewer would only have to mint a token
    to get past the very check their role exists to fail: the guard would hold on
    the cookie door and be a formality on the other one.
    """
    if caller.grant is not None:
        return caller.grant.role
    principal = caller.require_browser_session()
    membership = principal.membership(household_id)
    if membership is None:  # pragma: no cover - get_household_id refused it first
        raise household_forbidden()
    return membership.role


async def require_member(caller: CallerDep, household_id: HouseholdDep) -> uuid.UUID:
    """The resolved household, provided the caller may change something in it.

    Returns the identifier rather than the membership, so a write route is
    ``household_id: MemberDep`` where it was ``household_id: HouseholdDep`` and
    nothing else about the handler moves. A guard that made handlers rewrite
    themselves would be a guard somebody skipped on the busy afternoon.

    ``owner`` and ``member`` pass; ``viewer`` gets a ``403`` that names the role it
    holds, because that one is a dead end the caller can act on (``api/errors.py``,
    :func:`~chaudron.api.errors.insufficient_role`).
    """
    if _caller_role(caller, household_id) is MembershipRole.VIEWER:
        raise insufficient_role(required="member", held=MembershipRole.VIEWER.value)
    return household_id


MemberDep = Annotated[uuid.UUID, Depends(require_member)]


def _member_scoped(scope: MachineTokenScope) -> Any:
    """``Security(require_member, scopes=[scope])``, spelled once.

    The scope is declared **on the role guard**, not on
    :func:`get_household_id` below it, and that is not interchangeable. FastAPI
    caches a dependency per ``(callable, scopes)`` pair and accumulates scopes
    downwards, so a route that reached ``get_household_id`` once with a scope and
    once without -- which is what declaring them separately produces -- would
    solve :func:`resolve_caller` twice, the second time with an empty scope set,
    and every machine token would be refused by the copy that declared nothing.
    Found the hard way; ``tests/api/test_route_authentication.py`` pins the shape.
    """
    return Security(require_member, scopes=[scope.value])


#: Add stock, correct a quantity, remove a lot -- and be more than a viewer.
InventoryWriteDep = Annotated[uuid.UUID, _member_scoped(MachineTokenScope.INVENTORY_WRITE)]

#: Add a line, tick one off, remove one -- and be more than a viewer.
ShoppingWriteDep = Annotated[uuid.UUID, _member_scoped(MachineTokenScope.SHOPPING_WRITE)]


async def require_owner(principal: PrincipalDep, household_id: HouseholdDep) -> Principal:
    """The caller, provided they own the resolved household.

    For the handful of endpoints that hand out a credential rather than data --
    ``GET /v1/calendar/subscription`` is the first. A ``member`` can read the
    stock; giving them a bearer secret that keeps working after their membership
    is revoked is a different decision, and it belongs to the owner.

    The same argument, arriving from the other side, is why registering an export
    destination is owner-only (``routers/export_targets.py``): it *accepts* a
    third party's credential and dates an agreement in the household's name, and
    the household is what carries the consequence.
    """
    membership = principal.membership(household_id)
    if membership is None or not membership.is_owner:
        raise household_forbidden()
    return principal


OwnerDep = Annotated[Principal, Depends(require_owner)]


async def require_owner_household(_: OwnerDep, household_id: HouseholdDep) -> uuid.UUID:
    """The resolved household, provided the caller owns it.

    :data:`OwnerDep` for a route that wants the tenant rather than the person, so
    an owner-only handler reads ``household_id: OwnerHouseholdDep`` and carries no
    unused parameter whose only job is to be depended upon. Which of the two a
    route asks for is a statement about what it does with the answer.
    """
    return household_id


OwnerHouseholdDep = Annotated[uuid.UUID, Depends(require_owner_household)]


# Both guards below are ``async def``, and now unavoidably so: the rate caps are
# rows in PostgreSQL and are awaited. It was already deliberate before that -- a
# synchronous dependency is run by FastAPI in a worker *thread*, and the
# concurrency limiters' read-modify-write on a plain dictionary is only atomic
# because a single event loop never interleaves it.


async def enforce_product_lookup_limit(household_id: HouseholdDep, throttles: ThrottlesDep) -> None:
    """Keep one household from draining the instance's Open Food Facts budget.

    Counts *requests*, not upstream calls: a lookup served from the cache costs
    nothing to Open Food Facts, but counting only misses would let a household
    hammer the endpoint for free and still saturate the process. The limit is set
    below the instance-wide outbound budget so that a household exhausting its own
    allowance leaves some of the shared one for everybody else (ADR-0008).
    """
    try:
        await throttles.product_lookups.acquire(str(household_id))
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has made too many barcode lookups. The product catalogue "
                "budget is shared by the whole instance; retry shortly, or enter the "
                "product manually."
            ),
            retry_after=exc.retry_after,
        ) from None


async def enforce_recipe_limits(
    household_id: HouseholdDep, throttles: ThrottlesDep
) -> AsyncIterator[None]:
    """Bound both the rate and the concurrency of the endpoint that costs money.

    The rate cap is the wallet: every call is a billed inference. The concurrency
    slot is availability, and it is held for the whole request -- released by the
    ``finally`` of this generator once the response has been produced -- which is
    what makes "two tabs do not double the bill" true rather than aspirational.
    """
    key = str(household_id)
    try:
        await throttles.recipe_suggestions.acquire(key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has asked for too many recipe suggestions. Each one is a "
                "model call that costs tokens or local compute; retry later."
            ),
            retry_after=exc.retry_after,
        ) from None

    # An ExitStack rather than a `with` around the `yield`: FastAPI throws an
    # endpoint's exception back in at the yield point, and a bare `except
    # AtCapacityError` there would mistranslate an unrelated failure into a 429.
    with ExitStack() as stack:
        try:
            stack.enter_context(throttles.recipe_inferences.slot(key))
        except AtCapacityError as exc:
            raise rate_limited(
                detail=(
                    "Too many recipe suggestions are already being generated. Wait for the "
                    "one in flight to finish before asking for another."
                ),
                retry_after=exc.retry_after,
            ) from None
        yield


async def enforce_receipt_limits(
    household_id: HouseholdDep, throttles: ThrottlesDep
) -> AsyncIterator[None]:
    """Bound both the rate and the concurrency of the receipt import.

    The same shape as :func:`enforce_recipe_limits`, and needed more. This
    endpoint spends two things at once: a billed inference on the photograph
    path, and several megabytes of memory on every path -- the upload, plus the
    base64 copy the provider payload carries. A loop on it costs a household its
    credit and costs the instance its RAM, and the two failures do not arrive at
    the same time.

    The PDF path is throttled identically even though it calls no model. It still
    holds an upload and still runs a parser, and a second limiter keyed on "did
    this one call a provider?" would be a rule that has to be right about the
    file before it has read it.
    """
    key = str(household_id)
    try:
        await throttles.receipt_imports.acquire(key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has imported too many receipts in the last hour. A "
                "photographed receipt is a model call that costs tokens or local "
                "compute; retry later."
            ),
            retry_after=exc.retry_after,
        ) from None

    # An ExitStack rather than a `with` around the `yield`, for the reason given
    # on ``enforce_recipe_limits``: FastAPI throws the endpoint's exception back in
    # at the yield point, and a bare `except AtCapacityError` there would
    # mistranslate an unrelated failure into a 429.
    with ExitStack() as stack:
        try:
            stack.enter_context(throttles.receipt_inferences.slot(key))
        except AtCapacityError as exc:
            raise rate_limited(
                detail=(
                    "A receipt is already being read for this household. Wait for it to "
                    "finish before sending another."
                ),
                retry_after=exc.retry_after,
            ) from None
        yield


def get_catalog(request: Request) -> ProductCatalog:
    catalog: ProductCatalog = request.app.state.catalog
    return catalog


def get_location_service(session: SessionDep) -> LocationService:
    return LocationService(SqlLocationRepository(session))


def get_inventory_service(session: SessionDep) -> InventoryService:
    return InventoryService(
        SqlInventoryRepository(session),
        SqlProductRepository(session),
        SqlLocationRepository(session),
        SqlUnitRegistry(session),
    )


def get_product_service(
    session: SessionDep,
    catalog: Annotated[ProductCatalog, Depends(get_catalog)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ProductService:
    return ProductService(
        SqlProductRepository(session),
        SqlUnitRegistry(session),
        catalog,
        cache_ttl_seconds=settings.off_cache_ttl_seconds,
    )


def get_credential_cipher(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CredentialCipher:
    """The one object holding the master key, built from the validated settings.

    Cheap to construct (a key schedule), so there is no cache to invalidate and no
    key material parked on ``app.state`` for the lifetime of the process.
    """
    return CredentialCipher.from_settings(settings)


CipherDep = Annotated[CredentialCipher, Depends(get_credential_cipher)]


def get_provider_ports_builder(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cipher: CipherDep,
) -> ProviderPortsBuilder:
    """The seam the tests replace to exercise the real adapters over a fake socket.

    A builder rather than a factory: which instance-owned key applies depends on the
    provider the household chose, and that is only known once its configuration is
    read.
    """
    return provider_ports_builder(settings, cipher)


def get_provider_service(
    session: SessionDep,
    build_ports: Annotated[ProviderPortsBuilder, Depends(get_provider_ports_builder)],
    cipher: CipherDep,
) -> ProviderService:
    return ProviderService(session, build_ports, cipher)


def get_provider_credential_service(
    session: SessionDep, cipher: CipherDep
) -> ProviderCredentialService:
    return ProviderCredentialService(session, cipher)


def get_line_splitter(request: Request) -> ShoppingLineSplitter | None:
    """The optional model pass over unparsed shopping-list lines (contract 7.1).

    Read off ``app.state`` and **absent by default**, which is the whole point:
    ADR-0007 makes "no provider configured" a normal state, so the import has to
    work with nothing here. No adapter is registered today -- one belongs in
    ``infra/llm/``, which this change does not own -- and the seam exists so
    registering it later is one assignment rather than a rewrite of the service.
    """
    splitter: ShoppingLineSplitter | None = getattr(request.app.state, "line_splitter", None)
    return splitter


def get_allergen_screen(request: Request) -> AllergenScreen | None:
    """The optional dietary screen (contract 7.4). Signals, never filters.

    Absent until the dietary model exists. Its absence means no line is flagged;
    it can never mean a line is removed, because the port has no way to say so.
    """
    screen: AllergenScreen | None = getattr(request.app.state, "allergen_screen", None)
    return screen


def get_shopping_import_service(
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    splitter: Annotated[ShoppingLineSplitter | None, Depends(get_line_splitter)],
    allergens: Annotated[AllergenScreen | None, Depends(get_allergen_screen)],
) -> ShoppingImportService:
    return ShoppingImportService(
        session,
        bounds=import_bounds(settings),
        splitter=splitter,
        allergens=allergens,
    )


def import_bounds(settings: Settings) -> ImportBounds:
    """The configured document ceilings, in one place both the router and the
    service read them from."""
    return ImportBounds(
        max_bytes=settings.shopping_import_max_bytes,
        max_pdf_pages=settings.shopping_import_max_pdf_pages,
        max_text_chars=settings.shopping_import_max_text_chars,
        max_lines=settings.shopping_import_max_lines,
    )


def receipt_bounds(settings: Settings) -> ReceiptBounds:
    """The receipt ceilings, in one place both the router and the service read.

    Separate values from :func:`import_bounds` rather than the same ones: a
    shopping list is a few kilobytes of text and a till receipt is a photograph,
    and one ceiling for both would either refuse real photos or let a six-megabyte
    JPEG through the path sized for JSON.
    """
    return ReceiptBounds(
        max_bytes=settings.receipt_import_max_bytes,
        max_pdf_pages=settings.receipt_import_max_pdf_pages,
        max_text_chars=settings.receipt_import_max_text_chars,
        max_lines=settings.receipt_import_max_lines,
    )


def get_receipt_import_service(
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    inventory: Annotated[InventoryService, Depends(get_inventory_service)],
    providers: Annotated[ProviderService, Depends(get_provider_service)],
) -> ReceiptImportService:
    """The receipt import, wired to the two things it is not allowed to reimplement.

    :class:`InventoryService` owns canonical-unit conversion, the lot merge key and
    the stock movement ledger; :class:`ProviderService` owns which model a household
    may call and whether it can see. A second copy of either would be a second set
    of rules to keep in step, and the one that drifted would be the one nobody was
    testing.
    """
    return ReceiptImportService(
        session,
        bounds=receipt_bounds(settings),
        inventory=inventory,
        products=SqlProductRepository(session),
        providers=providers,
    )


def get_declined_store(session: SessionDep) -> DeclinedRepurchaseStore:
    """The durable memory of declined repurchase proposals (contract 6bis).

    A table since migration ``0007``, and unconditional: a refusal has no expiry
    by design, so an instance that could not keep one would have to refuse the
    call rather than accept it and forget. There is nothing left to be absent.
    """
    return SqlDeclinedRepurchaseStore(session)


def get_shopping_list_service(session: SessionDep) -> ShoppingListService:
    return ShoppingListService(session)


def get_declined_repurchase_service(
    store: Annotated[DeclinedRepurchaseStore, Depends(get_declined_store)],
) -> DeclinedRepurchaseService:
    return DeclinedRepurchaseService(store)


def get_depletion_service(
    session: SessionDep,
    store: Annotated[DeclinedRepurchaseStore, Depends(get_declined_store)],
) -> DepletionService:
    """Repurchase proposals for depleted stock (contract 6bis).

    Wired with the store, so ``previously_declined`` reports the household's real
    answer: a product waved away once is never offered again, which is the whole
    reason the refusal is durable.
    """
    return DepletionService(session, declined=store)


def get_member_service(session: SessionDep) -> MemberService:
    return MemberService(session)


def get_privacy_service(session: SessionDep) -> PrivacyService:
    return PrivacyService(session)


def get_dietary_service(session: SessionDep) -> DietaryService:
    return DietaryService(session)


def get_balance_service(session: SessionDep) -> BalanceService:
    return BalanceService(session)


def get_recipe_feedback_service(session: SessionDep) -> RecipeFeedbackService:
    return RecipeFeedbackService(session)


def get_recipe_service(
    session: SessionDep,
    providers: Annotated[ProviderService, Depends(get_provider_service)],
    dietary: Annotated[DietaryService, Depends(get_dietary_service)],
    balance: Annotated[BalanceService, Depends(get_balance_service)],
    feedback: Annotated[RecipeFeedbackService, Depends(get_recipe_feedback_service)],
) -> RecipeService:
    return RecipeService(
        session,
        providers,
        SqlLocationRepository(session),
        dietary,
        balance,
        # Read-only from here: the generation path asks which titles the household
        # dismissed and reorders with the answer. It never writes a verdict, and
        # nothing it produces is passed to the model (contract 4bis).
        feedback,
    )


LocationServiceDep = Annotated[LocationService, Depends(get_location_service)]
InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]
ProductServiceDep = Annotated[ProductService, Depends(get_product_service)]
ProviderServiceDep = Annotated[ProviderService, Depends(get_provider_service)]
ProviderCredentialServiceDep = Annotated[
    ProviderCredentialService, Depends(get_provider_credential_service)
]
RecipeServiceDep = Annotated[RecipeService, Depends(get_recipe_service)]
RecipeFeedbackServiceDep = Annotated[RecipeFeedbackService, Depends(get_recipe_feedback_service)]
MemberServiceDep = Annotated[MemberService, Depends(get_member_service)]
PrivacyServiceDep = Annotated[PrivacyService, Depends(get_privacy_service)]
DietaryServiceDep = Annotated[DietaryService, Depends(get_dietary_service)]
BalanceServiceDep = Annotated[BalanceService, Depends(get_balance_service)]
ShoppingImportServiceDep = Annotated[ShoppingImportService, Depends(get_shopping_import_service)]
ReceiptImportServiceDep = Annotated[ReceiptImportService, Depends(get_receipt_import_service)]
DepletionServiceDep = Annotated[DepletionService, Depends(get_depletion_service)]
ShoppingListServiceDep = Annotated[ShoppingListService, Depends(get_shopping_list_service)]
DeclinedRepurchaseServiceDep = Annotated[
    DeclinedRepurchaseService, Depends(get_declined_repurchase_service)
]


# --------------------------------------------------------------------------- #
# Shopping-list import limits
#
# Appended here, at the end, so the rest of this module is untouched. It belongs
# beside ``enforce_receipt_limits`` and reads the same way; it is not there
# because that would have meant reformatting a file another change is editing.
# --------------------------------------------------------------------------- #


async def enforce_shopping_import_limits(
    household_id: HouseholdDep, throttles: ThrottlesDep
) -> AsyncIterator[None]:
    """Bound the rate and the concurrency of the document import.

    The same shape as :func:`enforce_receipt_limits`, and it was missing. That
    endpoint calls no model, which is exactly why it had no limiter -- and exactly
    why it was the cheapest way to take the instance down: reading a PDF is CPU
    the caller does not own, fifteen concurrent one-megabyte uploads were answered
    fifteen times, and sign-up is open.

    The per-document limits in ``infra/documents/sandbox.py`` bound what one
    document may spend. **This** is what bounds how many. Neither is sufficient
    alone, and of the two this one is the load-bearing half: a bound on one
    document says nothing about a loop.
    """
    key = str(household_id)
    try:
        await throttles.shopping_imports.acquire(key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has imported too many shopping lists in the last hour. "
                "Reading a document costs this instance processing time; retry later."
            ),
            retry_after=exc.retry_after,
        ) from None

    # An ExitStack rather than a `with` around the `yield`, for the reason given on
    # ``enforce_recipe_limits``: FastAPI throws the endpoint's exception back in at
    # the yield point, and a bare `except AtCapacityError` there would mistranslate
    # an unrelated failure into a 429.
    with ExitStack() as stack:
        try:
            stack.enter_context(throttles.shopping_import_documents.slot(key))
        except AtCapacityError as exc:
            raise rate_limited(
                detail=(
                    "A shopping list is already being read for this household. Wait for "
                    "it to finish before sending another."
                ),
                retry_after=exc.retry_after,
            ) from None
        yield
