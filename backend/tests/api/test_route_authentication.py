"""Every route is authenticated, or is on a list somebody had to write.

This is the most important test in the authentication work, and the reason is
arithmetic rather than rhetorical: there are around forty routes across a dozen
routers written by different authors at different times, and **one of them left
open cancels the whole thing**. A suite of per-endpoint tests only covers the
endpoints somebody remembered to write a test for, which is never the one added
next month in a hurry.

So this file does not test endpoints. It walks ``app.routes``, resolves the
dependency graph FastAPI itself built for each one, and asserts that one of the
authenticators appears in it. A route added tomorrow without authentication
fails CI on this file, before anybody has to notice it in review.

The escape hatch is an explicit allow-list of paths with a reason attached, in
the same spirit as ``tests/tenancy/test_schema_tenant_guard.py``: the point is
not to make exemptions impossible but to make them *deliberate and visible*.

**A route can now be reached two ways** (contract v1.1 section 10): a browser
cookie, or an ``Authorization: Bearer`` machine token. That does not weaken the
census above -- both credentials arrive through the same authenticators, so a
route with none of them still fails -- but it raises a second question the census
above cannot answer: *which* routes a long-lived bearer credential may reach. So
this file carries a second census, at the bottom, comparing every route's
declared scopes against :data:`TOKEN_REACHABLE` in both directions. Without it,
adding one ``Security(...)`` annotation to the recipe suggestion endpoint would
pass the entire suite.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from chaudron.api.deps import (
    get_household_id,
    require_member,
    require_owner,
    require_owner_household,
    require_session,
    resolve_caller,
)
from chaudron.api.main import create_app
from chaudron.api.routers.calendar import authenticate as calendar_authenticate
from chaudron.config import Settings
from chaudron.domain.models import MachineTokenScope

#: Any one of these in a route's dependency tree means the route establishes who
#: is calling before it does anything.
#:
#: ``calendar_authenticate`` is the CalDAV feed's own scheme -- HTTP Basic with a
#: credential derived per household -- and is a real authenticator, not an
#: exemption: a phone subscribing to a calendar cannot carry a session cookie,
#: and RFC 4791 clients speak Basic. It resolves the household from the
#: credential and from nothing the client asserts (``routers/calendar.py``).
AUTHENTICATORS: Final = frozenset(
    {
        require_session,
        get_household_id,
        require_owner,
        require_owner_household,
        require_member,
        calendar_authenticate,
        resolve_caller,
    }
)

#: Every route a **machine token** may reach, and the scopes it must hold to do
#: so. Contract v1.1 section 10.
#:
#: This is the second census, and it is the mirror image of the first. The one
#: above asks "is anything unauthenticated?"; this one asks "is anything reachable
#: by a long-lived bearer credential that should not be?" -- a question that did
#: not exist before tokens did, and one that a per-endpoint test answers only for
#: the endpoints somebody remembered.
#:
#: The comparison runs **both ways**. A route that starts declaring a scope
#: without an entry here fails; an entry naming a route that no longer declares it
#: fails too. So widening what a token can reach is impossible to do quietly, in
#: either direction.
TOKEN_REACHABLE: Final[dict[str, frozenset[str]]] = {
    "GET /v1/inventory": frozenset({"inventory:read"}),
    "GET /v1/locations": frozenset({"inventory:read"}),
    "POST /v1/inventory": frozenset({"inventory:write"}),
    "PATCH /v1/inventory/{item_id}": frozenset({"inventory:write"}),
    "DELETE /v1/inventory/{item_id}": frozenset({"inventory:write"}),
    "GET /v1/shopping-lists/current": frozenset({"shopping:read"}),
    "POST /v1/shopping-lists/current/items": frozenset({"shopping:write"}),
    "PATCH /v1/shopping-lists/current/items/{item_id}": frozenset({"shopping:write"}),
    "DELETE /v1/shopping-lists/current/items/{item_id}": frozenset({"shopping:write"}),
    "GET /v1/budget": frozenset({"budget:read"}),
    "GET /v1/budget/history": frozenset({"budget:read"}),
    "GET /v1/budget/target": frozenset({"budget:read"}),
}

#: Paths a token must **never** reach, with the reason, checked as prefixes.
#:
#: Redundant with :data:`TOKEN_REACHABLE` -- anything absent from that mapping is
#: already unreachable -- and kept anyway, because these six areas are the ones whose
#: absence is an argued decision rather than an omission. A future edit that adds
#: a scope to one of them fails here, next to the sentence saying why it must not.
TOKEN_FORBIDDEN: Final[dict[str, str]] = {
    "/v1/recipes": (
        "the only endpoint that spends money; a long-lived credential in a "
        "home-automation appliance is the worst place to hold that"
    ),
    "/v1/members": "allergens and infant age bands are health data, GDPR article 9",
    "/v1/tokens": ("a token that could mint or revoke tokens would outrun its own revocation"),
    "/v1/providers": "provider credentials and instance-owner configuration",
    "/v1/shopping-lists/export": "export targets hold third-party credentials",
    "/v1/calendar": "hands out a bearer credential of its own, owner only",
    "/v1/households": (
        "the GDPR levers: one hands back the whole household in a file, the other "
        "deletes it. Neither is an operation a credential left in an appliance "
        "performs"
    ),
}

#: Paths that answer without any authentication, each with the reason it must.
#:
#: Adding an entry here is the deliberate gesture this test exists to force. The
#: default answer is "it authenticates"; anything else is argued in review, in
#: this dictionary, in one line.
PUBLIC_PATHS: Final[dict[str, str]] = {
    "/healthz": "liveness probe: reached by the orchestrator before anything is configured",
    "/readyz": "readiness probe: reports the database, never a household's data",
    "/v1/auth/register": "creates the account; there is by definition no session yet",
    "/v1/auth/login": "exchanges a password for a session; the whole point is to have none",
    # The two CalDAV discovery endpoints a client hits *before* it has been given
    # credentials. Both are answered by handlers that take no household and touch
    # no tenant table: one is a 301 to the context path (RFC 6764 6), the other
    # an OPTIONS advertising that this host speaks CalDAV. Neither discloses
    # whether any particular feed exists, and both are 404 when the feature is
    # switched off.
    "/.well-known/caldav": "RFC 6764 discovery: a redirect, sent before a client has credentials",
    "/caldav/": "OPTIONS: a client sends it before it has any credentials to send",
    # These three are `OPTIONS` only -- every other method on the same paths runs
    # through `authenticate`. This census is what found them; they were not on
    # anybody's list. They are answered by `options_root`, which ignores
    # `feed_id` entirely and replies 204 with a fixed `Allow` header, so the
    # answer is byte-identical for a feed that exists and one that does not, and
    # it is 404 for every feed when the feature is switched off. A CalDAV client
    # sends `OPTIONS` on the collection during account setup, sometimes before it
    # has been given anything to authenticate with.
    "/caldav/p/{feed_id}/": "OPTIONS: fixed 204, names no feed and confirms none",
    "/caldav/p/{feed_id}/cal/": "OPTIONS: fixed 204, names no feed and confirms none",
    "/caldav/p/{feed_id}/cal/expiry/": "OPTIONS: fixed 204, names no feed and confirms none",
    # OpenAPI's own routes, registered by FastAPI and only when
    # `CHAUDRON_ENABLE_DOCS` allows it -- which defaults closed everywhere but
    # `local` (`config.py`, audit AUD-018).
    "/openapi.json": "OpenAPI schema; served only where docs are enabled",
    "/docs": "Swagger UI; served only where docs are enabled",
    "/docs/oauth2-redirect": "Swagger UI helper; served only where docs are enabled",
}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One reachable path and method, and the FastAPI route behind it if any."""

    path: str
    methods: frozenset[str]
    route: APIRoute | None

    @property
    def label(self) -> str:
        return f"{','.join(sorted(self.methods)) or '-'} {self.path}"


