"""Machine access tokens: issuing them, listing them, revoking them, resolving one.

Contract v1.1 section 10. This module owns every decision about *what a program
may do*; ``api/deps.py`` owns the ``Authorization`` header and the status codes,
and nothing else.

Four properties are load-bearing, and each one is asserted by a test.

**A token is a row, and revoking it is a write.** Same argument as
:class:`chaudron.domain.models.UserSession`: a credential the server merely
validates is a credential it cannot withdraw. "Unplug this integration" has to
cut access at the instant it is asked for.

**The stored value is a digest.** :func:`chaudron.services.auth.hash_token` runs
on the way in and on every lookup. The plaintext exists in exactly one place --
the body of the creation response -- and is never written to the database, to a
log, or to a second response.

**Every refusal is one branch, in the database.** Revoked, expired, issued by a
disabled account, issued by somebody no longer a member of the household,
belonging to an archived household, and simply unknown are all "no row" from the
same ``WHERE`` clause (migration ``0011``). So the API answers them identically
*and takes the same time to do it*, which a post-filter in Python could not
promise -- it would have found the row first, and the difference is measurable.

**Resolution runs before any tenant is posted.** A bearer request has no
household yet: the token is what decides it. ``machine_token`` carries row-level
security, so a direct read would be filtered by the very policy this lookup
exists to arm. ``chaudron_resolve_machine_token`` is the ``SECURITY DEFINER``
function that answers it, keyed on the digest of a 256-bit secret so it can
enumerate nothing. It is the mirror image of ``chaudron_user_memberships``
(migration ``0009``), and revision ``0011`` argues both narrowings.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import MachineToken, MachineTokenScope, MembershipRole
from chaudron.infra.db import set_transaction_household
from chaudron.services.auth import TOKEN_BYTES, hash_token

#: What every machine token starts with. Not a secret and not entropy: it is
#: there so a value found in a configuration file, a log line or a screenshot is
#: recognisable as a Chaudron credential -- which is what makes an accidental
#: leak reportable by a secret scanner rather than by nobody.
TOKEN_PREFIX: Final = "chdr_"  # noqa: S105 -- a public marker, not a credential

#: How many characters of the tail a listing may show. Four is enough to tell two
#: tokens apart in a list and far too few to shorten a search by anything that
#: matters against 256 bits.
LAST4_LENGTH: Final = 4

#: How stale ``last_used_at`` may get before a request refreshes it. An hour, and
#: the reason is arithmetic: this column answers "is this integration still
#: running?", a question nobody asks to the minute, and a per-request refresh
#: would turn every authenticated ``GET`` into an ``UPDATE`` -- a write amplified
#: by exactly the traffic a polling integration generates.
LAST_USED_GRANULARITY: Final = timedelta(hours=1)

#: The longest expiry a caller may ask for. Ten years is "no practical limit"
#: without letting a typo in a JSON body produce a timestamp PostgreSQL refuses.
MAX_EXPIRY_DAYS: Final = 3650

#: The resolver, through the function that is allowed to see the table. The
#: digest is bound, never interpolated.
_RESOLVE = (
    text("SELECT * FROM chaudron_resolve_machine_token(:token_hash)")
    .bindparams(bindparam("token_hash", type_=MachineToken.__table__.c.token_hash.type))
    .columns(
        token_id=MachineToken.__table__.c.id.type,
        household_id=MachineToken.__table__.c.household_id.type,
        user_id=MachineToken.__table__.c.user_id.type,
    )
)


class MachineTokenError(Exception):
    """Base class for the failures this module reports to the HTTP layer."""


class NoScopesError(MachineTokenError):
    """A token was asked for with no scope at all.

    Refused rather than stored. The authorisation path already treats an empty
    scope set as "may do nothing" -- that is the fail-closed default and it is
    tested -- so such a row could never authorise anything; it would exist only
    to make a listing look like the household has an integration it does not.
    """


class ExpiryOutOfRangeError(MachineTokenError):
    """``expires_in_days`` was zero, negative, or past :data:`MAX_EXPIRY_DAYS`."""


class TooManyTokensError(MachineTokenError):
    """The household already holds as many tokens as it is allowed.

    Not in the contract, and deliberately generous: it exists so that a loop in a
    misconfigured client cannot fill a table, not to ration a legitimate use.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"a household may hold at most {limit} machine tokens")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class MachineTokenGrant:
    """What a presented token authorises, once resolved.

    Immutable, free of ORM instances, and carrying no secret: what reaches a
    handler is the *verdict*, never the credential that produced it.
    """

    token_id: uuid.UUID
    household_id: uuid.UUID
    user_id: uuid.UUID
    scopes: frozenset[MachineTokenScope]
    #: The role the issuer holds in the household **right now**, re-read from
    #: ``household_member`` on every request by the resolver (migration ``0011``,
    #: extended by ``0014``). Not a copy taken at issue time: a role frozen onto
    #: the row would keep a demoted person's integration writing, which is the
    #: same failure as a token surviving a revoked membership -- and that one the
    #: resolver already refuses. A token is therefore never more than its issuer
    #: is, whatever scopes it was minted with.
    role: MembershipRole

    def allows(self, required: Iterable[MachineTokenScope]) -> bool:
        """Whether this grant covers every scope in *required*.

        Additive and never implicit: no scope here follows from another, and an
        empty grant covers nothing but an empty requirement -- which is why the
        caller must never treat "no scope required" as "any token will do".
        """
        return all(scope in self.scopes for scope in required)


