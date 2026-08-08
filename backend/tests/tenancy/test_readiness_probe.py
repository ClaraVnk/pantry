"""The row-level security probe, and the two places that now act on its answer.

``Database.check_row_level_security`` was written for a readiness probe and then
called by nobody, which made it a comment with a docstring. This file is what
makes it a control: it drives the probe against both roles the suite can provide
-- the table owner, which bypasses every policy, and the provisioned application
role, which does not -- and asserts that the difference reaches ``/readyz`` and,
in production, stops the process from starting.

Why it matters more than its size suggests: an instance whose
``CHAUDRON_DATABASE_URL`` names the owner answers every request correctly, passes
every functional test, and isolates nothing. Migration ``0004`` deliberately does
not set ``FORCE ROW LEVEL SECURITY``, so the owner is exempt from its own
policies. There is no request whose answer differs, which is precisely why the
only thing that can catch it is a probe that asks the catalogue directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr

from chaudron.api.main import (
    RowLevelSecurityNotEnforcedError,
    create_app,
    verify_row_level_security,
)
from chaudron.config import Settings
from chaudron.infra.db import Database
from tests.conftest import build_test_settings

pytestmark = pytest.mark.integration


def _settings(database_url: str, *, production: bool = False, env: str | None = None) -> Settings:
    base = build_test_settings(database_url)
    chosen = env if env is not None else ("production" if production else None)
    if chosen is None:
        return base
    # `https` and a non-DEBUG level are what the production validators demand;
    # neither is what this file is about. Applied to `staging` too, harmlessly.
    return base.model_copy(update={"env": chosen, "base_url": "https://chaudron.test"})


@asynccontextmanager
async def _database(database_url: str, *, production: bool = False) -> AsyncIterator[Database]:
    database = Database(_settings(database_url, production=production))
    try:
        yield database
    finally:
        await database.dispose()


async def _readyz(settings: Settings) -> httpx.Response:
    """``/readyz`` on a real application built from *settings*.

    No dependency override: the point is the database this configuration would
    actually connect to, so the fixture session -- which is the owner's -- must
    not be substituted in.
    """
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/readyz")
    finally:
        await app.state.catalog.aclose()
        await app.state.database.dispose()


# --------------------------------------------------------------------------- #
# The probe itself
# --------------------------------------------------------------------------- #


async def test_the_owner_is_reported_as_bypassing_every_policy(
    initialised_database: str,
) -> None:
    """The misconfiguration with no symptom, given a symptom."""
    async with _database(initialised_database) as database:
        report = await database.check_row_level_security()

    assert not report.is_enforced
    assert report.problems
    assert any("bypass" in problem for problem in report.problems)


async def test_the_application_role_is_reported_as_subject_to_the_policies(
    app_role_url: str,
) -> None:
    """The other half: without it, a probe that always says "bypassed" would pass.

    ``scripts/provision_app_role.py`` produces this role, so a drift between the
    documented procedure and what the probe accepts fails here.
    """
    async with _database(app_role_url) as database:
        report = await database.check_row_level_security()

    assert report.is_enforced, report.problems
    assert report.problems == ()


# --------------------------------------------------------------------------- #
# /readyz
# --------------------------------------------------------------------------- #


async def test_readyz_reports_the_policies_as_in_force(app_role_url: str) -> None:
    response = await _readyz(_settings(app_role_url))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "row_level_security": "enforced"},
    }


async def test_readyz_names_the_bypass_outside_production(initialised_database: str) -> None:
    """Reported, not refused: the suite and every developer connect as the owner.

    A 503 here would be a red light nobody could act on, and a red light nobody
    can act on is one people stop reading -- including on the deployment where it
    means something.
    """
    response = await _readyz(_settings(initialised_database))

    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "bypassed"


async def test_readyz_refuses_readiness_in_production_when_the_policies_are_bypassed(
    initialised_database: str,
) -> None:
    response = await _readyz(_settings(initialised_database, production=True))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "row_level_security": "bypassed"},
    }


async def test_readyz_refuses_readiness_in_staging_when_the_policies_are_bypassed(
    initialised_database: str,
) -> None:
    """The one this file did not assert, and the one that shipped (audit AUD-029).

    ``/readyz`` used to refuse only when ``env == "production"`` exactly, so a
    staging instance pointed at the owner DSN isolated nothing between households,
    reported ``row_level_security: bypassed`` in its own body, and answered
    ``200`` -- which is what a load balancer reads. The project already makes this
    argument for ``/docs`` two properties away in ``config.py``: *a staging
    instance carries real data far more often than anyone admits*. It had simply
    not been applied here.
    """
    response = await _readyz(_settings(initialised_database, env="staging"))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "row_level_security": "bypassed"},
    }


async def test_readyz_is_ready_in_staging_when_the_policies_apply(app_role_url: str) -> None:
    """The other half: staging is refused for the bypass, not for being staging."""
    response = await _readyz(_settings(app_role_url, env="staging"))

    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "enforced"


async def test_a_staging_instance_refuses_to_start_without_enforcement(
    initialised_database: str,
) -> None:
    """Startup takes the same decision as the probe, from the same property.

    Two places used to read ``is_production`` and they had to move together: an
    instance that booted and then never became ready would be a restart loop with
    extra steps, and one that became ready without booting is impossible.
    """
    settings = _settings(initialised_database, env="staging")
    async with _database(initialised_database) as database:
        with pytest.raises(RowLevelSecurityNotEnforcedError):
            await verify_row_level_security(settings, database)


@pytest.mark.parametrize("env", ["local", "ci"])
async def test_the_two_deliberate_environments_still_start_and_report(
    initialised_database: str, env: str
) -> None:
    """``local`` and ``ci`` connect as the owner on purpose and must not be stopped.

    A developer runs migrations and the API from one DSN; ``tests/tenancy`` needs
    the owner *and* the provisioned role side by side to prove the policies apply
    to one and not the other. A 503 there would be a permanent red light nobody
    could act on, and a permanent red light is one people stop reading.
    """
    settings = _settings(initialised_database, env=env)
    async with _database(initialised_database) as database:
        await verify_row_level_security(settings, database)

    response = await _readyz(settings)
    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "bypassed"


async def test_readyz_says_nothing_about_roles_or_tables(initialised_database: str) -> None:
    """The reasons name the role and the tables; the body is read by strangers."""
    response = await _readyz(_settings(initialised_database, production=True))

    body = response.text
    assert "postgres" not in body
    assert "inventory_lot" not in body


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


async def test_a_production_instance_refuses_to_start_without_enforcement(
    initialised_database: str,
) -> None:
    settings = _settings(initialised_database, production=True)
    async with _database(initialised_database, production=True) as database:
        with pytest.raises(RowLevelSecurityNotEnforcedError) as raised:
            await verify_row_level_security(settings, database)

    assert "CHAUDRON_DATABASE_URL" in str(raised.value)


async def test_a_production_instance_starts_when_the_policies_apply(app_role_url: str) -> None:
    settings = _settings(app_role_url, production=True)
    async with _database(app_role_url, production=True) as database:
        await verify_row_level_security(settings, database)


async def test_a_non_production_instance_starts_and_says_so(initialised_database: str) -> None:
    """A developer connecting as the owner is not stopped from working."""
    settings = _settings(initialised_database)
    async with _database(initialised_database) as database:
        await verify_row_level_security(settings, database)


async def test_a_database_that_cannot_be_reached_does_not_stop_a_production_start() -> None:
    """ "Unable to verify" and "verified insecure" are different facts.

    Refusing to boot on the first turns a database thirty seconds late into a
    restart loop. The instance still never becomes *ready*, because ``/readyz``
    runs the same probe on every poll.
    """
    settings = build_test_settings("postgresql+asyncpg://nobody@127.0.0.1:1/none").model_copy(
        update={
            "env": "production",
            "base_url": "https://chaudron.test",
            "database_url": SecretStr("postgresql+asyncpg://nobody@127.0.0.1:1/none"),
        }
    )
    database = Database(settings)
    try:
        await verify_row_level_security(settings, database)
    finally:
        await database.dispose()
