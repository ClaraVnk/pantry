"""The smallest byte strings that are genuinely the images they claim to be.

Written by hand rather than produced with an imaging library, for the reason
``tests/support/pdfs.py`` gives about PDFs: a fixture built by the same ecosystem
that reads it tests a round trip rather than a file. These emit container bytes
directly -- markers, chunks, lengths, one CRC -- which is exactly the layer
``infra/documents/images.py`` inspects.

Four bytes of magic used to be enough for a fixture here, because four bytes of
magic used to be enough for the reader. Both changed together: a PDF with a JPEG
prefix passed the old check and was forwarded to a model provider, so the reader
now walks the container and a fixture has to be one.
"""

from __future__ import annotations

import zlib
from typing import Final

__all__ = ["gif", "jpeg", "png", "webp"]

_JFIF_APP0: Final = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


def jpeg(*, width: int = 8, height: int = 12, trailer: bytes = b"") -> bytes:
    """A baseline JPEG: SOI, APP0, a start of frame, a start of scan, EOI.

    ``trailer`` appends bytes after the end marker, which is what a camera's
    thumbnail or a photo pipeline's metadata looks like from here -- accepted on
    purpose, since refusing it would break real photographs.
    """
    frame = (
        b"\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00"
    )
    scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + _JFIF_APP0 + frame + scan + b"\x00" * 8 + b"\xff\xd9" + trailer


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return len(payload).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")


def png(*, width: int = 8, height: int = 12) -> bytes:
    """A PNG whose ``IHDR`` carries a correct CRC, because the reader checks it."""
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 0, 0, 0, 0))  # bit depth, greyscale, deflate, adaptive, no interlace
    )
    pixels = zlib.compress(b"\x00" + b"\x00" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )


def gif(*, width: int = 8, height: int = 12) -> bytes:
    """A GIF89a: logical screen descriptor, one empty block, the trailer byte."""
    return (
        b"GIF89a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\x00\x00\x00"
        + b"\x2c\x00\x00\x00\x00"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"\x00\x02\x02\x44\x01\x00"
        + b"\x3b"
    )


def webp(*, payload: bytes = b"\x00" * 16) -> bytes:
    """A RIFF container whose declared length matches the file, which is the check."""
    body = b"WEBP" + b"VP8 " + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + len(body).to_bytes(4, "little") + body