@dataclass(frozen=True, slots=True)
class MachineTokenSummary:
    """One token as its household may read it back. Carries no secret.

    ``prefix`` and ``last4`` are what is left of the value after the creation
    response, and together with ``name`` they are how a person tells two tokens
    apart. A token that could be read back in full would be a token a screenshot
    is enough to steal.
    """

    id: uuid.UUID
    name: str
    scopes: tuple[MachineTokenScope, ...]
    prefix: str
    last4: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedMachineToken:
    """A freshly minted token. ``token`` exists here and in one response body."""

    summary: MachineTokenSummary
    token: str


def new_machine_token() -> str:
    """A recognisable prefix followed by 256 bits from the operating system."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def normalise_scopes(raw: Iterable[MachineTokenScope]) -> tuple[MachineTokenScope, ...]:
    """Deduplicated and ordered as the enum declares them.

    A stable order so two tokens asking for the same authority store the same
    array, and a set so ``["inventory:read", "inventory:read"]`` is one scope
    rather than a row that reads as though it were two.
    """
    held = set(raw)
    return tuple(scope for scope in MachineTokenScope if scope in held)


class MachineTokenService:
    """Issuing, listing, revoking and resolving machine tokens."""

    def __init__(self, session: AsyncSession, *, max_per_household: int) -> None:
        self._session = session
        self._max_per_household = max_per_household

    # -- Issuing ------------------------------------------------------------ #

    async def issue(
        self,
        *,
        household_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        scopes: Iterable[MachineTokenScope],
        expires_in_days: int | None,
    ) -> IssuedMachineToken:
        """Mint a token for *household_id*, and return it in clear **once**.

        The household is the one the caller's browser session already resolved.
        It is stored on the row rather than selected per request, which is the
        whole of "a token opens one house": there is no header a holder could
        send to point it at another.
        """
        chosen = normalise_scopes(scopes)
        if not chosen:
            raise NoScopesError("a token must carry at least one scope")
        expires_at = self._expiry(expires_in_days)

        held = await self._session.scalar(
            select(func.count())
            .select_from(MachineToken)
            .where(
                MachineToken.household_id == household_id,
                MachineToken.revoked_at.is_(None),
            )
        )
        if held is not None and held >= self._max_per_household:
            raise TooManyTokensError(self._max_per_household)

        value = new_machine_token()
        record = MachineToken(
            household_id=household_id,
            user_id=user_id,
            name=name.strip() or "Jeton sans nom",
            token_hash=hash_token(value),
            prefix=TOKEN_PREFIX,
            last4=value[-LAST4_LENGTH:],
            scopes=list(chosen),
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        # `created_at` is a server default, so the row has to be read back before
        # the summary can carry it. Without this the response would quote a
        # timestamp Python invented next to one PostgreSQL chose.
        await self._session.refresh(record, ["created_at"])
        return IssuedMachineToken(summary=_summarise(record), token=value)

    @staticmethod
    def _expiry(expires_in_days: int | None) -> datetime | None:
        """``None`` means no expiry, which the contract allows explicitly."""
        if expires_in_days is None:
            return None
        if expires_in_days < 1 or expires_in_days > MAX_EXPIRY_DAYS:
            raise ExpiryOutOfRangeError(
                f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}, or null"
            )
        return datetime.now(UTC) + timedelta(days=expires_in_days)

    # -- Listing and revoking ----------------------------------------------- #

    async def list_for_household(self, household_id: uuid.UUID) -> tuple[MachineTokenSummary, ...]:
        """Every live token of the household, newest first.

        Revoked rows are excluded rather than flagged: revocation is meant to be
        a deletion as far as anybody reading the list is concerned, and a list
        that accumulates tombstones is a list nobody scrolls to the end of.
        """
        rows = await self._session.scalars(
            select(MachineToken)
            .where(
                MachineToken.household_id == household_id,
                MachineToken.revoked_at.is_(None),
            )
            .order_by(MachineToken.created_at.desc(), MachineToken.id.desc())
        )
        return tuple(_summarise(row) for row in rows)

    async def revoke(self, *, household_id: uuid.UUID, token_id: uuid.UUID) -> bool:
        """End one token now. Returns whether there was one to end.

        ``household_id`` is in the predicate as well as in the policy. The policy
        is what enforces it; repeating it here means the statement still says what
        it means when read on its own, and means a maintenance connection that
        bypasses the policy cannot revoke across households by accident.
        """
        revoked = await self._session.scalar(
            update(MachineToken)
            .where(
                MachineToken.id == token_id,
                MachineToken.household_id == household_id,
                MachineToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
            .returning(MachineToken.id)
        )
        return revoked is not None

    # -- Resolution --------------------------------------------------------- #

    async def resolve(self, presented: str) -> MachineTokenGrant | None:
        """The grant behind a bearer value, or ``None`` if there is none.

        ``None`` covers unknown, revoked, expired, issued by a since-disabled
        account, issued by somebody no longer a member, and belonging to an
        archived household. All six come out of one ``WHERE`` clause in
        ``chaudron_resolve_machine_token``, so this function has no branch that
        could tell them apart even if a future edit wanted to.
        """
        row = (await self._session.execute(_RESOLVE, {"token_hash": hash_token(presented)})).first()
        if row is None:
            return None

        grant = MachineTokenGrant(
            token_id=row.token_id,
            household_id=row.household_id,
            user_id=row.user_id,
            scopes=frozenset(MachineTokenScope(scope) for scope in row.scopes),
            # From the join the resolver already performed, so the role is the
            # issuer's current one and costs no second query.
            role=MembershipRole(row.role),
        )
        # The tenant is known now, and posting it here is the documented
        # "caller that knows its tenant up front" path (``infra/db.py``). It has
        # to happen before the touch below: `machine_token` is behind a policy,
        # and an UPDATE with no tenant posted would match no row.
        await set_transaction_household(self._session, grant.household_id)
        await self._touch(grant, row.last_used_at)
        return grant

    async def _touch(self, grant: MachineTokenGrant, last_used_at: datetime | None) -> None:
        """Record that the token was used, at most once an hour."""
        now = datetime.now(UTC)
        if last_used_at is not None and now - last_used_at < LAST_USED_GRANULARITY:
            return
        await self._session.execute(
            update(MachineToken).where(MachineToken.id == grant.token_id).values(last_used_at=now)
        )


def _summarise(record: MachineToken) -> MachineTokenSummary:
    return MachineTokenSummary(
        id=record.id,
        name=record.name,
        scopes=tuple(MachineTokenScope(scope) for scope in record.scopes),
        prefix=record.prefix,
        last4=record.last4,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
    )
