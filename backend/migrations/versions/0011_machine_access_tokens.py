"""machine access tokens, and a way to resolve one before the tenant is known

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-04 00:00:00.000000+00:00

Contract v1.1 section 10. Until this revision the only credential this API
accepted was a browser cookie, which means a program wanting to read the pantry
had to store **an account password** and replay a sign-in -- handing a third
party an access its owner can neither restrict nor withdraw without changing
that password everywhere.

Two database objects, and the second one is the awkward part.

*``machine_token``.* One row per issued credential, scoped to **one household**
rather than to an account: a person who belongs to a family home and a flatshare
issues two tokens, so installing an integration for one cannot open the other.
``token_hash`` is a SHA-256 of the value handed out once at creation, for the
reason argued on :class:`chaudron.domain.models.UserSession` -- 256 bits from
``secrets`` have no dictionary to slow down, and an Argon2 verification on every
request would be a denial of service we would be performing on ourselves.

The table carries ``household_id``, so it is protected exactly like the twelve
tables of revision ``0004``: ``ENABLE ROW LEVEL SECURITY`` with the same
``household_id = chaudron_current_household()`` predicate, and deliberately **no**
``FORCE`` -- revision ``0004`` argues that at length, and the argument has not
changed.

*``chaudron_resolve_machine_token(text)``.* The same inversion revision ``0009``
had to solve for memberships, arriving from the other side. A request carrying a
bearer token has posted no tenant yet -- the token is what *decides* the tenant --
so a direct read of ``machine_token`` would be filtered by the very policy the
lookup is meant to arm, and every token in existence would resolve to nothing.

``SECURITY DEFINER`` again, and narrowed the same three ways:

* it is keyed on the **digest of a 256-bit secret**, so it cannot be used to
  enumerate anything: a caller who can supply the argument already holds the
  token;
* it returns five columns, not the table -- ``name``, ``prefix``, ``last4`` and
  the timestamps stay behind the policy, where the household's own session reads
  them;
* ``search_path`` is pinned to ``pg_catalog, public``, without which a caller able
  to create a schema substitutes their own ``machine_token``.

**Every refusal is one branch.** Revoked, expired, issued by an account since
disabled, issued by somebody no longer a member, belonging to an archived
household, and simply unknown all produce zero rows from the same ``WHERE``. The
API therefore answers them identically *and takes the same time to do it*, which
a post-filter in Python could not promise: it would have found the row first.

That last clause -- the join back to ``household_member`` -- is what makes it
safe for an ordinary member to issue a token. The credential grants no more than
the person behind it still has, and it stops working the moment their membership
is revoked, without anybody having to remember to delete it.

Reversible: ``downgrade`` drops the function, the policy, the table and the enum
type. It destroys every issued token, which is the correct behaviour for removing
the token store -- every integration stops, nobody is silently left holding a
credential the schema can no longer validate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: str = "machine_token"
SCOPE_ENUM: str = "machine_token_scope"
RESOLVE_FUNCTION: str = "chaudron_resolve_machine_token"

#: Kept in sync with :class:`chaudron.domain.models.MachineTokenScope`; the
#: contract freezes the five and forbids a sixth without an argument.
SCOPES: tuple[str, ...] = (
    "inventory:read",
    "inventory:write",
    "shopping:read",
    "shopping:write",
    "budget:read",
)

#: Same helper and same predicate as revision ``0004``. Repeated rather than
#: imported so this file reads on its own, which is how a migration is read.
_TENANT_FUNCTION = "chaudron_current_household"
_OWN_ROW = f"household_id = {_TENANT_FUNCTION}()"
_POLICY = f"{TABLE}_household_isolation"


def _scope_enum(*, create_type: bool = False) -> postgresql.ENUM:
    return postgresql.ENUM(*SCOPES, name=SCOPE_ENUM, create_type=create_type)


def upgrade() -> None:
    _scope_enum().create(op.get_bind(), checkfirst=False)

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("household_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment=(
                "Who issued this token. Checked on every request: the token stops "
                "working when this account loses its membership or is disabled."
            ),
        ),
        sa.Column(
            "name",
            sa.String(length=120),
            nullable=False,
            comment="Chosen by the person who created it, so a list is readable.",
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of the presented value, lowercase hex. The plaintext is never "
                "stored: a dump of this table yields no usable token."
            ),
        ),
        sa.Column(
            "prefix",
            sa.String(length=16),
            nullable=False,
            comment=(
                "The fixed scheme marker the token carries, stored per row so a list "
                "still renders correctly if the format ever changes."
            ),
        ),
        sa.Column(
            "last4",
            sa.String(length=4),
            nullable=False,
            comment="The last four characters, to tell two tokens apart. Not a credential.",
        ),
        sa.Column(
            "scopes",
            postgresql.ARRAY(_scope_enum()),
            nullable=False,
            comment=(
                "Closed vocabulary, additive, never implicit. A token holds these and "
                "nothing that follows from them."
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # `name` is a *component*: `op.create_table` renders it through the
        # metadata's `ck_%(table_name)s_%(constraint_name)s` template, so writing
        # the prefix here would deploy `ck_machine_token_ck_machine_token_...`.
        # That is the mistake revision `0010` had to rename eighteen constraints
        # to undo, and `tests/test_schema_naming_guard.py` fails on it. The
        # foreign keys and the primary key above take their full names because
        # their templates interpolate columns, not this string.
        sa.CheckConstraint("cardinality(scopes) > 0", name="scopes_present"),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_machine_token_household_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_account.id"], name="fk_machine_token_user_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_machine_token"),
    )
    op.create_index("uq_machine_token_token_hash", TABLE, ["token_hash"], unique=True)
    op.create_index("ix_machine_token_household_id", TABLE, ["household_id"], unique=False)

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {TABLE}
        FOR ALL
        USING ({_OWN_ROW})
        WITH CHECK ({_OWN_ROW})
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {RESOLVE_FUNCTION}(p_token_hash text)
        RETURNS TABLE (
            token_id uuid,
            household_id uuid,
            user_id uuid,
            scopes {SCOPE_ENUM}[],
            last_used_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id, t.household_id, t.user_id, t.scopes, t.last_used_at
            FROM {TABLE} AS t
            JOIN household_member AS m
              ON m.household_id = t.household_id AND m.user_id = t.user_id
            JOIN user_account AS u ON u.id = t.user_id
            JOIN household AS h ON h.id = t.household_id
            WHERE t.token_hash = p_token_hash
              AND t.revoked_at IS NULL
              AND (t.expires_at IS NULL OR t.expires_at > now())
              AND u.disabled_at IS NULL
              AND h.archived_at IS NULL
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {RESOLVE_FUNCTION}(text) IS "
        "'Resolve a machine token from the SHA-256 of its value, past the row-level "
        "security on machine_token. SECURITY DEFINER because a bearer request has "
        "posted no tenant yet -- the token is what decides it; see revision 0011. "
        "Keyed on the digest of a 256-bit secret, so it enumerates nothing. Revoked, "
        "expired, disabled, un-membered and unknown are one branch, so the API can "
        "answer them identically and in the same time. search_path is pinned.'"
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {RESOLVE_FUNCTION}(text)")
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {TABLE}")
    op.drop_index("ix_machine_token_household_id", table_name=TABLE)
    op.drop_index("uq_machine_token_token_hash", table_name=TABLE)
    op.drop_table(TABLE)
    _scope_enum().drop(op.get_bind(), checkfirst=False)
