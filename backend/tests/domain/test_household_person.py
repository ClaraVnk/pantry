"""``household_person``: the eaters, and the handling their rows require.

Two things are being pinned here. The **modelling** decision -- a person is not
an account, and an infant is the proof -- and the **protection** that decision
makes necessary, because the columns below are health data under GDPR article 9
and for one of these rows they are a minor's.

The protection tests are the ones worth keeping honest. Every one of them fails
if somebody undoes a ``deferred=True``, drops a check constraint or adds a
``__repr__`` that prints the row.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from chaudron.domain.dietary import INFANT_AGE_BANDS
from chaudron.domain.models import (
    AgeBand,
    Allergen,
    Diet,
    Household,
    HouseholdPerson,
    InfantTexture,
)
from tests.conftest import MakeHousehold, MakeUser

# --------------------------------------------------------------------------- #
# A person is not an account
# --------------------------------------------------------------------------- #


async def test_an_infant_needs_no_user_account(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The reason this table exists rather than three columns on ``household_member``.

    A nine-month-old cannot hold an account, and minting one for them would mean
    an email address, a password reset path and a login surface for somebody who
    cannot consent to any of it.
    """
    household = await make_household()
    infant = HouseholdPerson(
        household_id=household.id,
        display_name="Nino",
        age_band=AgeBand.INFANT_9_12M,
        infant_texture=InfantTexture.SOFT_PIECES,
    )
    db_session.add(infant)
    await db_session.flush()

    assert infant.user_account_id is None


async def test_an_account_maps_to_at_most_one_person_per_household(
    db_session: AsyncSession, make_household: MakeHousehold, make_user: MakeUser
) -> None:
    household = await make_household()
    user = await make_user()
    for name in ("Camille", "Camille encore"):
        db_session.add(
            HouseholdPerson(household_id=household.id, user_account_id=user.id, display_name=name)
        )

    with pytest.raises(IntegrityError, match="uq_household_person_user_account"):
        await db_session.flush()


