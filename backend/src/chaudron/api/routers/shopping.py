"""Shopping-list import: one endpoint that proposes, one that writes.

The split is the feature (contract 7.2). ``POST /import`` reads a document and
returns lines; ``POST /import/{import_id}/confirm`` takes the reviewed lines and
is the only call that touches the database. The reason is the one that also put a
review step on receipt import: a shopping list that is quietly wrong costs trust
in the whole application, and nothing here is worth that.

This module owns its own error mapping, the way ``routers/recipes.py`` owns the
provider mapping, and for a related reason: what fails here is a *document a
stranger supplied*, and a format parser's own message quotes byte offsets and
object numbers out of it. The sentences below are written here; the library's go
to the log.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from pydantic import ValidationError

from chaudron.api.deps import (
    DeclinedRepurchaseServiceDep,
    MemberDep,
    ShoppingImportServiceDep,
    ShoppingListServiceDep,
    ShoppingReadDep,
    ShoppingWriteDep,
    enforce_shopping_import_limits,
    get_settings_dep,
    import_bounds,
)
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import (
    AddShoppingItemsIn,
    ConfirmImportIn,
    ConfirmImportOut,
    DeclineRepurchaseIn,
    ImportProposalOut,
    ImportTextIn,
    ProposedLineOut,
    ProposedQuantityOut,
    ShoppingItemOut,
    ShoppingListOut,
    ShoppingQuantityOut,
    UpdateShoppingItemIn,
)
from chaudron.domain.ports import UNSET
from chaudron.domain.shopping import (
    ConfirmLine,
    DocumentTooLargeError,
    DocumentUnreadableError,
    ImportProposal,
    ImportSource,
    NewShoppingItem,
    ShoppingItemNotFoundError,
    ShoppingItemView,
    ShoppingListNotFoundError,
    ShoppingListView,
)
from chaudron.infra.documents import (
    PDF_MEDIA_TYPES,
    TEXT_MEDIA_TYPES,
    decode_text,
    extract_pdf_text_isolated,
)
from chaudron.services.shopping_import import ImportBounds
from chaudron.services.shopping_lists import UpdateItemCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/shopping-lists", tags=["shopping-lists"])

#: The one path in the application whose body is a file. ``api/main.py`` reads
#: this to give the route its own ceiling in the request-size middleware, which
#: compares full paths -- so this value has to stay equal to the prefix plus the
#: route, and a test asserts it does.
IMPORT_PATH: Final = "/v1/shopping-lists/import"

#: Slack over the configured file bound, covering the multipart boundary, the
#: part headers and the filename. The middleware is the coarse floor that keeps a
#: 50 MB upload out of memory; the precise refusal, quoting the *file* limit
#: rather than a body limit, happens in the handler.
MULTIPART_OVERHEAD_BYTES: Final = 4096

#: File extensions read as plain text when the browser gave no useful media type,
#: which is common on mobile.
_TEXT_SUFFIXES: Final = frozenset({"txt", "md", "csv"})

#: The one thing that decides which of the two bodies this operation was sent.
#: Deliberately the *header* rather than "did a usable file part arrive": a
#: multipart body whose file part carries an empty filename produces no
#: ``UploadFile``, and falling through to the JSON branch there called
#: ``request.json()`` on a stream the multipart parser had already drained --
#: ``RuntimeError: Stream consumed``, answered ``500``, on a body a browser sends
#: when a file input is left empty.
_MULTIPART_PREFIX: Final = "multipart/"


@router.post(
    "/import",
    response_model=ImportProposalOut,
    summary="Read a shopping list from a PDF, a text file or pasted text",
    # The limits this endpoint went without. Reading a document is CPU the caller
    # does not own, and until this line fifteen concurrent one-megabyte uploads
    # were answered fifteen times while nothing else in the process ran.
    dependencies=[Depends(enforce_shopping_import_limits)],
)
async def import_shopping_list(
    request: Request,
    household_id: MemberDep,
    service: ShoppingImportServiceDep,
    file: Annotated[UploadFile | None, File()] = None,
) -> ImportProposalOut:
    """Return a proposal. **Nothing is written and nothing is stored.**

    Two content types reach one operation (contract 7.2): ``multipart/form-data``
    with a file, or ``application/json`` with ``{"text": "…"}``. Only the file is
    declared as a parameter -- FastAPI cannot describe both bodies on a single
    operation -- and the JSON form is read from the request and validated by
    :class:`ImportTextIn`, so it is checked by the same model either way.

    The **content type** chooses the branch, and the two never cross. A multipart
    body is answered as multipart even when its file part is unusable, because the
    alternative -- trying JSON afterwards -- reads a stream that is already gone.
    """
    bounds = import_bounds(get_settings_dep(request))
    body_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    if body_type.startswith(_MULTIPART_PREFIX):
        if file is None or not file.filename:
            raise ProblemError(
                slug="validation-failed",
                title="Request validation failed",
                status=422,
                detail=(
                    'Send the document as a multipart part named "file", with a filename. '
                    'To import pasted text, send {"text": "…"} as JSON instead.'
                ),
            )
        text, source = await _read_upload(file, bounds)
    else:
        text, source = _validated_text(await _json_text(request)), "text"

    proposal = await service.propose(household_id, text=text, source=source)
    logger.info(
        "shopping_import_proposed",
        extra={
            "import_id": str(proposal.import_id),
            "source": proposal.source,
            "parsed_by": proposal.parsed_by,
            "line_count": len(proposal.lines),
            "unparsed_line_count": proposal.unparsed_line_count,
            # Counts only, never line content. A shopping list says what a
            # household is about to eat, and a log file is the one place a document
            # this endpoint promised not to keep would quietly survive.
        },
    )
    return _to_out(proposal)


@router.post(
    "/import/{import_id}/confirm",
    response_model=ConfirmImportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Write the reviewed lines of an import",
)
async def confirm_shopping_import(
    import_id: uuid.UUID,
    household_id: MemberDep,
    service: ShoppingImportServiceDep,
    payload: ConfirmImportIn,
) -> ConfirmImportOut:
    """The only call of this feature that writes.

    ``import_id`` is a correlation token, not a key: the proposal was never
    stored, so there is nothing to look it up in and this body is authoritative on
    its own. Every value in it is validated again here -- the unit against the
    ``unit`` table, the product against what this household may see, the label
    through the same neutralisation as any untrusted text -- because a client is
    not a source of truth about a household's data.
    """
    try:
        result = await service.confirm(
            household_id,
            lines=[
                ConfirmLine(
                    product_id=line.product_id,
                    label=line.label,
                    amount=None if line.quantity is None else line.quantity.amount,
                    unit_code=None if line.quantity is None else line.quantity.unit,
                )
                for line in payload.lines
            ],
            shopping_list_id=payload.shopping_list_id,
            list_name=payload.list_name,
        )
    except ShoppingListNotFoundError:
        raise ProblemError(
            slug="shopping-list-not-found",
            title="Shopping list not found",
            status=404,
            detail="No shopping list with this identifier belongs to this household.",
        ) from None

    logger.info(
        "shopping_import_written",
        extra={"import_id": str(import_id), "item_count": result.created_item_count},
    )
    return ConfirmImportOut(
        shopping_list_id=result.shopping_list_id,
        shopping_list_name=result.shopping_list_name,
        created_item_count=result.created_item_count,
    )


# --------------------------------------------------------------------------- #
# Reading the request
# --------------------------------------------------------------------------- #


async def _read_upload(file: UploadFile, bounds: ImportBounds) -> tuple[str, ImportSource]:
    """Extract text from an upload, then forget the upload.

    The ``finally`` is load-bearing rather than tidy. At or below the configured
    ceiling the part is a spooled buffer in memory and closing it releases it;
    above it Starlette has written a temporary file to disk, and closing is what
    unlinks it. Either way the document does not outlive the request (contract
    7.3).

    ``read(limit + 1)`` rather than ``read()``: one byte past the bound is enough
    to know the file is too big, and it is the difference between refusing an
    oversized upload and materialising it first.
    """
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    try:
        data = await file.read(bounds.max_bytes + 1)
        if len(data) > bounds.max_bytes:
            raise _too_large(
                measure="bytes",
                limit=bounds.max_bytes,
                detail=(
                    f"This file is larger than the {bounds.max_bytes} byte limit of this endpoint."
                ),
            )
        return await _extract(data, media_type, file.filename or "", bounds)
    finally:
        await file.close()


async def _extract(
    data: bytes, media_type: str, filename: str, bounds: ImportBounds
) -> tuple[str, ImportSource]:
    """Choose a reader, and translate its refusals into problems.

    The media type decides, with the file extension as a fallback: a browser
    sends ``application/octet-stream`` often enough that trusting the header alone
    would refuse real uploads. Neither is *believed* -- the PDF reader checks the
    magic bytes and the text reader checks the content is text.

    The PDF branch is awaited rather than called, and that is the fix for the
    finding that made this endpoint able to stop the process: ``extract_pdf_text``
    is synchronous and CPU-bound, calling it here ran it **on the event loop**, and
    one one-megabyte upload froze ``/healthz`` for 48 seconds. It now runs in a
    worker process that is also bounded in memory and CPU
    (``infra/documents/sandbox.py``); getting off the loop makes the API
    responsive, and only the bound makes the work finite.

    ``decode_text`` stays a direct call. It is a ``bytes.decode`` of at most the
    configured file ceiling, measured in single-digit milliseconds at 1 MiB, with
    no expansion and no parser -- moving it would buy nothing and cost a process.
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    reads_as_pdf = media_type in PDF_MEDIA_TYPES or suffix == "pdf"
    reads_as_text = media_type in TEXT_MEDIA_TYPES or suffix in _TEXT_SUFFIXES

    try:
        if reads_as_pdf:
            return (
                await extract_pdf_text_isolated(
                    data, max_pages=bounds.max_pdf_pages, max_chars=bounds.max_text_chars
                ),
                "pdf",
            )
        if reads_as_text:
            return decode_text(data, max_chars=bounds.max_text_chars), "txt"
    except DocumentTooLargeError as error:
        raise _too_large(measure=error.measure, limit=error.limit, detail=error.detail) from None
    except DocumentUnreadableError as error:
        raise ProblemError(
            slug="document-unreadable",
            title="Document unreadable",
            status=422,
            # ``str(error)`` is a sentence written in ``infra/documents``. No
            # library message reaches it, by construction of those modules.
            detail=str(error),
        ) from None
    raise _unsupported(media_type)