def _walk(routes: Iterable[BaseRoute], prefix: str = "") -> Iterator[Endpoint]:
    """Every leaf route of the application, descending through included routers.

    Not simply ``app.routes``. Since FastAPI 0.141 / Starlette 1.3,
    ``include_router`` wraps each router in an ``_IncludedRouter`` node rather
    than copying its routes onto the application, so a flat read of ``app.routes``
    sees fifteen opaque containers and **zero endpoints** -- which is exactly the
    shape of census that passes while checking nothing. ``test_the_census_found_
    the_application`` exists because that is what happened when this file was
    first written.

    Plain Starlette ``Route`` objects (FastAPI's own ``/docs`` and
    ``/openapi.json``) are yielded with ``route=None``: they carry no dependency
    graph at all, so the only way for one of them to pass is to be listed in
    :data:`PUBLIC_PATHS`.
    """
    for route in routes:
        included: Any = getattr(route, "original_router", None)
        if included is not None:
            context = route.include_context  # type: ignore[attr-defined]
            yield from _walk(included.routes, prefix + context.prefix)
            continue
        path = prefix + str(getattr(route, "path", ""))
        methods = frozenset(getattr(route, "methods", None) or ())
        yield Endpoint(
            path=path, methods=methods, route=route if isinstance(route, APIRoute) else None
        )


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    """Every dependency of *dependant*, at any depth, including itself."""
    yield dependant
    for child in dependant.dependencies:
        yield from _dependencies(child)


