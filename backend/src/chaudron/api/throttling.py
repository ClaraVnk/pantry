"""Per-household caps on the two endpoints that spend something the caller does not own.

``POST /v1/recipes/suggest`` spends model tokens -- billed to the household in
``byok`` mode, to the operator in ``instance_owner`` mode -- or CPU on a
colocated Ollama that a single oversized request has already been observed to
OOM-kill (``infra/llm/settings.py``). ``POST /v1/receipts/import`` spends the same
tokens *and* several megabytes of memory per call, because a photograph travels
with the prompt. ``GET /v1/products/lookup`` spends the Open Food Facts budget,
which ADR-0008 fixes at ten calls per minute **for the whole instance**: one
household looping on it takes barcode resolution away from every other household
on the same deployment.

**The key is the household, not the source address.** An address is shared by
every device behind a home router and is changed by turning a phone's data
connection off and on; the household identifier is at least the unit the cost is
attributed to. It is emphatically *not* a proof of identity -- the header is
provisional, see :func:`chaudron.api.deps.get_household_id` -- so what is built
here is a **spend cap per tenant, not an anti-abuse control**: somebody holding
three household identifiers gets three budgets. Closing that requires
authentication, which is a separate decision and deliberately not made here.

**Scope: the whole deployment, for the rate caps.** Every limiter named in
:class:`Throttles` as a rate is a :class:`chaudron.infra.rate_limits.SharedRateLimiter`,
whose token buckets are rows in PostgreSQL. Two uvicorn workers therefore grant
one budget between them, a restart forgets nothing, and a second replica adds no
capacity to a password guesser. This module used to warn that the day somebody
added ``--workers 4`` the limits would quietly stop meaning what they said; that
day no longer has that consequence, and ``infra/rate_limits.py`` records why the
obvious cheap version of the move -- counting inside the request transaction --
would have been a hole rather than a shortcut.

**Scope: one process, for the concurrency caps.** :class:`ConcurrencyLimiter`
stays a plain dictionary on ``app.state``, deliberately. A slot is held for the
duration of a request, so a shared table means a lease with an expiry, and a
worker killed mid-inference leaks its slot until that expiry elapses: short, and
the cap stops binding while an inference runs; long, and one crash denies the
household for minutes. Its process-wide half -- "a small machine running Ollama
must not be asked for six inferences at once" -- is *correctly* per-process
anyway, being a statement about this process's memory.

:class:`ConcurrencyLimiter` is synchronous and mutates its state without awaiting
in between, which is what makes it safe under a single event loop: no other task
can observe a half-updated count.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # For annotations only. ``infra/rate_limits`` imports :class:`AtCapacityError`
    # from this module, so a runtime import back would close the cycle;
    # ``from __future__ import annotations`` leaves the dataclass fields below as
    # strings, and nothing here ever evaluates the name.
    from chaudron.infra.rate_limits import SharedRateLimiter

#: What a caller waiting on a concurrency slot is told. The honest answer is "as
#: long as the inference in front of you takes", which is unknown and provider
#: dependent; half a minute is short enough to be worth retrying and long enough
#: not to invite an immediate retry storm.
CONCURRENCY_RETRY_AFTER_SECONDS: int = 30


class AtCapacityError(Exception):
    """A limiter refused. ``retry_after`` is in seconds, and is never zero."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"at capacity, retry after {retry_after}s")
        self.retry_after = retry_after


class ConcurrencyLimiter:
    """A cap on requests in flight, per key and for the whole process.

    Refusing is the point: queueing would turn a burst into a pile of open
    connections each holding a database session and an outbound HTTP call, which
    is the failure this exists to prevent. A caller gets ``429`` immediately and
    decides for itself whether to come back.

    The per-key cap answers "two browser tabs must not double the bill"; the
    process-wide cap answers "a small machine running Ollama must not be asked for
    six inferences at once".
    """

    def __init__(self, *, per_key: int, total: int) -> None:
        if per_key < 1 or total < 1:
            raise ValueError("concurrency caps must be at least 1")
        if total < per_key:
            raise ValueError("the process-wide cap cannot be below the per-key cap")
        self._per_key = per_key
        self._total = total
        self._in_flight: dict[str, int] = {}
        self._current_total = 0

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        """Hold a slot for the duration of the block, or raise :class:`AtCapacityError`."""
        held = self._in_flight.get(key, 0)
        if held >= self._per_key or self._current_total >= self._total:
            raise AtCapacityError(CONCURRENCY_RETRY_AFTER_SECONDS)
        self._in_flight[key] = held + 1
        self._current_total += 1
        try:
            yield
        finally:
            remaining = self._in_flight[key] - 1
            if remaining:
                self._in_flight[key] = remaining
            else:
                del self._in_flight[key]
            self._current_total -= 1


