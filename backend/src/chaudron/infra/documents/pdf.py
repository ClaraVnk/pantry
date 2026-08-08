"""Text out of a PDF, and nothing else out of a PDF.

A PDF supplied by a user is a hostile input (contract 7.3): a binary container
format whose parsers have a long history of memory-safety bugs, and whose
*document model* is worse than its parsers -- it can carry JavaScript, an action
that opens a URL, an action that launches a program, and streams that declare
their data lives in an external file. Any of those turned into behaviour would
replay AUD-005's SSRF from a new direction, this time with the attacker choosing
the whole document.

Why pypdf
---------

Pinned at ``pypdf==6.14.2`` (BSD-3-Clause, released 2026-06-23, no OSV advisory
at time of writing, no runtime dependency at all on Python 3.14). What decided it
over pdfminer.six and PyMuPDF is not speed:

* **It has no network stack.** ``urllib``, ``requests``, ``socket``, ``http`` and
  ``ssl`` do not appear in a single import statement of the package. A PDF that
  references ``http://attacker/`` cannot make this process fetch it, because
  there is nothing in the library that could. This was checked against the
  installed code, not assumed -- and it is asserted in the test suite, which
  parses a PDF carrying ``/OpenAction`` JavaScript, a ``/URI`` link annotation, a
  ``/Launch`` action and two streams whose data is declared to live in an
  external file (one local path, one URL) with every socket entry point replaced
  by a raising stub, and with ``open`` instrumented. Nothing is opened, nothing
  is called, and the external-stream bodies come back empty rather than resolved.
* **It executes nothing.** No JavaScript interpreter is shipped or invoked.
* **PyMuPDF was rejected on licence** (AGPL-3.0 or commercial) and on surface: it
  is a renderer, and this module must not be able to render.
* **pdfminer.six** is comparable on safety but slower and heavier for the one
  operation needed here.

The single ``subprocess`` call in ``pypdf/filters.py`` runs ``jbig2dec`` to decode
JBIG2 *images*. It is reachable only through the image API -- ``page.images``,
``reader.attachments`` -- which this module never touches. Text extraction cannot
reach it.

Residual risk, and where it is actually bounded
------------------------------------------------

A content stream is Flate-compressed, and ``zlib`` decompression in pypdf is
unbounded: a small PDF can inflate to a very large string. This was measured, not
supposed -- a 1.28 MB one-page document expanding to 886 MB, and returning zero
characters, which is what made it pass every bound in this file. The bounds here
count *output*: bytes in, pages, characters produced. A bomb that produces
nothing is invisible to all three.

Nothing in this module can fix that, and it is important not to pretend
otherwise. ``pypdf`` has no decompression budget, and a limit that lives in the
same process as the allocation is a limit checked after the memory has already
been taken. :mod:`chaudron.infra.documents.sandbox` is where the real bound is:
this function runs in a worker process created under ``RLIMIT_AS`` and
``RLIMIT_CPU``, so the expansion fails against the kernel rather than against a
comparison. **Call it through that module, not directly**, on any path fed by an
upload.

What *is* fixed here is the second half of the same finding: the character bound
used to be tested after a page had been extracted in full, so a page inflating to
hundreds of megabytes of text was materialised before anything refused it. The
budget is now enforced *during* extraction, through the visitor pypdf calls as it
emits each chunk.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import Any, Final

from pypdf import PdfReader
from pypdf._page import PageObject
from pypdf.errors import PyPdfError

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError

logger = logging.getLogger(__name__)

#: What a browser sends for a ``.pdf``. ``application/x-pdf`` is a legacy
#: spelling still emitted by a few clients.
PDF_MEDIA_TYPES: Final = frozenset({"application/pdf", "application/x-pdf"})

#: Every PDF starts with this. Checked before the parser is handed the bytes so a
#: mislabelled upload fails on a sentence a user can act on rather than deep
#: inside a format parser.
_MAGIC: Final = b"%PDF-"

#: How far into the file the header may sit. The specification says byte zero;
#: real-world files sometimes carry a few bytes of junk, and readers tolerate it.
_MAGIC_SEARCH_WINDOW: Final = 1024


class _CharBudgetExceeded(BaseException):
    """The character bound was reached while a page was still being extracted.

    A :class:`BaseException` on purpose. It is raised from inside a pypdf visitor,
    and one of the call sites of that visitor -- the XObject branch of
    ``_page.py`` -- is wrapped in ``except Exception``, which would swallow an
    ordinary exception and let the page finish producing text nobody will read.
    """


def extract_pdf_text(data: bytes, *, max_pages: int, max_chars: int) -> str:
    """Return the text of ``data``, or raise. Never returns a partial read.

    ``max_pages`` and ``max_chars`` are hard bounds: passing either raises
    :class:`DocumentTooLargeError`, which the router answers ``413``. Truncating
    instead would hand back a shopping list missing its last items with no
    indication that anything was dropped (contract 7.3).
    """
    if _MAGIC not in data[:_MAGIC_SEARCH_WINDOW]:
        raise DocumentUnreadableError("this file does not start like a PDF")

    try:
        reader = PdfReader(io.BytesIO(data))
    except PyPdfError as error:
        # The library's message quotes byte offsets and object numbers from a file
        # a stranger supplied; it goes to the log, never to the response.
        logger.info("shopping_import_pdf_unreadable", extra={"error": type(error).__name__})
        raise DocumentUnreadableError("this PDF could not be read") from None

    if reader.is_encrypted:
        # Not "ask for a password": an encrypted PDF is a second parser (the
        # decryption handlers) reached before the first, for a document a user can
        # trivially re-export unencrypted. Refusing is cheaper than defending it.
        raise DocumentUnreadableError("this PDF is encrypted")

    try:
        page_count = len(reader.pages)
    except PyPdfError as error:
        logger.info("shopping_import_pdf_no_pages", extra={"error": type(error).__name__})
        raise DocumentUnreadableError("this PDF could not be read") from None

    if page_count > max_pages:
        raise DocumentTooLargeError(
            measure="pages",
            limit=max_pages,
            detail=(
                f"This PDF has {page_count} pages and this endpoint reads at most {max_pages}."
            ),
        )

    chunks: list[str] = []
    total = 0
    for index in range(page_count):
        try:
            # extract_text() and nothing else. No `page.images`, no
            # `reader.attachments`, no `/OpenAction`, no annotation traversal --
            # every one of those is a way for the document to choose what this
            # process does next.
            text = _bounded_page_text(reader.pages[index], remaining=max_chars - total)
        except _CharBudgetExceeded:
            raise _too_many_characters(max_chars) from None
        except (PyPdfError, ValueError, KeyError, TypeError) as error:
            # One unreadable page does not condemn the document: a shopping list
            # exported by a phone app often has a decorative last page. The page
            # is skipped, and the fact is logged rather than surfaced.
            logger.info(
                "shopping_import_pdf_page_skipped",
                extra={"page": index, "error": type(error).__name__},
            )
            continue
        total += len(text)
        if total > max_chars:
            raise _too_many_characters(max_chars)
        chunks.append(text)

    return "\n".join(chunks)


def _bounded_page_text(page: PageObject, *, remaining: int) -> str:
    """One page's text, abandoned the moment it passes what is left of the budget.

    The visitor is called by pypdf with every chunk it emits, so the count is
    running rather than final: a page that would produce a hundred megabytes stops
    at ``remaining`` characters instead of being built in full and then measured.
    That ordering is the finding this closes; the memory a *decompressed* stream
    takes before any text exists is bounded in :mod:`sandbox` instead, because it
    cannot be bounded here.
    """
    produced = 0

    def visit(text: str, *_: Any) -> None:
        nonlocal produced
        produced += len(text)
        if produced > remaining:
            raise _CharBudgetExceeded

    visitor: Callable[..., None] = visit
    return str(page.extract_text(visitor_text=visitor))


def _too_many_characters(max_chars: int) -> DocumentTooLargeError:
    return DocumentTooLargeError(
        measure="characters",
        limit=max_chars,
        detail=(
            f"This document holds more than {max_chars} characters of text, "
            "which is more than this endpoint reads."
        ),
    )