def _is_authenticated(endpoint: Endpoint) -> bool:
    if endpoint.route is None:
        return False
    return any(
        dependency.call in AUTHENTICATORS for dependency in _dependencies(endpoint.route.dependant)
    )


def _own_scopes(dependant: Dependant) -> frozenset[str]:
    """The scopes declared *at* this node, from FastAPI's own bookkeeping.

    ``Security(dep, scopes=[...])`` records them on the sub-dependant it builds.
    Read here rather than through ``fastapi.dependencies.models._get_oauth_scopes``
    so this file does not depend on a private function -- and the floor asserted
    by :func:`test_the_scope_census_found_the_scoped_routes` is what catches the
    day a FastAPI upgrade renames the attribute, because a reader that silently
    returns nothing would otherwise certify that no route is token-reachable.
    """
    declared: frozenset[str] = frozenset()
    for attribute in ("own_oauth_scopes", "parent_oauth_scopes"):
        value = getattr(dependant, attribute, None)
        if value:
            declared |= frozenset(value)
    return declared


def _declared_scopes(endpoint: Endpoint) -> frozenset[str]:
    """Every scope a machine token must hold to reach *endpoint*."""
    if endpoint.route is None:
        return frozenset()
    collected: frozenset[str] = frozenset()
    for dependency in _dependencies(endpoint.route.dependant):
        collected |= _own_scopes(dependency)
    return collected


def _scope_carriers(endpoint: Endpoint) -> set[Any]:
    """Which dependencies of *endpoint* carry a scope declaration."""
    if endpoint.route is None:
        return set()
    return {
        dependency.call
        for dependency in _dependencies(endpoint.route.dependant)
        if getattr(dependency, "own_oauth_scopes", None)
    }


def _build_app(*, docs: bool) -> FastAPI:
    """A real application, built from literals so no environment leaks in."""
    import base64

    from pydantic import SecretStr

    return create_app(
        Settings(
            env="ci",
            log_level="WARNING",
            database_url=SecretStr("postgresql+asyncpg://u:p@localhost/does-not-connect"),
            secret_key=SecretStr("k" * 48),
            credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
            enable_docs=docs,
        )
    )


#: Built once at import: the census needs no database, and this keeps the
#: parametrisation ids readable in the run output.
_APP: Final = _build_app(docs=True)
_ENDPOINTS: Final[tuple[Endpoint, ...]] = tuple(_walk(_APP.routes))


def test_the_census_found_the_application() -> None:
    """A guard on the guard: an empty census would pass every assertion below.

    It has already earned its place once. The first version of this file read
    ``app.routes`` flat, found fifteen router containers and no endpoints, and
    every per-route assertion below passed vacuously.
    """
    assert len(_ENDPOINTS) > 30, (
        f"only {len(_ENDPOINTS)} endpoints were found. If the application shrank this "
        f"much, this file is checking nothing -- fix the collection before trusting it."
    )
    # And it found the real ones, not fifteen copies of `/healthz`.
    assert {"/v1/inventory", "/v1/recipes/suggest", "/v1/auth/login"} <= {
        endpoint.path for endpoint in _ENDPOINTS
    }


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_every_route_is_authenticated_or_explicitly_public(endpoint: Endpoint) -> None:
    if endpoint.path in PUBLIC_PATHS:
        pytest.skip(f"public by decision: {PUBLIC_PATHS[endpoint.path]}")
    assert _is_authenticated(endpoint), (
        f"{endpoint.label} reaches none of the authenticators. Depend on "
        f"HouseholdDep (the usual answer), on PrincipalDep when the route needs the "
        f"account but no household, or add the path to PUBLIC_PATHS with the reason "
        f"it must answer a stranger."
    )


