"""A minimal PDF writer, so the PDF tests exercise a real file.

Written by hand rather than committed as a binary fixture, and rather than
produced with a PDF *library*: a fixture built by the same ecosystem that reads
it tests a round trip, not a document. This one emits the bytes directly --
header, objects, cross-reference table, trailer -- so what the reader receives is
a file, and so a hostile document can be assembled by adding the dictionary
entries an attacker would add.

Deliberately small: uncompressed content streams, one built-in font, no
metadata. It is enough to carry text, which is all the extractor reads.
"""

from __future__ import annotations

import zlib
from typing import Final

_HEADER: Final = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def _escape(text: str) -> bytes:
    """A PDF literal string: latin-1, with the three reserved bytes escaped."""
    out = text.encode("latin-1", "replace")
    return out.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def build_pdf(
    pages: list[list[str]],
    *,
    extra_catalog: str = "",
    extra_page: str = "",
) -> bytes:
    """A PDF whose pages carry ``pages[i]`` as lines of text.

    ``extra_catalog`` and ``extra_page`` are raw dictionary fragments spliced into
    the catalogue and into every page. They exist so a test can add ``/OpenAction
    /JavaScript``, a ``/URI`` link annotation or a ``/Launch`` action to an
    otherwise ordinary document, and assert that reading it does none of them.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_number = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    content_numbers: list[int] = []
    for lines in pages:
        parts = [b"BT /F1 12 Tf 72 760 Td 14 TL"]
        parts.extend(b"(" + _escape(line) + b") Tj T*" for line in lines)
        parts.append(b"ET")
        stream = b"\n".join(parts)
        content_numbers.append(
            add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        )

    # The page tree object is written after the pages, but every page has to name
    # it as its parent, so its number is computed first.
    pages_number = len(objects) + len(pages) + 1

    page_numbers = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R %s>>"
            % (pages_number, font_number, content_number, extra_page.encode())
        )
        for content_number in content_numbers
    ]

    kids = b" ".join(b"%d 0 R" % number for number in page_numbers)
    written = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_numbers)))
    assert written == pages_number, "page tree object number was mispredicted"

    catalog_number = add(
        b"<< /Type /Catalog /Pages %d 0 R %s>>" % (pages_number, extra_catalog.encode())
    )
    return _assemble(objects, catalog_number)


def build_raw_pdf(objects: list[bytes], catalog_number: int) -> bytes:
    """Assemble arbitrary objects into a valid file, for the odder cases."""
    return _assemble(objects, catalog_number)


def _assemble(objects: list[bytes], catalog_number: int) -> bytes:
    out = bytearray(_HEADER)
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog_number,
        xref_at,
    )
    return bytes(out)


def build_flate_bomb(*, pages: int = 1, inflated_bytes: int = 300 * 1024 * 1024) -> bytes:
    """A small PDF whose content streams inflate to ``inflated_bytes`` per page.

    The document the bounds in ``infra/documents/pdf.py`` cannot see. Every one of
    them counts *output* -- bytes uploaded, pages, characters produced -- and this
    file is a few hundred kilobytes, has one page, and returns no text at all,
    because what it inflates to is a text-drawing operator repeated until the
    address space runs out. It passed all three and cost 145 MB of resident memory
    per call; ``infra/documents/sandbox.py`` is what refuses it, and this is what
    that test needs.

    ``zlib`` at level 9 turns the repeated unit into roughly a thousandth of its
    size, so the file on disk stays small enough to be built in milliseconds.
    """
    # A real drawing operator rather than filler: it forces the reader to walk the
    # inflated stream token by token, which is where the CPU goes.
    unit = b"BT /F1 1 Tf 1 0 0 1 0 0 Tm (" + b"A" * 3000 + b") Tj ET\n"
    payload = unit * max(1, inflated_bytes // len(unit))
    compressed = zlib.compress(payload, 9)

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_number = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    content_numbers = [
        add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream"
            % (len(compressed), compressed)
        )
        for _ in range(pages)
    ]

    pages_number = len(objects) + pages + 1
    page_numbers = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_number, font_number, content_number)
        )
        for content_number in content_numbers
    ]

    kids = b" ".join(b"%d 0 R" % number for number in page_numbers)
    written = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages))
    assert written == pages_number, "page tree object number was mispredicted"
    catalog_number = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_number)
    return _assemble(objects, catalog_number)
