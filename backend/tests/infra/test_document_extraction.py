"""What the document readers do, and -- more to the point -- what they refuse.

The central test in this file is :func:`test_hostile_pdf_resolves_nothing`. It is
the one that would fail if the PDF library were swapped for a renderer, and it is
the reason a library was chosen on "has no network stack" rather than on speed
(contract 7.3, and AUD-005 for the SSRF this closes from a second direction).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import pathlib
import re
import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError
from chaudron.infra.documents import (
    SandboxLimits,
    configure_document_sandbox,
    decode_text,
    extract_pdf_text,
    extract_pdf_text_isolated,
    shutdown_document_sandbox,
)
from tests.support.pdfs import build_flate_bomb, build_pdf, build_raw_pdf

#: Everything an attacker can put in a PDF to make a reader talk to the network,
#: run code, or open a file. All of it in one document, all of it inert.
HOSTILE_CATALOG = (
    "/OpenAction << /S /JavaScript /JS (app.launchURL('http://127.0.0.1:9/pwned');) >> "
    "/Names << /JavaScript << /Names [(evil) << /S /JavaScript "
    "/JS (this.getURL('http://127.0.0.1:9/pwned2');) >> ] >> >> "
    "/AcroForm << /XFA [ (config) 1 0 R ] >>"
)
HOSTILE_PAGE = (
    "/Annots [ << /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
    "/A << /S /URI /URI (http://127.0.0.1:9/uri-annotation) >> >> "
    "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
    "/A << /S /Launch /F (/bin/sh) >> >> ] "
)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every socket entry point raises. A test that needs one fails loudly."""

    def refuse(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("the document reader opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every filesystem path opened while the reader runs."""
    seen: list[str] = []
    real_open = builtins.open
    real_path_open = pathlib.Path.open

    def watching_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    def watching_path_open(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watching_open)
    monkeypatch.setattr(pathlib.Path, "open", watching_path_open)
    yield seen


# --------------------------------------------------------------------------- #
# The security property
# --------------------------------------------------------------------------- #


def test_hostile_pdf_resolves_nothing(no_network: None, opened_paths: list[str]) -> None:
    """A PDF carrying script, a URL and a launch action yields only its text.

    Without the "text extraction only" rule this fails in one of three ways: the
    socket stubs raise, the opened-path recorder shows a file, or the extracted
    text carries something the content stream did not contain. It is the test that
    stops a future author from reaching for ``page.images`` or a renderer.
    """
    pdf = build_pdf(
        [["2 kg de pommes de terre", "pain"]],
        extra_catalog=HOSTILE_CATALOG,
        extra_page=HOSTILE_PAGE,
    )

    text = extract_pdf_text(pdf, max_pages=10, max_chars=10_000)

    assert "pommes de terre" in text, "the legitimate content must still be read"
    assert "127.0.0.1" not in text, "no URL from the document may reach the output"
    assert opened_paths == [], f"the reader opened {opened_paths}"


def test_external_stream_is_not_fetched(no_network: None, opened_paths: list[str]) -> None:
    """A stream declaring its data lives elsewhere reads as empty, not as elsewhere.

    ``/F`` on a stream is the PDF way of saying "my bytes are in that other file",
    with a local path or a URL. Honouring it would be an arbitrary file read and
    an SSRF in one feature. Both spellings are here.
    """
    objects = [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length 0 /F (/etc/passwd) >>\nstream\n\nendstream",
        b"<< /Length 0 /F << /FS /URL /F (http://127.0.0.1:9/steal) >> >>\nstream\n\nendstream",
        b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
        b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 1 0 R >> >> /Contents 3 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Catalog /Pages 6 0 R >>",
    ]

    text = extract_pdf_text(build_raw_pdf(objects, catalog_number=7), max_pages=5, max_chars=10_000)

    assert text.strip() == "", "an external stream must contribute nothing"
    assert "/etc/passwd" not in opened_paths, "the local file specification was followed"
    assert opened_paths == [], f"the reader opened {opened_paths}"


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #


def test_page_count_over_the_bound_is_refused() -> None:
    """413 with the bound quoted, never the first N pages read silently."""
    pdf = build_pdf([[f"article {index}"] for index in range(6)])

    with pytest.raises(DocumentTooLargeError) as caught:
        extract_pdf_text(pdf, max_pages=3, max_chars=100_000)

    assert caught.value.measure == "pages"
    assert caught.value.limit == 3


def test_extracted_text_over_the_bound_is_refused() -> None:
    """The character ceiling stops the read at the page that passes it.

    A partial answer would be a shopping list missing its end with nothing saying
    so, which is the failure mode the contract names explicitly.
    """
    pdf = build_pdf([[f"ligne {index} de la liste"] for index in range(40)])

    with pytest.raises(DocumentTooLargeError) as caught:
        extract_pdf_text(pdf, max_pages=100, max_chars=50)

    assert caught.value.measure == "characters"


def test_a_pdf_within_the_bounds_reads_every_page() -> None:
    pdf = build_pdf([["lait", "pain"], ["café", "sucre"]])

    text = extract_pdf_text(pdf, max_pages=10, max_chars=10_000)

    assert {"lait", "pain", "café", "sucre"} <= set(text.split())


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_bytes_that_are_not_a_pdf_are_refused_before_the_parser() -> None:
    with pytest.raises(DocumentUnreadableError):
        extract_pdf_text(b"PK\x03\x04 this is a zip", max_pages=10, max_chars=10_000)


def test_an_encrypted_pdf_is_refused_rather_than_decrypted() -> None:
    """Refusing is the point: decryption is a second parser, reached first."""
    objects = [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Filter /Standard /V 1 /R 2 /O <00> /U <00> /P -1 >>",
    ]
    body = build_raw_pdf(objects, catalog_number=3)
    encrypted = body.replace(b"/Root 3 0 R", b"/Root 3 0 R /Encrypt 4 0 R")

    with pytest.raises(DocumentUnreadableError):
        extract_pdf_text(encrypted, max_pages=10, max_chars=10_000)


# --------------------------------------------------------------------------- #
# Plain text
# --------------------------------------------------------------------------- #


def test_utf8_text_is_decoded() -> None:
    assert decode_text("pâtes\ncrème".encode(), max_chars=100) == "pâtes\ncrème"


def test_windows_encoded_french_is_decoded_rather_than_mangled() -> None:
    """cp1252 is what a Windows editor writes, and "pâtes" must survive it.

    Decoding as UTF-8 with ``errors="replace"`` would produce "p?tes" and hand a
    mangled label to the catalogue lookup -- a silent wrong answer rather than a
    refusal.
    """
    assert decode_text("pâtes".encode("cp1252"), max_chars=100) == "pâtes"


def test_a_utf8_bom_does_not_become_part_of_the_first_item() -> None:
    assert decode_text("﻿lait".encode(), max_chars=100) == "lait"


def test_binary_masquerading_as_text_is_refused() -> None:
    with pytest.raises(DocumentUnreadableError):
        decode_text(b"\x00\x01\x02 lait", max_chars=100)


def test_text_over_the_character_bound_is_refused() -> None:
    with pytest.raises(DocumentTooLargeError) as caught:
        decode_text(b"a" * 200, max_chars=100)

    assert caught.value.measure == "characters"
    assert caught.value.limit == 100


def test_the_pdf_library_has_no_network_stack() -> None:
    """The property the library was chosen for, asserted against its source.

    :func:`test_hostile_pdf_resolves_nothing` proves this document does not reach
    the network; this proves the library *cannot*, which is what makes the choice
    hold across a version bump. If a future pypdf grows an HTTP client, this fails
    at once rather than the first time somebody uploads a crafted file.
    """
    import pypdf

    package = pathlib.Path(pypdf.__file__).parent
    forbidden = re.compile(r"^\s*(?:import|from)\s+(urllib|requests|socket|http|ssl|ftplib)\b")
    offenders = [
        f"{module.name}:{number}"
        for module in package.rglob("*.py")
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), start=1)
        if forbidden.match(line)
    ]

    assert offenders == [], f"pypdf now imports a network module at {offenders}"


