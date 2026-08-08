"""Receipt import: one endpoint that proposes, one that writes, one that gives up.

Contract 6ter and 7. ``POST /import`` reads an upload and stores a **proposal**;
``POST /{id}/confirm`` is the only call that touches the inventory; ``DELETE
/{id}`` throws the proposal away. The third one is not a convenience: a parsed
receipt counts towards the budget from the moment it is stored (contract 6ter
counts a receipt as soon as it carries a total and a currency, deliberately
*before* its lines are accepted), so an import abandoned mid-review would
otherwise overstate what a household spent, permanently and with nothing on any
screen pointing at it.

This module owns its own request and response models, the way ``routers/budget.py``
does, and its own error mapping, the way ``routers/shopping.py`` does. The second
one matters more here than anywhere: what fails is a *document a stranger
supplied*, a format parser's message quotes byte offsets out of it, and a
provider SDK's message can contain the household's API key (SEC-003). Every
sentence a client receives is written in this file; the libraries' go to the log.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from chaudron.api.deps import (
    HouseholdDep,
    MemberDep,
    PrincipalDep,
    ReceiptImportServiceDep,
    enforce_receipt_limits,
    get_settings_dep,
    receipt_bounds,
)
from chaudron.api.errors import ProblemError
from chaudron.domain.llm_ports import (
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.domain.ports import ProductNotFoundError
from chaudron.domain.receipts import (
    ConfirmReceiptLine,
    ReceiptAlreadyConfirmedError,
    ReceiptImportUnavailableError,
    ReceiptNotFoundError,
    ReceiptProposal,
    SameReceiptTwiceError,
)
from chaudron.domain.shopping import DocumentTooLargeError, DocumentUnreadableError
from chaudron.infra.documents import IMAGE_MEDIA_TYPES, IMAGE_SUFFIXES, PDF_MEDIA_TYPES
from chaudron.services.receipts import MAX_CONFIRM_LINES, ReceiptBounds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/receipts", tags=["receipts"])

#: The one receipt path whose body is a file. ``api/main.py`` reads this to give
#: the route its own ceiling in the request-size middleware, which compares full
#: paths -- so this value has to stay equal to the prefix plus the route, and a
#: test asserts it does.
RECEIPT_IMPORT_PATH: Final = "/v1/receipts/import"

#: Receipts one listing may return. A household reviewing its shop has one or two
#: pending; a page of twenty is a backlog, not a screen.
MAX_PENDING_LISTED: Final = 20

_MONEY_QUANTUM: Final = Decimal("0.01")
_QUANTITY_QUANTUM: Final = Decimal("0.001")

#: File extensions read as a PDF when the browser gave no useful media type,
#: which is common on mobile.
_PDF_SUFFIX: Final = "pdf"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored -- as everywhere else at the boundary."""

    model_config = ConfigDict(extra="forbid")


class ReceiptLineOut(BaseModel):
    """One line as it was read. Nothing here is in stock yet.

    ``raw_label`` and ``label`` are both sent, and the interface shows the raw one
    whenever they differ. The expansion is a *reading* of an abbreviation, and a
    reviewer who cannot see what was printed cannot tell a good reading from a
    confident wrong one.
    """

    id: uuid.UUID
    line_no: int
    raw_label: str
    label: str
    #: A reading the lexicon produced but declined to trust. ``null`` when there
    #: is none -- which is the common case. Offered as a one-tap correction on the
    #: review screen, never applied.
    suggested_label: str | None
    quantity: Decimal | None
    unit: str | None
    unit_price: Decimal | None
    total_price: Decimal | None
    matched_product_id: uuid.UUID | None
    match_status: Literal["pending", "suggested", "confirmed", "rejected", "ignored"]
    #: Only ever a claim about how confidently the *abbreviation* was expanded.
    #: No provider in ADR-0005 exposes token probabilities, and this field never
    #: pretends otherwise.
    match_confidence: Decimal | None

    @field_serializer("quantity")
    def _serialise_quantity(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(_QUANTITY_QUANTUM))

    @field_serializer("unit_price", "total_price")
    def _serialise_money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(_MONEY_QUANTUM))

    @field_serializer("match_confidence")
    def _serialise_confidence(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)


