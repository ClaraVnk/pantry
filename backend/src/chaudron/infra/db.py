"""Async engine, session factory, and the one place a transaction begins.

**One HTTP request is one transaction.** :meth:`Database.session` opens it,
commits on a clean return and rolls back on any exception. Nothing else in the
application calls ``begin()``, and no handler commits on its own.

That is not a stylistic preference. It is the prerequisite listed in
``docs/security-review-baseline.md`` (SEC-001) for row-level security: the
tenant is posted with ``SET LOCAL chaudron.household_id``, which PostgreSQL
discards at ``COMMIT``. A request spread over two transactions would either lose
the setting halfway through or leak it into a recycled connection -- and the
whole mechanism is worthless if either can happen. Paying the discipline now
costs a context manager; retrofitting it later costs an audit of every handler.

Since revision ``0004`` that mechanism is live. Two ways in, and they are the
same code path:

* **Explicitly** -- :func:`set_transaction_household`, or
  ``Database.session(household_id=...)`` for a caller that knows its tenant up
  front (a background job, ``scripts/seed.py``, a test).
* **From the request context** -- the API resolves the household *after* the
  session dependency has already opened the transaction, so there is no single
  statement in ``api/deps.py`` that could carry it. The listeners installed on
  :class:`_TenantAwareSession` therefore post it lazily, at the first statement
  issued once ``household_id_var`` holds a value. That variable is set in exactly
  one place -- ``api.deps.get_household_id``, after the household has been
  resolved and checked server-side -- and reset per request by the middleware in
  ``api.main``, so it carries the *resolved* tenant and never a client-supplied
  one.

The lazy path is why the tenant is posted with ``SET LOCAL`` rather than a
session-level ``SET``: a session-level value survives ``COMMIT`` and is handed to
the next request that borrows the same pooled connection, which is the *inverse*
leak ``docs/data-model.md`` section 5.3 warns about. ``tests/tenancy`` proves
both halves against a real PostgreSQL -- that the value is visible inside the
transaction, and that the next checkout of the same backend sees nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction

from chaudron.config import Settings
from chaudron.infra.logging import household_id_var

#: The transaction-local PostgreSQL parameter every RLS policy reads. Must match
#: ``TENANT_SETTING`` in migration ``0004``; ``tests/tenancy`` asserts it does.
TENANT_SETTING: Final = "chaudron.household_id"

#: Where the posted tenant is remembered for the duration of the transaction, so
#: the listeners post it once rather than before every statement.
_POSTED_TENANT: Final = "chaudron_posted_tenant"


class TenantScopeError(RuntimeError):
    """A transaction was asked to serve two different households.

    Not a defensive nicety: the transaction is the unit of isolation, so a second
    tenant appearing inside one means either a session shared between two
    requests or a household resolved twice with different answers. Both are
    leaks, and both are silent if the second value simply overwrites the first.
    """


async def set_transaction_household(session: AsyncSession, household_id: uuid.UUID) -> None:
    """Post the current household for the rest of *this* transaction.

    ``set_config(..., is_local => true)`` is ``SET LOCAL`` with bind parameters,
    which ``SET`` does not accept. PostgreSQL restores the parameter at ``COMMIT``
    and at ``ROLLBACK``, so nothing survives into the next borrower of the
    connection.
    """
    posted = session.info.get(_POSTED_TENANT)
    wanted = str(household_id)
    if posted == wanted:
        return
    if posted is not None:
        raise TenantScopeError(
            f"this transaction is already scoped to household {posted}; "
            f"a single transaction serves a single household"
        )
    await session.execute(select(func.set_config(TENANT_SETTING, wanted, True)))
    session.info[_POSTED_TENANT] = wanted


async def current_transaction_household(session: AsyncSession) -> uuid.UUID | None:
    """What PostgreSQL believes the current household to be, or ``None``.

    Reads the parameter back from the server rather than the value Python thinks
    it posted: the point of this function is to catch the case where the two
    disagree.

    ``NULLIF`` is load-bearing. A ``SET LOCAL`` is undone at ``COMMIT`` by
    restoring the *session* value, which for a custom parameter never set at
    session level is the empty string -- not NULL. ``''::uuid`` raises.
    """
    raw = await session.scalar(select(func.nullif(func.current_setting(TENANT_SETTING, True), "")))
    return None if raw is None else uuid.UUID(raw)


class _TenantAwareSession(Session):
    """The sync session class the listeners below are attached to.

    A dedicated subclass rather than ``Session`` itself: listeners registered on
    the base class would also fire for sessions the test harness builds by hand,
    and a security mechanism that behaves differently depending on who imported
    what is a mechanism nobody can reason about.
    """


def _post_pending_tenant(session: Session) -> None:
    """Post the request's household if one is known and none has been posted yet."""
    wanted = household_id_var.get()
    if wanted is None:
        return
    posted = session.info.get(_POSTED_TENANT)
    if posted == wanted:
        return
    if posted is not None:
        raise TenantScopeError(
            f"this transaction is scoped to household {posted}, but the request "
            f"context now names {wanted}"
        )
    session.info[_POSTED_TENANT] = wanted
    # Through the connection, not the session: `Session.execute` inside an ORM
    # event re-enters the very event being handled.
    session.connection().execute(select(func.set_config(TENANT_SETTING, wanted, True)))


