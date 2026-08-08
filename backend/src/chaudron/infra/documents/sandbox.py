"""Reading a PDF in a process that cannot spend more than it is given.

Why this module exists at all
-----------------------------

``infra/documents/pdf.py`` states its own residual risk plainly: a Flate content
stream decompresses without a budget, so a small file can inflate to an arbitrary
string. The bounds beside it -- file size, page count, character count -- are all
measured on what comes *out*, and a decompression bomb is precisely the document
whose output is empty. A 1.28 MB, one-page PDF was measured expanding to 886 MB
of intermediate buffers and returning zero characters: it passed every bound
because it produced nothing to count. A second one, 1 MB over twenty pages, cost
120 seconds of CPU before the character bound refused it -- *after* the work.

No in-process fix is honest here. ``pypdf`` exposes no decompression budget, and
neither ``asyncio.wait_for`` nor a thread can take back CPU or memory already
being spent: a thread cannot be cancelled, and a ``MemoryError`` raised in one
thread arrives after the allocation has already happened in the shared heap. The
only bound that holds against a document is one the *kernel* enforces, which
means another process.

So the parse happens in a worker process created with ``RLIMIT_AS`` and
``RLIMIT_CPU`` already set. Both are per-document rather than per-worker because
each task gets a fresh process (``max_tasks_per_child=1``) -- ``RLIMIT_CPU``
counts a process's whole lifetime, so a reused worker would eventually be killed
doing legitimate work.

What each limit does, measured
------------------------------

* ``RLIMIT_AS`` is the wall. The 886 MB expansion fails allocating instead, and
  the document is refused. Peak resident growth of the worker is compared against
  ``expansion_bytes`` afterwards and answered ``413`` with ``measure="expansion"``
  -- an ordinary shopping list grows the worker by about 1 MiB, the bombs above by
  more than 140, so the two are not close.
* ``RLIMIT_CPU`` is the clock, and it is delivered as ``SIGXCPU``. The handler
  installed below turns it into an ordinary Python exception, which keeps the pool
  healthy: without it the kernel kills the worker on the hard limit, every task
  sharing that pool dies with it, and the executor is permanently broken. The hard
  limit is still set, a couple of seconds above the soft one, for the case where
  the signal arrives while the interpreter is inside a C call and cannot run a
  handler. The 120-second parse above stops at the configured budget.
* The **throttle** is what bounds the total. These limits cap one document; they
  say nothing about a thousand of them, and ``enforce_shopping_import_limits``
  and ``enforce_receipt_limits`` are what make that finite.

Two deliberate choices
----------------------

**Nothing crosses the process boundary as an exception.** The worker returns an
:class:`_Outcome` and the caller raises. Sending a domain exception back looks
tidier and is a trap: ``DocumentTooLargeError`` takes keyword-only arguments, so
the default exception pickling reconstructs it positionally and fails *in the
executor's result thread*, which breaks the whole pool. That failure was observed
before this returned data instead.

**There is no in-process fallback.** If the pool cannot be created the import
fails loudly. A fallback would silently restore exactly the unbounded parse this
module exists to remove, on the one deployment where nobody would notice.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import resource
import signal
import threading
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from dataclasses import dataclass, field
from types import FrameType
from typing import Final, Literal

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError

logger = logging.getLogger(__name__)

__all__ = [
    "SandboxLimits",
    "configure_document_sandbox",
    "extract_pdf_text_isolated",
    "shutdown_document_sandbox",
]

#: How far above the soft CPU limit the hard one sits. The soft limit is a signal
#: the worker turns into an exception; the hard limit is the kernel killing it,
#: and it only matters when the signal arrived inside a C call. Two seconds is
#: enough for a ``zlib`` block to return and short enough to stay a bound.
_CPU_HARD_MARGIN_SECONDS: Final = 2

#: ``ru_maxrss`` is in kilobytes on Linux.
_RSS_UNIT: Final = 1024

#: Imported in the forkserver so a worker is a fork rather than an interpreter
#: start. Measured at 13 ms per document, against roughly 300 ms without it.
_PRELOAD: Final = ("chaudron.infra.documents.pdf",)


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """What one document may spend. Configuration, never guessed at call time.

    ``address_space_bytes`` has to clear the interpreter's own virtual size --
    measured at about 310 MiB for a worker with ``pypdf`` imported -- plus the
    expansion budget, or every document fails. The default leaves room for both.
    """

    #: ``RLIMIT_AS``: the hard wall, above which allocation fails.
    address_space_bytes: int = 512 * 1024 * 1024
    #: ``RLIMIT_CPU`` soft limit, in seconds, per document.
    cpu_seconds: int = 10
    #: Resident growth above which the document is refused, whatever it returned.
    expansion_bytes: int = 64 * 1024 * 1024
    #: Documents parsed at once. The concurrency throttle is the real cap; this
    #: only stops the pool itself from being the way round it.
    workers: int = 2


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What the worker sends back. Plain data, for the reason in the docstring."""

    kind: Literal["text", "too-large", "unreadable"]
    text: str = ""
    measure: str = ""
    limit: int = 0
    detail: str = ""
    expanded_bytes: int = 0