def _validated_text(raw: str) -> str:
    """Pasted text, through the request model, so one shape validates both forms.

    The model is built by hand here rather than declared as a parameter, which
    means FastAPI's own translation of a :class:`ValidationError` into ``422``
    never runs: text past the model's ceiling used to reach the error handler as an
    unhandled exception and be answered ``500``. Caught and re-raised as the same
    problem the branch above already returns, so both entrances refuse a body the
    same way.
    """
    try:
        return ImportTextIn(text=raw).text
    except ValidationError:
        raise ProblemError(
            slug="validation-failed",
            title="Request validation failed",
            status=422,
            # No error detail from pydantic: the value it would quote is the
            # document, and this endpoint does not echo documents back.
            detail=(
                "The pasted text could not be read as a shopping list. It is either "
                "empty or longer than this endpoint accepts."
            ),
        ) from None


async def _json_text(request: Request) -> str:
    try:
        body = await request.json()
    except ValueError:
        raise _unsupported(request.headers.get("content-type", "")) from None
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        raise ProblemError(
            slug="validation-failed",
            title="Request validation failed",
            status=422,
            detail='Send a file in a multipart body, or {"text": "…"} as JSON.',
        )
    text: str = body["text"]
    return text


def _too_large(*, measure: str, limit: int, detail: str) -> ProblemError:
    return ProblemError(
        slug="document-too-large",
        title="Document too large",
        status=413,
        detail=detail,
        # Quoted so a client can act rather than bisect. ``measure`` says which of
        # the three bounds was hit -- bytes, pages or characters -- because "too
        # large" about a two-page PDF is a bug report nobody can answer.
        measure=measure,
        limit=limit,
    )


