"""Where a household's export destination lives, as a port over its table.

This module is the read port; the table arrived with migration ``0008`` and the
adapter is
:class:`chaudron.infra.repositories.shopping_export_target.SqlShoppingExportTargetStore`.
The port was written against the final shape before the table existed -- the same
way :class:`chaudron.domain.shopping.DeclinedRepurchaseStore` was -- so nothing in
the service layer had to change when it landed.

The table ADR-0010 asked for, and migration ``0008`` created::

    shopping_export_target
      id                    uuid    primary key
      household_id          uuid    not null references household(id) on delete cascade
      target_code           text    not null            -- 'todoist'
      token_ciphertext      bytea   not null      -- AES-256-GCM, AAD (domain, household, id)
      token_last4           varchar(4)  not null
      token_encryption_key_id varchar(32) not null
      external_list_id      text                        -- project id, null means Inbox
      consented_at          timestamptz not null        -- see below
      consent_revoked_at    timestamptz
      created_at            timestamptz not null default now()
      updated_at            timestamptz not null default now()
      unique (household_id, target_code)
      unique (household_id, id)        -- the composite key every tenant table carries

``consented_at`` is ``not null`` on purpose, and it is the only column here that
is a legal requirement rather than an engineering one. A shopping list says what
identifiable people eat, which in a household with an allergy or an observance is
health or religious data; sending it to a third party needs an agreement that was
actually given, is dated, and can be withdrawn. A nullable column would let a row
exist without one, and the first ``INSERT`` that forgot it would be a breach
rather than a bug. ``consent_revoked_at`` is what withdrawal writes: the row stays
so the household can see what it once allowed, and
:meth:`StoredExportTarget.is_consented` stops returning true.

Row-level security applies as it does to every tenant table (ADR-0006, migration
``0004``): ``household_id`` with the composite unique key, and the policy posted
by ``SET LOCAL chaudron.household_id``.

``token_ciphertext`` is sealed under
:data:`~chaudron.infra.crypto.EXPORT_TOKEN_DOMAIN`, the prefix ADR-0010 made a
prerequisite of this table, so a ciphertext cannot be replayed from a provider
configuration row even if the two happened to share an identifier.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chaudron.infra.crypto import SealedCredential

__all__ = ["ShoppingExportTargetStore", "StoredExportTarget"]


@dataclass(frozen=True, slots=True)
class StoredExportTarget:
    """One registered destination, as the row will hand it over.

    Carries the *sealed* token, never a decrypted one: the plaintext exists only
    inside :meth:`chaudron.infra.todo.factory.ShoppingExportFactory.exporter_for`,
    for as long as it takes to build a header. Nothing that outlives a request
    holds it.
    """

    target_id: uuid.UUID
    household_id: uuid.UUID
    target_code: str
    sealed_token: SealedCredential
    external_list_id: str | None = None
    consented_at: dt.datetime | None = None
    consent_revoked_at: dt.datetime | None = None
    #: Who registered it, when the row records that (migration ``0014``). ``None``
    #: for a row that predates the column.
    registered_by_user_id: uuid.UUID | None = None
    #: Whether that person is **still** a member of this household, re-read on
    #: every send. See :attr:`registrant_has_left`.
    registrant_is_member: bool = True

    @property
    def is_consented(self) -> bool:
        """Whether this household currently agrees to its list leaving the instance."""
        return self.consented_at is not None and self.consent_revoked_at is None

    @property
    def registrant_has_left(self) -> bool:
        """Whether the agreement is held by somebody no longer in the household.

        Kept apart from :attr:`is_consented` on purpose, because the two are
        different facts with different remedies: an agreement *withdrawn* is
        answered by granting it again, and an agreement whose author has gone is
        answered by an owner deciding whether the household still wants it. A
        single boolean would have sent the household to re-tick a box rather than
        to look at who is filing their groceries where.

        ``False`` when the registrant is unknown (``NULL``, a row older than the
        column): there is nobody to have left, and refusing an export over a
        missing audit field would break every installation on upgrade.
        """
        return self.registered_by_user_id is not None and not self.registrant_is_member


@runtime_checkable
class ShoppingExportTargetStore(Protocol):
    """Reads a household's registered destination. Reads only.

    There is no write method here, and there will not be one in this port:
    registering a destination means accepting a token, which is a different
    operation with a different threat model (it must never be logged, must be
    validated against the recipient before it is stored, and must record a
    consent). That belongs to the endpoint that creates the row, not to the one
    that sends a list.
    """

    async def target_for(
        self, household_id: uuid.UUID, target_code: str
    ) -> StoredExportTarget | None: ...
