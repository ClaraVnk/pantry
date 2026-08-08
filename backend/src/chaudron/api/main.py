"""Application factory.

``create_app`` builds a fully wired application from a :class:`Settings` value
and nothing else -- no import-time singletons, no reading of the environment
half-way down a call stack. That is what lets the test suite build an app
against a throwaway database, and what makes "which configuration is this
process running?" a question with one answer.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import RequestResponseEndpoint

from chaudron.api.errors import register_exception_handlers
from chaudron.api.middleware import RequestSizeLimitMiddleware, SecurityHeadersMiddleware
from chaudron.api.routers import (
    balance_router,
    budget_router,
    export_targets_router,
    health_router,
    inventory_router,
    locations_router,
    members_router,
    privacy_router,
    products_router,
    providers_router,
    receipts_router,
    recipes_router,
    shopping_router,
    tokens_router,
)
from chaudron.api.routers.auth import router as auth_router
from chaudron.api.routers.calendar import router as calendar_router
from chaudron.api.routers.receipts import RECEIPT_IMPORT_PATH
from chaudron.api.routers.shopping import IMPORT_PATH, MULTIPART_OVERHEAD_BYTES
from chaudron.api.routers.shopping_export import router as shopping_export_router
from chaudron.api.throttling import ConcurrencyLimiter, Throttles
from chaudron.config import Settings, get_settings
from chaudron.infra.db import Database
from chaudron.infra.documents import (
    SandboxLimits,
    configure_document_sandbox,
    shutdown_document_sandbox,
)
from chaudron.infra.logging import configure_logging, household_id_var, request_id_var
from chaudron.infra.openfoodfacts import OpenFoodFactsCatalog
from chaudron.infra.passwords import Passwords
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from chaudron.services.calendar import report_feed_scan_headroom

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

#: What an inbound ``X-Request-Id`` must look like to be worth writing to a log.
#: Deliberately narrow -- UUIDs and W3C trace identifiers both fit, and nothing
#: else can. The value is never returned to the client and never used as an
#: incident identifier, so this only has to keep the logs readable.
_UPSTREAM_REQUEST_ID: Final = re.compile(r"\A[0-9A-Za-z._-]{1,128}\Z")

#: The two windows the limits are expressed over. Fixed in code rather than
#: configurable: the *counts* are a product decision an operator may reasonably
#: retune, the units they are quoted in are not.
_RECIPE_WINDOW_SECONDS: Final = 3600.0
_PRODUCT_LOOKUP_WINDOW_SECONDS: Final = 60.0
#: Sign-in and sign-up are quoted per hour. A window shorter than that lets a
#: patient guesser spend a full budget every few minutes and still stay under the
#: advertised rate; an hour is the unit the counts in ``config.py`` are named in.
_AUTH_WINDOW_SECONDS: Final = 3600.0


def build_throttles(
    settings: Settings, engine: AsyncEngine, *, scope_prefix: str = ""
) -> Throttles:
    """The limiters this application instance owns.

    The rate caps are rows in the database ``engine`` reaches, so that two
    workers of the same deployment grant one budget between them rather than two
    (``infra/rate_limits.py``). The engine is the one :class:`Database` already
    owns -- passed in rather than built here, because a second pool aimed at the
    same server would double the connections for no benefit.

    ``scope_prefix`` namespaces those rows. Empty in production, and it has to
    be: sharing the buckets is the entire point, so every worker of every replica
    must land on the same scopes. The test suite sets it per test, because the
    limiter commits on its own connection precisely so the caller's rollback
    cannot reach it -- which means a row written by one test outlives it, and two
    tests on one scope would share a bucket.
    """

    def rate(name: str, limit: int, window_seconds: float) -> SharedRateLimiter:
        return SharedRateLimiter(
            engine,
            BucketPolicy(scope=f"{scope_prefix}{name}", limit=limit, window_seconds=window_seconds),
        )

    return Throttles(
        recipe_suggestions=rate(
            "recipe_suggestions", settings.recipe_suggestions_per_hour, _RECIPE_WINDOW_SECONDS
        ),
        recipe_inferences=ConcurrencyLimiter(
            per_key=settings.recipe_max_concurrent_per_household,
            total=settings.recipe_max_concurrent_total,
        ),
        product_lookups=rate(
            "product_lookups", settings.product_lookups_per_minute, _PRODUCT_LOOKUP_WINDOW_SECONDS
        ),
        receipt_imports=rate(
            "receipt_imports", settings.receipt_imports_per_hour, _RECIPE_WINDOW_SECONDS
        ),
        receipt_inferences=ConcurrencyLimiter(
            per_key=settings.receipt_max_concurrent_per_household,
            total=settings.receipt_max_concurrent_total,
        ),
        shopping_imports=rate(
            "shopping_imports", settings.shopping_imports_per_hour, _RECIPE_WINDOW_SECONDS
        ),
        shopping_import_documents=ConcurrencyLimiter(
            per_key=settings.shopping_import_max_concurrent_per_household,
            total=settings.shopping_import_max_concurrent_total,
        ),
        login_attempts_by_ip=rate(
            "login_attempts_by_ip", settings.login_attempts_per_ip_per_hour, _AUTH_WINDOW_SECONDS
        ),
        login_attempts_by_account=rate(
            "login_attempts_by_account",
            settings.login_attempts_per_account_per_hour,
            _AUTH_WINDOW_SECONDS,
        ),
        registrations=rate(
            "registrations", settings.registrations_per_ip_per_hour, _AUTH_WINDOW_SECONDS
        ),
        machine_token_attempts=rate(
            "machine_token_attempts",
            settings.machine_token_attempts_per_ip_per_hour,
            _AUTH_WINDOW_SECONDS,
        ),
    )


class RowLevelSecurityNotEnforcedError(RuntimeError):
    """A deployed instance connects to PostgreSQL as a role that bypasses RLS."""


async def verify_row_level_security(settings: Settings, database: Database) -> None:
    """Refuse to serve real traffic from an instance that isolates nothing.

    The failure this closes is silent by construction: a ``CHAUDRON_DATABASE_URL``
    naming the table owner passes every functional test, answers every request
    correctly, and enforces no policy at all -- migration ``0004`` deliberately
    leaves ``FORCE ROW LEVEL SECURITY`` off, so the owner bypasses its own tables.
    Nothing about a running instance would ever say so.

    A probe that cannot reach the database is *not* treated as a failure. "Unable
    to verify" and "verified insecure" are different facts, and refusing to boot
    on the first would turn a database that is thirty seconds late into a restart
    loop. The instance stays out of the load balancer regardless, because
    ``/readyz`` runs the same probe on every poll and does refuse there.

    Which environments this refuses in is ``Settings.requires_row_level_security``
    and no longer ``is_production``: ``staging`` used to boot happily on the owner
    DSN, isolating nothing (audit AUD-029). ``local`` and ``ci`` remain exempt,
    because connecting as the owner is deliberate there.
    """
    try:
        report = await database.check_row_level_security()
    except (SQLAlchemyError, OSError) as exc:
        # `OSError` as well as the SQLAlchemy hierarchy: a refused connection or a
        # name that does not resolve reaches this frame raw, from asyncpg's socket
        # rather than from the dialect, and "the database is not up yet" must not
        # be mistaken here for "the database is not safe".
        logger.error("row_level_security_unverified", extra={"error": type(exc).__name__})
        return
    if report.is_enforced:
        return
    logger.error("row_level_security_not_enforced", extra={"problems": list(report.problems)})
    if settings.requires_row_level_security:
        raise RowLevelSecurityNotEnforcedError(
            "row-level security is not in force for this connection: "
            + "; ".join(report.problems)
            + ". Point CHAUDRON_DATABASE_URL at the application role "
            "(scripts/provision_app_role.py), not at the owner of the tables."
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the tenancy guarantee, then release what the factory acquired.

    Nothing is *created* here. A dependency built in the lifespan is absent from
    an app driven by an ASGI transport that does not run it -- which is how the
    test client works, and how half the "works in production, None in tests"
    bugs start.
    """
    settings: Settings = app.state.settings
    await verify_row_level_security(settings, app.state.database)
    if settings.calendar_feed_enabled:
        # Credential resolution scans the household table and refuses past a bound
        # (``infra/calendar/credentials.py``). Said here, where an operator can add
        # capacity, rather than in the ``503`` the first subscriber would meet.
        await report_feed_scan_headroom(app.state.database)
    logger.info("chaudron_started", extra={"env": settings.env, "version": app.version})
    try:
        yield
    finally:
        catalog: OpenFoodFactsCatalog = app.state.catalog
        await catalog.aclose()
        database: Database = app.state.database
        await database.dispose()
        # The PDF workers outlive a request by design -- the pool is reused -- so
        # something has to end them, and a process that has stopped serving has no
        # reason to keep a forkserver alive.
        shutdown_document_sandbox()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Raises before the first request if misconfigured."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved.log_level)

    app = FastAPI(
        title="Chaudron",
        version="0.1.0",
        summary="Household food stock management.",
        lifespan=_lifespan,
        # Docs are a development affordance, not a production endpoint: they
        # describe every route and parameter to anyone who finds the host.
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
    )
    app.state.settings = resolved
    app.state.database = Database(resolved)
    app.state.catalog = OpenFoodFactsCatalog(resolved)
    # Empty in production, and load-bearing that it is: the rate buckets are
    # shared so that every worker of every replica lands on the same rows. It is
    # an attribute rather than a constant only because the test suite has to
    # namespace them -- the buckets outlive the transaction that wrote them, by
    # design, so tests that shared a scope would share a budget.
    app.state.rate_limit_scope_prefix = ""
    # `limiter_engine`, not `engine`: a limiter runs while the request already
    # holds a session connection, so drawing its own from the same pool makes
    # fifteen concurrent limited requests wait on each other for thirty seconds
    # and then answer 500. `infra/db.py` sets out why the pools are separate.
    app.state.throttles = build_throttles(resolved, app.state.database.limiter_engine)
    # Not on ``app.state``: what this configures is a pool of *processes*, which
    # belongs to the interpreter rather than to one application object. Set here
    # rather than in the lifespan for the reason the lifespan docstring gives --
    # a test client that never runs the lifespan must still get the bounded parse,
    # since the bound is the security property.
    configure_document_sandbox(
        SandboxLimits(
            address_space_bytes=resolved.document_sandbox_address_space_bytes,
            cpu_seconds=resolved.document_sandbox_cpu_seconds,
            expansion_bytes=resolved.document_sandbox_expansion_bytes,
            workers=resolved.shopping_import_max_concurrent_total,
        )
    )
    # Built once: the constructor computes a placeholder Argon2 digest -- 64 MiB
    # and three passes -- and paying that per request would be a denial of
    # service of our own making (``infra/passwords.py``).
    app.state.passwords = Passwords()

    # Middleware order matters and is not obvious: `add_middleware` *prepends*, so
    # the last call below is the outermost layer. Outer to inner, the stack ends up
    # as: request context, security headers, CORS, size limit. That is deliberate.
    # The size limit sits innermost so its 413 still passes back out through CORS
    # (a browser must be able to read it) and still collects a request identifier;
    # the security headers sit outside CORS so they reach preflight responses too.
    # The two import routes are the only ones whose body is a file rather than a
    # JSON document, so each carries its own ceiling instead of raising the general
    # one for every endpoint (``config.py``, ``max_request_body_bytes``). They are
    # not the same number: a shopping list is text and a till receipt is a
    # photograph. The values here are the coarse floor -- each handler refuses
    # again, quoting the *file* limit, which is the number a user can act on.
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=resolved.max_request_body_bytes,
        path_bounds={
            IMPORT_PATH: resolved.shopping_import_max_bytes + MULTIPART_OVERHEAD_BYTES,
            RECEIPT_IMPORT_PATH: (resolved.receipt_import_max_bytes + MULTIPART_OVERHEAD_BYTES),
        },
    )

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=resolved.cors_allow_credentials,
            # ``PUT`` is here for ``/v1/budget/target``, the one idempotent
            # replacement in the contract: without it the browser's preflight
            # refuses the call and the failure looks like a network error.
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            # ``X-CSRF-Token`` is what the interface echoes on every unsafe
            # method; without it here the browser's preflight refuses the call
            # and the failure looks like a network error rather than a missing
            # header. ``X-Household-Id`` stays, but it is a *selector* now and no
            # longer a credential -- ``api/deps.py`` accepts it only when the
            # authenticated account is a member of the household it names.
            allow_headers=["Authorization", "Content-Type", "X-Household-Id", "X-CSRF-Token"],
            expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
            max_age=600,
        )

    app.add_middleware(SecurityHeadersMiddleware, production=resolved.is_production)

    @app.middleware("http")
    async def attach_request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Give every request an identifier, and every log line that identifier.

        The identifier is **generated here, always**. It used to be whatever the
        client sent, which made it useless for the one job it has: an unauthenticated
        caller could give a million requests the same identifier to defeat aggregation,
        reuse an identifier seen in someone else's response to blend into their trail,
        or hand an investigator a plausible-looking value it had invented (audit
        AUD-014). An incident identifier a stranger chooses is evidence of nothing.

        An inbound value is not thrown away -- correlating with a proxy is a real
        need -- it is written to the log under a different name, once, after
        validation, and never reflected back to the client.
        """
        request_id = str(uuid.uuid4())
        request_token = request_id_var.set(request_id)
        household_token = household_id_var.set(None)
        incoming = request.headers.get(REQUEST_ID_HEADER)
        if incoming is not None and _UPSTREAM_REQUEST_ID.match(incoming):
            logger.info("upstream_request_id", extra={"upstream_request_id": incoming})
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(request_token)
            household_id_var.reset(household_token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(locations_router)
    app.include_router(inventory_router)
    app.include_router(products_router)
    app.include_router(providers_router)
    app.include_router(receipts_router)
    app.include_router(recipes_router)
    app.include_router(shopping_router)
    # Before the export router: its paths are literal (`/export/targets/...`)
    # while that one's are parameterised (`/{shopping_list_id}/export/...`), and
    # a literal route registered second would be shadowed by a pattern that
    # happens to have the same shape.
    app.include_router(export_targets_router)
    app.include_router(shopping_export_router)
    app.include_router(calendar_router)
    app.include_router(budget_router)
    app.include_router(members_router)
    app.include_router(balance_router)
    # The data-subject rights (GDPR 15, 17, 20). Registered last among the /v1
    # routers because it is the one that removes what the others wrote.
    app.include_router(privacy_router)
    app.include_router(tokens_router)
    return app


def __getattr__(name: str) -> Any:
    """Expose ``chaudron.api.main:app`` without building it at import time.

    The container entrypoint asks for that attribute, so it has to exist. Binding
    it eagerly would read the environment whenever *anything* imports this module
    -- including the test suite, which builds its own app and has no reason to
    need a valid production configuration to do so.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
