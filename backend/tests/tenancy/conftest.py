"""Fixtures for the row-level security tests: a non-owner role and real rows.

Everything here exists to make one assertion possible: that a query issued
without a tenant returns nothing **because PostgreSQL refused it**, not because
the application remembered to filter. Proving that needs three things the rest
of the suite deliberately does not provide.

*A role that is not the table owner.* An owner bypasses its own policies unless
``FORCE ROW LEVEL SECURITY`` is set, and migration ``0004`` explains at length
why it is not. The rest of the suite connects as the owner -- which is correct
for it, and useless here. The role is provisioned by calling
``scripts/provision_app_role.py``, so a drift between the documented procedure
and what the tests assume fails the build.

*Committed rows.* ``db_session`` hands every test a transaction that is rolled
back, and nothing inside an uncommitted transaction is visible to the second
connection these tests need. The fixture therefore commits, and deletes the two
households at teardown -- ``ON DELETE CASCADE`` from ``household`` takes the rest.

*A row in every tenant table.* An isolation test that runs against an empty
table passes for the wrong reason. Rows are generated from ``Base.metadata``
rather than hand-written per table, for the same reason
``test_schema_tenant_guard.py`` reads the metadata: the table nobody remembers
to add to the fixture is exactly the table the leak comes from.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from scripts.provision_app_role import provision
from sqlalchemy import URL, Table, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from chaudron.domain.models import Base

#: Not a secret: the role only ever exists inside a throwaway test database, and
#: a value read from the environment would make the suite depend on it.
_APP_ROLE_PASSWORD: Final = "rls-test-role-password"
_APP_ROLE: Final = "chaudron_app"

_TENANT_COLUMN: Final = "household_id"

#: Tables the policies protect, straight from the model. Ordered as
#: ``sorted_tables`` returns them, which is dependency order -- a parent row
#: always exists before the child that references it.
PROTECTED_TABLES: Final[tuple[Table, ...]] = tuple(
    table for table in Base.metadata.sorted_tables if _TENANT_COLUMN in table.c
)

#: Column values the generator cannot infer, because a ``CHECK`` constraint ties
#: several columns together. Each entry is a constraint, not a preference.
_COLUMN_OVERRIDES: Final[Mapping[str, Mapping[str, Any]]] = {
    # ck_llm_provider_config_mode_requirements: `byok` demands a ciphertext,
    # `ollama` a base URL; `instance_owner` is the one mode that needs neither.
    "llm_provider_config": {"mode": "instance_owner"},
    # ck_shopping_list_item_target_present: a line points at a product or carries
    # a label, and the generator leaves nullable foreign keys alone.
    "shopping_list_item": {"label": "row-level security fixture"},
    # ck_budget_target_currency_iso_4217: the generator's synthetic string is
    # not three upper-case letters. Not this table's test, but the fixture
    # covers every tenant table and one refusal fails all of them.
    "budget_target": {"currency": "CHF"},
    # ck_machine_token_scopes_present: a token with no scope can do nothing, so
    # the empty array the generator would produce is refused. `_synthesise` has
    # no rule for an array column either, which this override makes moot.
    "machine_token": {"scopes": ["inventory:read"]},
}


@dataclass(frozen=True, slots=True)
class TenantRows:
    """Two households, each owning exactly one row in every protected table."""

    household_a: uuid.UUID
    household_b: uuid.UUID
    #: ``(table name, household) -> primary key of the row created for it``.
    row_ids: Mapping[tuple[str, uuid.UUID], uuid.UUID]

    def row_id(self, table: str, household: uuid.UUID) -> uuid.UUID:
        return self.row_ids[table, household]


def _synthesise(column: sa.Column[Any]) -> Any:
    """A value of the right type for a ``NOT NULL`` column with no default.

    Deliberately minimal and constraint-friendly: ``1`` satisfies every
    ``> 0`` and ``BETWEEN 1 AND 5`` check in the schema, and a fresh UUID
    satisfies every uniqueness rule.
    """
    kind = column.type
    if isinstance(kind, sa.Enum):
        return kind.enums[0]
    if isinstance(kind, sa.Uuid):
        return uuid.uuid7()
    if isinstance(kind, sa.Boolean):
        return False
    if isinstance(kind, sa.Numeric) and not isinstance(kind, sa.Float):
        return Decimal("1")
    if isinstance(kind, sa.Integer):
        return 1
    if isinstance(kind, sa.Date) and not isinstance(kind, sa.DateTime):
        return dt.date(2026, 1, 1)
    if isinstance(kind, sa.DateTime):
        return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    if isinstance(kind, sa.LargeBinary):
        return b"rls"
    length = getattr(kind, "length", None)
    text = f"rls-{uuid.uuid7().hex}"
    return text[:length] if isinstance(length, int) else text


async def _borrow_parent(connection: AsyncConnection, table: Table, columns: list[str]) -> Any:
    """Values of *columns* from any existing row of *table*.

    For references to the global reference tables -- ``unit`` seeded by revision
    ``0002``, ``user_account`` created below -- where the composite key must be a
    pair the parent actually holds. ``(quantity_unit_code, quantity_dimension)``
    on ``inventory_lot`` is why this exists: two independently synthesised values
    would violate the foreign key nine times out of ten.
    """
    selection = sa.select(*(table.c[name] for name in columns)).limit(1)
    return (await connection.execute(selection)).one()


async def _insert_row(
    connection: AsyncConnection,
    table: Table,
    household_id: uuid.UUID,
    created: dict[tuple[str, uuid.UUID], uuid.UUID],
) -> uuid.UUID:
    """Insert one row of *table* belonging to *household_id*, and return its id."""
    values: dict[str, Any] = {_TENANT_COLUMN: household_id}
    # Generated here rather than left to the model's `default=uuid7`, because the
    # identifier has to be known to the fixture: children reference it, and the
    # tests address rows by it.
    if "id" in table.c:
        values["id"] = uuid.uuid7()

    for constraint in table.foreign_key_constraints:
        child = [column.name for column in constraint.columns]
        parent = constraint.referred_table
        if all(table.c[name].nullable for name in child):
            continue  # A nullable reference is left NULL: fewer moving parts.
        if parent.name == "household":
            continue  # Already pinned by `values[_TENANT_COLUMN]`.
        referred = [element.column.name for element in constraint.elements]
        if (parent.name, household_id) in created:
            known = created[parent.name, household_id]
            for child_name, parent_name in zip(child, referred, strict=True):
                values[child_name] = household_id if parent_name == _TENANT_COLUMN else known
            continue
        borrowed = await _borrow_parent(connection, parent, referred)
        for child_name, borrowed_value in zip(child, borrowed, strict=True):
            values[child_name] = borrowed_value

    for column in table.c:
        if column.name in values or column.nullable:
            continue
        if column.server_default is not None or column.default is not None:
            continue
        values[column.name] = _synthesise(column)

    values.update(_COLUMN_OVERRIDES.get(table.name, {}))
    await connection.execute(sa.insert(table).values(**values))
    identifier = values.get("id")
    return identifier if isinstance(identifier, uuid.UUID) else household_id


async def _seed(engine: AsyncEngine) -> TenantRows:
    household_a, household_b = uuid.uuid7(), uuid.uuid7()
    created: dict[tuple[str, uuid.UUID], uuid.UUID] = {}

    async with engine.begin() as connection:
        households = Base.metadata.tables["household"]
        users = Base.metadata.tables["user_account"]
        for index, household in enumerate((household_a, household_b)):
            await connection.execute(
                sa.insert(households).values(id=household, name=f"RLS fixture {index}")
            )
            await connection.execute(
                sa.insert(users).values(
                    id=uuid.uuid7(),
                    email=f"rls-{uuid.uuid7().hex}@example.test",
                    display_name="RLS fixture",
                )
            )
        for table in PROTECTED_TABLES:
            for household in (household_a, household_b):
                created[table.name, household] = await _insert_row(
                    connection, table, household, created
                )
    return TenantRows(household_a=household_a, household_b=household_b, row_ids=created)


async def _drop(engine: AsyncEngine, tenants: TenantRows) -> None:
    households = Base.metadata.tables["household"]
    async with engine.begin() as connection:
        await connection.execute(
            sa.delete(households).where(
                households.c.id.in_([tenants.household_a, tenants.household_b])
            )
        )


async def _provision(owner_url: URL, role: str, password: str) -> None:
    engine = create_async_engine(owner_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await provision(connection, role, password)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def app_role_url(initialised_database: str) -> str:
    """A DSN for a role that owns nothing and is therefore subject to the policies.

    Skips rather than fails when the connecting role cannot create one: a
    developer pointed at a managed database they do not administer should see the
    reason, not a red suite they cannot act on.
    """
    owner_url = make_url(initialised_database)
    try:
        asyncio.run(_provision(owner_url, _APP_ROLE, _APP_ROLE_PASSWORD))
    except SQLAlchemyError as exc:
        pytest.skip(f"cannot provision {_APP_ROLE} on this database: {exc}")
    return owner_url.set(username=_APP_ROLE, password=_APP_ROLE_PASSWORD).render_as_string(
        hide_password=False
    )


@pytest.fixture
async def tenant_rows(owner_engine: AsyncEngine) -> AsyncIterator[TenantRows]:
    """Two households with one committed row in each protected table.

    Function-scoped despite the cost of about sixty statements per test. These
    rows are *committed* -- they have to be, or the second connection would not
    see them -- so a longer-lived fixture would leave them behind for whichever
    test runs next, and ``tests/test_database_harness.py`` exists precisely to
    fail when something does that. Correct beats fast for a fixture whose whole
    job is to make an isolation claim believable.
    """
    tenants = await _seed(owner_engine)
    try:
        yield tenants
    finally:
        await _drop(owner_engine, tenants)


@pytest.fixture
async def app_engine(app_role_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine connecting as the application role, one connection at a time.

    ``pool_size=1`` with no overflow is not a performance choice: it guarantees
    that two consecutive transactions land on the *same* backend, which is what
    makes the ``SET LOCAL`` leak test meaningful.
    """
    engine = create_async_engine(app_role_url, pool_size=1, max_overflow=0, pool_pre_ping=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def owner_engine(initialised_database: str) -> AsyncIterator[AsyncEngine]:
    """An engine connecting as the table owner, which bypasses every policy."""
    engine = create_async_engine(initialised_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