class ReceiptOut(BaseModel):
    """A stored, unconfirmed reading.

    ``total_amount`` and ``line_sum`` are both present and are **never**
    reconciled. ``docs/technical-notes-ingestion.md`` section 3.4: models fabricate
    or alter lines to force their sum onto the printed total, so a gap between
    these two numbers is the best signal available that a line was invented.
    Closing it in the response would delete the signal to make a screen look
    tidy; ``line_sum_delta`` exists so the interface can show it instead.
    """

    id: uuid.UUID
    source: Literal["photo", "pdf"]
    read_by: Literal["text", "model"]
    status: Literal["parsed", "confirmed", "failed"]
    merchant: str | None
    retailer: str | None
    purchased_at: datetime | None
    total_amount: Decimal | None
    currency: str | None
    lines: list[ReceiptLineOut]
    line_sum: Decimal | None
    line_sum_delta: Decimal | None
    truncated: bool
    degradation_notice: str | None

    @field_serializer("total_amount", "line_sum", "line_sum_delta")
    def _serialise_money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(_MONEY_QUANTUM))

    @field_serializer("purchased_at")
    def _as_zulu(self, value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ReceiptSummaryOut(BaseModel):
    id: uuid.UUID
    source: Literal["photo", "pdf"]
    status: Literal["parsed", "confirmed", "failed"]
    merchant: str | None
    purchased_at: datetime | None
    total_amount: Decimal | None
    currency: str | None
    line_count: int
    created_at: datetime

    @field_serializer("total_amount")
    def _serialise_money(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value.quantize(_MONEY_QUANTUM))

    @field_serializer("purchased_at", "created_at")
    def _as_zulu(self, value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PendingReceiptsOut(BaseModel):
    receipts: list[ReceiptSummaryOut]


class ConfirmQuantityIn(StrictModel):
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))]
    unit: Annotated[str, Field(min_length=1, max_length=16)]


class ConfirmReceiptLineIn(StrictModel):
    """One line the reviewer kept, as they left it.

    Exactly one of ``product_id`` and ``label`` carries the target. Sending both
    would store a catalogue name and a free text that disagree, and the screen
    would then show a name nobody typed.
    """

    id: uuid.UUID
    product_id: uuid.UUID | None = None
    label: Annotated[str | None, Field(default=None, max_length=200)] = None
    quantity: ConfirmQuantityIn | None = None

    @model_validator(mode="after")
    def _one_target_only(self) -> ConfirmReceiptLineIn:
        if self.product_id is not None and (self.label or "").strip():
            raise ValueError("send product_id or label, never both")
        return self


class ConfirmReceiptIn(StrictModel):
    """The reviewed lines. This body is the only thing that writes stock.

    A line **absent** from this list is a line the reviewer removed: it is marked
    ``ignored`` and keeps its price, because "do not put this in my cupboard" is
    not "this was not on the receipt".
    """

    lines: Annotated[list[ConfirmReceiptLineIn], Field(max_length=MAX_CONFIRM_LINES)]
    #: Where the stock goes. Optional: a household that has not set up storage
    #: locations still gets its shopping into the inventory.
    location_id: uuid.UUID | None = None


class ConfirmReceiptOut(BaseModel):
    receipt_id: uuid.UUID
    created_lot_count: int
    ignored_line_count: int
    created_product_count: int


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post(
    "/import",
    response_model=ReceiptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Read a till receipt photograph or a drive order recap",
    dependencies=[Depends(enforce_receipt_limits)],
)
async def import_receipt(
    request: Request,
    household_id: MemberDep,
    principal: PrincipalDep,
    service: ReceiptImportServiceDep,
    file: Annotated[UploadFile, File()],
) -> ReceiptOut:
    """Store a proposal. **No stock is written and no image is kept.**

    ``201`` rather than ``200``: a row exists afterwards, at ``parsed``, and it
    has a URL. That row already counts towards the budget (contract 6ter), which
    is why ``DELETE`` on the same URL exists.

    The two entrances are told apart by the media type, with the file extension
    as a fallback -- a browser sends ``application/octet-stream`` often enough
    that trusting the header alone would refuse real uploads. Neither is
    *believed*: the PDF reader checks its magic bytes and the image reader
    returns what the bytes actually are.
    """
    bounds = receipt_bounds(get_settings_dep(request))
    data, reads_as_pdf, media_type = await _read_upload(file, bounds)

    try:
        proposal = await service.propose(
            household_id,
            data=data,
            media_type=media_type,
            reads_as_pdf=reads_as_pdf,
            uploaded_by_user_id=principal.user_id,
        )
    except SameReceiptTwiceError as error:
        raise ProblemError(
            slug="receipt-already-imported",
            title="Receipt already imported",
            status=409,
            detail=(
                "This household has already imported this exact file. Open the receipt "
                "it created rather than importing it again."
            ),
            receipt_id=str(error.receipt_id),
        ) from None
    except ReceiptImportUnavailableError as error:
        raise _unavailable(error) from None
    except DocumentTooLargeError as error:
        raise _too_large(measure=error.measure, limit=error.limit, detail=error.detail) from None
    except DocumentUnreadableError as error:
        raise ProblemError(
            slug="document-unreadable",
            title="Document unreadable",
            status=422,
            # ``str(error)`` is a sentence written in ``infra/documents`` or in
            # ``services/receipts``. No library message reaches it, by
            # construction of those modules.
            detail=str(error),
        ) from None
    except ProviderQuotaExceeded:
        raise ProblemError(
            slug="provider-quota-exceeded",
            title="Model provider quota exceeded",
            status=429,
            detail=(
                "The model provider configured for this household refused the call for "
                "quota or rate reasons. This is the household's own account; retry later."
            ),
        ) from None
    except ProviderResponseInvalid:
        raise ProblemError(
            slug="receipt-unreadable",
            title="Receipt could not be read",
            status=422,
            detail=(
                "The model answered, but not with something that could be read as a "
                "receipt. Photograph the receipt again, flat and straight on."
            ),
        ) from None
    except ProviderUnavailable:
        raise ProblemError(
            slug="provider-unavailable",
            title="Model provider unavailable",
            status=502,
            detail=(
                "The model provider configured for this household could not be reached. "
                "Nothing was stored; try again."
            ),
        ) from None

    return _to_out(proposal)


