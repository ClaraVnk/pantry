"""record who registered an export target, and close EXECUTE on the definers

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-04 18:00:00.000000+00:00

Three changes, and the last two have to ship together or authentication stops
working. Read the note on :func:`upgrade` before deploying this.

*``shopping_export_target.registered_by_user_id``.* The table has recorded, since
revision ``0008``, that a household agreed its shopping list may leave the
instance -- and never *who* agreed. Two failures follow from that one omission
and neither is theoretical (audit AUD-027).

An owner opening the export settings saw four characters of somebody's Todoist
token and had no way to learn whose. And nothing cascades from
``household_member`` to this table, so a member excluded from a household left
behind a consented row that kept filing the household's groceries into their
personal account, indefinitely, with no symptom. The column names the registrant;
``infra/todo/factory.py`` joins back to ``household_member`` on **every send** and
refuses when they have gone -- the same shape ``chaudron_resolve_machine_token``
already uses for tokens, and for the same reason: there is no membership-removal
endpoint in this application, so a rule that only ran when somebody remembered to
call it would never run at all.

Nullable, and deliberately not backfilled. ``NULL`` means "registered before this
column existed", which is a genuinely unknown registrant and *not* the same state
as a registrant who has left. Inventing a plausible-looking owner would have made
an unaudited consent look audited, and ``registrant_has_left`` reads ``NULL`` as
"nobody to have left" so no existing installation loses its export on upgrade.
``ON DELETE SET NULL`` rather than ``CASCADE``: erasing an account must not erase
the household's dated agreement, which is what it answers "who did you send this
to?" with.

*``chaudron_resolve_machine_token`` returns the issuer's role.* The resolver has
joined ``household_member`` since revision ``0011`` -- that join is what makes a
token die with its issuer's membership -- and threw the role away. So a ``viewer``
could mint an ``inventory:write`` token and write with it, walking straight past
the role checks the API layer now applies to the cookie door. Returning the column
costs nothing (the join is already there), and it makes a token never more than
its issuer *currently* is: a demotion takes effect on the next request rather than
whenever somebody remembers to revoke.

Changing a ``RETURNS TABLE`` needs ``DROP`` then ``CREATE``; PostgreSQL will not
alter one in place.

*``REVOKE EXECUTE ... FROM PUBLIC`` on both ``SECURITY DEFINER`` functions.*
``pg_proc.proacl`` was ``NULL`` for ``chaudron_user_memberships`` and
``chaudron_resolve_machine_token``, which is the PostgreSQL default and means
``EXECUTE`` to ``PUBLIC``. Those two are precisely the functions that **cross
row-level security by construction**: they run as the table owner, and revision
``0009`` and ``0011`` argue at length why they must. ``chaudron_current_household()``
-- the only one ``scripts/provision_app_role.py`` granted explicitly -- is the one
of the three that crosses nothing.

Not exploitable by anything that exists today: the application role is the only
non-owner login, and it needs both. It becomes exploitable at the moment somebody
adds the next role -- a reporting account, a read-only analyst, a backup user --
which would silently inherit the right to read the entire membership map past
every policy. No review would have flagged it, because the grant is invisible: it
is the absence of an ACL rather than the presence of one.

**The two halves are one change.** ``REVOKE`` alone leaves the application unable
to resolve a session or a token -- every request becomes ``401`` -- so
``scripts/provision_app_role.py`` grants ``EXECUTE`` on all three functions in the
same commit, and its ``--check`` mode reports a role that is missing one. Run it
after this migration, as ``ops/README.md`` and the script's own docstring say. The
test suite proves the pairing: ``tests/tenancy`` provisions the role through that
very script and then signs in over HTTP as it.

*Rollback.* ``downgrade`` restores the three-column resolver, drops the column and
puts ``EXECUTE`` back to ``PUBLIC``, so a database rolled back to ``0013`` behaves
exactly as one that never saw this revision -- including for a role provisioned by
the new script, which merely holds a grant it no longer needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_TABLE: Final = "shopping_export_target"
_REGISTRANT_COLUMN: Final = "registered_by_user_id"

_MEMBERSHIP_FUNCTION: Final = "chaudron_user_memberships(uuid)"
_RESOLVE_FUNCTION: Final = "chaudron_resolve_machine_token"
_SCOPE_ENUM: Final = "machine_token_scope"

#: The resolver as revision ``0011`` wrote it, minus the role. Kept verbatim so
#: ``downgrade`` restores the previous behaviour rather than an approximation of
#: it, and so the diff between the two is exactly one column.
_RESOLVER_WITHOUT_ROLE: Final = f"""
CREATE FUNCTION {_RESOLVE_FUNCTION}(p_token_hash text)
RETURNS TABLE (
    token_id uuid,
    household_id uuid,
    user_id uuid,
    scopes {_SCOPE_ENUM}[],
    last_used_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.household_id, t.user_id, t.scopes, t.last_used_at
    FROM machine_token AS t
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

#: The same, with ``m.role`` carried out. The ``WHERE`` clause is untouched:
#: revoked, expired, disabled, un-membered, archived and unknown must stay one
#: branch producing zero rows, so the API answers them identically and in the
#: same time (revision ``0011``). A role is read from a join that already had to
#: succeed; it adds no way to fail.
_RESOLVER_WITH_ROLE: Final = f"""
CREATE FUNCTION {_RESOLVE_FUNCTION}(p_token_hash text)
RETURNS TABLE (
    token_id uuid,
    household_id uuid,
    user_id uuid,
    role membership_role,
    scopes {_SCOPE_ENUM}[],
    last_used_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.household_id, t.user_id, m.role, t.scopes, t.last_used_at
    FROM machine_token AS t
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

_RESOLVER_COMMENT: Final = (
    "Resolve a machine token from the SHA-256 of its value, past the row-level "
    "security on machine_token. SECURITY DEFINER because a bearer request has "
    "posted no tenant yet -- the token is what decides it; see revision 0011. "
    "Keyed on the digest of a 256-bit secret, so it enumerates nothing. Revoked, "
    "expired, disabled, un-membered and unknown are one branch, so the API can "
    # No apostrophes anywhere in these constants: they are interpolated into a
    # single-quoted SQL literal, and the first `issuer's` closed the string and
    # produced a syntax error at `s`.
    "answer them identically and in the same time. Returns the CURRENT role of "
    "the issuer (revision 0014), so a token is never more than the person behind it. "
    "search_path is pinned. EXECUTE is revoked from PUBLIC: grant it to the "
    "application role with scripts/provision_app_role.py."
)

_MEMBERSHIP_COMMENT: Final = (
    "Households one account belongs to, read past the row-level security on "
    "household_member. SECURITY DEFINER because authentication must answer this "
    "before any tenant has been posted; see revision 0009 for why that is not a "
    "hole. search_path is pinned. EXECUTE is revoked from PUBLIC (revision 0014): "
    "grant it to the application role with scripts/provision_app_role.py."
)

#: The two comments revision ``0013`` leaves behind, restored verbatim by
#: ``downgrade``. A rolled-back database must not carry a comment describing
#: behaviour it no longer has -- these strings are what an operator reads in
#: ``\df+`` when they are trying to work out which revision they are on.
_RESOLVER_COMMENT_0011: Final = (
    "Resolve a machine token from the SHA-256 of its value, past the row-level "
    "security on machine_token. SECURITY DEFINER because a bearer request has "
    "posted no tenant yet -- the token is what decides it; see revision 0011. "
    "Keyed on the digest of a 256-bit secret, so it enumerates nothing. Revoked, "
    "expired, disabled, un-membered and unknown are one branch, so the API can "
    "answer them identically and in the same time. search_path is pinned."
)

_MEMBERSHIP_COMMENT_0009: Final = (
    "Households one account belongs to, read past the row-level security on "
    "household_member. SECURITY DEFINER because authentication must answer this "
    "before any tenant has been posted; see revision 0009 for why that is not a "
    "hole. search_path is pinned."
)


def upgrade() -> None:
    """Add the column, widen the resolver, and close both definers to PUBLIC.

    **Deployment order.** After this revision and before the application is
    restarted, run ``scripts/provision_app_role.py`` against the owner DSN. The
    ``REVOKE`` below takes ``EXECUTE`` away from ``PUBLIC``, which is where the
    application role's permission on these two functions came from; without the
    matching ``GRANT`` the API cannot resolve a session and answers ``401`` to
    everything. ``--check`` reports it, which is what a pipeline should run.
    """
    op.add_column(
        _TARGET_TABLE,
        sa.Column(
            _REGISTRANT_COLUMN,
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment=(
                "Who registered this destination and granted the consent. NULL means a "
                "row that predates migration 0014, not an anonymous agreement."
            ),
        ),
    )
    op.create_foreign_key(
        f"fk_{_TARGET_TABLE}_{_REGISTRANT_COLUMN}",
        _TARGET_TABLE,
        "user_account",
        [_REGISTRANT_COLUMN],
        ["id"],
        ondelete="SET NULL",
    )
    # No index on the new column, deliberately. Every read goes the other way --
    # the row is found by (household_id, target_code), which the unique constraint
    # from revision 0008 already serves, and the membership check that follows is
    # a lookup on household_member's own primary key. An index here would serve
    # "which destinations did this account register?", a question nothing asks,
    # and `tests/test_schema_naming_guard.py` compares pg_index against the model:
    # an index a migration creates and the model does not declare is a drift.

    op.execute(f"DROP FUNCTION IF EXISTS {_RESOLVE_FUNCTION}(text)")
    op.execute(_RESOLVER_WITH_ROLE)
    op.execute(f"COMMENT ON FUNCTION {_RESOLVE_FUNCTION}(text) IS '{_RESOLVER_COMMENT}'")

    # Both, and only these two: they are the functions that cross row-level
    # security. `chaudron_current_household()` reads a transaction-local setting
    # and crosses nothing, so it stays executable by PUBLIC -- and is granted
    # explicitly anyway, for a database whose administrator has revoked PUBLIC
    # wholesale.
    op.execute(f"REVOKE ALL ON FUNCTION {_MEMBERSHIP_FUNCTION} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_RESOLVE_FUNCTION}(text) FROM PUBLIC")
    op.execute(f"COMMENT ON FUNCTION {_MEMBERSHIP_FUNCTION} IS '{_MEMBERSHIP_COMMENT}'")


def downgrade() -> None:
    """Restore revision ``0013``: PUBLIC may execute, and the resolver drops the role.

    The ``GRANT`` is written explicitly rather than left to a ``REVOKE`` being
    undone: PostgreSQL's default for a new function is an implicit ``PUBLIC``
    entry, and once ``proacl`` has been materialised by the ``REVOKE`` above,
    nothing puts it back except saying so.
    """
    op.execute(f"DROP FUNCTION IF EXISTS {_RESOLVE_FUNCTION}(text)")
    op.execute(_RESOLVER_WITHOUT_ROLE)
    op.execute(f"COMMENT ON FUNCTION {_RESOLVE_FUNCTION}(text) IS '{_RESOLVER_COMMENT_0011}'")
    op.execute(f"COMMENT ON FUNCTION {_MEMBERSHIP_FUNCTION} IS '{_MEMBERSHIP_COMMENT_0009}'")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_MEMBERSHIP_FUNCTION} TO PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RESOLVE_FUNCTION}(text) TO PUBLIC")

    op.drop_constraint(
        f"fk_{_TARGET_TABLE}_{_REGISTRANT_COLUMN}", _TARGET_TABLE, type_="foreignkey"
    )
    op.drop_column(_TARGET_TABLE, _REGISTRANT_COLUMN)
