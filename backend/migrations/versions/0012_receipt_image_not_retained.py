"""stop requiring a stored image on a receipt

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-04 12:00:00.000000+00:00

``receipt.image_object_key`` was declared ``NOT NULL`` by revision ``0001``, on
the assumption that a photographed receipt would be kept in object storage and
the row would point at it. The receipt import, written since, does not keep it,
and the contract says why.

``docs/api-contract-v1.1-dietary.md`` section 6ter, on what a spending history
with the chain, the date and the amount reveals about a household -- where it
shops, at what rhythm, on what income, and when it is away:

    Il n'y a donc **aucune raison de conserver l'image du ticket** après
    extraction du total et des lignes, et le suivi de budget ne doit pas devenir
    le prétexte à la garder.

``docs/security-model.md`` classes the inventory as asset A3 and the budget
alongside it. The image is strictly more revealing than either: it carries the
store's address, the till, the hour, the loyalty number and, on many chains, the
last four digits of the card. It is read once, in memory, for the length of one
request.

*Why nullable rather than dropped.* Dropping the column would decide, for every
future deployment, that no receipt image may ever be retained -- including by an
operator who has a reason, a retention policy and a bucket, and who would then
have to migrate a table to express it. Nullable says what is true today: this
application writes ``NULL`` on every path, and the column is there for a use it
does not make. It also keeps the rows of any deployment that *did* store a key
readable, which dropping would not.

The ``image_sha256`` column stays ``NOT NULL`` and stays populated. It is the
duplicate-import guard (``uq_receipt_household_sha256``) for the most ordinary
mobile failure there is -- a photo re-sent after a perceived timeout -- and a
digest of bytes nobody stored cannot reconstruct them.

*Rollback.* ``downgrade`` fills the column before restoring the constraint,
because by then every row this application wrote holds ``NULL`` and the
``ALTER`` would fail on the first of them. The filler is a sentinel that reads as
what it is rather than a plausible-looking key: a downgraded database must not
appear to hold images it never had.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What ``downgrade`` writes where the application wrote nothing. Deliberately
#: not a path: anything that looked like one would be read as a key by whatever
#: reintroduced object storage, and a 404 in a retrieval loop is a worse bug than
#: a value that plainly says no image exists.
_SENTINEL: Final = "(no image retained)"


def upgrade() -> None:
    op.alter_column(
        "receipt",
        "image_object_key",
        existing_type=sa.Text(),
        nullable=True,
        comment=(
            "NULL means no image was retained, which is what this application "
            "always writes: the receipt is read in memory and discarded (api "
            "contract 6ter). The column exists for a deployment that decides "
            "otherwise and has a retention policy to go with it."
        ),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE receipt SET image_object_key = :sentinel WHERE image_object_key IS NULL"
        ).bindparams(sentinel=_SENTINEL)
    )
    op.alter_column(
        "receipt",
        "image_object_key",
        existing_type=sa.Text(),
        nullable=False,
        comment=None,
        existing_comment=(
            "NULL means no image was retained, which is what this application "
            "always writes: the receipt is read in memory and discarded (api "
            "contract 6ter). The column exists for a deployment that decides "
            "otherwise and has a retention policy to go with it."
        ),
    )
