"""The sweep of dead sessions and tokens, against a real database.

``user_session`` is read on every authenticated request and had nothing that ever
removed a row from it (audit AUD-031). The risk in fixing that is not the growth
-- it is the fix: a predicate that is a little too wide signs people out, and it
does so on a timer at three in the morning with nobody watching.

So this file asserts the *negative* first and at length. A live session, a session
that expired ten minutes ago, a token with no expiry at all: none of them may be
touched. Only then does it assert that anything is deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from scripts.purge_expired_credentials import DEFAULT_RETENTION_DAYS, count, purge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    HouseholdMember,
    MachineToken,
    MachineTokenScope,
    MembershipRole,
    UserAccount,
    UserSession,
)
from chaudron.services.auth import hash_token, new_token

pytestmark = pytest.mark.integration

_LONG_AGO = timedelta(days=DEFAULT_RETENTION_DAYS + 1)
_RECENTLY = timedelta(minutes=10)


def _session_row(user: UserAccount, **overrides: object) -> UserSession:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "user_id": user.id,
        "token_hash": hash_token(new_token()),
        "csrf_token": new_token(),
        "expires_at": now + timedelta(days=30),
        "idle_expires_at": now + timedelta(days=7),
    }
    values.update(overrides)
    return UserSession(**values)


async def _token_row(
    session: AsyncSession, household: Household, user: UserAccount, **overrides: object
) -> MachineToken:
    values: dict[str, object] = {
        "household_id": household.id,
        "user_id": user.id,
        "name": "Home Assistant",
        "token_hash": hash_token(new_token()),
        "prefix": "chdr_",
        "last4": "abcd",
        "scopes": [MachineTokenScope.INVENTORY_READ],
    }
    values.update(overrides)
    row = MachineToken(**values)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def household_with_owner(db_session: AsyncSession) -> tuple[Household, UserAccount]:
    """A household and an account, committed far enough to hang rows off.

    Built here rather than through ``make_household`` because this file needs the
    *account* as well and does not need the signed-in caller the shared fixtures
    imply.
    """
    user = UserAccount(email=f"purge-{new_token()[:8]}@example.test", display_name="Purge")
    db_session.add(user)
    household = Household(name="Purge")
    db_session.add(household)
    await db_session.flush()
    db_session.add(
        HouseholdMember(household_id=household.id, user_id=user.id, role=MembershipRole.OWNER)
    )
    await db_session.flush()
    return household, user


# --------------------------------------------------------------------------- #
# What must survive
# --------------------------------------------------------------------------- #


async def test_a_live_session_survives(
    db_session: AsyncSession, household_with_owner: tuple[Household, UserAccount]
) -> None:
    _, user = household_with_owner
    live = _session_row(user)
    db_session.add(live)
    await db_session.flush()

    await purge(await db_session.connection(), DEFAULT_RETENTION_DAYS)

    assert await db_session.scalar(select(UserSession.id).where(UserSession.id == live.id))


async def test_a_session_that_died_recently_survives(
    db_session: AsyncSession, household_with_owner: tuple[Household, UserAccount]
) -> None:
    """The retention window, which is the whole reason this is not a trigger.

    These rows are the only record of when a session ended and whose it was. An
    incident review that arrives a week later must still find them, so "dead" and
    "sweepable" are deliberately not the same date.
    """
    _, user = household_with_owner
    now = datetime.now(UTC)
    just_revoked = _session_row(user, revoked_at=now - _RECENTLY)
    just_expired = _session_row(user, expires_at=now - _RECENTLY)
    db_session.add_all([just_revoked, just_expired])
    await db_session.flush()

    await purge(await db_session.connection(), DEFAULT_RETENTION_DAYS)

    survivors = set(
        (
            await db_session.scalars(
                select(UserSession.id).where(UserSession.id.in_([just_revoked.id, just_expired.id]))
            )
        ).all()
    )
    assert survivors == {just_revoked.id, just_expired.id}


async def test_a_token_with_no_expiry_is_never_swept(
    db_session: AsyncSession, household_with_owner: tuple[Household, UserAccount]
) -> None:
    """A credential in an appliance polling since 2026 is old, not dead.

    ``expires_at IS NULL`` is the documented shape for exactly that, and a sweep
    keyed on age rather than on death would silently unplug it.
    """
    household, user = household_with_owner
    forever = await _token_row(
        db_session,
        household,
        user,
        expires_at=None,
        created_at=datetime.now(UTC) - timedelta(days=900),
    )

    await purge(await db_session.connection(), DEFAULT_RETENTION_DAYS)

    assert await db_session.scalar(select(MachineToken.id).where(MachineToken.id == forever.id))


# --------------------------------------------------------------------------- #
# What goes
# --------------------------------------------------------------------------- #


async def test_long_dead_credentials_are_removed(
    db_session: AsyncSession, household_with_owner: tuple[Household, UserAccount]
) -> None:
    household, user = household_with_owner
    now = datetime.now(UTC)
    stale = _session_row(user, expires_at=now - _LONG_AGO, idle_expires_at=now - _LONG_AGO)
    revoked = _session_row(user, revoked_at=now - _LONG_AGO)
    db_session.add_all([stale, revoked])
    await db_session.flush()
    dead_token = await _token_row(db_session, household, user, revoked_at=now - _LONG_AGO)

    sessions, tokens = await purge(await db_session.connection(), DEFAULT_RETENTION_DAYS)

    assert sessions >= 2
    assert tokens >= 1
    assert not await db_session.scalar(select(UserSession.id).where(UserSession.id == stale.id))
    assert not await db_session.scalar(select(UserSession.id).where(UserSession.id == revoked.id))
    assert not await db_session.scalar(
        select(MachineToken.id).where(MachineToken.id == dead_token.id)
    )


async def test_the_dry_run_counts_the_same_rows_and_deletes_none(
    db_session: AsyncSession, household_with_owner: tuple[Household, UserAccount]
) -> None:
    """A first run should be ``--dry-run``, so the two must agree.

    A counter that used a different predicate from the delete would make the dry
    run reassuring and wrong, which is worse than having no dry run at all.
    """
    _, user = household_with_owner
    now = datetime.now(UTC)
    db_session.add(_session_row(user, revoked_at=now - _LONG_AGO))
    await db_session.flush()

    predicted_sessions, predicted_tokens = await count(
        await db_session.connection(), DEFAULT_RETENTION_DAYS
    )
    assert predicted_sessions >= 1

    # Still there: counting is not deleting.
    recounted_sessions, _ = await count(await db_session.connection(), DEFAULT_RETENTION_DAYS)
    assert recounted_sessions == predicted_sessions

    deleted_sessions, deleted_tokens = await purge(
        await db_session.connection(), DEFAULT_RETENTION_DAYS
    )
    assert (deleted_sessions, deleted_tokens) == (predicted_sessions, predicted_tokens)
