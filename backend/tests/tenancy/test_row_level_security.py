"""Isolation enforced by PostgreSQL, not by the application (SEC-001, AUD-002).

The distinction this file exists to make is the whole point of migration
``0004``. ``tests/api/test_tenant_isolation.py`` proves that the *handlers*
filter by household; it would keep passing on the day someone adds a repository
method without a ``WHERE``. Everything below issues SQL with **no tenant
predicate at all** and requires zero rows -- so what is being tested is the
database's refusal, and the failure mode being covered is the query nobody
reviewed.

Every test is parameterised over the thirteen tables read from
``Base.metadata``, in the same spirit as ``test_schema_tenant_guard.py``: the
table somebody adds next month is covered before anybody remembers it exists.
"""

from __future__ import annotations

import uuid
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy import Table
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from chaudron.domain.models import Base, ProductSource
from chaudron.infra.db import TENANT_SETTING
from tests.tenancy.conftest import PROTECTED_TABLES, TenantRows

pytestmark = pytest.mark.integration

_TABLE_IDS = [table.name for table in PROTECTED_TABLES]

_PRODUCT: Table = Base.metadata.tables["product"]


def _public_product(product_id: uuid.UUID) -> sa.Insert:
    """A catalogue entry belonging to nobody -- the shared barcode cache."""
    return sa.insert(_PRODUCT).values(
        id=product_id,
        household_id=None,
        name="Public entry",
        source=ProductSource.OPEN_FOOD_FACTS,
    )


def _post_tenant(household_id: uuid.UUID) -> sa.Select[tuple[str]]:
    """``SET LOCAL``, with a bind parameter -- which ``SET`` itself does not take."""
    return sa.select(sa.func.set_config(TENANT_SETTING, str(household_id), True))


# --------------------------------------------------------------------------- #
# The headline: no tenant, no rows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_unscoped_query_returns_nothing(
    app_engine: AsyncEngine, tenant_rows: TenantRows, table: Table
) -> None:
    """A ``SELECT`` with no ``WHERE`` at all, and the answer is still empty.

    The rows exist -- ``test_owner_sees_every_row`` reads the same tables through
    the owner and finds them. What changes is who is asking.
    """
    async with app_engine.connect() as connection:
        rows = (await connection.execute(sa.select(table))).all()
    assert rows == [], (
        f"{table.name} returned {len(rows)} row(s) to a connection that never named a "
        f"household. Either the policy is missing, or this role bypasses it."
    )


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_scoped_query_returns_only_the_current_household(
    app_engine: AsyncEngine, tenant_rows: TenantRows, table: Table
) -> None:
    """With a tenant posted, the unfiltered query returns that tenant and no other.

    ``product`` is allowed one extra row: the mutualised public catalogue is
    visible to every scoped household by design (ADR-0008), and the assertion
    below is on the *other household's* rows, which is the property that matters.
    """
    async with app_engine.connect() as connection:
        await connection.execute(_post_tenant(tenant_rows.household_a))
        foreign = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.household_id == tenant_rows.household_b)
        )
        own = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.household_id == tenant_rows.household_a)
        )
    assert foreign == 0, f"{table.name} disclosed {foreign} row(s) of another household"
    assert own == 1, f"{table.name} hid the current household's own row ({own} found)"


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_owner_still_sees_everything(
    owner_engine: AsyncEngine, tenant_rows: TenantRows, table: Table
) -> None:
    """The rows are there; the owner is exempt, and that is a documented decision.

    This test is the control for the previous two -- without it, an empty table
    would make them pass for the wrong reason -- and it pins the decision
    migration ``0004`` argues: no ``FORCE ROW LEVEL SECURITY``, because the owner
    is Alembic, ``seed.py`` and the DBA. If that ever changes, this test is the
    one that says so.
    """
    async with owner_engine.connect() as connection:
        total = await connection.scalar(sa.select(sa.func.count()).select_from(table))
    assert total is not None and total >= 2


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_cross_household_update_changes_nothing(
    app_engine: AsyncEngine, tenant_rows: TenantRows, table: Table
) -> None:
    """Naming another household's row explicitly does not make it writable.

    ``UPDATE`` under RLS filters before it writes, so the statement succeeds and
    touches zero rows. Silent, which is why it is asserted rather than assumed.
    """
    async with app_engine.begin() as connection:
        await connection.execute(_post_tenant(tenant_rows.household_a))
        result = await connection.execute(
            sa.update(table)
            .where(table.c.household_id == tenant_rows.household_b)
            .values(household_id=tenant_rows.household_b)
        )
        assert result.rowcount == 0


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_cross_household_delete_removes_nothing(
    app_engine: AsyncEngine, tenant_rows: TenantRows, table: Table
) -> None:
    async with app_engine.begin() as connection:
        await connection.execute(_post_tenant(tenant_rows.household_a))
        result = await connection.execute(
            sa.delete(table).where(table.c.household_id == tenant_rows.household_b)
        )
        assert result.rowcount == 0


