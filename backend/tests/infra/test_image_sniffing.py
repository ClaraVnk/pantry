"""What an uploaded photograph is allowed to be, decided without decoding it.

The module under test never turns bytes into pixels, so these tests are about the
*header* and about the refusals. The refusals are the interesting half: each one
is a case where the alternative is a decoder, and a decoder fed by a stranger is
the surface ``infra/documents`` exists to not have.
"""

from __future__ import annotations

import pytest

from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError
from chaudron.infra.documents import sniff_image
from tests.support import images

_PAD = b"\x00" * 64

JPEG = images.jpeg()
PNG = images.png()
GIF = images.gif()
WEBP = images.webp()
HEIC = b"\x00\x00\x00\x18ftypheic" + _PAD
MP4 = b"\x00\x00\x00\x18ftypisom" + _PAD

#: A PDF wearing a JPEG's first four bytes. Accepted before the structure check
#: existed, and forwarded to a model provider under a media type its content
#: contradicts -- this module decoded nothing, and exported the risk instead.
POLYGLOT = b"\xff\xd8\xff\xe0" + b"%PDF-1.7\n" + b"\x00" * 512


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
    ],
)
def test_the_bytes_decide_the_media_type(data: bytes, expected: str) -> None:
    assert sniff_image(data, max_bytes=1024) == expected


def test_the_declared_name_is_irrelevant() -> None:
    """A file called ``.png`` holding a JPEG is forwarded as a JPEG.

    Sending it with the type its name claimed is how a provider either rejects the
    call or, worse, mis-decodes it -- and the household pays for the call either
    way.
    """
    assert sniff_image(JPEG, max_bytes=1024) == "image/jpeg"


def test_heic_is_refused_by_name_with_something_to_do_about_it() -> None:
    """The iPhone default, and the one refusal a user will actually meet.

    Converting would mean adding a decoder for the format most likely to be an
    attacker's choice, in the process that holds every household's encrypted API
    keys. Refusing costs one tap; the message says which tap.
    """
    with pytest.raises(DocumentUnreadableError) as raised:
        sniff_image(HEIC, max_bytes=1024)
    assert "HEIC" in str(raised.value)
    assert "Most Compatible" in str(raised.value)


def test_a_video_container_is_refused_as_one() -> None:
    with pytest.raises(DocumentUnreadableError, match="video"):
        sniff_image(MP4, max_bytes=1024)


def test_something_that_is_not_an_image_at_all_is_refused() -> None:
    with pytest.raises(DocumentUnreadableError):
        sniff_image(b"%PDF-1.7\n" + _PAD, max_bytes=1024)


def test_a_file_too_short_to_have_a_header_is_refused_before_anything_reads_it() -> None:
    with pytest.raises(DocumentUnreadableError):
        sniff_image(b"\xff\xd8", max_bytes=1024)


def test_the_byte_ceiling_is_a_hard_bound_carrying_what_it_is() -> None:
    """413 with ``measure`` and ``limit``, never a truncated read.

    A photograph cut short is a receipt with its bottom half missing, and reading
    one silently would put half a shop in the budget.
    """
    with pytest.raises(DocumentTooLargeError) as raised:
        sniff_image(JPEG + b"\x00" * 4096, max_bytes=128)
    assert raised.value.measure == "bytes"
    assert raised.value.limit == 128


# --------------------------------------------------------------------------- #
# Structure, and the polyglot it closes
# --------------------------------------------------------------------------- #


def test_a_pdf_wearing_a_jpeg_header_is_not_a_photograph() -> None:
    """The finding. Three bytes of magic decided, and a document went to a provider.

    Nothing here decoded it, which is why this module could keep its own promise
    and still be the reason an attacker-chosen file reached a third party's parser
    under a media type contradicting its content. Exporting a risk is not
    mitigating it.
    """
    with pytest.raises(DocumentUnreadableError, match="does not hold together"):
        sniff_image(POLYGLOT, max_bytes=4096)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"GIF89a" + b"%PDF-1.7\n" + b"\x00" * 64, id="gif-prefixed-pdf"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"%PDF-1.7\n" + b"\x00" * 64, id="png-prefixed-pdf"),
        pytest.param(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64, id="webp-lying-about-length"),
    ],
)
def test_every_accepted_format_is_checked_for_the_container_it_claims(data: bytes) -> None:
    """One format left on signatures alone is the one an attacker would pick."""
    with pytest.raises(DocumentUnreadableError):
        sniff_image(data, max_bytes=4096)


def test_bytes_after_a_jpegs_end_marker_are_accepted() -> None:
    """Cameras write them. A check strict enough to refuse this closes nothing and
    breaks real photographs, which is the failure mode that gets a check removed."""
    assert sniff_image(images.jpeg(trailer=b"\x00" * 128), max_bytes=4096) == "image/jpeg"


def test_a_header_claiming_an_impossible_size_is_refused() -> None:
    """Not a decode budget -- nothing decodes -- but a zero-pixel edge is a fuzzed
    file, and a provider is a worse place to discover that than here."""
    with pytest.raises(DocumentUnreadableError):
        sniff_image(images.jpeg(width=0, height=12), max_bytes=4096)