@router.get(
    "",
    response_model=PendingReceiptsOut,
    summary="Receipts read but not yet accepted into stock",
)
async def list_pending_receipts(
    household_id: HouseholdDep, service: ReceiptImportServiceDep
) -> PendingReceiptsOut:
    """What is waiting for a review, newest first.

    The screen this feeds is what keeps an abandoned import findable. A parsed
    receipt is already money in the budget; without a list, the only way to reach
    one after closing the tab would be to know its identifier.
    """
    pending = await service.list_pending(household_id, limit=MAX_PENDING_LISTED)
    return PendingReceiptsOut(
        receipts=[
            ReceiptSummaryOut(
                id=row.id,
                source=row.source,
                status=row.status,
                merchant=row.merchant,
                purchased_at=row.purchased_at,
                total_amount=row.total_amount,
                currency=row.currency,
                line_count=row.line_count,
                created_at=row.created_at,
            )
            for row in pending
        ]
    )


@router.get("/{receipt_id}", response_model=ReceiptOut, summary="One stored reading")
async def get_receipt(
    receipt_id: uuid.UUID, household_id: HouseholdDep, service: ReceiptImportServiceDep
) -> ReceiptOut:
    try:
        return _to_out(await service.get(household_id, receipt_id))
    except ReceiptNotFoundError:
        raise _not_found() from None


@router.post(
    "/{receipt_id}/confirm",
    response_model=ConfirmReceiptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Write the reviewed lines into stock",
)
async def confirm_receipt(
    receipt_id: uuid.UUID,
    household_id: MemberDep,
    service: ReceiptImportServiceDep,
    payload: ConfirmReceiptIn,
) -> ConfirmReceiptOut:
    """The only call of this feature that touches the inventory.

    Every value in the body is re-validated: the unit against the ``unit`` table,
    the product against what this household may see, the label through the same
    neutralisation as any untrusted text. What the client is authoritative about
    is which lines a human kept -- and only that.
    """
    try:
        result = await service.confirm(
            household_id,
            receipt_id,
            lines=[
                ConfirmReceiptLine(
                    id=line.id,
                    product_id=line.product_id,
                    label=line.label,
                    amount=None if line.quantity is None else line.quantity.amount,
                    unit_code=None if line.quantity is None else line.quantity.unit,
                )
                for line in payload.lines
            ],
            location_id=payload.location_id,
        )
    except ReceiptNotFoundError:
        raise _not_found() from None
    except ReceiptAlreadyConfirmedError:
        raise ProblemError(
            slug="receipt-already-confirmed",
            title="Receipt already confirmed",
            status=409,
            detail=(
                "The lines of this receipt are already in stock. Confirming again would "
                "double the inventory."
            ),
        ) from None
    except ProductNotFoundError:
        raise ProblemError(
            slug="product-not-found",
            title="Product not found",
            status=404,
            detail="A line names a product this household cannot see.",
        ) from None

    return ConfirmReceiptOut(
        receipt_id=result.receipt_id,
        created_lot_count=result.created_lot_count,
        ignored_line_count=result.ignored_line_count,
        created_product_count=result.created_product_count,
    )


