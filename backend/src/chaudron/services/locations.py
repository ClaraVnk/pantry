"""Storage locations use cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from chaudron.domain.ports import LocationDraft, LocationRepository, LocationSummary


class LocationService:
    def __init__(self, locations: LocationRepository) -> None:
        self._locations = locations

    async def list_locations(self, household_id: uuid.UUID) -> Sequence[LocationSummary]:
        return await self._locations.list_with_counts(household_id)

    async def create(self, household_id: uuid.UUID, draft: LocationDraft) -> LocationSummary:
        """Add a location to this household.

        A freshly registered household owns none, and until now nothing in the
        application could create one -- which made the first screen after sign-up
        a dead end. Registration still seeds nothing; the reasoning is on the
        ``POST`` handler in ``api/routers/locations.py``.
        """
        return await self._locations.create(household_id, draft)
