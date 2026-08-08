"""The token buckets, held in PostgreSQL so that a second worker cannot double them.

``api/throttling.py`` says what these limits are *for* and keeps the in-process
implementation; this module is the shared-state one it has been pointing at since
it was written:

    Before this deployment grows a second worker or a second replica, the state has
    to move to PostgreSQL -- a per-household window row updated with
    ``UPDATE ... RETURNING`` inside the request transaction -- or the limits stop
    meaning what they say.

Two things about that sentence turned out to be wrong once written down, and both
matter more than the move itself.

**"Inside the request transaction" would have been a hole, not a shortcut.**
A count that lives in the caller's transaction disappears when that transaction
rolls back -- and the requests a limiter most needs to count are exactly the ones
that end badly. A failed login rolls back; so does a refused import, a 500, a
constraint violation. Anyone able to make a request fail would get unlimited
attempts, and the login limiter -- the one control standing between a public
instance and password spraying -- would count only the attempts that succeeded.
So the deduction runs on its **own connection and commits on its own**, before the
work it bounds, and is durable whatever the request then does.

**The elapsed time has to come from the server, not from Python.** Two workers have
two clocks, and a bucket refilled against whichever clock arrived last grants more
than it says. Every interval here is measured by PostgreSQL. That is also why the
column is a ``timestamptz`` rather than the ``time.monotonic`` the in-memory
limiter uses: monotonic clocks are not comparable across processes, nor across a
restart of the same one.

**Atomicity: a row lock, not a clever statement.** The obvious shape -- one
``INSERT ... ON CONFLICT DO UPDATE`` computing the refill inline -- cannot report
whether it granted. ``RETURNING`` sees only the new row, and "refused, left at
0.4 tokens" is indistinguishable from "granted, 1.4 down to 0.4". Encoding the
answer around that would be a puzzle in the one place that must stay checkable, so
the bucket is locked with ``SELECT ... FOR UPDATE`` and the decision is made in the
open. Two extra round trips on a local socket, guarding operations that cost an
inference or a multi-megabyte upload.

**What this deliberately does not move.** :class:`chaudron.api.throttling.ConcurrencyLimiter`
stays in the process. A slot is held for the duration of a request, so a table
means a lease with an expiry, and a worker killed mid-inference leaks its slot
until that expiry elapses: short, and the cap stops binding while the inference
runs; long, and one crash denies the household for minutes. Its process-wide half
-- "a small machine running Ollama must not be asked for six inferences at once"
-- is *correctly* per-process anyway, being a statement about this process's
memory. Only its per-key half wants to be global, and that is a smaller hole than
the lease would open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chaudron.api.throttling import AtCapacityError

#: Create the bucket full if it is not there. ``DO NOTHING`` makes the race between
#: two workers cold-starting the same key a no-op for the loser, who then finds the
#: winner's row on the re-read.
#:
#: Run **only when the lock below found nothing**, which is once per key per window
#: rather than once per request. The first draft issued it unconditionally, ahead of
#: every acquisition, so every rate-limited request paid for an insert that could
#: succeed exactly once in the bucket's life. On the busiest limiter -- the
#: per-minute product lookup -- that was a wasted statement on the hot path.
_ENSURE: Final = text("""
    INSERT INTO rate_limit_bucket (scope, bucket_key, tokens, updated_at)
    VALUES (:scope, :bucket_key, :capacity, now())
    ON CONFLICT (scope, bucket_key) DO NOTHING
""")

#: Lock the bucket and report how much time the *server* says has passed. Anything
#: else in this transaction now waits, which is what makes the deduction below
#: safe across workers.
_LOCK: Final = text("""
    SELECT tokens, EXTRACT(EPOCH FROM (now() - updated_at)) AS elapsed_seconds
    FROM rate_limit_bucket
    WHERE scope = :scope AND bucket_key = :bucket_key
    FOR UPDATE
""")

_WRITE: Final = text("""
    UPDATE rate_limit_bucket SET tokens = :tokens, updated_at = now()
    WHERE scope = :scope AND bucket_key = :bucket_key
""")

#: Rows untouched for a full window have refilled to capacity, so deleting one and
#: re-creating it full are the same thing -- the arithmetic argument the in-memory
#: sweep already makes. Without it the table grows one row per distinct client
#: address, for ever.
_SWEEP: Final = text("""
    DELETE FROM rate_limit_bucket
    WHERE updated_at < now() - make_interval(secs => :window_seconds)
