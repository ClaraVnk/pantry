"""Where a household has agreed its shopping list may be sent, and on what terms.

The configuration side of ADR-0010. ``routers/shopping_export.py`` *uses* a
destination; this module is where one is registered, inspected and withdrawn, and
the separation is the same one ``services/export_targets.py`` makes internally:
exactly one place in the application accepts a third party's token, and it is not
the place that sends anything.

**Writing here is owner-only; reading is not.** ``PUT`` accepts a third party's
credential and dates an agreement in the household's name, and ``DELETE``
withdraws it; both are behind ``OwnerHouseholdDep``. Until audit AUD-027 they
were behind bare ``HouseholdDep``, which is to say behind nothing but membership:
a ``viewer`` -- the role whose name promises it cannot write -- could register
their own Todoist token, consent on the household's behalf and receive the
household's shopping list from then on. Replayed end to end; ``200``,
``exported_item_count: 1``.

**Every row records who registered it, and a departure stops it.** Nothing
cascades from ``household_member`` to ``shopping_export_target``, so an excluded
member used to leave behind a consented row that kept exporting into their
personal account indefinitely, while the household saw four characters of a token
and no name. ``registered_by`` is that name; ``registrant_is_member`` is whether
they are still here, re-read on every send rather than swept at some later date
(``infra/todo/factory.py``, and there is no membership-removal endpoint to hang a
sweep on).

**The token is never rendered.** No response model here has a field that could
hold one, which is structural rather than a convention -- a handler cannot
disclose a token by forgetting to strip one. ``token_last4`` is what a household
sees again, and it exists so someone with two Todoist accounts can tell which key
is installed. The token is never in a path, never in a query string, and never in
a log line; ``tests/todo/test_no_token_leaks.py`` covers this path alongside the
exporter.

**Two reads, and the difference between them is the whole point.**

``GET /v1/shopping-lists/export/targets`` answers "where may this list be sent
*right now*", and therefore lists **consented destinations only**. It is what an
interface reads to decide whether to offer a button, and a destination whose
agreement was withdrawn reappearing in it would put that button back -- which is
worse than never having offered it. The answer is always a well-formed array,
empty included: a client must never have to read a ``404`` as "none".

``GET /v1/shopping-lists/export/targets/{target}`` answers "what did we register,
and what happened to it", withdrawal included. ADR-0010 keeps the row precisely so
a household can see what it once authorised, and hiding a revoked row from every
read would delete that record in effect while leaving it in the table.

The request and response models live here rather than in ``api/schemas.py``, the
way ``routers/budget.py`` and ``routers/calendar.py`` keep their own: they are
used by one router, and nothing else has a reason to import them.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chaudron.api.deps import (
    CipherDep,
    HouseholdDep,
    OwnerDep,
    OwnerHouseholdDep,
    SessionDep,
)
from chaudron.api.errors import ProblemError
from chaudron.infra.crypto import MIN_API_KEY_LENGTH
from chaudron.infra.todo.factory import SUPPORTED_TARGETS
from chaudron.services.export_targets import (
    ExportConsentMissing,
    ExportTargetConfigurationError,
    ExportTargetNotRegistered,
    ExportTargetService,
    ExportTargetView,
    InvalidExportToken,
    RegisterTargetCommand,
    UnsupportedExportTarget,
)

router = APIRouter(prefix="/v1/shopping-lists/export/targets", tags=["shopping-lists"])

#: A Todoist project identifier is a short opaque string. Bounded and restricted
#: to an unambiguous alphabet because it is copied verbatim into an outbound form
#: body: anything that could carry a separator, a newline or a control character
#: belongs nowhere near a request built for somebody else's API.
_LIST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: No real personal token is anywhere near this long. The ceiling exists so a
#: multi-megabyte "token" is refused by the schema rather than encrypted.
_MAX_TOKEN_CHARS = 512


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored -- as everywhere else at the boundary."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class ExportTargetOut(BaseModel):
    """One registered destination. Deliberately without a field for the token.

    ``target`` rather than ``target_code``, to match
    ``ExportReceiptOut.target`` and the ``target`` extension every export problem
    document already carries: one concept, one name across the contract.
    """

    target: str
    #: The last four characters of the stored token, and the only fragment of it
    #: that ever leaves the instance. Enough to recognise a key, useless to
    #: anyone else.
    token_last4: str
    #: The container items land in. ``null`` means the recipient's own inbox.
    external_list_id: str | None
    consented_at: dt.datetime
    #: When the household withdrew its agreement, or ``null``. The row survives a
    #: withdrawal so this date has somewhere to live.
    consent_revoked_at: dt.datetime | None
    #: The one field a client should branch on. Computed here rather than left to
    #: the caller to derive from two dates, because deriving it wrongly means
    #: sending personal data on a withdrawn agreement.
    is_consented: bool
    #: The display name of whoever pasted this token and ticked the box, or
    #: ``null`` for a destination registered before migration ``0014`` recorded
    #: it. Until it existed, an owner opening this screen saw four characters of a
    #: credential and had no way to learn whose it was (audit AUD-027).
    registered_by: str | None
    #: Whether that person is still a member of this household. ``false`` means
    #: the household's list would be filed into the account of somebody who has
    #: left, so the export refuses until an owner registers again or withdraws --
    #: nothing cascades from ``household_member`` to this table, and nothing
    #: should: deleting the agreement would delete the record of it.
    registrant_is_member: bool


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class RegisterTargetIn(StrictModel):
    """A token, optionally a container, and an agreement that is its own field.

    ``consent_granted`` is **required and has no default**. A default of ``true``
    would be a pre-ticked box; a default of ``false`` would let a client omit the
    one field the whole feature turns on and get a refusal it could not explain.
    Making it required forces the question to be asked, which is what an explicit
    agreement means.

    ``token`` is a :class:`~pydantic.SecretStr` so that a model dumped into a log
    line, a traceback frame or a debugger prints a mask rather than a third
    party's credential. The validation handler in ``api/errors.py`` already strips
    pydantic's ``input`` echo from a 422; this is the second lock.
    """

    token: Annotated[SecretStr, Field(min_length=MIN_API_KEY_LENGTH, max_length=_MAX_TOKEN_CHARS)]
    consent_granted: bool
    external_list_id: Annotated[str | None, Field(pattern=_LIST_ID_PATTERN)] = None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def get_export_target_service(session: SessionDep, cipher: CipherDep) -> ExportTargetService:
    """The service, with the destinations this build actually ships.

    ``SUPPORTED_TARGETS`` is injected rather than imported inside the service so
    that "which destinations exist" is decided once, in the factory that has to
    build an adapter for each of them. A registration this endpoint accepted and
    the factory could not honour would be a row that only ever produces a 409.
    """
    return ExportTargetService(session, cipher, supported_targets=SUPPORTED_TARGETS)


ExportTargetServiceDep = Annotated[ExportTargetService, Depends(get_export_target_service)]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=list[ExportTargetOut],
    summary="Destinations this household may currently send its shopping list to",
)
async def list_export_targets(
    household_id: HouseholdDep,
    service: ExportTargetServiceDep,
) -> list[ExportTargetOut]:
    """Consented destinations only. Always an array, empty when there are none.

    An interface reads this to decide whether sending to a third party is even
    offered (contract 6bis): what is in the list is usable, and a destination
    whose agreement was withdrawn is not in it. Empty is the normal state -- most
    households never register one -- so it is an empty array rather than a
    ``404``, which a client would otherwise have to read as data.

    "Usable" now means consented **and** registered by somebody still in the
    household. A destination whose registrant has left is refused by the export
    path (``infra/todo/factory.py``), so listing it here would put a button back
    that produces a 403 nobody can explain. The row is still visible through the
    read below, which is where a household goes to see what happened to it.
    """
    registered = await service.list_targets(household_id)
    return [_to_out(view) for view in registered if view.is_usable]


@router.get(
    "/{target}",
    response_model=ExportTargetOut,
    summary="What this household registered for one destination, withdrawal included",
)
async def read_export_target(
    target: str,
    household_id: HouseholdDep,
    service: ExportTargetServiceDep,
) -> ExportTargetOut:
    """The settings view: it shows a withdrawn agreement rather than hiding it.

    ``404`` means nothing was ever registered, which is different from "registered
    and then withdrawn" -- and the difference is exactly what a household needs to
    see to know whether it has already handed a token to this instance.
    """
    registered = await service.list_targets(household_id)
    found = next((view for view in registered if view.target_code == target), None)
    if found is None:
        raise _not_registered(target)
    return _to_out(found)


@router.put(
    "/{target}",
    response_model=ExportTargetOut,
    summary="Register a destination, with the agreement that lets it be used",
)
async def register_export_target(
    target: str,
    body: RegisterTargetIn,
    principal: OwnerDep,
    household_id: HouseholdDep,
    service: ExportTargetServiceDep,
) -> ExportTargetOut:
    """Store a token and date the agreement it came with. **Owner only.**

    Idempotent by household and destination: a second call is the rotation
    procedure, the previous token overwritten rather than versioned. It also
    clears an earlier withdrawal -- the caller has just agreed again, and had to
    restate the token to do it, which puts the sentence about what leaves the
    instance back in front of them.

    Owner-only since audit AUD-027, and the reason is not seniority. This route
    is one of exactly two in the API that *accept* a third party's credential, and
    what it writes is a consent **in the household's name** to send what
    identifiable people eat to an American service -- health or religious data in
    a household with an allergy or an observance. A ``viewer`` could do it: paste
    their own Todoist token, tick the box on everybody's behalf, and receive the
    household's shopping list from then on. The decision belongs to whoever
    carries the consequence, which is the same argument
    ``GET /v1/calendar/subscription`` makes for handing one out.

    ``principal`` is here for :attr:`RegisterTargetCommand.registered_by_user_id`
    and not only for the guard: a consent nobody signed is what let an excluded
    member's token keep exporting.
    """
    try:
        stored = await service.register(
            household_id,
            RegisterTargetCommand(
                target_code=target,
                # The one place the plaintext is read, and it goes straight into
                # a method that seals it.
                token=body.token.get_secret_value(),
                consent_granted=body.consent_granted,
                registered_by_user_id=principal.user_id,
                external_list_id=body.external_list_id,
            ),
        )
    except ExportTargetConfigurationError as error:
        raise _problem_for(error, target=target) from None
    return _to_out(stored)


@router.delete(
    "/{target}",
    response_model=ExportTargetOut,
    summary="Withdraw the agreement to send this household's list to a destination",
)
async def revoke_export_target(
    target: str,
    household_id: OwnerHouseholdDep,
    service: ExportTargetServiceDep,
) -> ExportTargetOut:
    """Stop the sending, keep the record. **Owner only.**

    Symmetrical with the registration above, and the symmetry is deliberate:
    whoever may grant this household's agreement is whoever may withdraw it. The
    asymmetric reading -- anyone may switch off an export -- sounds safe and is
    not, because withdrawal is also how a household's integration is broken. A
    disgruntled viewer silently cutting the shopping list off is a support ticket
    nobody can trace, since the row keeps no record of who withdrew.

    ``200`` with the updated destination rather than ``204``, because this does
    not delete anything: the row survives so the household can still see what it
    authorised and when, and the response is what tells the interface the
    withdrawal landed. It takes effect at the next export -- the factory reads the
    consent on every send -- not at some later cleanup.
    """
    try:
        withdrawn = await service.revoke(household_id, target)
    except ExportTargetConfigurationError as error:
        raise _problem_for(error, target=target) from None
    return _to_out(withdrawn)


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


def _to_out(view: ExportTargetView) -> ExportTargetOut:
    return ExportTargetOut(
        target=view.target_code,
        token_last4=view.token_last4,
        external_list_id=view.external_list_id,
        consented_at=view.consented_at,
        consent_revoked_at=view.consent_revoked_at,
        is_consented=view.is_consented,
        registered_by=view.registered_by,
        registrant_is_member=view.registrant_is_member,
    )


def _not_registered(target: str) -> ProblemError:
    return ProblemError(
        slug="export-target-not-registered",
        title="No such export destination",
        status=404,
        detail=f"No {target} destination is registered for this household.",
        target=target,
    )


def _problem_for(error: ExportTargetConfigurationError, *, target: str) -> ProblemError:
    """Translate a refusal. ``str(error)`` is safe: no message here quotes a token.

    Every message in ``services/export_targets.py`` is built from our own fields
    and constants -- the submitted value is never interpolated, not even to say it
    is too short -- which is what makes passing it through as ``detail`` sound
    rather than convenient.
    """
    match error:
        case ExportConsentMissing():
            # 422 rather than 403: the request is well-formed HTTP against a
            # resource the caller owns, and what is wrong with it is its content.
            return ProblemError(
                slug="export-consent-required",
                title="Explicit agreement required",
                status=422,
                detail=str(error),
                target=target,
            )
        case InvalidExportToken():
            return ProblemError(
                slug="export-token-invalid",
                title="Token cannot be stored",
                status=422,
                detail=str(error),
                target=target,
            )
        case UnsupportedExportTarget():
            return ProblemError(
                slug="export-target-not-configured",
                title="No such export destination",
                status=409,
                detail=str(error),
                target=target,
                supported=sorted(SUPPORTED_TARGETS),
            )
        case ExportTargetNotRegistered():
            return _not_registered(target)
        case _:  # pragma: no cover -- the four subclasses above are exhaustive
            return ProblemError(
                slug="export-target-configuration-failed",
                title="Destination could not be configured",
                status=422,
                detail=str(error),
                target=target,
            )