async def test_insert_into_another_household_is_refused(
    app_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """``WITH CHECK`` rejects the write outright -- loudly, unlike a filtered read."""
    locations = Base.metadata.tables["storage_location"]
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with app_engine.begin() as connection:
            await connection.execute(_post_tenant(tenant_rows.household_a))
            await connection.execute(
                sa.insert(locations).values(
                    id=uuid.uuid7(),
                    household_id=tenant_rows.household_b,
                    name="planted",
                    kind="other",
                )
            )


async def test_insert_without_a_tenant_is_refused(app_engine: AsyncEngine) -> None:
    """No household posted means no household to write for. Not a default, an error."""
    locations = Base.metadata.tables["storage_location"]
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with app_engine.begin() as connection:
            await connection.execute(
                sa.insert(locations).values(
                    id=uuid.uuid7(),
                    household_id=uuid.uuid7(),
                    name="planted",
                    kind="other",
                )
            )


# --------------------------------------------------------------------------- #
# The public catalogue: the one nullable tenant column in the schema
# --------------------------------------------------------------------------- #


async def test_public_catalogue_is_readable_by_any_scoped_household(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """``product.household_id IS NULL`` is shared, and stays shared under RLS.

    The subtle half of this migration. A policy written as
    ``household_id = current_household()`` would have made the barcode cache
    invisible to everyone, and the first symptom would have been an Open Food
    Facts call per household per scan -- a rate-limit problem, not an obviously
    security-shaped one.
    """
    public_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(_public_product(public_id))
    try:
        async with app_engine.connect() as connection:
            await connection.execute(_post_tenant(tenant_rows.household_b))
            visible = await connection.scalar(
                sa.select(sa.func.count()).select_from(products).where(products.c.id == public_id)
            )
        assert visible == 1
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == public_id))


async def test_public_catalogue_is_invisible_without_a_tenant(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    """Shared is not the same as unauthenticated.

    Written as a separate test because the temptation to drop the
    ``current_household() IS NOT NULL`` conjunct from the policy is real -- it
    reads as redundant. It is what makes ``product`` obey the same rule as the
    other twelve tables: no tenant, no rows.
    """
    public_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(_public_product(public_id))
    try:
        async with app_engine.connect() as connection:
            visible = await connection.scalar(
                sa.select(sa.func.count()).select_from(products).where(products.c.id == public_id)
            )
        assert visible == 0
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == public_id))


async def test_public_catalogue_cannot_be_deleted_by_a_household(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """Any household may refresh the shared cache; none may empty it."""
    public_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(_public_product(public_id))
    try:
        async with app_engine.begin() as connection:
            await connection.execute(_post_tenant(tenant_rows.household_a))
            result = await connection.execute(sa.delete(products).where(products.c.id == public_id))
        assert result.rowcount == 0
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == public_id))


