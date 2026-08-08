"""per-household shopping-list export destinations

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-04 12:00:00.000000+00:00

The table ADR-0010 section 7 specified and left to another change, which is this
one. Until now the only usable destination was the *instance's* own -- one token
in the environment, usable by exactly one household -- so the export worked on a
single-family deployment and nowhere else. This revision is what makes the
feature multi-tenant, and it is also what closes the AAD deviation section 6 of
that ADR recorded: sealing an export token now carries
``chaudron/shopping_export_target/token/v1``, which was named there as a
prerequisite of this table.

Four points are load-bearing.

*``consented_at`` is ``NOT NULL``, by legal obligation rather than by taste.* A
shopping list says what identifiable people eat, which in a household with an
allergy or an observance is health or religious data (RGPD art. 9). Sending it to
a third party needs an agreement that was actually given, is dated, and can be
withdrawn. A nullable column would let a row exist without one, and the first
``INSERT`` that forgot it would be a breach rather than a bug. There is therefore
no server default either: a default would make the column satisfied by omission,
which is exactly what ``NOT NULL`` is here to prevent.

*``consent_revoked_at`` carries the withdrawal, and the row survives it.* The
household keeps the record of what it once authorised and when. Revocation takes
effect at the next send -- ``ShoppingExportFactory`` reads the pair on every
export -- rather than at the next cleanup, so there is no window in which a
withdrawn agreement still sends anything.

*``token_ciphertext`` is a secret column and the schema says so.* The comment is
carried into the database, where a DBA reading ``\\d+ shopping_export_target``
sees it without opening this repository. The encryption key is in the environment
and never here, so a stolen ``pg_dump`` is ciphertext.

*Row-level security, like every other tenant table.* Same helper, same predicate,
same name shape as revision ``0004``. ``FORCE ROW LEVEL SECURITY`` is deliberately
**not** set, for the reason revision ``0004`` argues at length and revisions
``0006`` and ``0007`` repeat: the owner of these tables is the migration and
maintenance identity -- Alembic, ``scripts/seed.py``, a DBA at a psql prompt --
and forcing policies on it would mean every one of them has to post a tenant
before it can touch a row. The security property comes from the *application*
connecting as a role that owns nothing (``scripts/provision_app_role.py``).
Forcing it on this one table and no other would buy nothing against the
application role, break the maintenance identities on exactly one table, and
leave the schema with a single exception nobody re-reads.

``downgrade`` drops the policy and then the table. ``target_code`` is ``text`` and
not a native enum precisely so that there is no type to drop here -- the trap
revisions ``0001``, ``0005`` and ``0006`` document does not apply.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE: str = "shopping_export_target"
_POLICY: str = f"{_TABLE}_household_isolation"

#: Kept identical to revision ``0004``'s helper and predicate on purpose: a
#: second spelling of the same rule is a second thing to audit.
_TENANT_FUNCTION: str = "chaudron_current_household"
_OWN_ROW: str = f"household_id = {_TENANT_FUNCTION}()"

_CIPHERTEXT_COMMENT: str = (
    "SECRET: AES-256-GCM ciphertext of a third party's personal token. "
    "Encryption key comes from the environment, never from this database. "
    "Never returned by the API; users only ever see token_last4."
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("household_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("target_code", sa.Text(), nullable=False),
        sa.Column(
            "token_ciphertext", sa.LargeBinary(), nullable=False, comment=_CIPHERTEXT_COMMENT
        ),
        sa.Column("token_last4", sa.String(length=4), nullable=False),
        sa.Column("token_encryption_key_id", sa.String(length=32), nullable=False),
        sa.Column("external_list_id", sa.Text(), nullable=True),
        # No server default: see the module docstring. An agreement satisfied by
        # omission is not an agreement.
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_shopping_export_target"),
        # A household that is erased takes its destinations with it, tokens
        # included: the erasure of ADR-0006 has to be total and atomic.
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_shopping_export_target_household_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "household_id", "target_code", name="uq_shopping_export_target_household_code"
        ),
        sa.UniqueConstraint("household_id", "id", name="uq_shopping_export_target_household_id"),
        # Bare names, not the `ck_<table>_` prefixed ones. The naming convention
        # of `Base.metadata` renders a check constraint as
        # `ck_%(table_name)s_%(constraint_name)s`, so the explicit name is a
        # *component* of the final name rather than the whole of it: passing the
        # prefixed form yields `ck_shopping_export_target_ck_shopping_export_...`,
        # truncated at 63 characters, which is what revision `0006` shipped and
        # what makes its constraints unnameable from the model. These two match
        # `domain/models.py` exactly.
        sa.CheckConstraint("char_length(token_last4) = 4", name="last4_length"),
        sa.CheckConstraint(
            "consent_revoked_at IS NULL OR consent_revoked_at >= consented_at",
            name="revocation_follows_consent",
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