@event.listens_for(_TenantAwareSession, "do_orm_execute")
def _post_tenant_before_query(state: ORMExecuteState) -> None:
    _post_pending_tenant(state.session)


@event.listens_for(_TenantAwareSession, "before_flush")
def _post_tenant_before_flush(
    session: Session, flush_context: Any, instances: Sequence[Any] | None
) -> None:
    """Covers ``session.add(...)`` followed by a commit with no read in between.

    Without it that flush would reach an ``INSERT`` whose ``WITH CHECK`` fails --
    loud rather than leaky, but a failure all the same, and one that only shows up
    on the code paths that happen not to read anything first.
    """
    _post_pending_tenant(session)


@event.listens_for(_TenantAwareSession, "after_transaction_end")
def _forget_tenant(session: Session, transaction: SessionTransaction) -> None:
    """Forget the posted tenant when the transaction PostgreSQL forgot it ends.

    Only for the outermost transaction: a released ``SAVEPOINT`` does not undo a
    ``SET LOCAL``, so forgetting on a nested end would make the next statement
    post it again for nothing.
    """
    if transaction.parent is None:
        session.info.pop(_POSTED_TENANT, None)


@dataclass(frozen=True, slots=True)
class RowLevelSecurityReport:
    """Whether row-level security is genuinely in force for this connection."""

    role_name: str
    is_superuser: bool
    bypasses_rls: bool
    #: Tables carrying ``household_id`` on which no policy applies at all.
    tables_without_policies: tuple[str, ...]
    #: Protected tables this very role owns -- and therefore bypasses, since
    #: migration ``0004`` deliberately leaves ``FORCE ROW LEVEL SECURITY`` off.
    tables_owned_by_this_role: tuple[str, ...]

    @property
    def problems(self) -> tuple[str, ...]:
        """Every reason the policies would not apply, in the order they matter."""
        found: list[str] = []
        if self.is_superuser:
            found.append(f"{self.role_name} is a superuser and bypasses every policy")
        if self.bypasses_rls:
            found.append(f"{self.role_name} holds BYPASSRLS and bypasses every policy")
        if self.tables_owned_by_this_role:
            found.append(
                f"{self.role_name} owns {', '.join(self.tables_owned_by_this_role)} "
                f"and bypasses their policies (no FORCE ROW LEVEL SECURITY)"
            )
        if self.tables_without_policies:
            found.append(
                f"tenant tables without row-level security: "
                f"{', '.join(self.tables_without_policies)}"
            )
        return tuple(found)

    @property
    def is_enforced(self) -> bool:
        return not self.problems


#: Tables carrying a tenant column but left unprotected by migration ``0004``.
_UNPROTECTED_TENANT_TABLES = text(
    """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND NOT c.relrowsecurity
      AND EXISTS (
          SELECT 1 FROM pg_attribute a
          WHERE a.attrelid = c.oid AND a.attname = 'household_id' AND NOT a.attisdropped
      )
    ORDER BY c.relname
    """
)

#: Protected tables owned by the connecting role, which therefore bypasses them.
_SELF_OWNED_PROTECTED_TABLES = text(
    """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND c.relrowsecurity
      AND NOT c.relforcerowsecurity
      AND pg_catalog.pg_get_userbyid(c.relowner) = current_user
    ORDER BY c.relname
    """
)

_CONNECTING_ROLE = text(
    """
    SELECT current_user AS role_name, rolsuper AS is_superuser, rolbypassrls AS bypasses_rls
    FROM pg_roles
    WHERE rolname = current_user
    """
)


#: The rate limiters' own pool. Deliberately fixed rather than configurable: it
#: is not a capacity dial but a decoupling, and the number that matters is that
#: it is *separate*, not that it is five. A limiter transaction holds its
#: connection for three statements against a local socket, so this serves a
#: request rate the size does not suggest.
_LIMITER_POOL_SIZE: Final = 5
_LIMITER_MAX_OVERFLOW: Final = 5


