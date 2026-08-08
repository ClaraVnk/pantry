"""Delete sessions and machine tokens that stopped being credentials long ago.

Both tables grow without bound and neither has ever been swept (audit AUD-031).
``user_session`` is the one that matters: it is read on **every authenticated
request**, by an exact-match index on ``token_hash``, and a browser that signs in
and out ten times a day leaves ten dead rows behind it. An index lookup does not
care much, but a table that only grows makes every backup, every restore drill and
every ``VACUUM`` slower for data that authenticates nobody. ``machine_token`` grows
more slowly and for the same reason.

**A dead row is not deleted the moment it dies.** Retention is deliberate and
defaults to thirty days: the rows are the only record of *when* a session ended
and which account it belonged to, which is exactly what an incident review reads.
Sweeping them at expiry would mean the first question after a compromise --
"which sessions were live, and when did they stop?" -- had no answer. Thirty days
also matches ``session_absolute_ttl_hours``, so the window is never shorter than
the credential it outlives.

**Nothing live is ever touched.** The predicates below select only rows that are
already unusable by ``AuthService.resolve`` and by
``chaudron_resolve_machine_token`` -- revoked, past their absolute expiry, or past
their idle deadline -- and each has to have been in that state for the retention
window. A row this deletes could not have authenticated anybody today.

Run from ``backend/``, against the **owner** DSN::

    uv run python scripts/purge_expired_credentials.py --dry-run
    uv run python scripts/purge_expired_credentials.py

The owner, not the application role, and that is not laziness: ``machine_token``
carries row-level security keyed on a transaction-local tenant (migration
``0011``), and a sweep is by definition not scoped to one household. The
application role would have to post a tenant it does not have and would delete
nothing. ``ops/`` installs this as a weekly timer under the same account that runs
the migrations, which is the identity that already holds that DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from chaudron.config import ConfigurationError, get_settings
from chaudron.domain.models import MachineToken, UserSession

logger = logging.getLogger("chaudron.purge")

#: How long a dead credential is kept before it is swept. Thirty days, which is
#: the absolute session lifetime: the record of a session never has a shorter life
#: than the session itself, and an incident review a month late still finds it.
DEFAULT_RETENTION_DAYS: Final = 30


def _cutoff(retention_days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=retention_days)


def _dead_sessions(cutoff: datetime) -> ColumnElement[bool]:
    """Sessions that stopped authenticating anybody before *cutoff*.

    The three clauses are the three ways ``AuthService.resolve`` refuses a row,
    read in the same order it applies them. A row matching none of them is live,
    whatever its age -- which is why this is not a query on ``created_at``.
    """
    return or_(
        UserSession.revoked_at < cutoff,
        UserSession.expires_at < cutoff,
        UserSession.idle_expires_at < cutoff,
    )


def _dead_tokens(cutoff: datetime) -> ColumnElement[bool]:
    """Machine tokens the resolver would already refuse, aged past *cutoff*.

    A token with ``expires_at IS NULL`` never expires and is never swept, however
    old: a household appliance issued a credential in 2026 and still polling in
    2030 is the documented use, and "old" is not "dead".
    """
    return or_(MachineToken.revoked_at < cutoff, MachineToken.expires_at < cutoff)


async def count(connection: AsyncConnection, retention_days: int) -> tuple[int, int]:
    """How many rows a run would remove, without removing any."""
    cutoff = _cutoff(retention_days)
    sessions = await connection.scalar(
        select(func.count()).select_from(UserSession).where(_dead_sessions(cutoff))
    )
    tokens = await connection.scalar(
        select(func.count()).select_from(MachineToken).where(_dead_tokens(cutoff))
    )
    return int(sessions or 0), int(tokens or 0)


async def purge(connection: AsyncConnection, retention_days: int) -> tuple[int, int]:
    """Remove them, and report what went."""
    cutoff = _cutoff(retention_days)
    sessions = await connection.execute(delete(UserSession).where(_dead_sessions(cutoff)))
    tokens = await connection.execute(delete(MachineToken).where(_dead_tokens(cutoff)))
    return sessions.rowcount, tokens.rowcount


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=(
            "how long a dead credential is kept before it is swept "
            f"(default {DEFAULT_RETENTION_DAYS}). Below 1 is refused: a zero-day "
            "retention deletes the record of a session as it ends."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count and report; delete nothing. What a first run should be.",
    )
    arguments = parser.parse_args()

    if arguments.retention_days < 1:
        logger.error("--retention-days must be at least 1")
        return 2

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        if arguments.dry_run:
            async with engine.connect() as connection:
                sessions, tokens = await count(connection, arguments.retention_days)
            logger.info(
                "would delete %d expired or revoked sessions and %d machine tokens "
                "older than %d days",
                sessions,
                tokens,
                arguments.retention_days,
            )
            return 0
        # `begin` rather than `connect`: both deletes commit together or neither
        # does, so a failure half-way cannot leave the two tables swept to
        # different dates.
        async with engine.begin() as connection:
            sessions, tokens = await purge(connection, arguments.retention_days)
    finally:
        await engine.dispose()

    logger.info(
        "deleted %d expired or revoked sessions and %d machine tokens older than %d days",
        sessions,
        tokens,
        arguments.retention_days,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
