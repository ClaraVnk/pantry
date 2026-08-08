"""Schema-level guards on tenant isolation (ADR-0006, `docs/data-model.md` section 5).

These tests read ``Base.metadata``. They need no database, no application and no
fixtures, they run in milliseconds, and they cover *every* table -- including the
one somebody adds next month while this file is not being looked at.

That last property is the point. Isolation tests written per resource only cover
resources someone remembered to write a test for; the leak always comes from the
table nobody thought about. Here, a new business table without ``household_id``, or
a unique constraint that forgets the tenant, fails the build the moment it is
declared.

They do not replace the runtime isolation tests (household A gets ``404`` on
household B's identifiers) -- they make them unnecessary for a whole class of
mistakes that a runtime test would only catch by accident.
"""

from __future__ import annotations

import warnings
from typing import Final

import pytest
from sqlalchemy import Table, UniqueConstraint

from chaudron.domain.models import Base

_TENANT_COLUMN: Final = "household_id"

#: Tables outside the tenant, with the reason. Adding an entry is the deliberate
#: gesture this list exists to force; the default answer is "it carries the tenant".
GLOBAL_TABLES: Final[dict[str, str]] = {
    "household": "the tenant root itself",
    "user_account": "a person's identity, independent of any household (ADR-0006)",
    # A session belongs to an *account*, and an account may open two households
    # (family home and flatshare). Putting a tenant on the session would mean one
    # login per household, which is the modelling mistake `user_account` exists to
    # avoid. It also could not work: the session is what *establishes* the tenant,
    # so a policy keyed on the tenant would have to be satisfied before the tenant
    # is known (migration `0009`).
    "user_session": "one signed-in browser, belonging to an account rather than a household",
    "unit": "shared reference data",
    "llm_provider": "shared reference data",
    # Published public-health guidance and its editions. Per-household copies
    # would mean a household could hold a *different* answer to "is honey
    # forbidden before twelve months", which is not a preference (ADR-0009).
    "nutrition_reference": "published guidance editions, identical for everyone",
    "pnns_guideline": "published food-group benchmarks, identical for everyone",
    "infant_food_restriction": "published infant safety rules, identical for everyone",
    "shelf_life_guideline": "indicative keeping times, identical for everyone",
    # Not business data, and the keys are the reason: a bucket may be keyed on a
    # client address seen before any household is known, or on the normalised
    # e-mail of an account that does not exist. A tenant column would be NULL on
    # exactly the pre-authentication rows that matter most -- the login and
    # registration limiters -- and a policy keyed on it would have to be satisfied
    # before the tenant is known, the same impossibility as `user_session`.
    "rate_limit_bucket": "shared token buckets, keyed on addresses and accounts, not tenants",
}

#: Tables where the tenant column may be NULL, with the reason.
NULLABLE_TENANT_TABLES: Final[dict[str, str]] = {
    "product": "NULL means the public catalogue entry, non-NULL a household-private one",
}

#: Unique constraints allowed not to be tenant-scoped, with the reason.
#: Anything not listed here and not scoped is a bug: a global uniqueness rule on a
#: tenant table lets one household's data collide with -- or disclose -- another's.
UNIQUE_CONSTRAINT_EXEMPTIONS: Final[dict[str, str]] = {
    "uq_product_gtin_global": "partial index over the public catalogue (household_id IS NULL)",
    # The digest of a machine token is what *decides* the household (migration
    # `0011`), so a composite `(household_id, token_hash)` index would have to be
    # probed with the answer it is being asked for. It is also not a leak of the
    # kind this guard exists to catch: a collision would need two 256-bit secrets
    # to hash alike, and the only caller able to trigger the error is one who
    # already holds the colliding value.
    "uq_machine_token_token_hash": "the lookup key of a bearer credential, global by necessity",
}