def _unsupported(media_type: str) -> ProblemError:
    return ProblemError(
        slug="unsupported-document",
        title="Unsupported document",
        status=415,
        detail=(
            "This endpoint reads a PDF, a plain text file, or pasted text. It cannot "
            f"read {media_type or 'this media type'}."
        ),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _to_out(proposal: ImportProposal) -> ImportProposalOut:
    return ImportProposalOut(
        import_id=proposal.import_id,
        source=proposal.source,
        parsed_by=proposal.parsed_by,
        lines=[
            ProposedLineOut(
                raw=line.raw,
                quantity=(
                    None
                    if line.quantity is None
                    else ProposedQuantityOut(
                        amount=line.quantity.amount, unit=line.quantity.unit_code
                    )
                ),
                product_name=line.product_name,
                matched_product_id=line.matched_product_id,
                confidence=line.confidence,
                needs_review=line.needs_review,
                flags=list(line.flags),
            )
            for line in proposal.lines
        ],
        unparsed_line_count=proposal.unparsed_line_count,
        truncated=proposal.truncated,
    )


# --------------------------------------------------------------------------- #
# The list itself (contract 6bis)
# --------------------------------------------------------------------------- #


@router.get(
    "/current",
    response_model=ShoppingListOut,
    summary="The household's current shopping list",
)
async def read_current_list(
    household_id: ShoppingReadDep, service: ShoppingListServiceDep
) -> ShoppingListOut:
    """The list, created on first read when the household has none.

    A ``GET`` that may write is unusual; it is here because a household's *first*
    list is not a decision anyone makes, and the alternative is every client
    handling "no list yet" before it can render anything. The creation is
    idempotent and guarded by a partial unique index.
    """
    return _list_out(await service.current(household_id))


@router.post(
    "/current/items",
    response_model=ShoppingListOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add one or more items to the current list",
)
async def add_current_items(
    household_id: ShoppingWriteDep, service: ShoppingListServiceDep, payload: AddShoppingItemsIn
) -> ShoppingListOut:
    """One call, one transaction, however many items (contract 6bis).

    Returns the whole list: the caller has just changed what it is displaying, and
    a second round trip to find out what it now looks like is how an interface
    goes stale.
    """
    result = await service.add_items(
        household_id,
        [
            NewShoppingItem(
                product_id=item.product_id,
                free_text=item.free_text,
                amount=item.amount,
                unit_code=item.unit,
                source=item.source,
            )
            for item in payload.items
        ],
    )
    return _list_out(result)


@router.patch(
    "/current/items/{item_id}",
    response_model=ShoppingItemOut,
    summary="Tick, untick or correct an item",
)
async def update_current_item(
    item_id: uuid.UUID,
    household_id: ShoppingWriteDep,
    service: ShoppingListServiceDep,
    payload: UpdateShoppingItemIn,
) -> ShoppingItemOut:
    provided = payload.model_fields_set
    command = UpdateItemCommand(
        # ``model_fields_set`` rather than a ``None`` test: "untick this" and "do
        # not touch the tick" are both expressible, and they are different edits.
        checked=payload.checked if "checked" in provided and payload.checked is not None else UNSET,
        quantity=(
            (payload.quantity.amount, payload.quantity.unit)
            if payload.quantity is not None
            else None
        )
        if "quantity" in provided
        else UNSET,
    )
    try:
        return _item_out(await service.update_item(household_id, item_id, command))
    except ShoppingItemNotFoundError:
        raise _item_not_found() from None


@router.delete(
    "/current/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an item from the current list",
)
async def remove_current_item(
    item_id: uuid.UUID, household_id: ShoppingWriteDep, service: ShoppingListServiceDep
) -> Response:
    try:
        await service.remove_item(household_id, item_id)
    except ShoppingItemNotFoundError:
        raise _item_not_found() from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Declining a repurchase proposal (contract 6bis)
# --------------------------------------------------------------------------- #


@router.post(
    "/declined",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Declare that these products should not be proposed again",
)
async def decline_repurchase(
    household_id: MemberDep,
    service: DeclinedRepurchaseServiceDep,
    payload: DeclineRepurchaseIn,
) -> Response:
    """A refusal, per household and per product, with no expiry (contract 6bis).

    Idempotent: the same proposal dismissed on the phone and then on the tablet is
    one refusal, and the second call is a no-op rather than a conflict.
    """
    await service.decline(household_id, payload.product_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/declined/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your mind about a declined product",
)
async def undecline_repurchase(
    product_id: uuid.UUID, household_id: MemberDep, service: DeclinedRepurchaseServiceDep
) -> Response:
    """People change their minds, and a decision taken in three seconds over a bin
    has no business being permanent (contract 6bis).

    ``204`` whether or not there was a refusal to clear: the caller asked for a
    state, and that state now holds.
    """
    await service.undecline(household_id, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _item_not_found() -> ProblemError:
    return ProblemError(
        slug="shopping-item-not-found",
        title="Shopping list item not found",
        status=404,
        detail="No shopping list item with this identifier belongs to this household.",
    )


def _list_out(view: ShoppingListView) -> ShoppingListOut:
    return ShoppingListOut(
        id=view.id, name=view.name, items=[_item_out(item) for item in view.items]
    )


def _item_out(item: ShoppingItemView) -> ShoppingItemOut:
    return ShoppingItemOut(
        id=item.id,
        product_id=item.product_id,
        product_name=item.product_name,
        free_text=item.free_text,
        quantity=(
            None
            if item.quantity is None
            else ShoppingQuantityOut(amount=item.quantity.amount, unit=item.quantity.unit_code)
        ),
        source=item.source,
        checked=item.checked,
        sort_order=item.sort_order,
    )
