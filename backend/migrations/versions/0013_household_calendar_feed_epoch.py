"""give each household its own calendar feed revocation counter

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-04 15:00:00.000000+00:00

The CalDAV feed credential is *derived*, never stored:
``HMAC(instance_key, purpose || household_id)``. That is what keeps it out of the
database and out of every backup, and it is also why nothing about it could be
deleted. Until this revision the only levers were instance-wide --
``CHAUDRON_CALENDAR_FEED_ENABLED=false`` and ``CHAUDRON_CALENDAR_FEED_EPOCH`` --
so withdrawing one household's feed meant disconnecting every household on the
deployment.

What that cost, in the case that actually happens: a person removed from
``household_member`` loses their session on the next request and keeps reading the
stock over CalDAV, because the feed authenticates with a credential rather than
with a membership. A flatmate who moved out, a former partner, an owner whose
access was withdrawn -- each of them keeps a permanent read of the household's
inventory (asset A3) if they ever opened the subscription page. Every property the
rest of the application has for revocation -- a session row that can be deleted, a
machine token that joins ``household_member`` on each request -- was bypassed by
this one parallel mechanism.

``calendar_feed_epoch`` closes it by putting a value the household controls inside
the derivation. Incrementing the column changes both halves of the pair, so the
old user name stops naming anything and the old password verifies against nothing;
``POST /v1/calendar/subscription/revoke`` is the one statement, exposed to the
owner alone.

*Why a counter and not a timestamp or a random token.* A counter only ever moves
forward, is readable in a support conversation ("your feed has been reset twice"),
and serialises to a fixed width inside the MAC input without a format decision.
A ``revoked_at`` timestamp would additionally have to answer "revoked until when",
and a stored random token would put a credential-shaped value back in the database
that this design deliberately keeps out of it.

*Default 1, ``NOT NULL``.* Existing rows land on the same value a fresh household
gets, which means the credentials already handed out keep working across this
migration: an upgrade must not silently disconnect the phones of an instance that
has the feed switched on. Revocation is then an explicit act, never a side effect
of deploying.

*Rollback.* ``downgrade`` drops the column, and with it every revocation performed
while it existed: a household that had revoked its feed returns to the credential
it revoked, because the derivation falls back to the instance epoch alone. That is
a real regression rather than a bookkeeping one, so it is written here rather than
discovered -- an operator rolling back past this revision has to rotate
``CHAUDRON_CALENDAR_FEED_EPOCH`` in the same move if any revocation has been used.

No row-level security to add: ``household`` is the table the tenant is *derived
from* and revision ``0004`` deliberately attaches no policy to it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "Revocation counter for this household's CalDAV feed credential, which is "
    "derived rather than stored. Mixed into the derivation, so incrementing it "
    "invalidates every device subscribed to this household and no other."
)


def upgrade() -> None:
    op.add_column(
        "household",
        sa.Column(
            "calendar_feed_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment=_COMMENT,
        ),
    )
    # Named without the `ck_household_` prefix the naming template already adds:
    # the declared string is a component of the final name, not the whole of it
    # (revision 0010, tests/test_schema_naming_guard.py).
    op.create_check_constraint(
        "calendar_feed_epoch_positive", "household", "calendar_feed_epoch > 0"
    )


def downgrade() -> None:
    # The component, not the rendered name: `drop_constraint` puts the template's
    # `ck_household_` prefix back on, exactly as `create_check_constraint` did.
    op.drop_constraint("calendar_feed_epoch_positive", "household", type_="check")
    op.drop_column("household", "calendar_feed_epoch")
