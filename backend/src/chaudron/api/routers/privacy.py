"""``/v1/households`` -- the data subject's two levers over their own household.

``GET /v1/households/export`` is articles 15 and 20; ``DELETE /v1/households`` is
article 17. Together they close open item O-01 of
``docs/security-pentest-2026-08-04.md``, which counted fifty-two routes and nine
``DELETE``s, none of which erased anything, and quoted
``docs/security-model.md`` conceding of access and portability: *"To be built.
Nothing exists."*

**Why the prefix is ``/v1/households`` and not ``/v1/privacy``.** It is the path
``services/export_targets.py`` has been pointing readers at since ADR-0010 --
*"withdrawal is not a deletion request; ``DELETE /v1/households`` is where erasure
lives"* -- and it named a route that did not exist. There is no identifier in the
path because there is none anywhere else in this API either: the tenant is
resolved server-side from the credential and, at most, selected by
``X-Household-Id`` (``api/deps.py``). A household in the path would be a household
a client asserts.

**Both are owner-only, and the two have different reasons.**

Erasure destroys data belonging to every member of the household, not only to
whoever asked. ``require_owner`` is the same guard that already covers handing out
a calendar credential and accepting a third party's export token: the decisions
whose consequence the household as a whole carries.

The export is owner-only for a narrower reason, and it is worth being plain about
what that costs. A member is a data subject too, and article 15 is their right,
not the owner's. But there is no per-person export to give them: an inventory, a
receipt, a shopping list belong to the household and cannot be split by person,
so any export a member could obtain would be the whole household's -- including
the other members' allergen records. What is on offer here is therefore the
*household's* copy, and it is bounded to the role that can already erase the same
data. **A member exercising article 15 against a Chaudron instance goes through
the operator**, who is the controller in any case (``docs/security-model.md``
section 8). That is a product decision, it is not a compliance opinion, and it sits
next to the row section 8.5 already flags as unsettled -- *"the member who leaves:
not settled"*.

**Neither is reachable by a machine token.** ``require_owner`` resolves a
:class:`~chaudron.services.auth.Principal`, which only a browser cookie produces,
so a long-lived credential in a home-automation appliance can neither read the
household out nor delete it -- whatever scopes it was issued with.
``tests/api/test_route_authentication.py`` asserts that in both directions.

**Nothing is logged here.** For the reason ``routers/members.py`` gives: the
safest way not to write a member's allergens into a log line is to have nowhere to
write one. The single record this feature keeps is written by
``services/privacy.py``, and it carries an identifier and a set of integers.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from chaudron.api.deps import OwnerDep, OwnerHouseholdDep, PrivacyServiceDep
from chaudron.api.errors import ProblemError
from chaudron.services.privacy import ErasureBlockedError, ErasureReceipt

router = APIRouter(prefix="/v1/households", tags=["privacy"])


class ErasureReceiptOut(BaseModel):
    """What the erasure removed, table by table.

    Returned rather than a bare ``204`` because this is the only artefact the
    person who asked walks away with: after this call there is no account screen
    left to check, no row to point at, and no support ticket that can be answered
    by looking. The counts are the household's own data, they name nothing, and
    they are the same numbers written to the operator's log.
    """

    household_id: str
    erased_at: dt.datetime
    #: Table name to rows removed, counted immediately before the delete and
    #: verified to be zero immediately after it.
    rows_erased: dict[str, int]
    #: What deliberately survived, and why. Both entries are argued in
    #: ``services/privacy.py``; saying so here is what keeps "erased" from being
    #: read as more than it is.
    not_erased: dict[str, str]


#: Fixed text, so the two sentences that qualify every erasure cannot drift from
#: the reasoning that produced them.
_NOT_ERASED: dict[str, str] = {
    "product (public catalogue)": (
        "Open Food Facts entries carry no household and are shared by every "
        "instance. They are not this household's data to erase; the products it "
        "created itself were erased with it."
    ),
    "user_account": (
        "An account is independent of any household and may belong to several. "
        "Erasing this household ended its membership and left the identity; no "
        "route erases an account today."
    ),
}


@router.get(
    "/export",
    response_model=None,
    summary="Everything this household holds, as a portable document",
)
async def export_household(
    household_id: OwnerHouseholdDep,
    principal: OwnerDep,
    service: PrivacyServiceDep,
) -> dict[str, Any]:
    """Article 15 and article 20, in one document.

    No response model on purpose. The body has one key per table of the schema and
    the schema's own column names, so a Pydantic model here would be a second
    declaration of the data model, kept in step by hand, and silently truncating
    the export the day the two disagreed. The shape is fixed by
    ``export_version``; the content is fixed by ``domain/models.py``.
    """
    return await service.export(household_id, requested_by=principal.user_id)


@router.delete(
    "",
    response_model=ErasureReceiptOut,
    summary="Erase this household and everything belonging to it",
)
async def erase_household(
    household_id: OwnerHouseholdDep, service: PrivacyServiceDep
) -> ErasureReceiptOut:
    """Article 17. Irreversible, and there is no archived state to fall back on.

    ``Household.archived_at`` exists and is written by nothing (the pentest says
    so, and it is still true after this route): archiving is not erasure and
    offering it here as a softer option would be the partial erasure the security
    model calls a non-compliance that looks like compliance.
    """
    try:
        receipt = await service.erase(household_id)
    except ErasureBlockedError as error:
        raise _erasure_blocked(error) from None
    return _to_out(receipt)


def _to_out(receipt: ErasureReceipt) -> ErasureReceiptOut:
    return ErasureReceiptOut(
        household_id=str(receipt.household_id),
        erased_at=receipt.erased_at,
        rows_erased=dict(receipt.rows_erased),
        not_erased=dict(_NOT_ERASED),
    )


def _erasure_blocked(error: ErasureBlockedError) -> ProblemError:
    """``409``: the request is legitimate and the instance cannot honour it yet.

    Not a ``500``. Nothing is broken -- a deployment has retained receipt images
    and this application has no object-storage client to delete them with, which
    is a configuration this build does not support rather than a failure. And not a
    ``202``: accepting the request and erasing the rows would leave the objects and
    report a complete erasure, which is exactly the shape of non-compliance
    ``docs/security-model.md`` section 8.5 warns about.

    ``str(error)`` is safe to pass through: the message is built from a count and
    constants, and quotes no key, no merchant and no line.
    """
    return ProblemError(
        slug="erasure-incomplete-by-construction",
        title="Erasure cannot be completed",
        status=409,
        detail=str(error),
        retained_images=error.retained_images,
    )