async def test_people_without_an_account_do_not_collide(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The partial index exists for exactly this: two NULLs are not a duplicate.

    An infant and a Sunday guest both have no account, and a plain unique on
    ``(household_id, user_account_id)`` would have made the second one
    unsaveable -- with an error message about a constraint nobody would connect
    to "you cannot add a second person who does not log in".
    """
    household = await make_household()
    for name in ("Nino", "Mamie"):
        db_session.add(HouseholdPerson(household_id=household.id, display_name=name))

    await db_session.flush()

    people = await db_session.scalars(
        select(HouseholdPerson).where(HouseholdPerson.household_id == household.id)
    )
    assert len(people.all()) == 2


# --------------------------------------------------------------------------- #
# Hard constraints the database refuses to leave undefined
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("band", INFANT_AGE_BANDS, ids=[band.value for band in INFANT_AGE_BANDS])
async def test_an_infant_without_a_texture_is_refused(
    db_session: AsyncSession, make_household: MakeHousehold, band: AgeBand
) -> None:
    """Texture is a hard constraint; an infant without one leaves it undefined.

    The contract makes this a 422. Expressing it in the schema as well means the
    invalid row cannot arrive by any other door -- a seed, an import, a psql
    prompt -- and quietly disable the rule for that child.
    """
    household = await make_household()
    db_session.add(HouseholdPerson(household_id=household.id, display_name="Nino", age_band=band))

    with pytest.raises(IntegrityError, match="infant_texture_band"):
        await db_session.flush()


async def test_an_adult_with_a_texture_is_refused(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    db_session.add(
        HouseholdPerson(
            household_id=household.id,
            display_name="Camille",
            age_band=AgeBand.ADULT,
            infant_texture=InfantTexture.SMOOTH,
        )
    )

    with pytest.raises(IntegrityError, match="infant_texture_band"):
        await db_session.flush()


async def test_a_null_allergen_cannot_enter_a_profile(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Same reason as on ``product``: NULL makes every containment test unknown."""
    household = await make_household()
    db_session.add(
        HouseholdPerson(
            household_id=household.id,
            display_name="Camille",
            allergens=[Allergen.NUTS, None],
        )
    )

    with pytest.raises(IntegrityError, match="allergens_wellformed"):
        await db_session.flush()


async def test_free_text_is_bounded_by_the_database(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """It reaches an LLM prompt, so its length is a budget, not a preference.

    Bounded here as well as at the API, because a limit enforced only by the
    handler is a limit the next writer -- an import, a migration, a fixture --
    does not have.
    """
    household = await make_household()
    db_session.add(
        HouseholdPerson(
            household_id=household.id,
            display_name="Camille",
            free_text_restrictions="x" * 501,
        )
    )

    with pytest.raises(IntegrityError, match="free_text_length"):
        await db_session.flush()


# --------------------------------------------------------------------------- #
# Health data handling
# --------------------------------------------------------------------------- #


async def test_a_plain_select_does_not_load_the_health_columns(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """``deferred=True``, the same enforcement ``api_key_ciphertext`` uses.

    Resolving a display name for a suggestion header must not drag somebody's
    allergies into memory, into a log line or into a serialiser that iterates
    ``__dict__``. Reading them takes an explicit ``undefer()``: a gesture that is
    visible in review and findable with grep.
    """
    household = await make_household()
    person = HouseholdPerson(
        household_id=household.id,
        display_name="Camille",
        allergens=[Allergen.NUTS],
        free_text_restrictions="pas de coriandre",
    )
    db_session.add(person)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.scalar(select(HouseholdPerson).where(HouseholdPerson.id == person.id))

    assert loaded is not None
    unloaded = inspect(loaded).unloaded
    assert "allergens" in unloaded
    assert "free_text_restrictions" in unloaded
    # The non-sensitive half is there, which is what makes the deferral usable
    # rather than merely obstructive.
    assert loaded.display_name == "Camille"


async def test_the_health_columns_load_when_a_caller_asks_for_them(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The deferral must be a speed bump, not a wall, or it will be removed."""
    household = await make_household()
    person = HouseholdPerson(
        household_id=household.id, display_name="Camille", allergens=[Allergen.NUTS]
    )
    db_session.add(person)
    await db_session.commit()
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(HouseholdPerson)
        .where(HouseholdPerson.id == person.id)
        .options(undefer(HouseholdPerson.allergens))
    )

    assert loaded is not None
    assert loaded.allergens == [Allergen.NUTS]


async def test_the_default_representation_discloses_nothing(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """``repr()`` ends up in tracebacks, in logs and in error reporters.

    SQLAlchemy's default prints the class and the identity, never the columns.
    The assertion is here so that adding a "helpful" ``__repr__`` to this class
    fails a test instead of quietly publishing a child's diet to Sentry.
    """
    household = await make_household()
    person = HouseholdPerson(
        household_id=household.id,
        display_name="Camille",
        allergens=[Allergen.NUTS],
        diet=Diet.VEGETARIAN,
        free_text_restrictions="pas de coriandre",
    )
    db_session.add(person)
    await db_session.flush()

    rendered = repr(person)

    for secret in ("Camille", "nuts", "vegetarian", "coriandre"):
        assert secret not in rendered


async def test_deleting_the_household_erases_the_people(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Erasure has to be total and atomic, not a cleanup script that forgets one table."""
    household = await make_household()
    db_session.add(
        HouseholdPerson(
            household_id=household.id, display_name="Camille", allergens=[Allergen.NUTS]
        )
    )
    await db_session.flush()

    await db_session.execute(sa.delete(Household).where(Household.id == household.id))

    remaining = await db_session.scalar(
        select(sa.func.count())
        .select_from(HouseholdPerson)
        .where(HouseholdPerson.household_id == household.id)
    )
    assert remaining == 0