async def test_a_household_cannot_claim_a_row_out_of_the_shared_catalogue(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """One statement used to take a catalogue entry away from everybody else.

    ``UPDATE product SET household_id = <me> WHERE household_id IS NULL`` passed
    both halves of the ``UPDATE`` policy: the old row is public, so ``USING``
    allowed it, and the new row is the household's own, so ``WITH CHECK`` allowed
    it too. The entry then stopped existing for every other household -- and for
    ``upsert_public``, which would refetch and re-insert it, so the symptom was a
    duplicate rather than an error.

    Narrowing ``WITH CHECK`` to ``household_id = chaudron_current_household()`` is
    the obvious repair and it is not one: that predicate *describes the claim*.
    Revision ``0015`` states the rule where a rule about a transition can be
    stated, in a ``BEFORE UPDATE`` trigger, so this raises rather than reporting
    zero rows.
    """
    public_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(_public_product(public_id))
    try:
        with pytest.raises(DBAPIError, match="immutable"):
            async with app_engine.begin() as connection:
                await connection.execute(_post_tenant(tenant_rows.household_a))
                await connection.execute(
                    sa.update(products)
                    .where(products.c.id == public_id)
                    .values(household_id=tenant_rows.household_a)
                )

        async with owner_engine.connect() as connection:
            owner = await connection.scalar(
                sa.select(products.c.household_id).where(products.c.id == public_id)
            )
        assert owner is None, "the catalogue entry is still shared"
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == public_id))


async def test_a_household_cannot_push_a_row_of_its_own_into_the_shared_catalogue(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """The same hole read backwards, and closed by the same rule.

    A household publishing one of its private entries writes a row into a table
    every other household reads, without any of them asking for it.
    """
    private_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(
            sa.insert(products).values(
                id=private_id,
                household_id=tenant_rows.household_a,
                name="Private entry",
                source=ProductSource.MANUAL,
            )
        )
    try:
        with pytest.raises(DBAPIError, match="immutable"):
            async with app_engine.begin() as connection:
                await connection.execute(_post_tenant(tenant_rows.household_a))
                await connection.execute(
                    sa.update(products).where(products.c.id == private_id).values(household_id=None)
                )
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == private_id))


async def test_the_shared_cache_can_still_be_refreshed_by_any_household(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, tenant_rows: TenantRows
) -> None:
    """The control, and the reason the repair is a trigger rather than a policy.

    ``ProductRepository.upsert_public`` writes back a row whose ``household_id``
    is still ``NULL``; that is the shared cache of ADR-0008 and it runs on every
    barcode scan of an already-known product. Under the narrowed ``WITH CHECK``
    this statement fails with ``new row violates row-level security policy``,
    which is a 500 on a scan -- measured, not assumed, which is why it is asserted
    here from the application role rather than the owner.
    """
    public_id = uuid.uuid7()
    products = _PRODUCT
    async with owner_engine.begin() as connection:
        await connection.execute(_public_product(public_id))
    try:
        async with app_engine.begin() as connection:
            await connection.execute(_post_tenant(tenant_rows.household_b))
            result = await connection.execute(
                sa.update(products)
                .where(products.c.id == public_id)
                .values(name="Refreshed from Open Food Facts")
            )
        assert result.rowcount == 1

        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(products.c.name, products.c.household_id).where(
                        products.c.id == public_id
                    )
                )
            ).one()
        assert row.name == "Refreshed from Open Food Facts"
        assert row.household_id is None
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(sa.delete(products).where(products.c.id == public_id))


# --------------------------------------------------------------------------- #
# Origin references across a household boundary
# --------------------------------------------------------------------------- #
#
# Referential integrity is evaluated in triggers that run as the table owner and
# are NOT subject to row-level security, so a policy that hides household B's rows
# from household A does nothing to stop household A's row from *pointing at* one.
# The only thing that refuses it is a composite foreign key, which revision `0015`
# gave the last three references that lacked one.

#: ``(child table, column, parent table)`` for each reference made composite.
_ORIGIN_REFERENCES: Final = (
    ("inventory_lot", "source_receipt_line_id", "receipt_line"),
    ("shopping_list_item", "origin_recipe_suggestion_id", "recipe_suggestion"),
    ("stock_movement", "recipe_suggestion_id", "recipe_suggestion"),
)


@pytest.mark.parametrize(
    ("child", "column", "parent"),
    _ORIGIN_REFERENCES,
    ids=[f"{child}.{column}" for child, column, _ in _ORIGIN_REFERENCES],
)
async def test_an_origin_reference_cannot_name_another_households_row(
    owner_engine: AsyncEngine, tenant_rows: TenantRows, child: str, column: str, parent: str
) -> None:
    """Issued as the **owner**, which bypasses every policy -- on purpose.

    The claim is about the schema, not about the policies. If this passes as the
    identity that is exempt from row-level security, it holds for every identity.
    """
    child_table = Base.metadata.tables[child]
    foreign_row = tenant_rows.row_id(parent, tenant_rows.household_b)

    async with owner_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                sa.update(child_table)
                .where(child_table.c.household_id == tenant_rows.household_a)
                .values(**{column: foreign_row})
            )