def test_the_public_list_has_no_stale_entries() -> None:
    """An allow-list naming a route that no longer exists is an allow-list nobody reads."""
    declared = {endpoint.path for endpoint in _ENDPOINTS}
    stale = sorted(path for path in PUBLIC_PATHS if path not in declared)
    assert not stale, f"PUBLIC_PATHS names routes that do not exist any more: {stale}"


def test_documentation_routes_are_absent_when_docs_are_disabled() -> None:
    """The allow-list carries three doc routes; they must not exist in production.

    Listing them as public is only acceptable because they are not *registered*
    unless an operator enabled them. This proves the second half.
    """
    closed = {endpoint.path for endpoint in _walk(_build_app(docs=False).routes)}
    assert "/openapi.json" not in closed
    assert "/docs" not in closed


def test_every_v1_route_resolves_a_household_or_is_an_auth_route() -> None:
    """Under ``/v1``, "authenticated" is not enough -- the tenant has to be pinned.

    A route that authenticates but never resolves a household would run with
    ``household_id_var`` unset, and every row-level security policy would show it
    nothing (migration ``0004``). That fails safe, but silently and confusingly.
    The ``/v1/auth`` routes are the deliberate exception: they are *about* the
    account, not about a household, and one of them is what a user with no
    household at all calls to find that out.
    """
    offenders = [
        endpoint.label
        for endpoint in _ENDPOINTS
        if endpoint.path.startswith("/v1/")
        and not endpoint.path.startswith("/v1/auth/")
        and (
            endpoint.route is None
            or get_household_id not in {d.call for d in _dependencies(endpoint.route.dependant)}
        )
    ]
    assert not offenders, (
        f"these /v1 routes never resolve a household, so they run outside row-level "
        f"security: {offenders}"
    )


# --------------------------------------------------------------------------- #
# The second census: what a machine token may reach
# --------------------------------------------------------------------------- #
#
# A route can now be reached two ways, and the first census does not distinguish
# them: a bearer token satisfies `get_household_id` exactly as a cookie does, so
# every assertion above stays green whether a route is token-reachable or not.
# That is the whole point of the design -- and it is also why this second census
# has to exist. Without it, adding `Security(get_household_id, scopes=[...])` to
# the recipe suggestion endpoint would pass the entire suite.


def test_the_scope_census_found_the_scoped_routes() -> None:
    """A guard on the guard, for the same reason as the one above it.

    Every assertion below compares a set of declared scopes against a table. If
    :func:`_own_scopes` reads nothing -- a FastAPI upgrade renaming
    ``own_oauth_scopes``, a refactor dropping the ``Security`` wrapper -- every one
    of them would conclude "no route is token-reachable" and pass. This is the one
    assertion that fails in that case, and it fails first.
    """
    scoped = {endpoint.label for endpoint in _ENDPOINTS if _declared_scopes(endpoint)}
    assert len(scoped) >= 10, (
        f"only {len(scoped)} routes declare a token scope: {sorted(scoped)}. Either the "
        f"scope reader stopped working -- in which case nothing below this line is "
        f"trustworthy -- or the token surface really shrank, which is a deliberate edit "
        f"to TOKEN_REACHABLE."
    )
    assert "GET /v1/inventory" in scoped


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_token_reachability_matches_the_declared_table(endpoint: Endpoint) -> None:
    """Both directions: nothing gains a scope quietly, nothing loses one quietly."""
    declared = _declared_scopes(endpoint)
    expected = TOKEN_REACHABLE.get(endpoint.label, frozenset())
    assert declared == expected, (
        f"{endpoint.label} declares {sorted(declared)} and TOKEN_REACHABLE says "
        f"{sorted(expected)}. A route reachable by a machine token must name its scopes "
        f"in that table, with the review that implies; a route that is not must declare "
        f"none, which is what makes it unreachable (api/deps.py, resolve_caller)."
    )


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_routes_closed_to_tokens_declare_no_scope(endpoint: Endpoint) -> None:
    """The six areas whose absence from the token surface is an argued decision."""
    forbidden = [
        (prefix, reason)
        for prefix, reason in TOKEN_FORBIDDEN.items()
        if endpoint.path.startswith(prefix)
    ]
    if not forbidden:
        pytest.skip("not one of the deliberately closed areas")
    declared = _declared_scopes(endpoint)
    assert not declared, (
        f"{endpoint.label} declares {sorted(declared)}, and it must be unreachable by any "
        f"machine token: {forbidden[0][1]}. Contract v1.1 section 10 freezes this; "
        f"changing it is a contract change, not an annotation."
    )