""")


#: The width of ``rate_limit_bucket.scope`` in migration ``0018``. Checked when a
#: policy is built rather than left to the database, because the database reports
#: it as a ``StringDataRightTruncation`` on the first request that reaches the
#: limiter -- a long way, in both code and time, from the line that chose the
#: name. The eight shipped scopes sit well inside it; what runs close is a test
#: harness prefixing them to keep its buckets apart.
MAX_SCOPE_LENGTH: Final = 48


@dataclass(frozen=True, slots=True)
class BucketPolicy:
    """``limit`` acquisitions per ``window_seconds``, per key, within one scope.

    ``scope`` keeps the ten limiters apart in one table -- the eight on
    :class:`chaudron.api.throttling.Throttles`, plus the calendar feed's poll and
    failure counters, which ``api/routers/calendar.py`` builds for itself. It is
    part of the primary key rather than a table per limiter, because the keys are
    of different kinds -- a household identifier, a client address, an account --
    and a table whose meaning depends on which column happens to be filled is
    worse than a label saying which limiter a row belongs to.
    """

    scope: str
    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if not self.scope:
            raise ValueError("scope must not be empty")
        if len(self.scope) > MAX_SCOPE_LENGTH:
            raise ValueError(
                f"scope must be at most {MAX_SCOPE_LENGTH} characters "
                f"(rate_limit_bucket.scope), got {len(self.scope)}: {self.scope!r}"
            )
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    @property
    def rate(self) -> float:
        """Tokens per second, the continuous refill of the in-memory limiter."""
        return self.limit / self.window_seconds


class SharedRateLimiter:
    """A token bucket every worker of every replica shares.

    Takes the :class:`AsyncEngine` rather than a session, on purpose: a session
    belongs to a request, and the point of this class is that its writes survive
    that request's rollback. Each call opens a short connection of its own and
    commits it.
    """

    def __init__(self, engine: AsyncEngine, policy: BucketPolicy) -> None:
        self._engine = engine
        self._policy = policy

    @property
    def policy(self) -> BucketPolicy:
        return self._policy

    async def acquire(self, key: str) -> None:
        """Spend one token for ``key``, or raise :class:`AtCapacityError`.

        The refusal carries the true wait: the time the server needs to refill one
        whole token from where the bucket actually stands, rounded up and never
        below a second. A ``Retry-After`` of zero invites an immediate retry, which
        is how a limiter becomes a busy loop.
        """
        policy = self._policy
        parameters = {"scope": policy.scope, "bucket_key": key}

        async with self._engine.begin() as connection:
            row = (await connection.execute(_LOCK, parameters)).one_or_none()
            if row is None:
                # Cold bucket. Create it full and re-read under the lock rather than
                # returning here: a worker that lost the insert race has to see the
                # winner's row, and one that won still has to hold the lock while it
                # spends from it.
                await connection.execute(_ENSURE, {**parameters, "capacity": float(policy.limit)})
                row = (await connection.execute(_LOCK, parameters)).one()
            refilled = min(
                float(policy.limit),
                float(row.tokens) + float(row.elapsed_seconds) * policy.rate,
            )
            if refilled >= 1.0:
                await connection.execute(_WRITE, {**parameters, "tokens": refilled - 1.0})

        # Raised *after* the transaction closes, not inside it. ``engine.begin()``
        # rolls back on an exception, so a refusal raised in the block would undo
        # the very row it had just written -- which is how the first version of
        # this method silently discarded the deduction it thought it was banking.
        #
        # A refusal deliberately writes nothing at all. The earlier draft stored
        # the partial refill "so the next caller does not count the same seconds
        # twice", and that reasoning was wrong: leaving the row alone leaves
        # ``tokens`` and ``updated_at`` *both* untouched, so the next caller
        # measures from the same origin against the same balance and reaches the
        # same answer. The two spellings agree on every input; this one costs no
        # write on the path a caller under a limit takes most often.
        if refilled < 1.0:
            raise AtCapacityError(self._retry_after(refilled))

    async def refund(self, key: str) -> None:
        """Give back one token taken by :meth:`acquire`.

        For the limiter charged *before* the work it bounds rather than after it --
        ``routers/calendar.py``, where what an unauthenticated caller spends is the
        cost of *checking* a credential, so a counter consulted after the check
        changes the response and not the work.

        Capped at capacity, so a refund never creates a token: a double refund, or
        one for a bucket whose window has rolled over, leaves it exactly full. A key
        the table has forgotten is a no-op, matching the in-memory limiter.
        """
        policy = self._policy
        parameters = {"scope": policy.scope, "bucket_key": key}

        async with self._engine.begin() as connection:
            row = (await connection.execute(_LOCK, parameters)).one_or_none()
            if row is None:
                return
            refilled = min(
                float(policy.limit),
                float(row.tokens) + float(row.elapsed_seconds) * policy.rate + 1.0,
            )
            await connection.execute(_WRITE, {**parameters, "tokens": refilled})

    async def sweep(self) -> int:
        """Drop buckets idle for a full window. Returns how many rows went."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                _SWEEP, {"window_seconds": self._policy.window_seconds}
            )
        return result.rowcount

    def _retry_after(self, tokens: float) -> int:
        return max(1, math.ceil((1.0 - tokens) / self._policy.rate))