@router.delete(
    "/{receipt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard a reading that will not be used",
)
async def discard_receipt(
    receipt_id: uuid.UUID, household_id: MemberDep, service: ReceiptImportServiceDep
) -> Response:
    """Remove a proposal, and with it the spend it was counting.

    Refused once confirmed: those lines are lots in a cupboard, and deleting the
    receipt would leave them with a dangling provenance while the money they were
    bought with silently left the budget.
    """
    try:
        await service.discard(household_id, receipt_id)
    except ReceiptNotFoundError:
        raise _not_found() from None
    except ReceiptAlreadyConfirmedError:
        raise ProblemError(
            slug="receipt-already-confirmed",
            title="Receipt already confirmed",
            status=409,
            detail=(
                "This receipt is already in stock. Remove the stock it created if it was "
                "a mistake; the receipt itself is the record of what was spent."
            ),
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Reading the request
# --------------------------------------------------------------------------- #


async def _read_upload(file: UploadFile, bounds: ReceiptBounds) -> tuple[bytes, bool, str]:
    """Read the upload into memory, then forget the upload.

    The ``finally`` is load-bearing rather than tidy. Below 1 MiB the part is a
    spooled buffer and closing it releases it; above it Starlette has written a
    temporary file, and closing is what releases that. A photograph of a till
    receipt is routinely above that threshold, so this is the ordinary path here
    rather than the exceptional one.

    What that file is, precisely, because the difference matters: Starlette
    creates it unnamed -- it is unlinked at creation, so no other process can open
    it by path -- in ``TMPDIR``, which on a default deployment is ``/tmp`` on
    persistent storage rather than a tmpfs. The bytes of a receipt therefore reach
    a disk for the length of one request even though nothing keeps them. The
    module docstring of ``services/receipts.py`` says so rather than claiming the
    image is only ever in memory, and ``TMPDIR`` on a tmpfs is what would make the
    stronger claim true.

    ``read(limit + 1)``: one byte past the bound is enough to know the file is too
    big, and it is the difference between refusing an oversized upload and
    materialising it first.
    """
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = file.filename or ""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        data = await file.read(bounds.max_bytes + 1)
    finally:
        await file.close()

    if len(data) > bounds.max_bytes:
        raise _too_large(
            measure="bytes",
            limit=bounds.max_bytes,
            detail=f"This file is larger than the {bounds.max_bytes} byte limit of this endpoint.",
        )

    if media_type in PDF_MEDIA_TYPES or suffix == _PDF_SUFFIX:
        return data, True, "application/pdf"
    if media_type in IMAGE_MEDIA_TYPES or suffix in IMAGE_SUFFIXES:
        return data, False, media_type
    raise _unsupported(media_type)


def _too_large(*, measure: str, limit: int, detail: str) -> ProblemError:
    return ProblemError(
        slug="document-too-large",
        title="Document too large",
        status=413,
        detail=detail,
        # Quoted so a client can act rather than bisect. ``measure`` says which of
        # the bounds was hit -- bytes, pages or characters -- because "too large"
        # about a two-page PDF is a bug report nobody can answer.
        measure=measure,
        limit=limit,
    )


def _unsupported(media_type: str) -> ProblemError:
    return ProblemError(
        slug="unsupported-document",
        title="Unsupported document",
        status=415,
        detail=(
            "This endpoint reads a photograph (JPEG, PNG, WebP) or the PDF of a drive "
            f"order recap. It cannot read {media_type or 'this media type'}."
        ),
    )


def _not_found() -> ProblemError:
    """One answer for "does not exist" and "belongs to somebody else".

    They are the same branch in the service, not merely reported alike, so this
    endpoint cannot be used to discover that a receipt exists.
    """
    return ProblemError(
        slug="receipt-not-found",
        title="Receipt not found",
        status=404,
        detail="No receipt with this identifier belongs to this household.",
    )


def _unavailable(error: ReceiptImportUnavailableError) -> ProblemError:
    """The ``unavailable`` case of ADR-0005, carried to the interface intact.

    ``409`` rather than ``501`` or ``503``: nothing failed and nothing is broken.
    The household's configuration is in a state that cannot do this, and the way
    out is a screen they own. ``reason`` and ``remedy`` are in French because they
    are rendered, not logged.
    """
    return ProblemError(
        slug="receipt-import-unavailable",
        title="Receipt import unavailable",
        status=409,
        detail=error.reason,
        remedy=error.remedy,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _to_out(proposal: ReceiptProposal) -> ReceiptOut:
    return ReceiptOut(
        id=proposal.id,
        source=proposal.source,
        read_by=proposal.read_by,
        status=proposal.status,
        merchant=proposal.merchant,
        retailer=proposal.retailer,
        purchased_at=proposal.purchased_at,
        total_amount=proposal.total_amount,
        currency=proposal.currency,
        lines=[
            ReceiptLineOut(
                id=line.id,
                line_no=line.line_no,
                raw_label=line.raw_label,
                label=line.label,
                suggested_label=line.suggested_label,
                quantity=line.quantity,
                unit=line.unit_code,
                unit_price=line.unit_price,
                total_price=line.total_price,
                matched_product_id=line.matched_product_id,
                match_status=line.match_status,
                match_confidence=line.match_confidence,
            )
            for line in proposal.lines
        ],
        line_sum=proposal.line_sum,
        line_sum_delta=proposal.line_sum_delta,
        truncated=proposal.truncated,
        degradation_notice=proposal.degradation_notice,
    )