def test_every_declared_scope_is_one_the_contract_names() -> None:
    """Five scopes, and a sixth cannot appear by typo.

    An unknown string would not merely be untidy: ``resolve_caller`` turns each
    declared scope into a :class:`MachineTokenScope`, so a misspelling would raise
    on the first request rather than at build time. This is the build time.
    """
    known = {scope.value for scope in MachineTokenScope}
    offenders = {
        endpoint.label: sorted(_declared_scopes(endpoint) - known)
        for endpoint in _ENDPOINTS
        if _declared_scopes(endpoint) - known
    }
    assert not offenders, f"routes declaring scopes outside the contract's five: {offenders}"
    assert {scope for scopes in TOKEN_REACHABLE.values() for scope in scopes} <= known


def test_the_token_table_has_no_stale_entries() -> None:
    """An entry naming a route that no longer exists is an entry nobody reads."""
    labels = {endpoint.label for endpoint in _ENDPOINTS}
    stale = sorted(label for label in TOKEN_REACHABLE if label not in labels)
    assert not stale, f"TOKEN_REACHABLE names routes that do not exist any more: {stale}"

    paths = {endpoint.path for endpoint in _ENDPOINTS}
    unmatched = sorted(
        prefix for prefix in TOKEN_FORBIDDEN if not any(path.startswith(prefix) for path in paths)
    )
    assert not unmatched, f"TOKEN_FORBIDDEN names areas that do not exist any more: {unmatched}"


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_scopes_hang_only_off_the_household_resolver(endpoint: Endpoint) -> None:
    """One place declares a scope, and it is the one that resolves the tenant.

    ``resolve_caller`` reads FastAPI's accumulated scopes wherever they were
    declared, so a scope attached to some *other* dependency would authorise the
    route just as well -- and would be invisible to a reader looking at how the
    household is resolved. Keeping the declaration on :func:`get_household_id`
    means "which tenant, and by what authority" is one annotation, in one place,
    per route.
    """
    carriers = _scope_carriers(endpoint)
    assert carriers <= {get_household_id, require_member}, (
        f"{endpoint.label} declares token scopes on {sorted(c.__name__ for c in carriers)}. "
        f"Declare them on get_household_id, or on require_member for a route that also "
        f"needs a role -- use one of the aliases in api/deps.py -- so the tenant and the "
        f"authority to reach it are read together."
    )
    assert len(carriers) <= 1, (
        f"{endpoint.label} declares token scopes on two dependencies at once: "
        f"{sorted(c.__name__ for c in carriers)}. FastAPI caches a dependency per "
        f"(callable, scopes), so the chain below would be solved twice -- once with the "
        f"scopes and once without -- and the copy that declared none refuses every "
        f"machine token. Declare them in exactly one place (api/deps.py, _member_scoped)."
    )


# --------------------------------------------------------------------------- #
# The third census: which role guards which route
# --------------------------------------------------------------------------- #
#
# ``household_member.role`` has had three values since the first migration, and
# until audit AUD-027 exactly one line of ``src/`` read any of them. A ``viewer``
# -- the role whose name is a promise that it cannot write -- could add stock,
# erase an eater's allergen record, revoke the household's machine tokens, and
# register a third party's export token on the household's behalf.
#
# So this census is the same shape as the two above and exists for the same
# arithmetic reason: sixty-seven routes, and the one that matters is the one
# somebody adds next month in a hurry. It asserts in **both** directions -- a
# route that gains or loses a guard fails here -- plus one rule that needs no
# table: **every state-changing route under /v1 is guarded**, or is named in
# :data:`UNGUARDED_WRITES` with the reason it is not.


