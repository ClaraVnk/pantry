"""Data-subject rights over a household: access, portability and erasure.

GDPR articles 15, 20 and 17, and the one open item revision ``0016`` did not
touch (``docs/security-pentest-2026-08-04.md``, O-01). ``docs/security-model.md``
section 8.5 said of the first two "To be built. Nothing exists.", and of the third
that the ``CASCADE`` was ready and nothing could trigger it.

Five decisions are taken here rather than described, because each one is a place
where a plausible implementation would be wrong.

**The export is generated from the schema, not from a hand-written list.** Every
table carrying ``household_id`` is walked, every column of it is exported, and the
only exceptions are the five in :data:`WITHHELD_COLUMNS` -- each a credential,
each with the reason attached. A hand-written projection would silently stop
disclosing the column somebody adds next month, and under-disclosure is an
article 15 failure in exactly the way over-disclosure is a security one. The
inverse risk is real too, which is why the deny-list is asserted against the model
in ``tests/api/test_privacy.py``: a new secret column that nobody denies fails the
build rather than appearing in an export.

**The Open Food Facts catalogue is not the household's to hand over or to
erase.** ``product.household_id IS NULL`` is shared reference data with no tenant
(``docs/data-model.md``, ADR-0008), and the security model's own table of legal
bases says so: *"Not applicable. No personal data: it is a shared external
reference."* So the private products are exported as the household's rows, and
the public ones a household's rows *point at* are exported separately, in a
reduced form, labelled as reference data. Erasure removes the first and leaves
the second: the ``CASCADE`` on a nullable foreign key already behaves that way,
and this module does not second-guess it.

**Nothing is anonymised to make the numbers balance.** Section 8.4 proposes
keeping ``receipt_line.raw_label`` long-term "after anonymising the link to the
household", and that proposal is deliberately **not** applied at erasure time. The
column is ``NOT NULL``, so there is no link to null; and a till label with a
timestamp and a merchant is re-identifiable whatever the schema calls it. Revision
``0014`` refused to invent a registrant and ``0016`` refused to invent a consent
date, both because a fabricated fact is worse than a missing one. Erasure is the
same trap seen from the other side: a row kept because it "no longer names
anybody" is a claim about re-identification that nobody here is in a position to
make. Erasure erases.

**A refusal beats a partial erasure.** Section 8.5 requires the receipt images to
go before the row, and calls a partial erasure presented as complete a
non-compliance. This application retains no image -- revision ``0012`` made
``receipt.image_object_key`` nullable because every path writes NULL -- and there
is no object-storage client in the repository to call. Rather than delete the rows
and let the response imply a bucket was cleaned, :meth:`PrivacyService.erase`
refuses when it finds a retained key and says what has to happen first.

**One log line, no audit table.** Section 8.6 wants the erasure recorded. The
record is a structured log line carrying the household identifier and a count of
rows per table -- strictly less than what ``infra/logging.py`` already writes on
every request, and outside the database the erasure just emptied. A table would
either be scoped to the household and cascade away with it, or survive it and
retain an identifier of the subject who asked to be forgotten; migration ``0017``
argues that at length. **Nothing else is logged from this module**: no name, no
email, no product, no member. The counts are integers, and ``_scrub`` leaves
integers alone.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Column, Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Base
from chaudron.infra.db import set_transaction_household

__all__ = [
    "EXPORT_VERSION",
    "TENANT_TABLES",
    "WITHHELD_COLUMNS",
    "ErasureBlockedError",
    "ErasureReceipt",
    "IncompleteErasureError",
    "PrivacyService",
]

logger = logging.getLogger(__name__)

#: Bumped when the *shape* of the document changes, never when a column is added
#: or removed. A recipient parsing an export needs to know which reading rules
#: apply; it does not need a new version every time the schema grows a field.
EXPORT_VERSION: Final = 1

_TENANT_COLUMN: Final = "household_id"

#: The five tables addressed by name rather than through the generic walk below.
#: Taken from ``Base.metadata`` rather than through ``Model.__table__``, which the
#: SQLAlchemy stubs type as ``FromClause`` -- enough for a query and not enough for
#: ``delete()`` or for reading ``.name`` under ``mypy --strict``.
_HOUSEHOLD: Final[Table] = Base.metadata.tables["household"]
_HOUSEHOLD_MEMBER: Final[Table] = Base.metadata.tables["household_member"]
_USER_ACCOUNT: Final[Table] = Base.metadata.tables["user_account"]
_PRODUCT: Final[Table] = Base.metadata.tables["product"]
_RECEIPT: Final[Table] = Base.metadata.tables["receipt"]

#: Every table that belongs to a household, in dependency order. Read from the
#: model for the reason ``tests/tenancy/test_schema_tenant_guard.py`` gives: the
#: table nobody remembers is the one the leak -- or here, the omission -- comes
#: from.
TENANT_TABLES: Final[tuple[Table, ...]] = tuple(
    table for table in Base.metadata.sorted_tables if _TENANT_COLUMN in table.c
)

#: Columns a household holds and this export does not hand back, with the reason.
#:
#: All five are credential material, and that is the only admissible reason to be
#: on this list. An export is a file: it is mailed, backed up, put on a USB stick
#: and read by whoever finds it. A ciphertext plus the key identifier that names
#: which master key opens it is an offline attack somebody can keep; the four
#: characters of ``*_last4`` beside it are not, and they stay, because they are
#: what lets a household recognise which of its own keys is installed.
#:
#: ``token_hash`` is not decryptable, and it is still not exported: it is the
#: lookup key of a live bearer credential (``uq_machine_token_token_hash``), and a
#: document that carries one is a document that can be replayed against the
#: instance if the hashing is ever weakened or the digest is ever accepted
#: directly.
WITHHELD_COLUMNS: Final[Mapping[tuple[str, str], str]] = {
    ("llm_provider_config", "api_key_ciphertext"): (
        "SECRET: AES-256-GCM ciphertext of a third party's API key. Exported as "
        "api_key_last4, which identifies the key without being one."
    ),
    ("llm_provider_config", "api_key_encryption_key_id"): (
        "Names which master key opens the ciphertext above; useless alone and "
        "half of an offline attack beside it."
    ),
    ("shopping_export_target", "token_ciphertext"): (
        "SECRET: AES-256-GCM ciphertext of a third party's personal token. Exported as token_last4."
    ),
    ("shopping_export_target", "token_encryption_key_id"): (
        "Names which master key opens the ciphertext above; useless alone and "
        "half of an offline attack beside it."
    ),
    ("machine_token", "token_hash"): (
        "The lookup key of a live bearer credential. The token itself left this "
        "instance once, at creation; prefix and last4 are what identify it here."
    ),
}

#: Held about the household and outside the export, with the reason. Rendered into
#: the document itself: article 15(1) is a right to know what is processed, and a
#: silent omission answers it less well than a named one.
_WITHHELD_ELSEWHERE: Final[Mapping[str, str]] = {
    "household.calendar_feed_epoch": (
        "A revocation counter, not personal data, and one component of the CalDAV "
        "credential this household's feed is derived from."
    ),
    "user_session": (
        "Sessions belong to an account rather than to a household, so they are "
        "outside the scope of this document. They hold the digest of a live "
        "cookie."
    ),
    "user_account.password_hash": (
        "A credential, and never disclosed to anybody, the owner included."
    ),
    "product.off_payload (public catalogue rows)": (
        "The raw Open Food Facts record for a shared catalogue entry. Public "
        "reference data with no household, reproducible from the barcode."
    ),
}


class ErasureBlockedError(Exception):
    """Erasure would leave data behind that this application cannot reach."""

    def __init__(self, retained_images: int) -> None:
        super().__init__(
            f"{retained_images} receipt(s) in this household still name a stored "
            f"image. Deleting the rows would leave those objects in place, and an "
            f"erasure that is reported as complete while objects survive is a "
            f"non-compliance rather than a bug. This deployment reintroduced image "
            f"retention (revision 0012 turned it off); it has to delete the objects "
            f"and clear receipt.image_object_key before the erasure can be honoured."
        )
        self.retained_images = retained_images


class IncompleteErasureError(RuntimeError):
    """The household row is gone and rows that should have cascaded are not.

    Raised **after** the delete and before anything is committed, so the
    transaction rolls back and the caller gets a failure rather than a receipt
    that says the data is gone. There is no worse outcome for this feature than
    reporting a successful erasure over surviving rows, so the post-condition is
    checked rather than assumed: what makes the ``CASCADE`` total is a property of
    seventeen foreign keys, and no test in production runs.
    """


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """What was removed, so the person who asked has something to keep."""

    household_id: uuid.UUID
    erased_at: datetime
    #: Table name to the number of rows removed, counted before the delete.
    rows_erased: Mapping[str, int]


class PrivacyService:
    """Reads and erases one household, and holds no opinion about who may ask.

    Authorisation is ``api/deps.py`` (``OwnerHouseholdDep``) and tenancy is
    PostgreSQL (migration ``0004`` for the rows, ``0017`` for the household
    itself). This class is handed a household identifier that both have already
    agreed on.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ----------------------------------------------------------------- #
    # Article 15 and 20
    # ----------------------------------------------------------------- #

    async def export(self, household_id: uuid.UUID, *, requested_by: uuid.UUID) -> dict[str, Any]:
        """The household, as a structured document in a commonly used format.

        Article 20 asks for "a structured, commonly used and machine-readable
        format"; this is JSON with one key per table and the schema's own column
        names, which is the only naming a recipient can check against the
        published data model.

        *requested_by* is not an authorisation -- it has been checked already --
        it decides one thing: whose account fields the document carries. The
        household's other members appear with an identifier and a display name,
        which is what makes the rows readable; their email address and their sign-in
        history are their data and not this household's to hand out.
        """
        household = await self._household_row(household_id)
        sections: dict[str, list[dict[str, Any]]] = {}
        for table in TENANT_TABLES:
            sections[table.name] = await self._rows_of(table, household_id)

        return {
            "export_version": EXPORT_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "household_id": str(household_id),
            "household": household,
            "accounts": await self._member_accounts(household_id, requested_by=requested_by),
            "tables": sections,
            "referenced_public_products": await self._referenced_public_products(sections),
            "withheld": self._withheld_notice(),
        }

    async def _household_row(self, household_id: uuid.UUID) -> dict[str, Any]:
        row = (
            (await self._session.execute(select(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id)))
            .mappings()
            .one()
        )
        return {
            name: _render(value)
            for name, value in row.items()
            if name != "calendar_feed_epoch"  # see _WITHHELD_ELSEWHERE
        }

    async def _rows_of(self, table: Table, household_id: uuid.UUID) -> list[dict[str, Any]]:
        withheld = {column for (name, column) in WITHHELD_COLUMNS if name == table.name}
        statement = (
            select(table)
            .where(table.c[_TENANT_COLUMN] == household_id)
            .order_by(*table.primary_key.columns)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        return [
            {name: _render(value) for name, value in row.items() if name not in withheld}
            for row in rows
        ]

    async def _member_accounts(
        self, household_id: uuid.UUID, *, requested_by: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The accounts that currently belong to this household.

        Deliberately the *current* members and not every account identifier the
        rows happen to mention. An ``actor_user_id`` left behind by somebody who
        has since gone is exported as the bare identifier it is: resolving it to a
        name would disclose a person who is no longer part of this household to
        whoever now owns it.
        """
        members, accounts = _HOUSEHOLD_MEMBER, _USER_ACCOUNT
        rows = (
            await self._session.execute(
                select(
                    accounts.c.id,
                    accounts.c.display_name,
                    accounts.c.email,
                    accounts.c.last_login_at,
                )
                .join(members, members.c.user_id == accounts.c.id)
                .where(members.c.household_id == household_id)
                .order_by(accounts.c.id)
            )
        ).all()
        exported: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"id": str(row.id), "display_name": row.display_name}
            if row.id == requested_by:
                entry["email"] = row.email
                entry["last_login_at"] = _render(row.last_login_at)
            exported.append(entry)
        return exported

    async def _referenced_public_products(
        self, sections: Mapping[str, Sequence[Mapping[str, Any]]]
    ) -> list[dict[str, Any]]:
        """The shared catalogue entries this household's rows point at.

        Not the household's personal data -- ``docs/security-model.md`` section 8.3
        is explicit that the Open Food Facts cache carries none, which is why it
        has no tenant column -- and the export would be unreadable without it: a
        lot referencing a product identifier and nothing else says nothing about
        what is in the cupboard. Reduced to the fields that make a row legible; the
        raw upstream payload is public and reproducible from the barcode.
        """
        wanted: set[uuid.UUID] = set()
        for table_name, column_name in _PRODUCT_REFERENCES:
            for row in sections.get(table_name, ()):
                value = row.get(column_name)
                if isinstance(value, str):
                    wanted.add(uuid.UUID(value))
        if not wanted:
            return []

        products = _PRODUCT
        rows = (
            (
                await self._session.execute(
                    select(
                        products.c.id,
                        products.c.gtin,
                        products.c.name,
                        products.c.brand,
                        products.c.category_tag,
                        products.c.source,
                        products.c.off_synced_at,
                    )
                    .where(products.c.id.in_(wanted), products.c.household_id.is_(None))
                    .order_by(products.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [{name: _render(value) for name, value in row.items()} for row in rows]

    def _withheld_notice(self) -> dict[str, str]:
        notice = {
            f"{table}.{column}": reason for (table, column), reason in WITHHELD_COLUMNS.items()
        }
        notice.update(_WITHHELD_ELSEWHERE)
        return notice

    # ----------------------------------------------------------------- #
    # Article 17
    # ----------------------------------------------------------------- #

    async def erase(self, household_id: uuid.UUID) -> ErasureReceipt:
        """Delete the household and everything that belongs to it.

        The ``ON DELETE CASCADE`` on every tenant table does the work; this method
        exists to make three things true that the cascade alone does not.

        *The transaction is scoped before anything is deleted.* Posting the tenant
        explicitly arms migration ``0017``'s policy and, just as usefully, raises
        rather than proceeds if this transaction was already serving a different
        household (``infra/db.py``, :class:`~chaudron.infra.db.TenantScopeError`).

        *Data this application cannot reach blocks the erasure* rather than being
        silently left behind -- see :class:`ErasureBlockedError`.

        *The result is verified by reading the tables back.* If any row survived,
        the exception rolls the whole thing back: a receipt that says "erased" over
        surviving rows is the one outcome worse than a failure.
        """
        await set_transaction_household(self._session, household_id)

        retained = await self._retained_image_count(household_id)
        if retained:
            raise ErasureBlockedError(retained)

        rows_erased = await self._row_counts(household_id)
        if rows_erased[_HOUSEHOLD.name] != 1:
            # `api/deps.py` proved the caller owns a membership of this household,
            # so the row existed when the request was authorised. Not finding it
            # here means somebody erased it concurrently, and answering "erased"
            # would credit this request with work it did not do.
            raise IncompleteErasureError(f"household {household_id} is not there to erase")

        await self._session.execute(delete(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id))

        survivors = {
            table: count for table, count in (await self._row_counts(household_id)).items() if count
        }
        if survivors:
            raise IncompleteErasureError(
                f"rows survived the erasure of household {household_id}: {survivors}"
            )

        erased_at = datetime.now(UTC)
        # The whole audit record, and everything it may carry: an identifier every
        # request already logs, and integers. See the module docstring.
        logger.info("household_erased", extra={"rows_erased": dict(rows_erased)})
        return ErasureReceipt(
            household_id=household_id, erased_at=erased_at, rows_erased=rows_erased
        )

    async def _retained_image_count(self, household_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(_RECEIPT)
            .where(
                _RECEIPT.c[_TENANT_COLUMN] == household_id,
                _RECEIPT.c.image_object_key.is_not(None),
            )
        )
        return int(count or 0)

    async def _row_counts(self, household_id: uuid.UUID) -> dict[str, int]:
        """How many rows each table holds for this household, the root included."""
        counts: dict[str, int] = {}
        for table in TENANT_TABLES:
            count = await self._session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c[_TENANT_COLUMN] == household_id)
            )
            counts[table.name] = int(count or 0)
        count = await self._session.scalar(
            select(func.count()).select_from(_HOUSEHOLD).where(_HOUSEHOLD.c.id == household_id)
        )
        counts[_HOUSEHOLD.name] = int(count or 0)
        return counts


# --------------------------------------------------------------------------- #
# Reading the model
# --------------------------------------------------------------------------- #


def _references_product(column: Column[Any]) -> bool:
    return any(key.column.table.name == "product" for key in column.foreign_keys)


#: Every ``(table, column)`` in a tenant table that points at the catalogue.
#: Derived from the foreign keys rather than listed, so a new reference is picked
#: up by the export the day it is declared -- five of these exist today and each
#: one was added by a different feature.
_PRODUCT_REFERENCES: Final[tuple[tuple[str, str], ...]] = tuple(
    (table.name, column.name)
    for table in TENANT_TABLES
    for column in table.c
    if _references_product(column)
)


def _render(value: Any) -> Any:
    """One column value, as something ``json.dumps`` accepts and a human can read.

    ``Decimal`` becomes a string rather than a float: a quantity that arrives back
    as ``0.30000000000000004`` is not the quantity that was stored, and the whole
    point of a portability export is that the numbers survive the round trip.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return _render(value.value)
    if isinstance(value, Mapping):
        return {str(key): _render(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_render(item) for item in value]
    if isinstance(value, bytes | bytearray):  # pragma: no cover - every one is withheld
        raise AssertionError(
            "a binary column reached the export; every one of them is credential "
            "material and belongs in WITHHELD_COLUMNS"
        )
    return str(value)
