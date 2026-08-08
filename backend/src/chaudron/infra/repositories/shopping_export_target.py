"""The registered destination of one household, read for exactly one purpose.

This is the SQL half of :class:`chaudron.infra.todo.store.ShoppingExportTargetStore`,
which the export factory has been written against since ADR-0010 and which had no
implementation until the table existed. It is a **read** adapter and stays one:
the port has no write method, because registering a destination means accepting a
third party's token and recording an agreement, which is a different operation
with a different threat model. That one lives in
:mod:`chaudron.services.export_targets`.

Two things here are decisions rather than mechanics.

*The ciphertext is undeferred explicitly, once, here.* ``token_ciphertext`` is a
deferred column, so a plain ``select`` does not load it -- that is the point of
the deferral, and it makes every place that *does* read the secret a greppable
gesture. This module is that place, and the read happens only on the path that is
about to build an ``Authorization`` header.

*What comes back is sealed.* :class:`~chaudron.infra.todo.store.StoredExportTarget`
carries a :class:`~chaudron.infra.crypto.SealedCredential`, never a plaintext, and
:func:`~chaudron.infra.todo.credentials.sealed_export_token` stamps the AAD domain
so the value can only be opened as what it is. The plaintext appears inside
``ShoppingExportFactory.exporter_for`` and nowhere that outlives a request.

*Consent is not filtered here.* A revoked destination is returned, with its dates,
and refused one layer up by ``ShoppingExportFactory`` with a sentence naming the
remedy. Filtering it away in the query would turn "you withdrew your agreement" --
which a household can act on -- into "no destination is registered", which sends
them to re-enter a token they already have.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from chaudron.domain.models import HouseholdMember, ShoppingExportTarget
from chaudron.infra.todo.credentials import sealed_export_token
from chaudron.infra.todo.store import StoredExportTarget

__all__ = ["SqlShoppingExportTargetStore"]


class SqlShoppingExportTargetStore:
    """:class:`ShoppingExportTargetStore` over PostgreSQL.

    Takes the session and never commits: the transaction belongs to the request
    (``infra/db.py``), so an export and whatever else the same call writes either
    both happen or neither does.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def target_for(
        self, household_id: uuid.UUID, target_code: str
    ) -> StoredExportTarget | None:
        """The household's destination of that kind, sealed token included.

        ``household_id`` is in the predicate even though row-level security
        already scopes the table (migration ``0008``): the policies protect the
        application role, this predicate protects the maintenance identities that
        own the table and are therefore exempt from them.
        """
        found = (
            await self._session.execute(
                select(
                    ShoppingExportTarget,
                    # Evaluated per send rather than cached on the row, for the
                    # reason `chaudron_resolve_machine_token` re-joins
                    # `household_member` on every request (migration `0011`): a
                    # membership that ended has to stop granting *now*, not at
                    # whatever later moment somebody remembers to sweep the table.
                    # There is no API that removes a membership today, so a
                    # column updated by the removal path would be a column nothing
                    # ever writes.
                    exists().where(
                        HouseholdMember.household_id == ShoppingExportTarget.household_id,
                        HouseholdMember.user_id == ShoppingExportTarget.registered_by_user_id,
                    ),
                )
                .where(
                    ShoppingExportTarget.household_id == household_id,
                    ShoppingExportTarget.target_code == target_code,
                )
                .options(undefer(ShoppingExportTarget.token_ciphertext))
            )
        ).first()
        if found is None:
            return None
        row, registrant_is_member = found
        return StoredExportTarget(
            target_id=row.id,
            household_id=row.household_id,
            target_code=row.target_code,
            sealed_token=sealed_export_token(
                household_id=row.household_id,
                target_id=row.id,
                ciphertext=row.token_ciphertext,
                key_id=row.token_encryption_key_id,
            ),
            external_list_id=row.external_list_id,
            consented_at=row.consented_at,
            consent_revoked_at=row.consent_revoked_at,
            registered_by_user_id=row.registered_by_user_id,
            registrant_is_member=registrant_is_member,
        )
