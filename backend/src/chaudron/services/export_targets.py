"""Registering, inspecting and withdrawing a household's export destination.

The write side of ADR-0010 section 7, and the counterpart of
:class:`chaudron.infra.repositories.shopping_export_target.SqlShoppingExportTargetStore`,
which only reads. The split is the one ``services/providers.py`` already makes
between :class:`ProviderService` and :class:`ProviderCredentialService`, for the
same reason: the read side must never be able to produce a token, and this module
is the single place in the application that accepts one.

Four rules govern everything below.

**The token is never returned, never logged, never put in a URL.** It arrives in
a request body, is sealed within one method call, and the only fragment that
survives is ``token_last4`` -- which exists so a household can recognise *which*
of its tokens is installed and is useless to anyone else. Every refusal in this
module is phrased from our own fields; the submitted value is never quoted, not
even to say it is too short, because a validation message is exactly the kind of
string that ends up in a client log.

**Consent is an act, not a column that gets filled in.** :meth:`register` refuses
outright when the caller did not grant it, rather than storing the row and
leaving ``consented_at`` for later: the whole point of the ``NOT NULL`` in
migration ``0008`` is that no row can exist without an agreement. Granting is
therefore always simultaneous with handing over a token, which is also the moment
the user is shown what will leave the instance.

**Withdrawal keeps the row.** :meth:`revoke` writes ``consent_revoked_at`` and
stops there. The household keeps the record of what it once authorised and when,
and the ciphertext stays because the columns are ``NOT NULL`` and because a
withdrawal is not a deletion request -- ``DELETE /v1/households`` is where erasure
lives. What changes is that ``is_consented`` stops returning true, and the
factory reads that on every single export, so the withdrawal takes effect at the
next send rather than at the next cleanup.

**Re-consenting means restating the token.** There is no "grant again" operation
that would flip a date on a row nobody looked at. A household that withdrew and
changed its mind registers again, which puts the same sentence about what leaves
the instance back in front of them.

**No probe of the destination before storing.** ADR-0010 section 8 forbids the
one documented Todoist call that would validate a token -- a ``sync`` read hands
back the account's whole dataset, which is more of somebody's life than this
application has any business receiving -- and the write call would create a task
nobody asked for. An empty-command ``sync`` is undocumented, so a build that
relied on it would reject valid tokens the day Todoist answered ``400``.
Rejecting a valid token is worse than accepting a dead one: a dead one surfaces
on the first export as ``ExportTargetRejected``, which already says "it was
revoked or regenerated. Enter a new one".
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import HouseholdMember, ShoppingExportTarget, UserAccount
from chaudron.infra.crypto import MIN_API_KEY_LENGTH, CredentialCipher
from chaudron.infra.todo.credentials import seal_export_token

logger = logging.getLogger(__name__)

__all__ = [
    "ExportConsentMissing",
    "ExportTargetConfigurationError",
    "ExportTargetNotRegistered",
    "ExportTargetService",
    "ExportTargetView",
    "InvalidExportToken",
    "RegisterTargetCommand",
    "UnsupportedExportTarget",
]


# --------------------------------------------------------------------------- #
# What the API sees
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExportTargetView:
    """One registered destination, in the only shape that may leave this layer.

    There is no ciphertext field and no token field, and that is structural
    rather than a convention: this type is what the router serialises, so a
    handler cannot disclose a token by forgetting to strip one.
    """

    target_code: str
    token_last4: str
    external_list_id: str | None
    consented_at: dt.datetime
    consent_revoked_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime
    #: The display name of whoever registered it, or ``None`` for a row that
    #: predates migration ``0014``. A name rather than an identifier, because the
    #: question a household asks in front of this screen is "who put that there?"
    #: and a UUID does not answer it. It is read through ``household_member``, so
    #: it can only ever be a name this household already knows.
    registered_by: str | None
    #: Whether that person is still a member. ``True`` when nobody is recorded:
    #: there is no one to have left, and an unknown registrant must not read as an
    #: absent one.
    registrant_is_member: bool

    @property
    def is_consented(self) -> bool:
        """Whether this household currently agrees to its list leaving the instance."""
        return self.consent_revoked_at is None

    @property
    def is_usable(self) -> bool:
        """Whether an export would actually be allowed to run right now.

        Consent **and** a registrant who is still here. Computed in one place
        because the listing endpoint decides from it whether to offer a button,
        and a button offered for a destination the export path refuses is worse
        than no button: it produces a 403 the household cannot explain.
        """
        return self.is_consented and self.registrant_is_member


@dataclass(frozen=True, slots=True)
class RegisterTargetCommand:
    """What registering a destination takes. ``consent_granted`` is not optional.

    It is a separate field rather than something inferred from the presence of a
    token, because the two are separate acts: pasting a credential says "I have
    an account there", and consenting says "send my household's shopping list to
    it". A build that inferred one from the other would be pre-ticking the box.
    """

    target_code: str
    token: str
    consent_granted: bool
    #: Who is granting it. Not optional and not inferred: a consent nobody signed
    #: is what left an excluded member's token exporting for months (audit
    #: AUD-027), and a default here would let a future caller reintroduce that by
    #: omission.
    registered_by_user_id: uuid.UUID
    external_list_id: str | None = None


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class ExportTargetConfigurationError(Exception):
    """Base of the refusals this module produces. Never carries a token."""


class UnsupportedExportTarget(ExportTargetConfigurationError):  # noqa: N818 -- mirrors the domain
    """A destination this build has no adapter for."""


class InvalidExportToken(ExportTargetConfigurationError):  # noqa: N818 -- mirrors the domain
    """The submitted token cannot be stored. The value itself is never quoted."""


class ExportConsentMissing(ExportTargetConfigurationError):  # noqa: N818 -- mirrors the domain
    """Registration was attempted without an agreement, which cannot be stored."""


class ExportTargetNotRegistered(ExportTargetConfigurationError):  # noqa: N818 -- mirrors the domain
    """No destination of that kind belongs to this household."""


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class ExportTargetService:
    """Accepts a token and an agreement; hands back everything except the token."""

    def __init__(
        self,
        session: AsyncSession,
        cipher: CredentialCipher,
        *,
        supported_targets: frozenset[str],
    ) -> None:
        self._session = session
        self._cipher = cipher
        #: Injected rather than imported, so the set of destinations is decided in
        #: one place (``infra/todo/factory.py``) and this service cannot drift into
        #: accepting a code the factory would then refuse to build an adapter for.
        self._supported = supported_targets

    async def list_targets(self, household_id: uuid.UUID) -> list[ExportTargetView]:
        """Every destination this household registered, ordered by code.

        ``token_ciphertext`` is a deferred column and is deliberately *not*
        undeferred here: listing destinations has no reason to load a secret, and
        the query that does is the one in the read store, on the export path.

        Two extra columns come back with each row, and both are about *who*.
        ``user_account`` is joined directly on the recorded registrant rather than
        through ``household_member``, so the name survives their departure -- which
        is the entire point of recording it: a household that cannot name the
        person whose token is installed cannot decide what to do about it. The
        membership itself is a separate ``EXISTS``, evaluated now rather than
        stored, for the reason ``infra/repositories/shopping_export_target.py``
        gives at the same join.
        """
        rows = await self._session.execute(
            select(
                ShoppingExportTarget,
                UserAccount.display_name,
                exists().where(
                    HouseholdMember.household_id == ShoppingExportTarget.household_id,
                    HouseholdMember.user_id == ShoppingExportTarget.registered_by_user_id,
                ),
            )
            .outerjoin(UserAccount, UserAccount.id == ShoppingExportTarget.registered_by_user_id)
            .where(ShoppingExportTarget.household_id == household_id)
            .order_by(ShoppingExportTarget.target_code)
        )
        return [
            _view(row, registered_by=display_name, registrant_is_member=is_member)
            for row, display_name, is_member in rows
        ]

    async def _read_back(self, household_id: uuid.UUID, target_code: str) -> ExportTargetView:
        """The view of one destination, through the one query that builds views.

        A write returns what a subsequent read would return, rather than a
        projection assembled from the objects that happened to be in hand. It
        costs one indexed select on an operation a household performs a handful of
        times in its life, and it buys the property that ``PUT`` and the following
        ``GET`` can never disagree about the registrant's name.
        """
        views = await self.list_targets(household_id)
        found = next((view for view in views if view.target_code == target_code), None)
        if found is None:  # pragma: no cover - the row was just written in this transaction
            raise ExportTargetNotRegistered(
                f"no {target_code} destination is registered for this household"
            )
        return found

    async def register(
        self, household_id: uuid.UUID, command: RegisterTargetCommand
    ) -> ExportTargetView:
        """Store a token and the agreement that came with it, replacing any earlier one.

        Idempotent by household and destination: a second registration is the
        rotation procedure, exactly as storing a provider key over an existing one
        is in ADR-0007. It also clears a previous withdrawal, because the caller
        has just agreed again -- and had to restate the token to do it.
        """
        if command.target_code not in self._supported:
            supported = ", ".join(sorted(self._supported))
            raise UnsupportedExportTarget(
                f"{command.target_code!r} is not a destination this instance can send to; "
                f"supported: {supported}"
            )
        if not command.consent_granted:
            # Refused before anything is written. The NOT NULL in migration 0008
            # makes a row without an agreement unrepresentable; this makes the
            # attempt a sentence rather than an IntegrityError.
            raise ExportConsentMissing(
                "a destination cannot be registered without an explicit agreement to "
                "send this household's shopping list to it"
            )

        # Pasted tokens arrive with trailing whitespace far more often than not,
        # and a stray newline turns a valid token into an authentication failure
        # nobody can see in a database dump.
        token = command.token.strip()
        if len(token) < MIN_API_KEY_LENGTH:
            raise InvalidExportToken(
                f"an export token of at least {MIN_API_KEY_LENGTH} characters is required"
            )

        row = await self._session.scalar(
            select(ShoppingExportTarget).where(
                ShoppingExportTarget.household_id == household_id,
                ShoppingExportTarget.target_code == command.target_code,
            )
        )
        if row is None:
            # The identifier is drawn here rather than left to the column default,
            # because it is half of the AAD: the ciphertext cannot be built until
            # the row it is bound to has a name.
            row = ShoppingExportTarget(
                id=uuid.uuid7(),
                household_id=household_id,
                target_code=command.target_code,
            )
            self._session.add(row)

        sealed = seal_export_token(self._cipher, token, household_id=household_id, target_id=row.id)
        row.token_ciphertext = sealed.ciphertext
        row.token_last4 = sealed.last4
        row.token_encryption_key_id = sealed.key_id
        row.external_list_id = command.external_list_id
        row.consented_at = dt.datetime.now(dt.UTC)
        row.consent_revoked_at = None
        # Rewritten on every registration, including a rotation over an existing
        # row: whoever last pasted a token is who is standing behind the agreement
        # from now on, and leaving the first registrant's name on somebody else's
        # token would be worse than recording nobody.
        row.registered_by_user_id = command.registered_by_user_id
        await self._session.flush()
        await self._session.refresh(row)

        # `target` and nothing else: the household is already on every log line
        # through `household_id_var`, and no field here can hold a token.
        logger.info("shopping_export_target_registered", extra={"target": row.target_code})
        return await self._read_back(household_id, row.target_code)

    async def revoke(self, household_id: uuid.UUID, target_code: str) -> ExportTargetView:
        """Withdraw the agreement, keeping the row and its history.

        Idempotent: withdrawing an agreement already withdrawn keeps the original
        date rather than moving it forward. The first refusal is the one that
        matters, and overwriting it would quietly rewrite when the household
        stopped consenting.
        """
        row = await self._session.scalar(
            select(ShoppingExportTarget).where(
                ShoppingExportTarget.household_id == household_id,
                ShoppingExportTarget.target_code == target_code,
            )
        )
        if row is None:
            # Same answer whether it never existed or belongs to someone else:
            # distinguishing them would turn this into a configuration oracle.
            raise ExportTargetNotRegistered(
                f"no {target_code} destination is registered for this household"
            )
        if row.consent_revoked_at is None:
            row.consent_revoked_at = dt.datetime.now(dt.UTC)
            await self._session.flush()
            await self._session.refresh(row)
            logger.info("shopping_export_target_revoked", extra={"target": row.target_code})
        return await self._read_back(household_id, target_code)


def _view(
    row: ShoppingExportTarget, *, registered_by: str | None, registrant_is_member: bool
) -> ExportTargetView:
    """Project a row onto the fields that may leave this layer.

    Written as a function rather than a method so there is exactly one place that
    decides what a destination looks like from outside, and so no future handler
    can build its own projection from the ORM object.

    ``registered_by`` and ``registrant_is_member`` are parameters rather than
    reads off *row*: the first would be a lazy load of another table from inside a
    projection, and the second is not on the row at all -- it is a fact about the
    membership table right now. Both come from the query in :meth:`list_targets`.
    """
    return ExportTargetView(
        target_code=row.target_code,
        token_last4=row.token_last4,
        external_list_id=row.external_list_id,
        consented_at=row.consented_at,
        consent_revoked_at=row.consent_revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        registered_by=registered_by,
        # An unrecorded registrant is nobody who could have left. `EXISTS` on a
        # NULL comparison is false, so this correction has to be made here rather
        # than relied upon from the query.
        registrant_is_member=registrant_is_member or row.registered_by_user_id is None,
    )