# --------------------------------------------------------------------------- #
# The bound that is not in this process (infra/documents/sandbox.py)
#
# Everything above measures what a document *produced*. The three tests below are
# about a document that produces nothing and spends everything, which is the one
# no bound in ``pdf.py`` could see: a 1.28 MB, one-page file was measured
# inflating to 886 MB, returning zero characters, and being accepted.
# --------------------------------------------------------------------------- #


@pytest.fixture
def tight_sandbox() -> Iterator[None]:
    """Smaller budgets, so the bombs below cost a test run a second rather than ten."""
    configure_document_sandbox(
        SandboxLimits(
            address_space_bytes=384 * 1024 * 1024,
            cpu_seconds=3,
            expansion_bytes=32 * 1024 * 1024,
            workers=2,
        )
    )
    try:
        yield
    finally:
        shutdown_document_sandbox()
        configure_document_sandbox(SandboxLimits())


async def test_a_decompression_bomb_that_returns_nothing_is_still_refused(
    tight_sandbox: None,
) -> None:
    """The finding, exactly. Under the file, page and character bounds, and rejected.

    ``measure="expansion"`` is the whole point: this document passes every bound
    that counts output, because it has none. What gives it away is what it
    *allocated*, and that is only measurable in a process whose allocations are
    the document's alone.
    """
    bomb = build_flate_bomb(inflated_bytes=400 * 1024 * 1024)
    assert len(bomb) < 1024 * 1024, "the fixture must sit under the upload ceiling"

    with pytest.raises(DocumentTooLargeError) as raised:
        await extract_pdf_text_isolated(bomb, max_pages=20, max_chars=200_000)

    assert raised.value.measure == "expansion"


