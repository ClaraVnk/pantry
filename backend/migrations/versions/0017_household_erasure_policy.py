"""let the engine decide which household an erasure may remove

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-06 00:00:00.000000+00:00

``docs/security-pentest-2026-08-04.md`` open item O-01: *no endpoint erases a
household*. The schema was already ready -- ``ON DELETE CASCADE`` from
``household`` reaches every tenant table, atomically -- and the finding is that
**nothing could trigger it**. ``DELETE /v1/households`` closes that, and this
revision is the half the application cannot enforce for itself.

Why ``household`` needed a policy the day a route started deleting from it
--------------------------------------------------------------------------

Revision ``0004`` put row-level security on the thirteen tables carrying
``household_id``. ``household`` itself was left out, and correctly so: it carries
no tenant column, nothing wrote to it from a request, and the two things that
*read* it -- authentication resolving a caller's memberships, and the CalDAV feed
scanning for a credential to match (``infra/calendar/repository.py``) -- both run
**before any tenant has been posted**. A policy keyed on the tenant would have had
to be satisfied before the tenant was known.

That argument covers reads. It stops covering the table the moment an HTTP request
can emit ``DELETE FROM household``, because the application role holds ``DELETE``
on every table (``scripts/provision_app_role.py``) and the row it may delete would
then be decided entirely by the identifier the handler happened to pass. That is
precisely the property revision ``0004`` exists to stop relying on: *"the
application remembering to write ``WHERE household_id = :current`` is a property
that degrades silently"*. Here the failure would not return another family's
pantry, it would delete it.

So: row-level security on ``household``, with the three read/write commands left
exactly as permissive as they are today and **only** ``DELETE`` restricted.

    SELECT / INSERT / UPDATE  USING (true)
    DELETE                    USING (id = chaudron_current_household())

The permissive three are not decoration and not theatre. ``ENABLE ROW LEVEL
SECURITY`` is a table-wide switch: without them, enabling it to constrain
``DELETE`` would silently deny every ``SELECT`` as well, and the first symptom
would be a login that reports no memberships and a CalDAV feed that answers 404
for every subscriber. They are written down so the next reader sees that the
openness is a decision with a reason rather than an omission.

The ``DELETE`` predicate reads: *a tenant is posted, and it is this row*. An
unscoped transaction -- a background job, a migration script, anything running
outside a request -- posts no tenant, ``chaudron_current_household()`` returns
NULL, and the comparison yields NULL, which a policy reads as "no rows". Deleting
a household is now something only a request that has already resolved that
household through ``api/deps.py`` can do.

*This is not the authorisation.* Membership and the owner role are checked in
``api/deps.py`` and stay there; nothing in PostgreSQL knows what an owner is. What
the engine adds is that a bug which passes the wrong identifier deletes **nothing**
instead of deleting a stranger's household.

*``FORCE ROW LEVEL SECURITY`` stays off*, for the reason revision ``0004`` gives at
length: the table owner is the migration and maintenance identity, and forcing
policies on it would mean Alembic and ``scripts/seed.py`` had to post a tenant
before touching a row. The protection comes from the application connecting as a
role that owns nothing.

What this revision deliberately does not add
---------------------------------------------

**No audit table.** ``docs/security-model.md`` section 8.6 asks for one -- "log
accesses to the sensitive assets: ... exporting a household, deleting a household"
-- and an erasure is exactly the event an operator must be able to prove they
honoured. A table is still the wrong shape for it, twice over.

A row that names the erased household is either scoped to that household, in which
case the ``CASCADE`` takes it and it proves nothing; or it survives the household,
in which case an article 17 erasure has deliberately retained an identifier of the
subject who asked to be forgotten. Hashing the identifier does not escape that:
recital 26 treats a value that anyone holding the original can re-link as personal
data, and everybody who could ask the question holds the original.

So the record is a **structured log line**, written by ``services/privacy.py`` at
the moment of the erasure, carrying the household identifier and a count of rows
removed per table and nothing else -- no name, no email, no product. That is
strictly less than what every ordinary request already writes (``infra/logging.py``
puts ``household_id`` on every record), it lands in the operator's log pipeline
rather than in the database the erasure just emptied, and section 8.4 already
fixes its retention at thirty days. The data subject gets the same counts back in
the response body, which is the receipt they can keep.

**No object-storage hook.** Section 8.5 requires that erasure delete the receipt
images *before* the row, and calls a partial erasure presented as a complete one a
non-compliance. This application stores no images at all -- revision ``0012`` made
``receipt.image_object_key`` nullable precisely because every path writes NULL --
and there is no object-storage client anywhere in the repository to call. Adding a
no-op that pretends to have cleaned a bucket would be the non-compliance that
section warns about, written in code. ``services/privacy.py`` instead **refuses**
the erasure when it finds a receipt carrying a retained key, and says why: a
deployment that reintroduced image retention has to reintroduce its deletion too.

*Rollback.* ``downgrade`` drops the four policies and disables the switch, which
returns ``household`` to the unrestricted state every revision before this one left
it in. Nothing is lost: no data, no column, and no behaviour except the refusal
itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tenant root. It carries no ``household_id``: its own primary key *is* the
#: tenant, which is why the predicate below compares ``id`` rather than a column
#: every other policy in the schema names.
_TABLE: Final = "household"

#: The helper revision ``0004`` created. Reading the posted tenant through it
#: rather than through ``current_setting`` keeps one definition of "which
#: household is this transaction serving" for the whole schema.
_TENANT_FUNCTION: Final = "chaudron_current_household"

#: Named after the three commands they leave alone and the one they do not, so
#: ``\d household`` reads as the decision rather than as four similar strings.
_OPEN_POLICIES: Final[tuple[tuple[str, str], ...]] = (
    ("household_read_unrestricted", "FOR SELECT USING (true)"),
    ("household_insert_unrestricted", "FOR INSERT WITH CHECK (true)"),
    ("household_update_unrestricted", "FOR UPDATE USING (true) WITH CHECK (true)"),
)

_DELETE_POLICY: Final = "household_erasure_is_scoped"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")

    for name, clause in _OPEN_POLICIES:
        op.execute(f"CREATE POLICY {name} ON {_TABLE} {clause}")

    op.execute(
        f"""
        CREATE POLICY {_DELETE_POLICY} ON {_TABLE}
        FOR DELETE USING (id = {_TENANT_FUNCTION}())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {_DELETE_POLICY} ON {_TABLE} IS "
        "'A household may only be erased by a transaction already scoped to it. "
        "An unscoped transaction posts no tenant, the comparison is NULL, and the "
        "policy shows no row -- so nothing outside a resolved request can delete "
        "one. The membership and owner checks live in api/deps.py; this is the "
        "engine half.'"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_DELETE_POLICY} ON {_TABLE}")
    for name, _clause in _OPEN_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {name} ON {_TABLE}")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