class Database:
    """Owns the engine and hands out request-scoped sessions."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # A connection dropped by a restarted database otherwise surfaces as
            # a failed user request instead of a transparent reconnect.
            pool_pre_ping=True,
            echo=False,
            # SQLAlchemy renders the bound parameters into `str(StatementError)`,
            # and `api/errors.py` logs any unhandled exception with `exc_info`. So
            # a single `IntegrityError` on a household member would otherwise write
            # that member's name, allergens and infant age band to the log -- health
            # data about a minor, on disk, under journald's retention rather than
            # ours. `infra/logging.py` redacts every line it writes, but redaction
            # knows credential *shapes*: a child's first name has none, and no
            # pattern will ever catch it. This is the half of that fix that no
            # pattern can do. The statement itself and the violated constraint stay
            # in the message, which is what an operator actually diagnoses from.
            hide_parameters=True,
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=True,
            sync_session_class=_TenantAwareSession,
        )
        # A second pool, for the rate limiters alone, and it is not a tuning
        # preference -- sharing the first one turns the limiters into the outage
        # they exist to prevent.
        #
        # `SharedRateLimiter` opens a connection of its own, on purpose: its
        # deduction has to survive the request transaction rolling back
        # (`infra/rate_limits.py`). But a limiter runs *inside* a request, whose
        # session connection is already checked out. On one pool, a rate-limited
        # request therefore holds one connection and asks for a second. Fill the
        # pool with such requests -- `pool_size + max_overflow` of them, fifteen
        # by default -- and every one of them is holding what the others are
        # waiting for. Nothing is deadlocked in the database's sense, so nothing
        # detects it: they all sit on `pool_timeout`, thirty seconds, and then
        # return `500`. The limiter's whole job at that moment was to answer
        # `429` in a millisecond.
        #
        # Separate pools break the cycle by construction: a limiter connection is
        # never held while waiting for a session connection, so the two can never
        # queue behind each other.
        #
        # Small on purpose. A limiter transaction is three statements against a
        # local socket and is released immediately, so this pool serves a request
        # rate far above what its size suggests -- unlike the session pool, which
        # holds a connection for a whole request including any model call.
        self._limiter_engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=_LIMITER_POOL_SIZE,
            max_overflow=_LIMITER_MAX_OVERFLOW,
            pool_pre_ping=True,
            echo=False,
            hide_parameters=True,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def limiter_engine(self) -> AsyncEngine:
        """The pool the rate limiters use, and nothing else. See :meth:`__init__`."""
        return self._limiter_engine

    @asynccontextmanager
    async def session(self, household_id: uuid.UUID | None = None) -> AsyncIterator[AsyncSession]:
        """One session, one transaction, committed or rolled back as a whole.

        ``household_id`` is for callers that know their tenant before opening the
        transaction -- background jobs above all, which run outside any request
        and are the ones ``docs/data-model.md`` section 5.4 expects to leak first.
        An HTTP request leaves it ``None`` and the listeners post it as soon as
        the household has been resolved.
        """
        async with self._sessionmaker() as session, session.begin():
            if household_id is not None:
                await set_transaction_household(session, household_id)
            yield session

    async def check_row_level_security(self) -> RowLevelSecurityReport:
        """Report whether this connection is actually subject to the policies.

        Called by ``/readyz`` and, in production, at startup (``api/main.py``); also
        by ``scripts/provision_app_role.py``. Being called is the whole point: for
        a while it was called by nothing, which made it documentation rather than a
        control -- and documentation does not fail a deployment. An
        instance whose DSN still names the table owner passes every functional
        test and enforces nothing: the owner bypasses policies unless ``FORCE ROW
        LEVEL SECURITY`` is set, which migration ``0004`` deliberately does not
        set. That misconfiguration has no symptom, so it needs a probe.
        """
        async with self._engine.connect() as connection:
            role = (await connection.execute(_CONNECTING_ROLE)).one()
            unprotected = (await connection.scalars(_UNPROTECTED_TENANT_TABLES)).all()
            owned = (await connection.scalars(_SELF_OWNED_PROTECTED_TABLES)).all()
        return RowLevelSecurityReport(
            role_name=str(role.role_name),
            is_superuser=bool(role.is_superuser),
            bypasses_rls=bool(role.bypasses_rls),
            tables_without_policies=tuple(unprotected),
            tables_owned_by_this_role=tuple(owned),
        )

    async def dispose(self) -> None:
        await self._engine.dispose()
        await self._limiter_engine.dispose()
