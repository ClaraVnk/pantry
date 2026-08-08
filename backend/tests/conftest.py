"""Shared fixtures for the Chaudron backend test suite.

Two rules shape everything below.

* **PostgreSQL only.** There is no SQLite mode, not even for "unit" tests. The
  schema relies on ``timestamptz``, ``numeric``, ``jsonb``, partial indexes and
  composite unique constraints; an engine that fakes those hides exactly the bugs
  worth catching (ADR-0003).
* **Podman only.** When no database URL is provided, an ephemeral container is
  started through testcontainers, pointed at the rootless Podman socket. See
  :func:`_configure_container_runtime` -- that wiring is the classic failure point
  of the testcontainers + Podman combination.

The database is resolved in this order:

1. ``CHAUDRON_TEST_DATABASE_URL`` -- explicit override, wins over everything.
2. ``CHAUDRON_DATABASE_URL`` -- what CI sets for its ``postgres:16`` service.
3. An ephemeral container started here.

If none of the three can be obtained, the database fixtures **skip** with the
reason attached. They never fall back to another engine, and they never fail the
run on a machine that simply has no container runtime.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE, get_session
from chaudron.api.main import build_throttles, create_app
from chaudron.config import Settings
from chaudron.domain.models import (
    Household,
    HouseholdMember,
    MembershipRole,
    UserAccount,
    UserSession,
)
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.passwords import Passwords
from chaudron.services.auth import hash_token, new_token


@lru_cache(maxsize=1)
def _shared_passwords() -> Passwords:
    """One :class:`Passwords` for the whole run.

    Its constructor computes a placeholder Argon2 digest -- 64 MiB and three
    passes. Building one per test would add that cost to every database test in
    the suite for no benefit: the object is stateless once built.
    """
    return Passwords()


# --------------------------------------------------------------------------- #
# Container runtime
# --------------------------------------------------------------------------- #

_POSTGRES_IMAGE: Final = "postgres:16"

# Checked in order; the first one set wins.
_DATABASE_URL_ENV_VARS: Final = ("CHAUDRON_TEST_DATABASE_URL", "CHAUDRON_DATABASE_URL")

_PODMAN_SETUP_HINT: Final = (
    "Enable the rootless Podman socket with "
    "`systemctl --user enable --now podman.socket`, or set CHAUDRON_TEST_DATABASE_URL "
    "to an existing PostgreSQL 16 instance."
)


def _podman_socket() -> Path | None:
    """Locate the Podman API socket, rootless first, then rootful."""
    candidates: list[Path] = []
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "podman" / "podman.sock")
    candidates.append(Path("/run/podman/podman.sock"))
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _configure_container_runtime() -> str | None:
    """Point testcontainers at Podman. Returns a reason string when it cannot.

    testcontainers speaks the Docker HTTP API, which Podman implements, so the whole
    job is telling it where the socket is -- plus one Podman-specific correction:

    ``TESTCONTAINERS_RYUK_DISABLED``. Ryuk is the reaper container testcontainers
    starts to garbage-collect after a crashed run. It bind-mounts the container
    socket and expects to run privileged; under rootless Podman it never becomes
    ready, and every run dies on ``TimeoutError: container did not become running``
    -- reported at the *database* container, which sends you looking in the wrong
    place entirely. Containers started here are stopped by the session fixture's
    ``finally`` instead, which covers everything but a hard kill of the interpreter.

    The disabling is applied whenever the socket looks like Podman, including when
    ``DOCKER_HOST`` was exported by the developer's shell -- that case is the common
    one, and skipping the correction for it was this function's first bug.

    An explicit ``DOCKER_HOST`` is otherwise left alone: someone pointing at a remote
    or rootful daemon on purpose should not be second-guessed here.
    """
    docker_host = os.environ.get("DOCKER_HOST")
    if not docker_host:
        socket = _podman_socket()
        if socket is None:
            return f"no Podman socket found. {_PODMAN_SETUP_HINT}"
        docker_host = f"unix://{socket}"
        os.environ["DOCKER_HOST"] = docker_host
    if "podman" in docker_host:
        os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    return None


def _as_asyncpg_url(raw_url: str) -> str:
    """Normalise any PostgreSQL URL to the async driver the application uses."""
    return (
        make_url(raw_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A reachable PostgreSQL 16, from the environment or from a fresh container."""
    for env_var in _DATABASE_URL_ENV_VARS:
        configured = os.environ.get(env_var)
        if configured:
            yield _as_asyncpg_url(configured)
            return

    unavailable = _configure_container_runtime()
    if unavailable is not None:
        pytest.skip(f"database tests need PostgreSQL: {unavailable}")

    try:
        # `testcontainers.postgres` still resolves but is a deprecated shim whose wait
        # strategy probes the database from the host with a *synchronous* driver that
        # is not installed here, and reports the miss as a connection refusal against
        # Podman. The community module is the maintained path.
        from testcontainers.community.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - dev group is always installed in CI
        pytest.skip(f"testcontainers is not installed: {exc}")

    container = PostgresContainer(_POSTGRES_IMAGE, driver="asyncpg")
    try:
        container.start()
    except Exception as exc:  # broad on purpose: any failure here means "no database"
        pytest.skip(
            f"could not start {_POSTGRES_IMAGE} through Podman: {exc}. {_PODMAN_SETUP_HINT}"
        )

    try:
        yield str(container.get_connection_url())
    finally:
        container.stop()


