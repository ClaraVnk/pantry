"""declined repurchase proposals

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-04 09:00:00.000000+00:00

One table, one policy. It closes the last gap of contract 6bis: until now
``previously_declined`` could never become true, because nothing could record a
refusal -- ``POST /v1/shopping-lists/declined`` answered ``503`` rather than
accept one it had nowhere to keep.

Three points are load-bearing.

*No expiry column, and that is the feature.* A refusal that quietly forgot itself
after a few months would re-propose exactly what the household waved away, with
nothing on screen to explain the return. Perpetuity is bought with one revocation
endpoint (``DELETE /v1/shopping-lists/declined/{product_id}``), which is a delete
of the row -- the row *is* the refusal, so there is no state to flip.

*The unique constraint is the idempotence.* Dismissing the same proposal on the
phone and then on the tablet is one refusal, and
``ON CONFLICT DO NOTHING`` needs the constraint to name.
``(household_id, product_id)`` doubles as the index for the only read there is:
"of these five depleted products, which were refused before?"

*Row-level security, like every other tenant table.* ``declined_repurchase``
carries ``household_id``, so without a policy it is a hole straight through the
isolation revision ``0004`` established, and ``Database.check_row_level_security``
would start reporting it. Same predicate, same name shape, same
``chaudron_current_household()`` helper.

``FORCE ROW LEVEL SECURITY`` is deliberately **not** set here, for the reason
revision ``0004`` argues at length and revision ``0006`` repeats: the owner of
these tables is the migration and maintenance identity -- Alembic,
``scripts/seed.py``, a DBA at a psql prompt -- and forcing policies on it would
mean every one of them has to post a tenant before it can touch a row. The
security property comes from the *application* connecting as a role that owns
nothing (``scripts/provision_app_role.py``). Forcing it on this one table and no
other would buy nothing against the application role, break the maintenance
identities on exactly one table, and leave the schema with a single exception
nobody re-reads.

``downgrade`` drops the policy and then the table. No enum type is created here,
so there is none to drop -- the trap revisions ``0001``, ``0005`` and ``0006``
document does not apply.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE: str = "declined_repurchase"
_POLICY: str = f"{_TABLE}_household_isolation"

#: Kept identical to revision ``0004``'s helper and predicate on purpose: a
#: second spelling of the same rule is a second thing to audit.
_TENANT_FUNCTION: str = "chaudron_current_household"
_OWN_ROW: str = f"household_id = {_TENANT_FUNCTION}()"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("household_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "declined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("declined_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_declined_repurchase"),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_declined_repurchase_household_id",
            ondelete="CASCADE",
        ),
        # A product that no longer exists cannot be proposed, so its refusal is
        # meaningless rather than merely stale.
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name="fk_declined_repurchase_product_id",
            ondelete="CASCADE",
        ),
        # SET NULL, not CASCADE: a member can leave the household while the
        # household's decision stands. Losing who decided must not lose the
        # decision.
        sa.ForeignKeyConstraint(
            ["declined_by_user_id"],
            ["user_account.id"],
            name="fk_declined_repurchase_declined_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "household_id", "product_id", name="uq_declined_repurchase_household_product"
        ),
    )

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {_TABLE}
        FOR ALL
        USING ({_OWN_ROW})
        WITH CHECK ({_OWN_ROW})
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE}")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_table(_TABLE)
