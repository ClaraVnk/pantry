"""record whether a receipt reading was cut short, and what it left out

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-06 09:00:00.000000+00:00

Two fields the API has always declared and never once returned truthfully.

``ReceiptProposal`` carries ``truncated`` and ``degradation_notice``, and
``routers/receipts.py`` serialises both. Both are computed correctly at the point
of reading -- ``services/receipts.py`` sets ``truncated`` when a document held
more candidate lines than the ceiling, and lifts ``degradation_notice`` off what
the model itself reported leaving out. Neither survived the write: ``_store``
persisted the reading, ``_proposal`` rebuilt the response from the stored row, and
the row had nowhere to put them. So ``truncated`` was hard-coded ``False`` and
``degradation_notice`` fell back to its ``None`` default, on every response, for
every receipt.

**Why that is worse than a missing feature.** A field that is absent tells a
client to go and look; a field that is present and always says "nothing was left
out" tells it there is nothing to look for. The truncation case is exactly the one
where a household must be told: the ceiling exists so a hostile document cannot
exhaust the instance, and a legitimate long receipt that trips it produces a
proposal that silently *omits purchases*. Confirming it writes a stock that is
short by however many lines went over the limit, with nothing on screen having
suggested so.

The same argument decided ADR-0005's ``degraded`` contract: a reading made without
a capability has to say which capability was missing, or the household reads a
partial answer as a complete one.

**Not backfilled.** Existing rows get ``lines_truncated = false`` from the server
default and a NULL notice -- but that is the default's doing, not an assertion
about those readings. Whether an already-imported receipt was cut short is not
recoverable: the ceiling is applied at read time and the surplus lines were never
stored. Writing ``false`` because it is the common case would be inventing the
provenance this revision exists to record, which is the reasoning revisions `0014`
and `0016` both used when they declined to invent a registrant and a consent.
The distinction that remains legible is that no row *written before this revision*
can carry a notice, and that is a property of the schema history rather than a
claim about any receipt.

Reversible in full: both columns drop, and nothing else reads them.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "receipt",
        sa.Column(
            "lines_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "True when the document held more candidate lines than the ceiling, "
                "so the stored proposal omits purchases. Surfaced at review: "
                "confirming a truncated reading writes a stock that is short, and "
                "nothing else on that screen would say so. Rows written before "
                "revision 0019 carry the default rather than a finding."
            ),
        ),
    )
    op.add_column(
        "receipt",
        sa.Column(
            "degradation_notice",
            sa.Text(),
            nullable=True,
            comment=(
                "What the reading left out, in French, when a capability was "
                "missing (ADR-0005 'degraded'). NULL means nothing was left out. "
                "Stored rather than recomputed because the proposal is persisted "
                "and re-read: a notice that lived only in the first response would "
                "vanish on the reload that precedes every confirmation."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("receipt", "degradation_notice")
    op.drop_column("receipt", "lines_truncated")