_BACKEND_ROOT: Final = Path(__file__).resolve().parent.parent


def _apply_migrations(url: str) -> None:
    """Bring the database to ``head`` with Alembic.

    Not ``metadata.create_all``: that would validate a schema no environment ever
    applies, and a broken migration would reach production behind a green suite.
    It also means the reference tables (``unit``, ``llm_provider``) are seeded
    here exactly as they are in production, by revision ``0002``.

    The URL is handed to Alembic explicitly rather than through the environment,
    so the suite needs no application secrets to build a schema.
    """
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def initialised_database(postgres_url: str) -> str:
    """Migrate the database once per session.

    Deliberately a *synchronous* fixture: Alembic drives its own event loop, and a
    session-scoped async fixture would force a session-scoped loop that asyncpg
    connections created in function-scoped tests cannot be used across.
    """
    _apply_migrations(postgres_url)
    return postgres_url


@pytest.fixture
async def engine(initialised_database: str) -> AsyncIterator[AsyncEngine]:
    """A per-test engine. ``NullPool`` keeps no connection alive across event loops."""
    test_engine = create_async_engine(initialised_database, poolclass=NullPool)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back when the test ends.

    The session joins an outer transaction opened on the connection;
    ``join_transaction_mode="create_savepoint"`` turns any ``commit()`` made by the
    code under test into a savepoint release, so committing inside a test is legal
    and still leaves nothing behind. No truncation, no re-created schema, no
    ordering coupling between tests.
    """
    async with engine.connect() as connection:
        outer = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if outer.is_active:
                await outer.rollback()


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
#
# Callable protocols rather than bare `Callable[..., Awaitable[T]]`: keyword
# arguments stay named and checked under `mypy --strict`, so a typo in a test is a
# type error rather than a runtime `TypeError` discovered on the next CI run.


class MakeHousehold(Protocol):
    async def __call__(
        self,
        *,
        name: str | None = None,
        is_instance_owner: bool = False,
        member: bool = True,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> Household: ...


class MakeUser(Protocol):
    async def __call__(
        self, *, email: str | None = None, display_name: str | None = None
    ) -> UserAccount: ...


class MakeMember(Protocol):
    async def __call__(
        self,
        household: Household,
        user: UserAccount,
        *,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> HouseholdMember: ...


def _unique_suffix() -> str:
    """Short, collision-free and time-ordered, from the same source as the PKs."""
    return uuid.uuid7().hex[-12:]


@pytest.fixture
def make_household(db_session: AsyncSession, signed_in_user: UserAccount) -> MakeHousehold:
    """A household, with the signed-in test account as a member of it by default.

    The default is what keeps three hundred existing tests meaning what they
    meant. They create a household and send ``X-Household-Id``; under
    authentication that header is a *selector* checked against the caller's
    memberships (``api/deps.py``), so a household the test account does not belong
    to now answers ``403`` -- correctly, and uselessly for a test about inventory
    paging.

    ``member=False`` is the deliberate opposite, and
    ``tests/api/test_authentication.py`` uses it to build the household a caller
    must *not* be able to open.
    """

    async def _make_household(
        *,
        name: str | None = None,
        is_instance_owner: bool = False,
        member: bool = True,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> Household:
        household = Household(
            name=name if name is not None else f"Household {_unique_suffix()}",
            is_instance_owner=is_instance_owner,
        )
        db_session.add(household)
        await db_session.flush()
        if member:
            db_session.add(
                HouseholdMember(household_id=household.id, user_id=signed_in_user.id, role=role)
            )
            await db_session.flush()
        return household

    return _make_household


@pytest.fixture
def make_user(db_session: AsyncSession) -> MakeUser:
    async def _make_user(
        *, email: str | None = None, display_name: str | None = None
    ) -> UserAccount:
        suffix = _unique_suffix()
        user = UserAccount(
            email=email if email is not None else f"user-{suffix}@example.test",
            display_name=display_name if display_name is not None else f"User {suffix}",
            # No password: hashing belongs to the auth module, which does not exist
            # yet, and a fixture must not invent a format the real code will contradict.
            password_hash=None,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make_user


@pytest.fixture
def make_member(db_session: AsyncSession) -> MakeMember:
    async def _make_member(
        household: Household,
        user: UserAccount,
        *,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> HouseholdMember:
        member = HouseholdMember(household_id=household.id, user_id=user.id, role=role)
        db_session.add(member)
        await db_session.flush()
        return member

    return _make_member


@dataclass(frozen=True, slots=True)
class TenantPair:
    """Two unrelated households, each with an owner. The seed of every isolation test.

    Systematic by construction: a test that asks for this fixture cannot "forget" to
    create the second tenant, which is how isolation tests usually end up proving
    nothing (see `docs/testing-strategy.md`, section 4).
    """

    household_a: Household
    owner_a: UserAccount
    household_b: Household
    owner_b: UserAccount


@pytest.fixture
async def tenant_pair(
    make_household: MakeHousehold, make_user: MakeUser, make_member: MakeMember
) -> TenantPair:
    household_a = await make_household(name="Household A")
    owner_a = await make_user(display_name="Owner A")
    await make_member(household_a, owner_a)

    household_b = await make_household(name="Household B")
    owner_b = await make_user(display_name="Owner B")
    await make_member(household_b, owner_b)

    return TenantPair(
        household_a=household_a, owner_a=owner_a, household_b=household_b, owner_b=owner_b
    )


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #

#: Test-only values that satisfy the configuration's fail-fast rules. They are
#: constants, not secrets: nothing signs or encrypts anything in the suite, and a
#: value read from the developer's environment would make the tests depend on it.
_TEST_SECRET_KEY: Final = "test-secret-key-that-is-long-enough-for-validation"
_TEST_ENCRYPTION_KEY: Final = base64.b64encode(b"0" * 32).decode()


def build_test_cipher() -> CredentialCipher:
    """The very cipher :func:`build_test_settings` yields, for tests that seed a row.

    Storing a household key means writing a ciphertext the *application* can read
    back, so a test that inserts one must encrypt with the same master key the app
    was configured with. Exposed here rather than re-derived per test file, so the two
    cannot drift.
    """
    return CredentialCipher(base64.b64decode(_TEST_ENCRYPTION_KEY))


def build_test_settings(database_url: str) -> Settings:
    return Settings(
        env="ci",
        log_level="WARNING",
        database_url=SecretStr(database_url),
        secret_key=SecretStr(_TEST_SECRET_KEY),
        credential_encryption_key=SecretStr(_TEST_ENCRYPTION_KEY),
        cors_origins=["http://localhost:5173"],
        # Required now that the session is a cookie: `Settings` refuses a listed
        # origin without it, because a cross-origin browser client could never
        # send one (`config.py`).
        cors_allow_credentials=True,
    )


def isolate_rate_limit_buckets(app: FastAPI) -> None:
    """Give this application its own scopes in the shared ``rate_limit_bucket`` table.

    Not tidiness, and not optional. The rate caps are rows in PostgreSQL now
    (``infra/rate_limits.py``), written on a connection of the limiter's own and
    committed there, precisely so the caller's rollback cannot reach them -- which
    is exactly why they also survive the test that wrote them. The buckets keyed
    on a household are unique by accident, since every test makes a new
    household; the ones keyed on the *source address* are not, and every test in
    the suite arrives from the same one. Without this, five registrations spread
    over five unrelated tests would exhaust one hourly budget and the sixth test
    would fail on whatever it happened to be checking.

    A prefix rather than a truncation between tests, and rather than a rollback:
    both of those would undo the property the class exists for. Call this on any
    application a test drives over HTTP.

    Short on purpose: ``rate_limit_bucket.scope`` is 48 characters
    (:data:`chaudron.infra.rate_limits.MAX_SCOPE_LENGTH`), and the longest scope
    it has to leave room for is ``login_attempts_by_account`` under a further
    per-file segment. Forty-eight bits of randomness is far more than a test run
    needs to avoid a collision.
    """
    prefix = f"t{uuid.uuid4().hex[:12]}:"
    app.state.rate_limit_scope_prefix = prefix
    app.state.throttles = build_throttles(
        app.state.settings, app.state.database.limiter_engine, scope_prefix=prefix
    )


@pytest.fixture
def api_app(initialised_database: str, db_session: AsyncSession) -> Iterator[FastAPI]:
    """The real application, with only its database session replaced.

    Exactly one dependency is overridden, and it is the transaction. Neither
    authentication nor household resolution is: ADR-0006 requires the tenant to
    be derived server-side, and a fixture that injected ``household_id`` -- or a
    :class:`Principal` -- directly would bypass the one piece of code the
    isolation and authentication tests exist to exercise. The suite signs in with
    a real cookie against a real session row (:func:`signed_in`) and gets the real
    ``401`` when it does not.

    Overriding the session also gives every request the test's transaction, so
    what a handler writes is rolled back with the test.
    """
    app = create_app(build_test_settings(initialised_database))
    isolate_rate_limit_buckets(app)
    # Not a behaviour change: the same class the factory just built, shared so
    # that the placeholder Argon2 digest its constructor computes is paid once
    # for the run rather than once per test.
    app.state.passwords = _shared_passwords()

    async def _use_test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    yield app
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# The signed-in caller
# --------------------------------------------------------------------------- #

#: The password behind :func:`signed_in_user`. Long enough to satisfy
#: ``MIN_PASSWORD_LENGTH``, and a constant rather than a fixture argument so a
#: test that wants to sign in over HTTP can quote it.
TEST_PASSWORD: Final = "correct-horse-battery-staple"


@pytest.fixture
def passwords() -> Passwords:
    """One hasher for the whole session: constructing one costs a full Argon2 hash."""
    return _shared_passwords()


@pytest.fixture
async def signed_in_user(db_session: AsyncSession, passwords: Passwords) -> UserAccount:
    """The account every authenticated fixture below belongs to."""
    suffix = _unique_suffix()
    user = UserAccount(
        email=f"signed-in-{suffix}@example.test",
        display_name=f"Signed In {suffix}",
        password_hash=passwords.hash(TEST_PASSWORD),
    )
    db_session.add(user)
    await db_session.flush()
    return user


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A live session for :func:`signed_in_user`: the cookie and the CSRF token."""

    user: UserAccount
    token: str
    csrf_token: str


