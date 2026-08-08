"""Migration ``0017``: only a transaction scoped to a household may erase it.

``tests/api/test_privacy.py`` proves the endpoint erases the right household. This
file proves the property that survives the endpoint being wrong: that a
``DELETE FROM household`` issued by the application role removes nothing unless
the transaction has already posted that household as its tenant.

The distinction matters because the two failures look nothing alike. A handler
that passes the wrong identifier is a bug somebody eventually notices; a handler
that passes the wrong identifier *and deletes another family's household* is not
recoverable. Revision ``0004`` made that argument for the thirteen tables carrying
``household_id`` and left ``household`` out, correctly, because nothing wrote to
it from a request. Revision ``0017`` is what changed when something did.

Everything here runs as ``chaudron_app`` -- the role that owns nothing, provisioned
by ``scripts/provision_app_role.py`` through ``tests/tenancy/conftest.py``. The rest
of the suite connects as the table owner, which bypasses every policy: correct for
it, and useless here.
"""

from __future__ import annotations

from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from chaudron.infra.db import TENANT_SETTING
from tests.tenancy.conftest import TenantRows

pytestmark = pytest.mark.integration

_HOUSEHOLD_COUNT: Final = sa.text("SELECT count(*) FROM household WHERE id = :household")
_DELETE_HOUSEHOLD: Final = sa.text("DELETE FROM household WHERE id = :household")
_POST_TENANT: Final = sa.text(f"SELECT set_config('{TENANT_SETTING}', :household, true)")


async def test_an_unscoped_transaction_erases_no_household(
    app_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """No tenant posted, no household deleted -- the background-job case.

    ``chaudron_current_household()`` returns NULL, the comparison is NULL, and a
    policy shows no row for it. So a script, a worker or a migration helper running
    outside a request cannot delete a household even holding ``DELETE`` on the
    table, which the application role does.
    """
    async with app_engine.begin() as connection:
        await connection.execute(_DELETE_HOUSEHOLD, {"household": tenant_rows.household_a})

    async with app_engine.connect() as connection:
        await connection.execute(_POST_TENANT, {"household": str(tenant_rows.household_a)})
        survived = await connection.scalar(_HOUSEHOLD_COUNT, {"household": tenant_rows.household_a})
    assert survived == 1, "an unscoped transaction deleted a household"


async def test_a_scoped_transaction_erases_only_its_own_household(
    app_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """The whole point: the identifier in the ``WHERE`` clause is not the authority.

    A single transaction is scoped to household A and asks to delete household B --
    which is what a handler with a swapped variable, a stale identifier or a
    forgotten filter would emit. The engine removes nothing.
    """
    async with app_engine.begin() as connection:
        await connection.execute(_POST_TENANT, {"household": str(tenant_rows.household_a)})
        await connection.execute(_DELETE_HOUSEHOLD, {"household": tenant_rows.household_b})

    async with app_engine.connect() as connection:
        await connection.execute(_POST_TENANT, {"household": str(tenant_rows.household_b)})
        survived = await connection.scalar(_HOUSEHOLD_COUNT, {"household": tenant_rows.household_b})
    assert survived == 1, "a household was erased by a transaction scoped to another"


async def test_a_scoped_transaction_does_erase_its_own_household(
    app_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """The other half. A policy that refused everybody would pass the two above.

    And it checks the cascade at the same time, from the role the application
    actually connects as: referential integrity runs as the table owner and is not
    subject to row-level security, so the children go even though the deleting role
    could not have selected half of them.
    """
    async with app_engine.begin() as connection:
        await connection.execute(_POST_TENANT, {"household": str(tenant_rows.household_a)})
        await connection.execute(_DELETE_HOUSEHOLD, {"household": tenant_rows.household_a})

    async with app_engine.connect() as connection:
        await connection.execute(_POST_TENANT, {"household": str(tenant_rows.household_a)})
        gone = await connection.scalar(_HOUSEHOLD_COUNT, {"household": tenant_rows.household_a})
        orphans = await connection.scalar(
            sa.text("SELECT count(*) FROM inventory_lot WHERE household_id = :household"),
            {"household": tenant_rows.household_a},
        )
    assert gone == 0, "a household scoped to itself was not erased"
    assert orphans == 0, "the cascade left rows behind"


async def test_reading_a_household_needs_no_tenant(
    app_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """The three permissive policies are load-bearing, not decoration.

    Authentication resolves a caller's memberships and the CalDAV feed scans for a
    credential to match, both **before** any tenant has been posted. Enabling
    row-level security to constrain ``DELETE`` without them would have denied every
    ``SELECT`` too, and the first symptom would have been a login reporting no
    households.
    """
    async with app_engine.connect() as connection:
        visible = await connection.scalar(
            sa.text("SELECT count(*) FROM household WHERE id IN (:a, :b)"),
            {"a": tenant_rows.household_a, "b": tenant_rows.household_b},
        )
    assert visible == 2, "an unscoped read of household came back empty"


async def test_the_policies_are_the_ones_the_migration_declared(app_engine: AsyncEngine) -> None:
    """A guard on the guard: the assertions above pass on an unprotected table too.

    If ``ENABLE ROW LEVEL SECURITY`` were ever dropped from ``household``, the
    delete-scoped tests would still pass -- the application role would simply be
    allowed to delete anything, and the two households the fixture creates are the
    only ones it would try. This is the assertion that fails in that case.
    """
    async with app_engine.connect() as connection:
        enabled = await connection.scalar(
            sa.text("SELECT relrowsecurity FROM pg_class WHERE relname = 'household'")
        )
        commands = (
            await connection.scalars(
                # ``::text`` because ``polcmd`` is PostgreSQL's internal ``"char"``,
                # which asyncpg hands back as a one-byte ``bytes`` object.
                sa.text(
                    "SELECT polcmd::text FROM pg_policy "
                    "WHERE polrelid = 'household'::regclass ORDER BY polcmd"
                )
            )
        ).all()
    assert enabled is True, "row-level security is off on household (migration 0017)"
    # `a` INSERT, `d` DELETE, `r` SELECT, `w` UPDATE -- one policy each, and the
    # DELETE one is the only restricted member of the set.
    assert sorted(commands) == ["a", "d", "r", "w"], commands
