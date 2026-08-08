"""The properties that made the limiters worth moving out of the process.

``api/throttling.py`` has carried the warning since it was written: per-process
counters mean two workers grant two budgets, and on the login and machine-token
limiters that is the difference between a rate-limited password spray and an
unlimited one. :mod:`chaudron.infra.rate_limits` is the shared state that closes
it.

What is exercised here is deliberately not "the arithmetic is right" -- the
in-memory limiter already has that, and duplicating it would only prove the two
were written by the same hand. These tests target the three properties the move
was *for*, each of which is invisible to a single-process test:

* a deduction survives the request transaction rolling back,
* two workers racing for the last token do not both get it,
* the refill is measured by the server, not by whichever Python clock asked.

Every test keys its bucket on a unique string. That is not tidiness: the limiter
commits on its own connection precisely so that the surrounding rollback cannot
reach it, which means rows written here outlive the test that wrote them.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AsyncExitStack

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from chaudron.api.throttling import AtCapacityError
from chaudron.infra.db import Database
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from tests.conftest import build_test_settings

pytestmark = pytest.mark.anyio


def _unique_key() -> str:
    return f"test-{uuid.uuid4()}"


async def _tokens(engine: AsyncEngine, scope: str, key: str) -> float | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT tokens FROM rate_limit_bucket WHERE scope = :s AND bucket_key = :k"),
                {"s": scope, "k": key},
            )
        ).one_or_none()
    return None if row is None else float(row.tokens)


async def _backdate(engine: AsyncEngine, scope: str, key: str, seconds: float) -> None:
    """Move a bucket's clock backwards, in the database, by the server's own reckoning.

    The only honest way to test a refill whose whole point is that Python does not
    measure it. Sleeping for the real duration would make the suite pay the window
    it is testing; patching a Python clock would test a clock this code does not
    read.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE rate_limit_bucket SET updated_at = now() - make_interval(secs => :secs)"
                " WHERE scope = :s AND bucket_key = :k"
            ),
            {"secs": seconds, "s": scope, "k": key},
        )


async def test_a_bucket_grants_its_limit_and_then_refuses(engine: AsyncEngine) -> None:
    """The cap binds, and the refusal is an exception rather than a silent pass."""
    policy = BucketPolicy(scope="test-cap", limit=3, window_seconds=3600)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    for _ in range(3):
        await limiter.acquire(key)

    with pytest.raises(AtCapacityError):
        await limiter.acquire(key)

    assert await _tokens(engine, policy.scope, key) == pytest.approx(0.0, abs=0.01)


async def test_the_refusal_carries_a_wait_that_would_actually_help(engine: AsyncEngine) -> None:
    """``Retry-After`` is never zero, and is the time to *one* token rather than to a full bucket.

    A zero invites an immediate retry, which is how a limiter becomes a busy loop;
    a full-bucket wait would hold a caller back far longer than the limit says.
    """
    policy = BucketPolicy(scope="test-retry", limit=2, window_seconds=60)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    await limiter.acquire(key)
    await limiter.acquire(key)

    with pytest.raises(AtCapacityError) as refusal:
        await limiter.acquire(key)

    # One token at 2 per 60s is 30 seconds; never zero, and well short of the window.
    assert refusal.value.retry_after >= 1
    assert refusal.value.retry_after <= 30


