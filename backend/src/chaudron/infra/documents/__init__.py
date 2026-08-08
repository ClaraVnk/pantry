"""Turning an uploaded document into text, and refusing to do anything else.

Three readers live here, one per accepted family of media types, and they share a
shape: bytes in, a bounded answer out, or an exception. None renders, none
follows a reference, none writes to disk, and none keeps the bytes it was given.

The image reader is the odd one and says so in its own docstring: it returns a
*media type* rather than text, because the only thing that can read a photograph
here is a vision model, and turning bytes into pixels is precisely the operation
this package refuses to perform.

The bounds are arguments rather than module constants because they are
configuration (``CHAUDRON_SHOPPING_IMPORT_*``, ``CHAUDRON_RECEIPT_IMPORT_*``),
and because a reader that reads its own limits from the environment cannot be
tested against a small one.

One reader is not called directly. ``extract_pdf_text`` is CPU-bound, and against
a hostile document it is *unboundedly* CPU-bound and memory-bound -- so every
path fed by an upload goes through :func:`extract_pdf_text_isolated`, which runs
it in a worker process under ``RLIMIT_AS`` and ``RLIMIT_CPU``. The synchronous
form stays exported because that is what the worker calls, and because the tests
of the parser itself have no reason to pay for a process.
"""

from chaudron.infra.documents.images import IMAGE_MEDIA_TYPES, IMAGE_SUFFIXES, sniff_image
from chaudron.infra.documents.pdf import PDF_MEDIA_TYPES, extract_pdf_text
from chaudron.infra.documents.sandbox import (
    SandboxLimits,
    configure_document_sandbox,
    extract_pdf_text_isolated,
    shutdown_document_sandbox,
)
from chaudron.infra.documents.text import TEXT_MEDIA_TYPES, decode_text

__all__ = [
    "IMAGE_MEDIA_TYPES",
    "IMAGE_SUFFIXES",
    "PDF_MEDIA_TYPES",
    "TEXT_MEDIA_TYPES",
    "SandboxLimits",
    "configure_document_sandbox",
    "decode_text",
    "extract_pdf_text",
    "extract_pdf_text_isolated",
    "shutdown_document_sandbox",
    "sniff_image",
]