@pytest.fixture
async def signed_in(db_session: AsyncSession, signed_in_user: UserAccount) -> SignedIn:
    """Insert a real ``user_session`` row and hand back the credentials for it.

    Built by writing the row rather than by calling ``POST /v1/auth/login``,
    for one reason: a fixture that issued an HTTP request would make every test
    in the suite depend on the login endpoint working, and the failure of one
    would be reported as the failure of all of them. The *login endpoint itself*
    is tested against the real thing in ``tests/api/test_authentication.py``,
    including that the cookie it sets is accepted by this same code path.
    """
    token = new_token()
    now = datetime.now(UTC)
    record = UserSession(
        user_id=signed_in_user.id,
        token_hash=hash_token(token),
        csrf_token=new_token(),
        expires_at=now + timedelta(days=30),
        idle_expires_at=now + timedelta(days=7),
    )
    db_session.add(record)
    await db_session.flush()
    return SignedIn(user=signed_in_user, token=token, csrf_token=record.csrf_token)


@pytest.fixture
async def api_client(api_app: FastAPI, signed_in: SignedIn) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client speaking to the application in-process, signed in.

    Two details are not incidental.

    ``https://testserver`` -- the session cookie is ``Secure`` and
    ``__Host-``-prefixed, and ``http.cookiejar`` (which httpx uses) refuses to
    send a ``Secure`` cookie over ``http``. On an ASGI transport the scheme costs
    nothing and buys a client that exercises the cookie exactly as a browser
    would, ``Set-Cookie`` from a login included.

    The CSRF token is a **default header**, so it rides on every request
    including the safe ones, where it is ignored. That is what keeps the three
    hundred existing tests unchanged. The tests that prove the check actually
    refuses something build their own client without it
    (``tests/api/test_authentication.py``).
    """
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: signed_in.csrf_token},
    ) as client:
        yield client
    # The factory builds these eagerly; the lifespan that would normally release
    # them does not run under an ASGI transport.
    await api_app.state.catalog.aclose()
    await api_app.state.database.dispose()


@pytest.fixture
async def anonymous_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The same application, with no cookie and no CSRF token.

    What an unauthenticated stranger sees, and the client the sign-in and
    sign-up tests drive.
    """
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client
    await api_app.state.catalog.aclose()
    await api_app.state.database.dispose()


def household_headers(household: Household) -> dict[str, str]:
    """Select which of the caller's households a request addresses.

    Still ``X-Household-Id``, and no longer a credential: since authentication
    landed the header is accepted only when the signed-in account is a member of
    the household it names (``api/deps.py``). Tests keep sending it because a
    household the caller belongs to is exactly what it selects.
    """
    return {"X-Household-Id": str(household.id)}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

# Requesting any of these means the test needs a live PostgreSQL.
_DATABASE_FIXTURES: Final = frozenset(
    {
        "postgres_url",
        "initialised_database",
        "engine",
        "db_session",
        "make_household",
        "make_user",
        "make_member",
        "tenant_pair",
        "api_app",
        "api_client",
        "anonymous_client",
        "signed_in",
        "signed_in_user",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark database-backed tests ``integration`` automatically.

    A marker applied by hand is a marker forgotten by hand: `-m "not integration"`
    would then run tests that hang waiting for a database. Deriving it from the
    requested fixtures makes the marker impossible to desynchronise.
    """
    for item in items:
        if not isinstance(item, pytest.Function):
            continue
        if _DATABASE_FIXTURES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.integration)
