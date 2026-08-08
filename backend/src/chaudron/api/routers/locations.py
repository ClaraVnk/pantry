"""Storage locations.

**Nothing is seeded at registration**, and that is a decision rather than an
omission. A household created today owns no location, and the first screen it
sees offers to create one.

The argument for seeding "Frigo / Congélateur / Placard" is that it makes the
first screen immediately usable. Three things weigh against it, and they won:

* *A default that does not fit cannot be taken back.* There is no ``DELETE`` and
  no archive endpoint on this resource -- ``archived_at`` exists on the column and
  nothing writes it -- so a seeded location a household does not have is permanent
  furniture. "A default nobody wants gets deleted" is not true here.
* *It would fix only the households created after it.* Every household that
  already exists with zero locations still lands on the empty screen, so the
  interface has to answer that state anyway. Once it does, seeding is a second
  mechanism producing the same outcome for a subset of users.
* *The vocabulary is not universal.* Cellier, garage, cave, congélateur du
  garage: a pre-filled list is lived with rather than corrected, and it teaches
  the wrong idea of what the field is for.

What replaces it belongs to the client, not to this module: the empty state
offers the three common names as one-tap suggestions. The convenience of a seed,
without a row nobody chose.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from chaudron.api.deps import InventoryReadDep, LocationServiceDep, MemberDep
from chaudron.api.schemas import LocationCreateIn, LocationOut
from chaudron.domain.ports import LocationDraft, LocationSummary

router = APIRouter(prefix="/v1/locations", tags=["locations"])


def _to_out(location: LocationSummary) -> LocationOut:
    return LocationOut(
        id=location.id,
        name=location.name,
        kind=location.kind,
        item_count=location.item_count,
    )


@router.get("", response_model=list[LocationOut], summary="List storage locations")
async def list_locations(
    household_id: InventoryReadDep, service: LocationServiceDep
) -> list[LocationOut]:
    locations = await service.list_locations(household_id)
    return [_to_out(location) for location in locations]


@router.post(
    "",
    response_model=LocationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a storage location",
)
async def create_location(
    household_id: MemberDep, service: LocationServiceDep, payload: LocationCreateIn
) -> LocationOut:
    """Add a place to put things, in the household the session resolved.

    No ``try``: a duplicate name surfaces as ``LocationNameTakenError`` and is
    mapped centrally by ``api/errors.py``, the same way every other domain failure
    reaches the client.
    """
    draft = LocationDraft(name=payload.name, kind=payload.kind)
    return _to_out(await service.create(household_id, draft))