#: References to a tenant-scoped parent that are *not* composite yet, with the reason.
#:
#: A ratchet, not an absolution. Each of these is a row that can point at another
#: household's row if an identifier is guessed or a bug builds one; the database
#: cannot refuse it. New entries are meant to be argued in review, and the list is
#: meant to shrink -- `docs/data-model.md` section 5.2 documents the product case as
#: knowingly accepted and section 11 lists it as open.
SIMPLE_FOREIGN_KEY_EXEMPTIONS: Final[dict[tuple[str, str], str]] = {
    ("receipt", "llm_provider_config_id"): "provenance only, never read back as a scope",
    ("recipe_suggestion", "llm_provider_config_id"): "provenance only, never read back as a scope",
    ("receipt_line", "matched_product_id"): (
        "product.household_id is nullable and cannot be a composite target "
        "(known hole, data-model.md section 5.2)"
    ),
    ("recipe_suggestion_ingredient", "product_id"): "same nullable-product hole",
    ("shopping_list_item", "product_id"): "same nullable-product hole",
    ("inventory_lot", "product_id"): "same nullable-product hole",
    ("declined_repurchase", "product_id"): "same nullable-product hole",
    # The three "origin trace, not an access path" entries that used to sit here
    # are gone, and the reasoning behind them is worth keeping as a warning. It
    # was: nothing ever writes these columns from a request, the join back is
    # scoped so a cross-household value renders as NULL, and all three are
    # `ON DELETE SET NULL`. Every clause of that is true and none of it is a
    # schema property -- it is a description of today's service layer, offered as
    # an argument about what the database permits. PostgreSQL's referential
    # integrity triggers are not subject to row-level security, so the database
    # permitted the row; what refused it was code, and code is what this file
    # exists to stop relying on. Revision `0015` made all three composite.
}

_ALL_TABLES: Final[tuple[Table, ...]] = tuple(Base.metadata.sorted_tables)
_TENANT_TABLES: Final[frozenset[str]] = frozenset(
    table.name for table in _ALL_TABLES if _TENANT_COLUMN in table.c
)


def _table_ids(tables: tuple[Table, ...]) -> list[str]:
    return [table.name for table in tables]


@pytest.mark.parametrize("table", _ALL_TABLES, ids=_table_ids(_ALL_TABLES))
def test_business_tables_carry_the_tenant_column(table: Table) -> None:
    if table.name in GLOBAL_TABLES:
        pytest.skip(f"{table.name} is global: {GLOBAL_TABLES[table.name]}")
    assert _TENANT_COLUMN in table.c, (
        f"{table.name} has no {_TENANT_COLUMN}. Either add it (ADR-0006: every business "
        f"table carries the tenant, even where a join could derive it), or declare the "
        f"table in GLOBAL_TABLES with the reason."
    )


@pytest.mark.parametrize("table", _ALL_TABLES, ids=_table_ids(_ALL_TABLES))
def test_tenant_column_is_not_nullable(table: Table) -> None:
    column = table.c.get(_TENANT_COLUMN)
    if column is None:
        pytest.skip(f"{table.name} carries no {_TENANT_COLUMN}")
    if table.name in NULLABLE_TENANT_TABLES:
        pytest.skip(f"{table.name}: {NULLABLE_TENANT_TABLES[table.name]}")
    assert not column.nullable, (
        f"{table.name}.{_TENANT_COLUMN} is nullable. A NULL tenant belongs to nobody and "
        f"is filtered out by every scoped query -- the row becomes invisible, not shared."
    )


def _is_tenant_anchored(table: Table, column_names: frozenset[str]) -> bool:
    """True when the columns are pinned to one household by construction.

    Either the tenant column is right there, or the columns include a reference whose
    own foreign key is composite on ``household_id`` -- in which case the parent is
    already pinned and the child inherits the pinning (data-model.md section 5.2,
    layer 2). ``UNIQUE (receipt_id, line_no)`` is safe for that reason.
    """
    if _TENANT_COLUMN in column_names:
        return True
    return any(
        _TENANT_COLUMN in {c.name for c in constraint.columns}
        and constraint.referred_table.name in _TENANT_TABLES
        and column_names & {c.name for c in constraint.columns}
        for constraint in table.foreign_key_constraints
    )