@pytest.mark.parametrize(
    ("child", "column", "parent"),
    _ORIGIN_REFERENCES,
    ids=[f"{child}.{column}" for child, column, _ in _ORIGIN_REFERENCES],
)
async def test_an_origin_reference_still_accepts_its_own_households_row(
    owner_engine: AsyncEngine, tenant_rows: TenantRows, child: str, column: str, parent: str
) -> None:
    """The control: a constraint that refused everything would pass the test above."""
    child_table = Base.metadata.tables[child]
    own_row = tenant_rows.row_id(parent, tenant_rows.household_a)

    async with owner_engine.begin() as connection:
        result = await connection.execute(
            sa.update(child_table)
            .where(child_table.c.household_id == tenant_rows.household_a)
            .values(**{column: own_row})
        )
        assert result.rowcount >= 1
        await connection.rollback()


@pytest.mark.parametrize(
    ("child", "column", "parent"),
    _ORIGIN_REFERENCES,
    ids=[f"{child}.{column}" for child, column, _ in _ORIGIN_REFERENCES],
)
async def test_deleting_the_parent_forgets_the_origin_without_nulling_the_tenant(
    owner_engine: AsyncEngine, tenant_rows: TenantRows, child: str, column: str, parent: str
) -> None:
    """``ON DELETE SET NULL (column)``, and why the column list is not decoration.

    A bare ``SET NULL`` on a composite key nulls every column of it, including
    ``household_id`` -- which is ``NOT NULL`` here, so deleting a receipt would
    have failed outright instead of forgetting where a lot came from.
    """
    child_table = Base.metadata.tables[child]
    parent_table = Base.metadata.tables[parent]
    own_row = tenant_rows.row_id(parent, tenant_rows.household_a)

    async with owner_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                sa.update(child_table)
                .where(child_table.c.household_id == tenant_rows.household_a)
                .values(**{column: own_row})
            )
            await connection.execute(sa.delete(parent_table).where(parent_table.c.id == own_row))
            rows = (
                await connection.execute(
                    sa.select(child_table.c.household_id, child_table.c[column]).where(
                        child_table.c.household_id == tenant_rows.household_a
                    )
                )
            ).all()
            assert rows, "the child row survived the parent's deletion"
            for row in rows:
                assert row[0] == tenant_rows.household_a, "the tenant column was nulled"
                assert row[1] is None, "the origin was not forgotten"
        finally:
            await transaction.rollback()


# --------------------------------------------------------------------------- #
# The schema itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", PROTECTED_TABLES, ids=_TABLE_IDS)
async def test_every_tenant_table_has_row_level_security_enabled(
    owner_engine: AsyncEngine, table: Table
) -> None:
    """Reads ``pg_class`` and ``pg_policies``: the query an operator runs by hand."""
    async with owner_engine.connect() as connection:
        enabled = await connection.scalar(
            sa.text("SELECT relrowsecurity FROM pg_class WHERE oid = cast(:name as regclass)"),
            {"name": table.name},
        )
        policies = await connection.scalar(
            sa.text("SELECT count(*) FROM pg_policies WHERE tablename = :name"),
            {"name": table.name},
        )
    assert enabled is True, f"{table.name} carries household_id but has RLS disabled"
    assert policies is not None and policies >= 1, f"{table.name} has RLS enabled but no policy"


async def test_the_application_role_owns_nothing_and_cannot_bypass(
    app_engine: AsyncEngine,
) -> None:
    """The three exemptions PostgreSQL grants, all checked on the real role.

    A policy is only worth what the connecting role makes it worth: superuser,
    ``BYPASSRLS`` and ownership each turn every ``CREATE POLICY`` in migration
    ``0004`` into decoration.
    """
    async with app_engine.connect() as connection:
        attributes = (
            await connection.execute(
                sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        owned = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT c.relname FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND pg_catalog.pg_get_userbyid(c.relowner) = current_user
                    """
                )
            )
        ).all()
    assert attributes.rolsuper is False
    assert attributes.rolbypassrls is False
    assert list(owned) == []
