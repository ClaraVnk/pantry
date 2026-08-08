"""Bytes that claim to be plain text, decoded without believing the claim.

Simpler than the PDF reader and not trivial: "a ``.txt`` file" is a byte string
plus a guess about its encoding, and the guess is usually wrong exactly where it
matters. A French shopping list exported from a Windows editor is cp1252, and
decoding it as UTF-8 either raises or -- worse, with ``errors="replace"`` --
turns "pâtes" into "p?tes" and hands a mangled label to a catalogue lookup.
"""

from __future__ import annotations

from typing import Final

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError

#: Media types accepted for the file upload. ``text/plain`` is what a browser
#: sends for ``.txt``; ``application/octet-stream`` is what it sends when it has
#: no idea, which is common enough on mobile that refusing it would break real
#: uploads -- the content is validated below regardless of what the type claimed.
TEXT_MEDIA_TYPES: Final = frozenset(
    {"text/plain", "text/markdown", "text/csv", "application/octet-stream"}
)

#: Encodings tried in order. UTF-8 first because a file that decodes as UTF-8 is
#: almost certainly UTF-8 -- the encoding's structure makes an accidental valid
#: decoding of cp1252 bytes very unlikely. cp1252 second because it is what
#: Windows editors still write, and it is a superset of latin-1 over the byte
#: range that differs, so it decodes strictly more real files.
_ENCODINGS: Final = ("utf-8-sig", "cp1252")

#: A byte that no plain-text shopping list contains, and that UTF-16 and most
#: binary formats are full of. Its presence means the upload is not text,
#: whatever the media type said.
_NUL: Final = 0


def decode_text(data: bytes, *, max_chars: int) -> str:
    """Decode ``data`` as plain text, bounded, or raise.

    ``max_chars`` is a hard bound answered ``413``, for the same reason as in the
    PDF reader: a silently shortened shopping list is worse than a refused one.
    """
    if _NUL in data:
        raise DocumentUnreadableError("this file is not plain text")

    for encoding in _ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError, LookupError:
            continue
        break
    else:
        raise DocumentUnreadableError("this file is not text in a recognised encoding")

    if len(text) > max_chars:
        raise DocumentTooLargeError(
            measure="characters",
            limit=max_chars,
            detail=(
                f"This document holds more than {max_chars} characters of text, "
                "which is more than this endpoint reads."
            ),
        )
    return text
