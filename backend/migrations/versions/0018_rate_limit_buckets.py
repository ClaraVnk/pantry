"""hold the rate-limit token buckets in PostgreSQL rather than in one process

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-04 00:00:00.000000+00:00

``api/throttling.py`` has carried this warning since it was written:

    **Scope: one process, and nothing wider.** The counters are plain dictionaries
    living on ``app.state``. Two uvicorn workers therefore grant two budgets, a
    restart forgets every counter, and a second replica doubles everything again.
    [...] This paragraph exists so that the day somebody adds ``--workers 4``, the
    regression is written down rather than discovered.

A penetration test confirmed it as a live gap rather than a future one, and named
what it costs: every budget multiplies, **including the login and machine-token
guessing limiters**. On a publicly reachable instance those two are the difference
between a rate-limited password spray and an unlimited one, and nothing about the
deployment prevents a second worker -- the operator adds a flag, the limits halve
in meaning, and no test fails.

The table
---------

One row per ``(scope, bucket_key)``, holding a token count and when it was last
touched. ``scope`` is which limiter -- there are eight -- and ``bucket_key`` is
that limiter's unit: a household identifier, a client address, an account. One
table with a scope column rather than eight tables, because the alternative is
eight schemas that differ only in a name; and a scope column rather than a scope
*table*, because the set of limiters is decided by the code that constructs them,
not by data, and a foreign key would make adding one a migration.

**Not tenant-scoped, and it must not be.** Every other table carrying a
``household_id`` is under row-level security, and this one deliberately carries
none: its keys include client addresses seen before any household is known, and
normalised e-mail addresses of accounts that may not exist. A tenant column here
would be null for exactly the rows that matter most -- the pre-authentication
ones -- and ``ADR-0006``'s rule ("every business table carries the tenant") does
not apply because this is not business data. It is declared in the tenancy guard's
``GLOBAL_TABLES`` with that reason, which is how this schema records such an
exemption rather than leaving it to be rediscovered.

``tokens`` is ``double precision``
-----------------------------------

The in-memory limiter is a *continuous* token bucket, not a fixed window, and for
a reason its docstring gives: a fixed window lets a caller spend the whole budget
in the last second of one window and the whole of the next in the first second of
the following one -- twice the advertised rate at exactly the wrong moment. Keeping
that behaviour means keeping fractional tokens. An integer column would have
quietly turned the shared limiter into the fixed window the in-memory one was
written to avoid, and the drift would have shown up as "twice the rate, sometimes"
rather than as an error.

What is *not* here
------------------

No lease table for ``ConcurrencyLimiter``. A concurrency slot is held for the
duration of a request, so sharing it means a lease with an expiry, and a worker
killed mid-inference leaks its slot until that expiry elapses. Too short and the
cap stops binding while the inference is still running; too long and one crash
denies a household for minutes -- a worse failure than the one being fixed. Its
process-wide half is *correctly* per-process regardless, being a statement about
this process's memory. See ``infra/rate_limits.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_bucket",
        sa.Column(
            "scope",
            sa.String(length=48),
            nullable=False,
            comment="Which limiter this bucket belongs to; see infra/rate_limits.py.",
        ),
        sa.Column(
            "bucket_key",
            sa.String(length=320),
            nullable=False,
            comment=(
                "That limiter's unit of account: a household id, a client address, "
                "or a normalised e-mail. 320 characters is the longest addressable "
                "e-mail, which is the widest of the three."
            ),
        ),
        sa.Column(
            "tokens",
            sa.Float(),
            nullable=False,
            comment=(
                "Fractional on purpose: this is a continuous token bucket, and an "
                "integer column would silently make it the fixed window the "
                "in-memory limiter was written to avoid."
            ),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Server time, always. Refill is computed from the interval "
                "PostgreSQL measures, because two workers have two clocks and a "
                "bucket refilled against the last one to arrive grants more than "
                "it says."
            ),
        ),
        sa.PrimaryKeyConstraint("scope", "bucket_key", name=op.f("pk_rate_limit_bucket")),
        sa.CheckConstraint("tokens >= 0.0", name=op.f("ck_rate_limit_bucket_tokens_not_negative")),
        comment=(
            "Shared token buckets for the API rate limiters. Deliberately carries no "
            "household_id: its keys include client addresses seen before any household "
            "is known. Written outside the request transaction so a rollback cannot "
            "erase an attempt that was made."
        ),
    )
    # The sweep deletes by age across every scope at once, so the index it wants is
    # on the timestamp alone. Lookups go through the primary key.
    op.create_index(
        "ix_rate_limit_bucket_updated_at",
        "rate_limit_bucket",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_bucket_updated_at", table_name="rate_limit_bucket")
    op.drop_table("rate_limit_bucket")