def _role_guard(endpoint: Endpoint) -> str | None:
    """The strongest role this route requires: ``"owner"``, ``"member"``, or none.

    Read from the dependency graph rather than from a decorator or a docstring,
    for the reason the whole file exists: a guard that has to be *declared*
    somewhere other than where it is *enforced* is a guard that drifts.
    """
    if endpoint.route is None:
        return None
    calls = {dependency.call for dependency in _dependencies(endpoint.route.dependant)}
    if calls & {require_owner, require_owner_household}:
        return "owner"
    if require_member in calls:
        return "member"
    return None


#: Every route that requires a role, and which. Contract v1.1, and audit AUD-027.
#:
#: ``owner`` is deliberately rare and every entry is argued where it is applied:
#: the calendar subscription **hands out** a bearer credential, and the two export
#: target routes **accept** one and date a consent in the household's name. Those
#: are decisions about what leaves the instance, and they belong to whoever
#: carries the consequence.
#:
#: ``member`` is everything else that writes, and it is not an argued list so much
#: as the meaning of the word ``viewer``.
ROLE_GUARDED: Final[dict[str, str]] = {
    "GET /v1/calendar/subscription": "owner",
    "POST /v1/calendar/subscription/revoke": "owner",
    # Articles 15/20 and article 17. Erasure destroys data belonging to every
    # member, not only to whoever asked; the export assembles all of it -- the
    # other members' allergen records included -- into one file. Both are argued
    # in ``routers/privacy.py``, including what bounding the export to the owner
    # costs a member exercising article 15.
    "GET /v1/households/export": "owner",
    "DELETE /v1/households": "owner",
    "PUT /v1/shopping-lists/export/targets/{target}": "owner",
    "DELETE /v1/shopping-lists/export/targets/{target}": "owner",
    "POST /v1/inventory": "member",
    "PATCH /v1/inventory/{item_id}": "member",
    "DELETE /v1/inventory/{item_id}": "member",
    "POST /v1/locations": "member",
    "POST /v1/products": "member",
    "POST /v1/members": "member",
    "PATCH /v1/members/{member_id}": "member",
    "DELETE /v1/members/{member_id}": "member",
    "PUT /v1/budget/target": "member",
    "DELETE /v1/budget/target": "member",
    "POST /v1/receipts/import": "member",
    "POST /v1/receipts/{receipt_id}/confirm": "member",
    "DELETE /v1/receipts/{receipt_id}": "member",
    "POST /v1/recipes/suggest": "member",
    "PUT /v1/recipes/suggestions/{suggestion_id}/feedback": "member",
    "DELETE /v1/recipes/suggestions/{suggestion_id}/feedback": "member",
    "POST /v1/shopping-lists/current/items": "member",
    "PATCH /v1/shopping-lists/current/items/{item_id}": "member",
    "DELETE /v1/shopping-lists/current/items/{item_id}": "member",
    "POST /v1/shopping-lists/declined": "member",
    "DELETE /v1/shopping-lists/declined/{product_id}": "member",
    "POST /v1/shopping-lists/import": "member",
    "POST /v1/shopping-lists/import/{import_id}/confirm": "member",
    "POST /v1/shopping-lists/{shopping_list_id}/export/{target}": "member",
    "POST /v1/tokens": "member",
    "DELETE /v1/tokens/{token_id}": "member",
}

#: State-changing routes under ``/v1`` that deliberately require no role, each
#: with the reason. Adding an entry here is the visible gesture this census exists
#: to force, exactly like :data:`PUBLIC_PATHS` above it.
#:
#: Empty, and that is the state to keep it in. It held one entry --
#: ``POST /v1/recipes/suggest``, excused as "a POST that writes nothing" -- and the
#: excuse was simply false: ``services/recipes.py`` adds a ``recipe_suggestion``
#: row on every call. An exemption argued from a claim about the code is only ever
#: as good as the claim, so anything added here has to be checked against the
#: service, not against the verb.
UNGUARDED_WRITES: Final[dict[str, str]] = {}

