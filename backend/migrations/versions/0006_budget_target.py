"""shopping budget targets

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-04 07:00:00.000000+00:00

One table, one enum type, one policy. Everything the budget feature *computes*
comes from tables that already exist -- ``receipt``, ``receipt_line`` and
``inventory_lot`` -- so the only thing that needs storage is the household's
optional target (contract 6ter).

Three points are load-bearing.

*Row-level security, like every other tenant table.* ``budget_target`` carries
``household_id``, so without a policy it is a hole straight through the
isolation revision ``0004`` established, and
``scripts/provision_app_role.py --check`` would start reporting it. The policy
is the uniform one: same predicate, same name shape, same
``chaudron_current_household()`` helper. ``FORCE ROW LEVEL SECURITY`` is left
off here for the reason ``0004`` documents at length -- the owner is the
migration identity.

*One target per (household, period).* The unique constraint is what makes
``PUT /v1/budget/target`` a replacement rather than an accumulation the
interface would have to pick a winner from.

*The currency is checked, not trusted.* ``'eur'`` and ``'EUR'`` stored side by
side would split one household's spending into two currencies that never add
up -- and they must never be added up anyway, so the split would be invisible
until someone read the screen and found half their money missing.

``downgrade`` drops the policy, the table and then the enum type, in that
order: ``op.drop_table`` leaves a native enum behind, and a re-``upgrade``
after an incomplete ``downgrade`` would fail on "type already exists" -- the
trap revisions ``0001`` and ``0005`` both document.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from chaudron.domain.models import BudgetPeriod

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE: str = "budget_target"
_ENUM_NAME: str = "budget_period"
_POLICY: str = f"{_TABLE}_household_isolation"

#: Kept identical to revision ``0004``'s helper and predicate on purpose: a
#: second spelling of the same rule is a second thing to audit.
_TENANT_FUNCTION: str = "chaudron_current_household"
_OWN_ROW: str = f"household_id = {_TENANT_FUNCTION}()"


def upgrade() -> None:
    period = postgresql.ENUM(
        *(member.value for member in BudgetPeriod),
        name=_ENUM_NAME,
        create_type=False,
    )
    period.create(op.get_bind(), checkfirst=False)

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("household_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("period", period, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget_target"),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_budget_target_household_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("household_id", "period", name="uq_budget_target_household_period"),
        sa.CheckConstraint("amount > 0", name="ck_budget_target_amount_positive"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_budget_target_currency_iso_4217"),
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
    # After the table: a type still used by a column cannot be dropped.
    postgresql.ENUM(name=_ENUM_NAME, create_type=False).drop(op.get_bind(), checkfirst=False)
