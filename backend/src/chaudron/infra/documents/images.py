"""Deciding what an uploaded photo really is, without ever decoding it.

This module reads the first few bytes of an upload and answers one question:
which media type may be forwarded to a vision model? It never decodes, never
re-encodes, never resizes and never renders. That is the whole design, and it is
the same posture as the PDF reader beside it: an image decoder is a large C
surface fed by a stranger, and the receipt import does not need one.

Not resizing has a cost worth naming. ``docs/technical-notes-ingestion.md``
section 3.1 shows a receipt-shaped photo is *cheaper* than a square one -- the
long-edge cap bites before the token cap, so 1000x3000 costs 2852 visual tokens
against 3888 for 1500x2000 -- and every provider downscales on its side anyway.
What the household pays for is therefore the provider's resize, not ours, and the
byte ceiling is what bounds it. A client-side downscale in the PWA would be the
right place to shrink it further; a server-side decode would not.

Reading the header is not the same as trusting the first three bytes
--------------------------------------------------------------------

The signature check alone was a polyglot away from useless: a PDF with
``\xff\xd8\xff\xe0`` glued on the front answered ``image/jpeg`` and was forwarded
to a model provider, which is a document of the attacker's choosing arriving at a
*third party's* parser under a media type it contradicts. Nothing decoded it
here, so this module kept its own promise -- and exported the risk, which is
worse than owning it.

So each format is now checked for the structure its container requires, and
nothing more than that: the JPEG segment chain up to the start of scan, the PNG
``IHDR`` and its CRC, the RIFF length of a WebP, the trailer of a GIF. All of it
is header arithmetic -- offsets, lengths, one CRC over thirteen bytes -- and none
of it decodes a pixel. A prefixed polyglot fails at the first length field,
because the bytes after the marker are not a segment.

The checks are deliberately *structural* rather than strict. They ask "is this
container what it says it is", never "is this file well formed": trailing bytes
after a JPEG's end marker are accepted, because cameras and photo pipelines write
them and refusing would break real photographs to close nothing.

HEIC is refused rather than converted, and that is a decision
------------------------------------------------------------

An iPhone's own format. No provider in ADR-0005 accepts it, so forwarding the
bytes would fail deep inside an adapter with a message about an unsupported
media type -- the opposite of the taxonomy this project holds itself to.
Converting instead would mean adding a decoder for the one format most likely to
be an attacker's choice, in a process that holds every household's encrypted API
keys. So it is refused *by name*, with a sentence that tells the user what to do
(share the photo, or turn on "Most Compatible"), which costs one tap and no
attack surface.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from typing import Final

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError

__all__ = [
    "IMAGE_MEDIA_TYPES",
    "IMAGE_SUFFIXES",
    "sniff_image",
]

#: What a browser sends for the formats every ADR-0005 provider accepts.
IMAGE_MEDIA_TYPES: Final = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
)

#: File extensions that route to this reader when the browser sent no useful
#: media type, which is common on mobile.
IMAGE_SUFFIXES: Final = frozenset({"jpg", "jpeg", "png", "webp", "gif", "heic", "heif"})

#: Magic bytes, and the media type they *actually* mean. The upload's declared
#: type is never believed: a file named ``.png`` and typed ``image/png`` is still
#: whatever its first bytes say, and that is the value forwarded to the provider.
_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: ISO-BMFF brands. The container is shared by HEIC and WebP-adjacent formats, so
#: the brand at offset 8 is what separates them.
_HEIF_BRANDS: Final = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim"})

_RIFF: Final = b"RIFF"
_WEBP: Final = b"WEBP"
_FTYP: Final = b"ftyp"

#: How many bytes have to be present before any of the checks above can conclude.
_MIN_HEADER: Final = 16

#: The largest edge a photograph of a receipt may claim. Not a decode budget --
#: nothing here decodes -- but a header declaring 400 000 pixels of width is a
#: fuzzed file rather than a photograph, and it is cheaper to refuse it than to
#: let a provider find out.
_MAX_EDGE_PIXELS: Final = 65_535

#: JPEG markers that carry no payload, so no length field follows them.
_JPEG_STANDALONE: Final = frozenset({0xD8, 0x01, *range(0xD0, 0xD8)})

#: Start-of-frame markers. Every JPEG has exactly one, and it holds the
#: dimensions. ``0xC4`` (Huffman tables), ``0xC8`` and ``0xCC`` are *not* frames
#: despite sitting in the range.
_JPEG_FRAME_MARKERS: Final = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})

_JPEG_START_OF_SCAN: Final = 0xDA
_JPEG_END_OF_IMAGE: Final = b"\xff\xd9"

_PNG_IHDR: Final = b"IHDR"
_PNG_IEND: Final = b"IEND"
_PNG_HEADER_LENGTH: Final = 13

_GIF_TRAILER: Final = 0x3B

#: The chunk that has to follow ``WEBP``. Anything else is not one of the three
#: WebP forms, whatever the RIFF header claims.
_WEBP_CHUNKS: Final = frozenset({b"VP8 ", b"VP8L", b"VP8X"})

_NOT_A_PHOTOGRAPH: Final = (
    "this file starts like a photograph but is not one: its header does not hold "
    "together. Share the photo again from the Photos or Gallery app rather than "
    "renaming a file."
)


def sniff_image(data: bytes, *, max_bytes: int) -> str:
    """Return the media type of ``data``, or raise.

    ``max_bytes`` is a hard bound answered ``413`` by the router, for the same
    reason as everywhere else in this package: a truncated photo is a receipt
    with its bottom half missing, and reading one silently would put half a shop
    in the budget.
    """
    if len(data) > max_bytes:
        raise DocumentTooLargeError(
            measure="bytes",
            limit=max_bytes,
            detail=f"This image is larger than the {max_bytes} byte limit of this endpoint.",
        )
    if len(data) < _MIN_HEADER:
        raise DocumentUnreadableError("this file is too short to be a photograph")

    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            _CHECKS[media_type](data)
            return media_type

    if data[0:4] == _RIFF and data[8:12] == _WEBP:
        _check_webp(data)
        return "image/webp"

    if data[4:8] == _FTYP:
        if data[8:12] in _HEIF_BRANDS:
            raise DocumentUnreadableError(
                "this photograph is in Apple's HEIC format, which no model provider reads. "
                "Share it from the Photos app, or set Camera > Formats to 'Most Compatible' "
                "and take it again."
            )
        raise DocumentUnreadableError("this file is a video container, not a photograph")

    raise DocumentUnreadableError(
        "this file is not a JPEG, PNG, WebP or GIF photograph, whatever its name says"
    )


# --------------------------------------------------------------------------- #
# Structure, not content
#
# Each function below answers one question: does this file's *container* hold
# together as the format its signature claims? None of them decodes anything, and
# none of them is a validator -- a file these accept can still be a corrupt
# photograph, and that is the provider's problem rather than this module's. What
# they close is the polyglot: bytes chosen to satisfy a signature check and then
# be something else entirely to whoever reads them next.
# --------------------------------------------------------------------------- #


def _unreadable() -> DocumentUnreadableError:
    return DocumentUnreadableError(_NOT_A_PHOTOGRAPH)


def _check_dimensions(width: int, height: int) -> None:
    if not 0 < width <= _MAX_EDGE_PIXELS or not 0 < height <= _MAX_EDGE_PIXELS:
        raise _unreadable()


def _check_jpeg(data: bytes) -> None:
    """Walk the segment chain to the start of scan, and read the frame on the way.

    This is the check the polyglot fails, and it fails immediately: a PDF carrying
    a JPEG prefix has ``%PDF`` where the first segment's length belongs, so the
    walk lands outside any marker on its very first step. A real JPEG is a chain
    of ``0xFF <marker> <big-endian length>`` records, and the walk simply follows
    it.
    """
    offset = 2
    frame_seen = False
    while offset + 1 < len(data):
        if data[offset] != 0xFF:
            raise _unreadable()
        # Any number of 0xFF bytes may pad the gap before a marker.
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise _unreadable()
        marker = data[offset]
        offset += 1
        if marker in _JPEG_STANDALONE:
            continue
        if offset + 2 > len(data):
            raise _unreadable()
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise _unreadable()
        if marker in _JPEG_FRAME_MARKERS:
            if length < 7:
                raise _unreadable()
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            _check_dimensions(width, height)
            frame_seen = True
        if marker == _JPEG_START_OF_SCAN:
            # Entropy-coded data follows, which is not a segment chain. The end
            # marker is looked for rather than walked to: inside that data every
            # 0xFF is byte-stuffed, so 0xFFD9 cannot occur by accident.
            if not frame_seen or _JPEG_END_OF_IMAGE not in data[offset + length :]:
                raise _unreadable()
            return
        offset += length
    raise _unreadable()


def _check_png(data: bytes) -> None:
    """``IHDR`` first, its CRC correct, and ``IEND`` at the end.

    The CRC is what makes this decisive rather than indicative: thirteen bytes of
    header and four of checksum that a file has to have got right on purpose.
    """
    if int.from_bytes(data[8:12], "big") != _PNG_HEADER_LENGTH or data[12:16] != _PNG_IHDR:
        raise _unreadable()
    if len(data) < 33:
        raise _unreadable()
    declared = int.from_bytes(data[29:33], "big")
    if zlib.crc32(data[12:29]) != declared:
        raise _unreadable()
    _check_dimensions(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if _PNG_IEND not in data[-12:]:
        raise _unreadable()


def _check_gif(data: bytes) -> None:
    """The logical screen descriptor, and the trailer byte that ends every GIF."""
    _check_dimensions(int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
    if data[-1] != _GIF_TRAILER:
        raise _unreadable()


def _check_webp(data: bytes) -> None:
    """The RIFF length, which a prefixed file cannot get right by accident.

    ``RIFF`` declares the size of everything after its own eight bytes. A trailing
    payload makes the file longer than it says it is, and that is the whole check.
    """
    declared = int.from_bytes(data[4:8], "little")
    if declared + 8 != len(data) or data[12:16] not in _WEBP_CHUNKS:
        raise _unreadable()


#: Which check belongs to which signature. A dictionary rather than a chain of
#: ``if``s so that adding a media type to ``_SIGNATURES`` without a structural
#: check is a ``KeyError`` at the first upload rather than a silent gap.
_CHECKS: Final[dict[str, Callable[[bytes], None]]] = {
    "image/jpeg": _check_jpeg,
    "image/png": _check_png,
    "image/gif": _check_gif,
}
