"""The eaters: the only write path over ``household_person``.

Every row this service touches is health data (GDPR article 9), and for an
infant a minor's. Three rules follow from that and are enforced here rather than
described:

*Nothing is logged.* Not the allergens, not the age band, not the free text. This
module contains no ``logger`` on purpose -- the temptation to add one "just for
debugging" is the whole risk, and there is nothing here worth the exposure.

*Erasure means erasure.* ``DELETE`` removes the row. There is no ``archived_at``
on this table (``domain/models.py``), so a deleted member's constraints do not
survive anywhere, and the suggestions generated while they were at the table do
not carry a copy of them either (``services/recipes.py``).

*The 422 of the contract is a database constraint first.* A texture outside an
infant band is refused by ``ck_household_person_infant_texture_band`` whatever
this code does; it is checked here as well so the client gets a sentence instead
of a constraint name.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from chaudron.domain.constraints import INFANT_BANDS, Person
from chaudron.domain.models import AgeBand, Allergen, Diet, HouseholdPerson, InfantTexture
from chaudron.domain.ports import (
    UNSET,
    InfantTextureInconsistentError,
    Maybe,
    MemberNotFoundError,
    UnsetType,
)

__all__ = ["MemberDraft", "MemberPatch", "MemberService"]


@dataclass(frozen=True, slots=True)
class MemberDraft:
    display_name: str
    age_band: AgeBand
    diet: Diet
    allergens: tuple[Allergen, ...]
    infant_texture: InfantTexture | None
    free_text_restrictions: str | None


@dataclass(frozen=True, slots=True)
class MemberPatch:
    """A partial change. ``UNSET`` leaves a column alone, ``None`` clears it."""

    display_name: Maybe[str] = UNSET
    age_band: Maybe[AgeBand] = UNSET
    diet: Maybe[Diet] = UNSET
    allergens: Maybe[tuple[Allergen, ...]] = UNSET
    infant_texture: Maybe[InfantTexture | None] = UNSET
    free_text_restrictions: Maybe[str | None] = UNSET


class MemberService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_members(self, household_id: uuid.UUID) -> Sequence[Person]:
        rows = (
            await self._session.scalars(
                select(HouseholdPerson)
                .where(HouseholdPerson.household_id == household_id)
                .options(
                    undefer(HouseholdPerson.allergens),
                    undefer(HouseholdPerson.free_text_restrictions),
                )
                .order_by(HouseholdPerson.sort_order, HouseholdPerson.display_name)
            )
        ).all()
        return [_to_person(row) for row in rows]

    async def create(self, household_id: uuid.UUID, draft: MemberDraft) -> Person:
        _check_texture(draft.age_band, draft.infant_texture)
        person = HouseholdPerson(
            household_id=household_id,
            display_name=draft.display_name,
            age_band=draft.age_band,
            diet=draft.diet,
            allergens=list(dict.fromkeys(draft.allergens)),
            infant_texture=draft.infant_texture,
            free_text_restrictions=draft.free_text_restrictions or None,
        )
        self._session.add(person)
        await self._session.flush()
        return _to_person(person)

    async def update(
        self, household_id: uuid.UUID, member_id: uuid.UUID, patch: MemberPatch
    ) -> Person:
        person = await self._get(household_id, member_id)
        # Read both before writing either: the two are checked against each
        # other, and a half-applied patch would leave the row in the state the
        # check constraint exists to refuse.
        band = _or_keep(patch.age_band, person.age_band)
        texture = _or_keep(patch.infant_texture, person.infant_texture)
        _check_texture(band, texture)

        person.display_name = _or_keep(patch.display_name, person.display_name)
        person.age_band = band
        person.infant_texture = texture
        person.diet = _or_keep(patch.diet, person.diet)
        person.allergens = list(dict.fromkeys(_or_keep(patch.allergens, tuple(person.allergens))))
        person.free_text_restrictions = (
            _or_keep(patch.free_text_restrictions, person.free_text_restrictions) or None
        )
        await self._session.flush()
        return _to_person(person)

    async def delete(self, household_id: uuid.UUID, member_id: uuid.UUID) -> None:
        """Erase the row, constraints included. There is no soft delete here."""
        person = await self._get(household_id, member_id)
        await self._session.delete(person)
        await self._session.flush()

    async def _get(self, household_id: uuid.UUID, member_id: uuid.UUID) -> HouseholdPerson:
        person = await self._session.scalar(
            select(HouseholdPerson)
            .where(
                HouseholdPerson.household_id == household_id,
                HouseholdPerson.id == member_id,
            )
            .options(
                undefer(HouseholdPerson.allergens),
                undefer(HouseholdPerson.free_text_restrictions),
            )
        )
        if person is None:
            raise MemberNotFoundError(member_id)
        return person


def _or_keep[T](value: Maybe[T], current: T) -> T:
    """The patched value, or what the row already holds.

    ``isinstance`` rather than ``is not UNSET`` because it is the form the type
    checker understands: the sentinel is a class, and narrowing a
    ``T | UnsetType`` union is exactly what it can do with it.
    """
    return current if isinstance(value, UnsetType) else value


def _check_texture(band: AgeBand, texture: InfantTexture | None) -> None:
    """A texture and an infant band imply each other, both ways.

    A texture on an adult is meaningless; an infant band without one leaves a
    hard constraint undefined, which is worse -- the suggestion would go out with
    nothing said about how the food has to be served.
    """
    if (band in INFANT_BANDS) != (texture is not None):
        raise InfantTextureInconsistentError(
            "infant_texture must be set for an infant age band, and null otherwise"
        )


def _to_person(row: HouseholdPerson) -> Person:
    return Person(
        id=row.id,
        display_name=row.display_name,
        age_band=row.age_band,
        diet=row.diet,
        allergens=frozenset(row.allergens),
        infant_texture=row.infant_texture,
        free_text_restrictions=row.free_text_restrictions,
    )
