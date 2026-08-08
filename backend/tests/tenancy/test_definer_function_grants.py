"""Who may execute the two functions that walk past row-level security.

``chaudron_user_memberships`` and ``chaudron_resolve_machine_token`` are
``SECURITY DEFINER``: they run as the owner of the tables and are exempt from
every policy, which revisions ``0009`` and ``0011`` argue is unavoidable --
authentication has to answer "which households may this caller open?" *before* a
tenant has been posted, and posting one first would mean trusting the client's
header to arm the check that validates it.

``pg_proc.proacl`` was ``NULL`` for both, which is PostgreSQL's default and means
``EXECUTE`` to ``PUBLIC``. ``scripts/provision_app_role.py`` granted ``EXECUTE``
explicitly on ``chaudron_current_household()`` -- the one of the three that
crosses nothing -- and on neither of these, because neither needed it (audit
AUD-027).

Nothing could exploit that while the application role was the only non-owner
login. It became exploitable at the moment somebody added a second one, and the
grant would have been invisible to the review: an implicit ``PUBLIC`` leaves no
ACL entry to notice.

**This file asserts both halves of the fix, and the second is why it exists.**
Revoking alone leaves the application unable to resolve a session or a token --
every request answers ``401`` -- so the revocation in migration ``0014`` and the
grants in ``scripts/provision_app_role.py`` are one change. The tests below drive
the *provisioned* role, produced by that very script, so a revocation that shipped
without its grant fails here rather than in production.
"""

from __future__ import annotations

import uuid
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

#: The two that cross the policies. ``chaudron_current_household()`` is
#: deliberately absent: it reads a transaction-local setting and crosses nothing,
#: which is why it was the only one granted before and why it is the only one
#: PUBLIC may still hold.
DEFINERS: Final[tuple[tuple[str, str], ...]] = (
    ("chaudron_user_memberships", "uuid"),
    ("chaudron_resolve_machine_token", "text"),
)


@pytest.mark.parametrize(("name", "argument"), DEFINERS, ids=[d[0] for d in DEFINERS])
async def test_public_may_not_execute_a_definer(
    owner_engine: AsyncEngine, name: str, argument: str
) -> None:
    """The revocation, read from the catalogue rather than from the migration.

    ``has_function_privilege('public', ...)`` is the question that matters: an ACL
    a migration wrote and a later ``GRANT`` widened would still read as revoked in
    the source and as open here.
    """
    async with owner_engine.connect() as connection:
        granted = await connection.scalar(
            sa.text("SELECT has_function_privilege('public', :signature, 'EXECUTE')"),
            {"signature": f"{name}({argument})"},
        )

    assert granted is False, (
        f"{name} is executable by PUBLIC. It runs as the table owner and is exempt "
        f"from every row-level security policy, so the next role anybody adds -- a "
        f"reporting account, a backup user -- would inherit the right to read the "
        f"whole membership map past those policies."
    )


@pytest.mark.parametrize(("name", "argument"), DEFINERS, ids=[d[0] for d in DEFINERS])
async def test_the_application_role_may_execute_a_definer(
    app_engine: AsyncEngine, name: str, argument: str
) -> None:
    """The other half, and the one that fails loudly if only the revoke ships.

    Without it the API cannot resolve a session or a machine token, so every
    request answers ``401`` and nothing in the logs says why. The engine here
    connects as the role ``scripts/provision_app_role.py`` produced, so this is
    the documented procedure being exercised rather than a grant a test wrote.
    """
    async with app_engine.connect() as connection:
        granted = await connection.scalar(
            sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
            {"signature": f"{name}({argument})"},
        )

    assert granted is True, (
        f"the application role may not execute {name}. Migration 0014 revokes it from "
        f"PUBLIC; the matching GRANT lives in scripts/provision_app_role.py and the two "
        f"are one change. Re-run that script against the owner DSN."
    )


async def test_the_application_role_can_actually_call_them(app_engine: AsyncEngine) -> None:
    """Privilege granted is not privilege usable -- call both and read the answer.

    A grant on the wrong overload, a signature changed by a later revision, a
    pinned ``search_path`` that no longer resolves: each of those passes the check
    above and breaks authentication. Both calls are made with arguments that match
    nothing, so the assertion is "it executed and returned no rows" rather than
    anything about the data.
    """
    async with app_engine.connect() as connection:
        memberships = (
            await connection.execute(
                sa.text("SELECT * FROM chaudron_user_memberships(:user_id)"),
                {"user_id": uuid.uuid4()},
            )
        ).all()
        grant = (
            await connection.execute(
                sa.text("SELECT * FROM chaudron_resolve_machine_token(:digest)"),
                {"digest": "0" * 64},
            )
        ).all()

    assert memberships == []
    assert grant == []


async def test_the_resolver_carries_the_role_of_the_issuer(owner_engine: AsyncEngine) -> None:
    """A token must never be more than the person behind it currently is.

    The join onto ``household_member`` has been there since revision ``0011`` --
    it is what makes a token die with its issuer's membership -- and the role it
    had in hand was thrown away, so a ``viewer`` could write through a credential
    they were refused through a cookie. Asserted on the *signature* because the
    application reads the column by name: a revision that dropped it would fail
    here rather than at the first request of the next deployment.
    """
    async with owner_engine.connect() as connection:
        signature = await connection.scalar(
            sa.text(
                "SELECT pg_get_function_result(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'chaudron_resolve_machine_token'"
            )
        )

    assert isinstance(signature, str)
    assert "role membership_role" in signature, signature