async def test_a_document_that_burns_cpu_is_refused_at_the_budget(tight_sandbox: None) -> None:
    """120 seconds of CPU on a one-megabyte upload, answered after the work. Now bounded.

    The wall is ``RLIMIT_CPU`` rather than a timeout, and that distinction is the
    reason this file exists: ``asyncio.wait_for`` around a thread returns to the
    caller and leaves the thread running, so the CPU is spent either way. Only the
    kernel can take it back.
    """
    bomb = build_flate_bomb(pages=20, inflated_bytes=30 * 1024 * 1024)

    started = time.perf_counter()
    with pytest.raises(DocumentTooLargeError) as raised:
        await extract_pdf_text_isolated(bomb, max_pages=20, max_chars=2_000_000)
    elapsed = time.perf_counter() - started

    assert raised.value.measure in {"expansion", "processing"}
    assert elapsed < 30, "the budget is what bounds this, and it is three seconds"


async def test_the_event_loop_keeps_running_while_a_document_is_read(
    tight_sandbox: None,
) -> None:
    """The other half of the finding: one upload froze ``/healthz`` for 48 seconds.

    A parse on the event loop is not a slow request, it is a stopped process. This
    asserts the loop is *free* during the parse by counting ticks that could not
    have happened if it were blocked -- the bound above makes the work finite, and
    this makes it invisible to everybody else.
    """
    bomb = build_flate_bomb(pages=8, inflated_bytes=20 * 1024 * 1024)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        with contextlib.suppress(DocumentTooLargeError, DocumentUnreadableError):
            await extract_pdf_text_isolated(bomb, max_pages=20, max_chars=200_000)
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    assert ticks > 5, "the loop was blocked for the whole parse"


async def test_an_ordinary_document_still_reads_through_the_worker() -> None:
    """The bound must not be a feature removal: the normal path is unchanged."""
    text = await extract_pdf_text_isolated(
        build_pdf([["PAIN COMPLET", "LAIT 1 L"]]), max_pages=20, max_chars=200_000
    )
    assert "PAIN COMPLET" in text
    assert "LAIT 1 L" in text


async def test_a_refusal_from_the_worker_keeps_its_sentence() -> None:
    """Nothing crosses the process boundary as an exception, so this could silently
    have become a generic 500. It is the same message the in-process reader gives."""
    with pytest.raises(DocumentUnreadableError, match="does not start like a PDF"):
        await extract_pdf_text_isolated(b"not a pdf at all" * 8, max_pages=5, max_chars=1000)