_TENANT_SCOPED_TABLES: Final[tuple[Table, ...]] = tuple(
    table for table in _ALL_TABLES if table.name in _TENANT_TABLES
)


@pytest.mark.parametrize("table", _TENANT_SCOPED_TABLES, ids=_table_ids(_TENANT_SCOPED_TABLES))
def test_unique_constraints_are_tenant_scoped(table: Table) -> None:
    """``UNIQUE (barcode)`` instead of ``UNIQUE (household_id, barcode)`` is a leak.

    Not only does it stop the second household from storing its own row, the error it
    raises confirms that some other household already has that value.
    """
    offenders: list[str] = []

    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        name = constraint.name if isinstance(constraint.name, str) else str(constraint)
        if name in UNIQUE_CONSTRAINT_EXEMPTIONS:
            continue
        if not _is_tenant_anchored(table, frozenset(c.name for c in constraint.columns)):
            offenders.append(f"{name} ({[c.name for c in constraint.columns]})")

    for index in table.indexes:
        if not index.unique:
            continue
        name = index.name if isinstance(index.name, str) else str(index)
        if name in UNIQUE_CONSTRAINT_EXEMPTIONS:
            continue
        # Expression indexes (lower(email), (true)) expose no column name; only the
        # plain columns can be inspected, which is enough to spot a missing tenant.
        if not _is_tenant_anchored(table, frozenset(c.name for c in index.columns)):
            offenders.append(f"{name} ({[c.name for c in index.columns]})")

    assert not offenders, (
        f"{table.name}: unique rules that are not tenant-scoped: {offenders}. Prefix them "
        f"with {_TENANT_COLUMN}, or list them in UNIQUE_CONSTRAINT_EXEMPTIONS with a reason."
    )


@pytest.mark.parametrize("table", _TENANT_SCOPED_TABLES, ids=_table_ids(_TENANT_SCOPED_TABLES))
def test_references_to_tenant_tables_are_composite(table: Table) -> None:
    """A composite foreign key makes cross-household references impossible in the engine.

    ``FOREIGN KEY (household_id, storage_location_id) REFERENCES storage_location
    (household_id, id)`` costs one extra unique index on the parent and removes an
    entire class of leaks, including the ones an application bug would create.
    """
    offenders: list[str] = []
    for constraint in table.foreign_key_constraints:
        parent = constraint.referred_table.name
        if parent == "household" or parent not in _TENANT_TABLES:
            continue
        column_names = [c.name for c in constraint.columns]
        if _TENANT_COLUMN in column_names:
            continue
        business_columns = [name for name in column_names if name != _TENANT_COLUMN]
        key = (table.name, business_columns[0]) if business_columns else (table.name, "")
        if key in SIMPLE_FOREIGN_KEY_EXEMPTIONS:
            continue
        offenders.append(f"{column_names} -> {parent}")

    assert not offenders, (
        f"{table.name}: references to a tenant-scoped table that are not composite: "
        f"{offenders}. Include {_TENANT_COLUMN} in the foreign key, or add the pair to "
        f"SIMPLE_FOREIGN_KEY_EXEMPTIONS with a reason."
    )


def test_exemption_lists_have_no_stale_entries() -> None:
    """Warn -- never fail -- when an exemption stops being needed.

    Failing here would punish the commit that *fixed* something, which is the surest
    way to get the whole guard deleted. A warning shows up in the run summary and in
    review.
    """
    known_tables = {table.name for table in _ALL_TABLES}
    stale: list[str] = [name for name in GLOBAL_TABLES if name not in known_tables]
    stale += [name for name in NULLABLE_TENANT_TABLES if name not in known_tables]

    declared_columns = {(table.name, column.name) for table in _ALL_TABLES for column in table.c}
    stale += [
        f"{table}.{column}"
        for table, column in SIMPLE_FOREIGN_KEY_EXEMPTIONS
        if (table, column) not in declared_columns
    ]

    if stale:
        warnings.warn(
            f"stale tenant-guard exemptions, delete them: {sorted(stale)}",
            UserWarning,
            stacklevel=1,
        )