async def test_the_deduction_survives_the_request_transaction_rolling_back(
    engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    """**The reason this class exists.**

    If the counter lived in the request's transaction, anyone who could make a
    request fail would get unlimited attempts -- and on the login limiter, failing
    is what an attacker is doing anyway. So the limiter commits on a connection of
    its own, and this test proves it by rolling the caller's work back and finding
    the charge still there.
    """
    policy = BucketPolicy(scope="test-rollback", limit=5, window_seconds=3600)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    await db_session.execute(text("SELECT 1"))
    await limiter.acquire(key)
    await db_session.rollback()

    assert await _tokens(engine, policy.scope, key) == pytest.approx(4.0, abs=0.01)


async def test_two_workers_racing_for_the_last_token_do_not_both_get_it(
    engine: AsyncEngine,
) -> None:
    """The row lock, exercised rather than asserted from the SQL.

    Eight callers, one token. Read-modify-write without the ``FOR UPDATE`` would
    let several read the same 1.0 and each conclude it was theirs; that is the
    exact shape of the bug the in-process limiter avoided only by never being
    interleaved. Here the interleaving is real, so the lock has to do the work.
    """
    policy = BucketPolicy(scope="test-race", limit=1, window_seconds=3600)
    key = _unique_key()

    async def attempt() -> bool:
        # A limiter each, as eight separate workers would have.
        limiter = SharedRateLimiter(engine, policy)
        try:
            await limiter.acquire(key)
        except AtCapacityError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(8)))

    assert sum(outcomes) == 1, "exactly one caller may spend the only token"
    assert await _tokens(engine, policy.scope, key) == pytest.approx(0.0, abs=0.01)


async def test_time_refills_the_bucket_by_the_server_s_reckoning(engine: AsyncEngine) -> None:
    """Elapsed time comes from PostgreSQL, so a worker with a skewed clock cannot mint tokens."""
    policy = BucketPolicy(scope="test-refill", limit=4, window_seconds=60)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    for _ in range(4):
        await limiter.acquire(key)
    with pytest.raises(AtCapacityError):
        await limiter.acquire(key)

    # Half a window at 4 per 60s is two tokens back.
    await _backdate(engine, policy.scope, key, 30)
    await limiter.acquire(key)
    await limiter.acquire(key)

    with pytest.raises(AtCapacityError):
        await limiter.acquire(key)


async def test_the_refill_never_overflows_the_capacity(engine: AsyncEngine) -> None:
    """An idle bucket is full, not fuller. Otherwise a quiet hour buys a burst of hours."""
    policy = BucketPolicy(scope="test-overflow", limit=3, window_seconds=60)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    await limiter.acquire(key)
    await _backdate(engine, policy.scope, key, 60 * 24)

    for _ in range(3):
        await limiter.acquire(key)
    with pytest.raises(AtCapacityError):
        await limiter.acquire(key)


async def test_hammering_a_refused_bucket_does_not_make_it_refill_faster(
    engine: AsyncEngine,
) -> None:
    """Asking more often must not buy anything.

    The first version of :meth:`SharedRateLimiter.acquire` raised *inside* its
    ``engine.begin()`` block, so the write it made on a refusal was rolled back by
    the context manager -- the deduction it believed it was banking never reached
    the table. The fix was to stop writing on refusals altogether, which makes the
    question this test asks the interesting one: with the refusal path writing
    nothing, does a caller who retries in a tight loop still wait the same time as
    one who asks once?

    Phrased against what a caller can observe -- when a token becomes available --
    rather than against the column, because the column is the part that changed.
    """
    policy = BucketPolicy(scope="test-hammering", limit=1, window_seconds=60)
    limiter = SharedRateLimiter(engine, policy)
    patient, impatient = _unique_key(), _unique_key()

    await limiter.acquire(patient)
    await limiter.acquire(impatient)

    # Half a window for both; one of them spends it retrying.
    await _backdate(engine, policy.scope, patient, 30)
    await _backdate(engine, policy.scope, impatient, 30)
    for _ in range(5):
        with pytest.raises(AtCapacityError):
            await limiter.acquire(impatient)

    # Half a window is not a whole one, for either of them.
    with pytest.raises(AtCapacityError):
        await limiter.acquire(patient)
    with pytest.raises(AtCapacityError):
        await limiter.acquire(impatient)

    # A full window since the grant, and both are served -- the retries bought
    # nothing, and cost nothing.
    await _backdate(engine, policy.scope, patient, 60)
    await _backdate(engine, policy.scope, impatient, 60)
    await limiter.acquire(patient)
    await limiter.acquire(impatient)