#: The methods that change something. ``POST`` is here despite being the verb of
#: every non-idempotent read as well, which is what makes
#: :data:`UNGUARDED_WRITES` necessary rather than merely tidy.
UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def test_the_role_census_found_the_guarded_routes() -> None:
    """A guard on the guard, third of three.

    If :func:`_role_guard` stopped reading the graph -- a rename in ``api/deps.py``,
    a refactor that wraps the dependency -- every assertion below would conclude
    "no route is guarded" and pass. This is the one that fails first.
    """
    guarded = {e.label: _role_guard(e) for e in _ENDPOINTS if _role_guard(e) is not None}
    assert len(guarded) >= 25, (
        f"only {len(guarded)} routes carry a role guard: {sorted(guarded)}. Either the "
        f"reader stopped working -- in which case nothing below is trustworthy -- or the "
        f"role model was dismantled, which is a deliberate edit to ROLE_GUARDED."
    )
    assert guarded.get("PUT /v1/shopping-lists/export/targets/{target}") == "owner"


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_role_guards_match_the_declared_table(endpoint: Endpoint) -> None:
    """Both directions: nothing gains a role quietly, nothing loses one quietly."""
    found = _role_guard(endpoint)
    expected = ROLE_GUARDED.get(endpoint.label)
    assert found == expected, (
        f"{endpoint.label} is guarded by {found!r} and ROLE_GUARDED says {expected!r}. "
        f"A route that requires a role must name it in that table; one that does not "
        f"must reach neither require_member nor require_owner."
    )


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[e.label for e in _ENDPOINTS])
def test_every_state_changing_v1_route_requires_a_role(endpoint: Endpoint) -> None:
    """A viewer reads. Anything that writes needs at least a member, or an excuse.

    The ``/v1/auth`` routes are exempt as a family: they are about the *account*,
    not about a household, and three of them are what a user with no membership at
    all calls. Roles are a property of a membership and there is none to read.
    """
    if not endpoint.path.startswith("/v1/") or endpoint.path.startswith("/v1/auth/"):
        pytest.skip("not a household route")
    if not endpoint.methods & UNSAFE_METHODS:
        pytest.skip("changes nothing")
    if endpoint.label in UNGUARDED_WRITES:
        pytest.skip(f"unguarded by decision: {UNGUARDED_WRITES[endpoint.label]}")
    assert _role_guard(endpoint) is not None, (
        f"{endpoint.label} changes state and any member of the household may call it, "
        f"including a viewer. Depend on MemberDep instead of HouseholdDep (the usual "
        f"answer), on OwnerHouseholdDep when the route hands out or accepts a "
        f"credential, or add the label to UNGUARDED_WRITES with the reason."
    )


def test_the_role_tables_have_no_stale_entries() -> None:
    """An allow-list naming a route that no longer exists is an allow-list nobody reads."""
    labels = {endpoint.label for endpoint in _ENDPOINTS}
    stale = sorted(label for label in ROLE_GUARDED if label not in labels)
    assert not stale, f"ROLE_GUARDED names routes that do not exist any more: {stale}"
    stale = sorted(label for label in UNGUARDED_WRITES if label not in labels)
    assert not stale, f"UNGUARDED_WRITES names routes that do not exist any more: {stale}"


def test_owner_only_routes_are_closed_to_machine_tokens() -> None:
    """An owner guard needs a browser session, and that is not an accident.

    :func:`require_owner` asks for a :class:`Principal`, which only a cookie can
    produce, so an owner-only route is unreachable by any bearer token however it
    was scoped. Asserted rather than assumed, because the day somebody writes a
    token-aware owner check, the two properties stop being one.
    """
    offenders = {
        endpoint.label: sorted(_declared_scopes(endpoint))
        for endpoint in _ENDPOINTS
        if _role_guard(endpoint) == "owner" and _declared_scopes(endpoint)
    }
    assert not offenders, (
        f"owner-only routes declaring a machine-token scope: {offenders}. An owner guard "
        f"resolves a browser session; a token reaching one would be refused with a 401 "
        f"that names the wrong reason."
    )