class _CpuBudgetExceededError(Exception):
    """``SIGXCPU`` arrived. Raised in the worker, never seen outside it."""


# --------------------------------------------------------------------------- #
# The worker side
# --------------------------------------------------------------------------- #

_worker_baseline_rss_bytes = 0


def _peak_rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * _RSS_UNIT


def _on_sigxcpu(signum: int, frame: FrameType | None) -> None:
    raise _CpuBudgetExceededError("cpu budget exhausted")


def _initialise_worker(limits: SandboxLimits) -> None:
    """Put the limits in place *before* the worker is given anything to read."""
    global _worker_baseline_rss_bytes
    signal.signal(signal.SIGXCPU, _on_sigxcpu)
    resource.setrlimit(resource.RLIMIT_AS, (limits.address_space_bytes, limits.address_space_bytes))
    resource.setrlimit(
        resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + _CPU_HARD_MARGIN_SECONDS)
    )
    _worker_baseline_rss_bytes = _peak_rss_bytes()


def _extract_in_worker(
    data: bytes, max_pages: int, max_chars: int, expansion_bytes: int, cpu_seconds: int
) -> _Outcome:
    """Run the real extractor, and answer with data whatever happens."""
    from chaudron.infra.documents.pdf import extract_pdf_text

    try:
        text = extract_pdf_text(data, max_pages=max_pages, max_chars=max_chars)
    except DocumentTooLargeError as error:
        return _Outcome(
            kind="too-large", measure=error.measure, limit=error.limit, detail=error.detail
        )
    except DocumentUnreadableError as error:
        return _Outcome(kind="unreadable", detail=str(error))
    except _CpuBudgetExceededError:
        return _Outcome(
            kind="too-large",
            measure="processing",
            limit=cpu_seconds,
            detail=(
                f"Reading this PDF takes more than the {cpu_seconds} seconds this endpoint "
                "spends on one document."
            ),
        )
    except MemoryError:
        # The expansion hit ``RLIMIT_AS``. Reported as the bound it passed rather
        # than as a failure, because it is one.
        return _Outcome(
            kind="too-large",
            measure="expansion",
            limit=expansion_bytes,
            detail=_EXPANSION_DETAIL,
        )

    expanded = _peak_rss_bytes() - _worker_baseline_rss_bytes
    if expanded > expansion_bytes:
        # The bomb that returns nothing. Every output bound passed -- there was no
        # output -- and this is the only measurement that saw it.
        return _Outcome(
            kind="too-large",
            measure="expansion",
            limit=expansion_bytes,
            detail=_EXPANSION_DETAIL,
            expanded_bytes=expanded,
        )
    return _Outcome(kind="text", text=text, expanded_bytes=max(expanded, 0))


_EXPANSION_DETAIL: Final = (
    "This PDF expands to far more data than it contains, which this endpoint does not "
    "read. Export the document again from the application that produced it."
)


# --------------------------------------------------------------------------- #
# The caller side
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Sandbox:
    limits: SandboxLimits
    lock: threading.Lock = field(default_factory=threading.Lock)
    pool: ProcessPoolExecutor | None = None


_sandbox = _Sandbox(limits=SandboxLimits())