async def test_a_refund_never_creates_a_token(engine: AsyncEngine) -> None:
    """Capped at capacity, and a no-op on a bucket the table has forgotten.

    Matches the in-memory limiter exactly: ``routers/calendar.py`` charges before
    it checks a credential and refunds on success, so a double refund or one for a
    rolled-over window must leave the bucket full rather than above full.
    """
    policy = BucketPolicy(scope="test-refund", limit=2, window_seconds=3600)
    limiter = SharedRateLimiter(engine, policy)
    key = _unique_key()

    await limiter.acquire(key)
    await limiter.refund(key)
    assert await _tokens(engine, policy.scope, key) == pytest.approx(2.0, abs=0.01)

    await limiter.refund(key)
    assert await _tokens(engine, policy.scope, key) == pytest.approx(2.0, abs=0.01)

    # A key never charged is not an error, and does not conjure a row.
    unknown = _unique_key()
    await limiter.refund(unknown)
    assert await _tokens(engine, policy.scope, unknown) is None


async def test_the_sweep_drops_only_buckets_idle_for_a_whole_window(engine: AsyncEngine) -> None:
    """Without it the table grows one row per distinct client address, for ever.

    Safe by arithmetic rather than by heuristic, the same argument the in-memory
    sweep makes: a bucket untouched for a full window has refilled to capacity, so
    deleting it and re-creating it full are the same thing.
    """
    policy = BucketPolicy(scope="test-sweep", limit=2, window_seconds=60)
    limiter = SharedRateLimiter(engine, policy)
    idle, busy = _unique_key(), _unique_key()

    await limiter.acquire(idle)
    await limiter.acquire(busy)
    await _backdate(engine, policy.scope, idle, 120)

    await limiter.sweep()

    assert await _tokens(engine, policy.scope, idle) is None
    assert await _tokens(engine, policy.scope, busy) is not None


async def test_scopes_do_not_share_a_budget(engine: AsyncEngine) -> None:
    """Eight limiters in one table, kept apart by the scope half of the primary key.

    The same household id is a key for the recipe limiter and the receipt one; if
    the scope were not part of the key, suggesting recipes would spend the budget
    for importing a receipt.
    """
    key = _unique_key()
    first = SharedRateLimiter(
        engine, BucketPolicy(scope="test-scope-a", limit=1, window_seconds=60)
    )
    second = SharedRateLimiter(
        engine, BucketPolicy(scope="test-scope-b", limit=1, window_seconds=60)
    )

    await first.acquire(key)
    with pytest.raises(AtCapacityError):
        await first.acquire(key)

    await second.acquire(key)


# --------------------------------------------------------------------------- #
# The pool the limiters draw from
# --------------------------------------------------------------------------- #


async def test_a_saturated_session_pool_does_not_stall_the_limiter(
    initialised_database: str,
) -> None:
    """A limiter must still answer when every request connection is in use.

    This is a regression test for a bug this module's own design created. The
    limiter opens a connection of its own, deliberately, so its deduction
    survives the request transaction rolling back. But it runs *inside* a
    request, which already holds a session connection -- so on a single pool a
    rate-limited request holds one connection and asks for a second. Fill the
    pool with such requests and each is holding what the others are waiting for:
    no deadlock the database can see, just every one of them sitting on
    ``pool_timeout`` and then returning ``500``. The limiter's job at that moment
    was to answer ``429`` in a millisecond, so the control meant to keep the
    instance up was what took it down.

    :class:`Database` therefore gives the limiters a pool of their own. The
    assertion is the one that would have failed before: with the session pool
    checked out to the last connection, an acquisition still completes -- and
    quickly, rather than after the thirty-second timeout that made the old
    behaviour look like a hang.
    """
    database = Database(build_test_settings(initialised_database))
    policy = BucketPolicy(scope="test-pool", limit=5, window_seconds=60)
    limiter = SharedRateLimiter(database.limiter_engine, policy)

    settings = build_test_settings(initialised_database)
    saturating = settings.db_pool_size + settings.db_max_overflow

    async with AsyncExitStack() as stack:
        for _ in range(saturating):
            await stack.enter_async_context(database.engine.connect())

        # Every request connection is now held. On one pool this call would wait
        # for one that nothing is going to return.
        await asyncio.wait_for(limiter.acquire(_unique_key()), timeout=10)

    await database.dispose()
