"""Shopping-list export: one endpoint that returns text, one that sends it away.

The split is the feature, and it is also the privacy boundary. ``GET
/export/text`` answers the household's own browser -- nothing leaves the
instance, the browser hands the string to ``navigator.share`` and the user picks
where it goes, which is the path ADR-0010 makes primary. ``POST
/export/{target}`` transmits the same list to a named third party under a stored
credential, and is therefore the one that needs a consent, a token, and a log
line saying it happened.

This module owns its own request and response models rather than adding to
``api/schemas.py``: the shapes are used by these two operations and nothing else,
and a shared schema module is the wrong place for a type with one caller.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from chaudron.api.deps import CipherDep, HouseholdDep, MemberDep, SessionDep
from chaudron.api.errors import ProblemError
from chaudron.domain.shopping import ShoppingListNotFoundError
from chaudron.domain.shopping_export import (
    ExportCredentialUnreadable,
    ExportRegistrantHasLeft,
    ExportTargetNotConfigured,
    ExportTargetRejected,
    ShoppingExportConsentMissing,
    ShoppingExportError,
    ShoppingExportPartiallyApplied,
    ShoppingExportQuotaExceeded,
    ShoppingExportUnavailable,
)
from chaudron.infra.repositories import SqlShoppingExportTargetStore
from chaudron.infra.todo.factory import SUPPORTED_TARGETS, ShoppingExportFactory
from chaudron.infra.todo.settings import load_settings
from chaudron.services.shopping_export import ShoppingExportService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/shopping-lists", tags=["shopping-lists"])

#: ``text/plain; charset=utf-8``. The charset is not optional: unit symbols
#: ("c. à s.") and the multiplication sign in a counted item are outside ASCII,
#: and a client that guesses latin-1 renders a shopping list as mojibake.
_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"


class ExportReceiptOut(BaseModel):
    """What was accepted, in counts. Never an echo of the list itself."""

    target: str
    export_id: uuid.UUID
    exported_item_count: int = Field(ge=0)
    #: The container the destination wrote into, when it names one. ``null``
    #: means the recipient's own default inbox.
    external_list_id: str | None = None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def get_shopping_export_service(session: SessionDep) -> ShoppingExportService:
    return ShoppingExportService(session)


def get_export_factory(
    request: Request, session: SessionDep, cipher: CipherDep
) -> ShoppingExportFactory:
    """The factory, from ``app.state`` when something put one there, else built here.

    The ``app.state`` lookup is the seam the tests use to substitute the socket
    without substituting the adapter. The fallback is the production path, and
    since migration ``0008`` it carries the household's *own* registered
    destination: the store reads ``shopping_export_target`` and the cipher opens
    what it holds. The instance-owned destination read from the environment stays
    behind it as the special case ADR-0010 always meant it to be -- one operator,
    one household -- rather than the only thing that works.
    """
    existing = getattr(request.app.state, "shopping_export_factory", None)
    if isinstance(existing, ShoppingExportFactory):
        return existing
    return ShoppingExportFactory(
        load_settings(),
        cipher=cipher,
        store=SqlShoppingExportTargetStore(session),
    )


ExportServiceDep = Annotated[ShoppingExportService, Depends(get_shopping_export_service)]
ExportFactoryDep = Annotated[ShoppingExportFactory, Depends(get_export_factory)]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "/{shopping_list_id}/export/text",
    response_class=PlainTextResponse,
    summary="The shopping list as plain text, one item per line",
    responses={200: {"content": {"text/plain": {}}}},
)
async def export_shopping_list_as_text(
    shopping_list_id: uuid.UUID,
    household_id: HouseholdDep,
    service: ExportServiceDep,
) -> PlainTextResponse:
    """Return the pending items as text. **Nothing leaves the instance.**

    This is what the browser passes to ``navigator.share({ text })`` -- with no
    ``url``, because iOS replaces a cross-domain one with the current page's and
    strips the query string of the other (research note §4.5). The frontend owns
    that call; this endpoint owns the string, so the share sheet, the clipboard
    fallback and every adapter render an item identically.
    """
    try:
        body = await service.plain_text(household_id, shopping_list_id)
    except ShoppingListNotFoundError:
        raise _not_found() from None
    return PlainTextResponse(body, media_type=_TEXT_MEDIA_TYPE)


@router.post(
    "/{shopping_list_id}/export/{target}",
    response_model=ExportReceiptOut,
    summary="Send the shopping list to a task application",
)
async def export_shopping_list_to_target(
    shopping_list_id: uuid.UUID,
    target: str,
    household_id: MemberDep,
    service: ExportServiceDep,
    factory: ExportFactoryDep,
) -> ExportReceiptOut:
    """Transmit the list to a third party, under this household's stored credential.

    Personal data leaves the instance here, so the destination must be one this
    household registered and agreed to (ADR-0010). Every refusal below names what
    to do about it, because "export failed" cannot be told apart from a revoked
    token, an unregistered destination or a vendor outage -- and the three have
    three different remedies.

    ``MemberDep`` rather than ``HouseholdDep``: this is the one route in the pair
    that causes personal data to leave the instance, and "read the household's
    data" -- which is all a ``viewer`` is promised -- does not include "send it
    somewhere else". Registering the destination is stricter still and belongs to
    an owner (``routers/export_targets.py``); pressing send afterwards is an
    ordinary household operation, so a member may do it.
    """
    try:
        exporter = await factory.exporter_for(household_id, target)
        receipt = await service.send(household_id, shopping_list_id, exporter)
    except ShoppingListNotFoundError:
        raise _not_found() from None
    except ShoppingExportError as error:
        raise _problem_for(error, target=target) from None

    return ExportReceiptOut(
        target=receipt.target,
        export_id=receipt.export_id,
        exported_item_count=receipt.accepted_line_count,
        external_list_id=receipt.external_list_id,
    )


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


def _not_found() -> ProblemError:
    return ProblemError(
        slug="shopping-list-not-found",
        title="Shopping list not found",
        status=404,
        detail="No shopping list with this identifier belongs to this household.",
    )


def _problem_for(error: ShoppingExportError, *, target: str) -> ProblemError:
    """Translate an export failure, quoting our own sentence and never the vendor's.

    ``str(error)`` is safe by construction: every message in
    ``domain/shopping_export.py`` and ``infra/todo/`` is built from our own
    fields, and the adapters are forbidden from interpolating a response body
    (proved by ``tests/todo/test_no_token_leaks.py``).
    """
    match error:
        # Before the consent arm below it, which it inherits from: structural
        # patterns are tried in order, so the general case first would swallow
        # this one and send a household to re-tick a box when what they need is
        # to see whose token is still filing their groceries.
        case ExportRegistrantHasLeft():
            return ProblemError(
                slug="export-registrant-has-left",
                title="The person who authorised this export has left",
                status=403,
                detail=str(error),
                target=target,
            )
        case ShoppingExportConsentMissing():
            return ProblemError(
                slug="export-consent-missing",
                title="Export not authorised",
                status=403,
                detail=str(error),
                target=target,
            )
        case ExportTargetRejected():
            return ProblemError(
                slug="export-target-rejected",
                title="Destination refused the credential",
                status=409,
                detail=str(error),
                target=target,
            )
        # Before ``ExportTargetNotConfigured``, which it inherits from, and for
        # the reason the consent pair above gives: order decides, and the general
        # arm would answer "no such destination" with a list of the ones to pick
        # from -- to a household whose destination is registered and whose only
        # problem is that this instance can no longer read its token (audit
        # O-06). ``supported`` is deliberately absent for the same reason: there
        # is nothing to choose.
        case ExportCredentialUnreadable():
            return ProblemError(
                slug="export-credential-unreadable",
                title="Stored credential can no longer be read",
                status=409,
                detail=str(error),
                target=target,
            )
        case ExportTargetNotConfigured():
            return ProblemError(
                slug="export-target-not-configured",
                title="No such export destination",
                status=409,
                detail=str(error),
                target=target,
                supported=sorted(SUPPORTED_TARGETS),
            )
        case ShoppingExportQuotaExceeded():
            return ProblemError(
                slug="export-rate-limited",
                title="Destination is rate limiting",
                status=429,
                detail=str(error),
                target=target,
            )
        case ShoppingExportPartiallyApplied():
            # 502 rather than 200-with-a-warning: the request did not do what it
            # was asked, and a client that only checks the status code must not
            # tell the user their list was sent.
            return ProblemError(
                slug="export-partially-applied",
                title="Some items were not exported",
                status=502,
                detail=str(error),
                target=target,
                exported_item_count=error.accepted,
                rejected_item_count=error.rejected,
            )
        case ShoppingExportUnavailable():
            return ProblemError(
                slug="export-target-unavailable",
                title="Destination unavailable",
                status=502,
                detail=str(error),
                target=target,
            )
        case _:
            return ProblemError(
                slug="export-failed",
                title="Export failed",
                status=502,
                detail=str(error),
                target=target,
            )
