"""``/v1/members`` -- who eats here, and what they cannot eat.

The most sensitive resource in the application after the provider credentials,
and it is sensitive in a different way: a credential is a secret, a member row is
a *disclosure*. Three consequences are enforced here rather than described.

**Nothing is logged.** This module installs no logger. A request log line naming
the endpoint is fine; a body, a member id or a validation message quoting an
allergen is not, and the safest way not to write one is to have nowhere to write
it.

**The identifier in the path is the only thing echoed.** Errors say "no member
with this identifier belongs to this household" and never why the household
believed otherwise -- the same reasoning that gives the tenant header a single
indistinguishable failure (``api/errors.py``).

**Deleting means deleting.** ``household_person`` has no ``archived_at``, so the
``DELETE`` below removes the row and the constraints with it. Suggestions already
generated hold no copy (``services/recipes.py``), which is what makes the
erasure real rather than nominal.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from chaudron.api.deps import HouseholdDep, MemberDep, MemberServiceDep
from chaudron.api.schemas import MemberIn, MemberOut, MemberPatchIn
from chaudron.domain.constraints import Person
from chaudron.domain.ports import UNSET
from chaudron.services.members import MemberDraft, MemberPatch

router = APIRouter(prefix="/v1/members", tags=["members"])


@router.get("", response_model=list[MemberOut], summary="The eaters of this household")
async def list_members(household_id: HouseholdDep, service: MemberServiceDep) -> list[MemberOut]:
    return [_to_out(person) for person in await service.list_members(household_id)]


@router.post(
    "",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record an eater and their constraints",
)
async def create_member(
    household_id: MemberDep, service: MemberServiceDep, payload: MemberIn
) -> MemberOut:
    # No ``try``: a ``DomainError`` is mapped centrally by ``api/errors.py``, and
    # the mapping there is written to quote nothing about the person.
    return _to_out(await service.create(household_id, _to_draft(payload)))


@router.patch("/{member_id}", response_model=MemberOut, summary="Change an eater's constraints")
async def update_member(
    household_id: MemberDep,
    service: MemberServiceDep,
    member_id: uuid.UUID,
    payload: MemberPatchIn,
) -> MemberOut:
    return _to_out(await service.update(household_id, member_id, _to_patch(payload)))


@router.delete(
    "/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase an eater and their constraints",
)
async def delete_member(
    household_id: MemberDep, service: MemberServiceDep, member_id: uuid.UUID
) -> Response:
    await service.delete(household_id, member_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _to_draft(payload: MemberIn) -> MemberDraft:
    return MemberDraft(
        display_name=payload.display_name.strip(),
        age_band=payload.age_band,
        diet=payload.diet,
        allergens=tuple(payload.allergens),
        infant_texture=payload.infant_texture,
        free_text_restrictions=payload.free_text_restrictions.strip() or None,
    )


def _to_patch(payload: MemberPatchIn) -> MemberPatch:
    """Turn "absent" into ``UNSET`` and "sent as null" into ``None``.

    ``model_fields_set`` is what carries the difference; reading the value alone
    would make ``{"infant_texture": null}`` -- the request that turns a toddler
    into a child -- indistinguishable from a request that never mentioned the
    field. Only ``infant_texture`` is genuinely nullable: a name, a band, a diet
    and a list of allergens have no "cleared" state, so ``null`` on those is read
    as "not sent" rather than invented into a meaning.
    """
    sent = payload.model_fields_set
    return MemberPatch(
        display_name=(
            payload.display_name.strip()
            if "display_name" in sent and payload.display_name is not None
            else UNSET
        ),
        age_band=(
            payload.age_band if "age_band" in sent and payload.age_band is not None else UNSET
        ),
        diet=payload.diet if "diet" in sent and payload.diet is not None else UNSET,
        allergens=(
            tuple(payload.allergens)
            if "allergens" in sent and payload.allergens is not None
            else UNSET
        ),
        infant_texture=payload.infant_texture if "infant_texture" in sent else UNSET,
        free_text_restrictions=(
            ((payload.free_text_restrictions or "").strip() or None)
            if "free_text_restrictions" in sent
            else UNSET
        ),
    )


def _to_out(person: Person) -> MemberOut:
    return MemberOut(
        id=person.id,
        display_name=person.display_name,
        age_band=person.age_band,
        diet=person.diet,
        # Sorted for a stable response; the enum order is the regulation's.
        allergens=sorted(person.allergens),
        free_text_restrictions=person.free_text_restrictions or "",
        infant_texture=person.infant_texture,
    )