def configure_document_sandbox(limits: SandboxLimits) -> None:
    """Set what one document may spend. Called once, from the application factory.

    Process-wide rather than passed per call, because what it configures is a
    process pool: the limits belong to the workers, and a second set of them would
    mean a second pool.
    """
    with _sandbox.lock:
        if _sandbox.limits == limits and _sandbox.pool is not None:
            return
        _discard_pool_locked()
        _sandbox.limits = limits


def shutdown_document_sandbox() -> None:
    """Release the workers. Idempotent, and safe to call on a pool never created."""
    with _sandbox.lock:
        _discard_pool_locked()


def _discard_pool_locked() -> None:
    pool, _sandbox.pool = _sandbox.pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _pool() -> tuple[ProcessPoolExecutor, SandboxLimits]:
    with _sandbox.lock:
        limits = _sandbox.limits
        if _sandbox.pool is None:
            context = multiprocessing.get_context("forkserver")
            # The forkserver is a fresh interpreter, not a copy of this one: a
            # worker therefore inherits the preloaded parser and *nothing* of the
            # application -- no database pool, no cipher, no household key.
            context.set_forkserver_preload(list(_PRELOAD))
            _sandbox.pool = ProcessPoolExecutor(
                max_workers=limits.workers,
                mp_context=context,
                initializer=_initialise_worker,
                initargs=(limits,),
                # One document per process: this is what makes ``RLIMIT_CPU`` a
                # per-document budget rather than a per-worker lifetime, and what
                # makes peak resident memory a measurement of *this* document.
                max_tasks_per_child=1,
            )
            # The forkserver is spawned by the first ``submit``, not by the
            # constructor. Doing it here, from the thread this function runs in,
            # keeps an ``exec`` and an import off the event loop.
            _sandbox.pool.submit(_warm_up).result()
        return _sandbox.pool, limits


def _warm_up() -> bool:
    """Nothing, in a worker, so that the forkserver exists before it is needed."""
    return True


async def extract_pdf_text_isolated(data: bytes, *, max_pages: int, max_chars: int) -> str:
    """The text of ``data``, read in a worker that cannot outspend its budget.

    Same contract as :func:`chaudron.infra.documents.pdf.extract_pdf_text` --
    bounded text out, :class:`DocumentTooLargeError` or
    :class:`DocumentUnreadableError` otherwise -- plus two bounds it cannot
    express in-process: ``measure="expansion"`` for a document that inflates past
    the memory budget, and ``measure="processing"`` for one that runs past the CPU
    budget.

    Awaiting rather than calling is the other half of the fix: the parse used to
    run on the event loop, and a single one-megabyte upload froze every other
    request in the process for 48 seconds. Measured again after this change, with
    ``/healthz`` polled twenty times a second for the whole parse: median 3 ms,
    worst 14 ms. The first document a process reads still costs a single sample
    of about 90 ms while the pool and its forkserver come up -- once per process,
    and stated here rather than rounded away.
    """
    loop = asyncio.get_running_loop()
    # Built in a thread, so that starting the pool -- an ``exec`` and an import --
    # is not something the event loop waits for.
    pool, limits = await loop.run_in_executor(None, _pool)
    try:
        outcome = await loop.run_in_executor(
            pool,
            _extract_in_worker,
            data,
            max_pages,
            max_chars,
            limits.expansion_bytes,
            limits.cpu_seconds,
        )
    except BrokenExecutor:
        # The worker died rather than answered: the hard CPU limit, or the kernel
        # reclaiming it. The pool is unusable from here on, so it is thrown away
        # and the next document builds a new one.
        with _sandbox.lock:
            _discard_pool_locked()
        logger.warning("document_worker_lost", extra={"cpu_seconds": limits.cpu_seconds})
        raise DocumentTooLargeError(
            measure="processing",
            limit=limits.cpu_seconds,
            detail=(
                f"Reading this PDF takes more than the {limits.cpu_seconds} seconds this "
                "endpoint spends on one document."
            ),
        ) from None

    if outcome.kind == "too-large":
        if outcome.measure == "expansion":
            logger.info(
                "document_expansion_refused",
                extra={"expanded_bytes": outcome.expanded_bytes, "limit": outcome.limit},
            )
        raise DocumentTooLargeError(
            measure=outcome.measure, limit=outcome.limit, detail=outcome.detail
        )
    if outcome.kind == "unreadable":
        raise DocumentUnreadableError(outcome.detail)
    return outcome.text