@dataclass(frozen=True, slots=True)
class Throttles:
    """The limiters a single application instance owns, built once by the factory.

    The rate caps are shared -- one bucket per key across every worker and every
    replica (``infra/rate_limits.py``) -- and therefore awaited. The concurrency
    caps are per-process and synchronous, for the reason the module docstring
    gives.
    """

    recipe_suggestions: SharedRateLimiter
    recipe_inferences: ConcurrencyLimiter
    product_lookups: SharedRateLimiter

    # -- Receipt import ----------------------------------------------------- #
    #
    # The third endpoint that spends something the caller does not own, and the
    # only one that spends two things at once: a photographed receipt is a billed
    # inference *and* a multi-megabyte upload held in memory while base64 doubles
    # it. The rate cap is the wallet; the concurrency slot is the machine.

    #: Receipt imports per household per hour.
    receipt_imports: SharedRateLimiter
    #: Receipt reads in flight, per household and for the whole process.
    receipt_inferences: ConcurrencyLimiter

    # -- Shopping-list import ------------------------------------------------ #
    #
    # The endpoint that spends nothing a *provider* bills for, and therefore the
    # one that had no limiter at all. That was the finding: reading a PDF is CPU
    # the caller does not own, and an uncapped parse of an uncapped number of
    # documents is a denial of service with an open sign-up in front of it.
    # Fifteen concurrent one-megabyte uploads were answered fifteen times while
    # every other request in the process waited.
    #
    # Both limiters cover the pasted-text entrance as well as the file one. The
    # cheap path does not need a cap of its own; leaving it uncapped would simply
    # move the attack to whichever entrance was measured less.

    #: Shopping-list imports per household per hour.
    shopping_imports: SharedRateLimiter
    #: Documents being read in flight, per household and for the whole process.
    #: This is the cap that bounds total parsing CPU: the per-document limits in
    #: ``infra/documents/sandbox.py`` bound one document and say nothing about a
    #: thousand.
    shopping_import_documents: ConcurrencyLimiter

    # -- Sign-in and sign-up ------------------------------------------------ #
    #
    # The four below are the only limiters that guard an **unauthenticated**
    # endpoint, which is why they were the ones the move to shared state was for:
    # per-process counters gave a password guesser one budget per worker, and
    # adding a replica doubled it again. They are now rows in PostgreSQL like the
    # rest, so the budget is the deployment's rather than the process's.
    #
    # Two limiters on sign-in rather than one, because neither sees what the
    # other does. Keyed by source address, it bounds one host spraying a password
    # across many accounts -- invisible to a per-account counter, which sees one
    # attempt per account. Keyed by email address, it bounds a botnet grinding at
    # a single account -- invisible to a per-address counter, which sees one
    # attempt per host. Both, or neither is worth having.

    #: Sign-in attempts per source address.
    login_attempts_by_ip: SharedRateLimiter
    #: Sign-in attempts per email address, counted whether or not it exists --
    #: skipping the miss would make the limiter itself answer "does this account
    #: exist?", which is the question the endpoint refuses to answer.
    login_attempts_by_account: SharedRateLimiter
    #: Account creations per source address.
    registrations: SharedRateLimiter

    #: Machine tokens that were **rejected**, per source address. The fourth
    #: limiter guarding an unauthenticated endpoint, and the note above applies
    #: to it in full. Only failures are charged: a running integration presents
    #: the same valid token on a timer, so counting successes would throttle the
    #: legitimate use and leave a guesser exactly the same budget
    #: (``api/deps.py``).
    machine_token_attempts: SharedRateLimiter
