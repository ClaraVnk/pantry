"""server-side sessions, and a way to read memberships before the tenant is known

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-04 00:00:00.000000+00:00

Closes AUD-001. Until this revision ``X-Household-Id`` was the whole of the
access control: a header naming a household, checked only for existence. This
adds the two database objects real authentication needs.

*``user_session``.* One row per signed-in browser. It is a table rather than a
signed token because revocation has to be immediate -- see the class docstring on
:class:`chaudron.domain.models.UserSession` for the argument. ``token_hash`` is a
SHA-256 of the cookie value: a dump of this table yields no usable session. There
is no ``household_id`` column and none is wanted, so the table sits outside
row-level security exactly as ``user_account`` does; ``tests/tenancy`` records
that in ``GLOBAL_TABLES``.

*``chaudron_user_memberships(uuid)``.* The awkward part, and the reason this
migration is not just a ``CREATE TABLE``.

Revision ``0004`` put row-level security on ``household_member`` with the
predicate ``household_id = chaudron_current_household()``. That is right for
every ordinary query and impossible for this one: authentication has to answer
"which households may this account open?" *before* any household has been posted,
and posting one first would mean trusting the client's header to arm the very
check meant to validate it. With no tenant posted the policy shows zero rows, so
a direct read would report that every account belongs to nothing.

``SECURITY DEFINER`` is the mechanism PostgreSQL provides for exactly this. The
function runs as its owner -- the migration identity, which owns the tables --
and revision ``0004`` deliberately does not set ``FORCE ROW LEVEL SECURITY``, so
the owner is not subject to the policies. Three deliberate narrowings keep that
power small:

* it takes one argument and returns rows for that account only, so it cannot be
  used to enumerate a household's members;
* it returns two columns, not the table -- ``invited_by_user_id`` and
  ``joined_at`` stay behind the policy;
* ``search_path`` is pinned to ``pg_catalog, public``. A ``SECURITY DEFINER``
  function with an inherited search path is the classic privilege-escalation
  bug: a caller who can create a schema earlier in the path substitutes their own
  ``household_member``. Pinning it is not optional.

Reversible: ``downgrade`` drops the function and the table. It destroys every
live session, which is the correct behaviour for removing the session store --
everyone is signed out, nobody is silently left with a credential the schema can
no longer validate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBERSHIP_FUNCTION: str = "chaudron_user_memberships"


def upgrade() -> None:
    op.create_table(
        "user_session",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of the cookie value, lowercase hex. The plaintext is never "
                "stored: a dump of this table yields no live session."
            ),
        ),
        sa.Column(
            "csrf_token",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Echoed by the client in X-CSRF-Token on every unsafe method. Stored "
                "in the clear on purpose: it is worthless without the cookie."
            ),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name="fk_user_session_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_session"),
    )
    op.create_index("uq_user_session_token_hash", "user_session", ["token_hash"], unique=True)
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"], unique=False)

    op.execute(
        f"""
        CREATE FUNCTION {MEMBERSHIP_FUNCTION}(p_user_id uuid)
        RETURNS TABLE (household_id uuid, role membership_role)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT m.household_id, m.role
            FROM household_member AS m
            WHERE m.user_id = p_user_id
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {MEMBERSHIP_FUNCTION}(uuid) IS "
        "'Households one account belongs to, read past the row-level security on "
        "household_member. SECURITY DEFINER because authentication must answer this "
        "before any tenant has been posted; see revision 0009 for why that is not a "
        "hole. search_path is pinned.'"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {MEMBERSHIP_FUNCTION}(uuid)")
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_index("uq_user_session_token_hash", table_name="user_session")
    op.drop_table("user_session")
