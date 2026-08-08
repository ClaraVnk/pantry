"""``/v1/members`` -- the resource that holds an infant's age band.

Two things are being checked, and only one of them is CRUD.

The first is the contract: the closed vocabularies, the ``infant_texture``
coupling that is a 422 on both sides of the wire, and the fact that a deletion is
a deletion.

The second is what must **not** happen: no allergen, no diet and no age band may
appear in an error body, and no household may read another's members. Both are
asserted rather than reviewed, because both are the kind of regression that
passes code review.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import HouseholdPerson
from tests.conftest import MakeHousehold, TenantPair, household_headers

MEMBERS_URL = "/v1/members"

ADULT = {
    "display_name": "Camille",
    "age_band": "adult",
    "diet": "vegetarian",
    "allergens": ["nuts", "celery"],
    "free_text_restrictions": "pas de coriandre",
    "infant_texture": None,
}

INFANT = {
    "display_name": "Nino",
    "age_band": "infant_6_9m",
    "diet": "omnivore",
    "allergens": [],
    "free_text_restrictions": "",
    "infant_texture": "smooth",
}


async def test_a_member_is_created_listed_and_read_back_whole(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=ADULT)

    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {
        "id",
        "display_name",
        "age_band",
        "diet",
        "allergens",
        "free_text_restrictions",
        "infant_texture",
    }
    assert body["allergens"] == ["celery", "nuts"], "sorted, so the response is stable"
    assert body["diet"] == "vegetarian"
    assert body["infant_texture"] is None

    listed = await api_client.get(MEMBERS_URL, headers=household_headers(household))
    assert [member["id"] for member in listed.json()] == [body["id"]]


async def test_an_unset_free_text_reads_back_as_an_empty_string(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``null`` and ``""`` are the same fact, and a client handles one of them wrong.

    The column is nullable because an absent restriction is not an empty one in
    the database; on the wire it is a text input, and a form binding to ``null``
    renders the word "null" in the box.
    """
    household = await make_household()

    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=INFANT)

    assert created.json()["free_text_restrictions"] == ""


async def test_an_infant_band_without_a_texture_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Contract 2. A hard constraint left undefined is worse than a wrong one.

    Without the texture, a suggestion for a six-month-old goes out with nothing
    said about how the food has to be served.
    """
    household = await make_household()

    response = await api_client.post(
        MEMBERS_URL,
        headers=household_headers(household),
        json={**INFANT, "infant_texture": None},
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/infant-texture-inconsistent"


async def test_a_texture_on_an_adult_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.post(
        MEMBERS_URL,
        headers=household_headers(household),
        json={**ADULT, "infant_texture": "smooth"},
    )

    assert response.status_code == 422, response.text


async def test_an_allergen_outside_the_regulated_list_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The vocabulary is closed on the *regulation*, not on what people report.

    "Lactose" is a real intolerance and not one of the fourteen; it belongs in
    the free-text field, which is a preference. Accepting it here would make it
    look like a filter, and no catalogue column says which products contain it.
    """
    household = await make_household()

    response = await api_client.post(
        MEMBERS_URL,
        headers=household_headers(household),
        json={**ADULT, "allergens": ["lactose"]},
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/validation-failed"


async def test_a_patch_changes_only_what_it_sends(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=ADULT)
    member_id = created.json()["id"]

    patched = await api_client.patch(
        f"{MEMBERS_URL}/{member_id}",
        headers=household_headers(household),
        json={"diet": "vegan"},
    )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["diet"] == "vegan"
    assert body["allergens"] == ["celery", "nuts"], "an untouched constraint is not cleared"
    assert body["display_name"] == "Camille"


async def test_a_patch_can_move_a_toddler_out_of_the_infant_bands(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Children grow, and the two fields have to move together in one request."""
    household = await make_household()
    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=INFANT)
    member_id = created.json()["id"]

    patched = await api_client.patch(
        f"{MEMBERS_URL}/{member_id}",
        headers=household_headers(household),
        json={"age_band": "child", "infant_texture": None},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["infant_texture"] is None


async def test_a_patch_that_would_break_the_pairing_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=INFANT)
    member_id = created.json()["id"]

    patched = await api_client.patch(
        f"{MEMBERS_URL}/{member_id}",
        headers=household_headers(household),
        json={"age_band": "adult"},
    )

    assert patched.status_code == 422, patched.text


async def test_deleting_a_member_erases_the_row(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """ "Erased" has to mean erased: this table carries no ``archived_at``.

    Health data of a minor under GDPR article 9. A soft delete would keep an
    infant's age band alive behind a flag, which is the state the schema
    deliberately cannot represent.
    """
    household = await make_household()
    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=INFANT)
    member_id = created.json()["id"]

    removed = await api_client.delete(
        f"{MEMBERS_URL}/{member_id}", headers=household_headers(household)
    )

    assert removed.status_code == 204, removed.text
    rows = (
        await db_session.scalars(
            select(HouseholdPerson).where(HouseholdPerson.household_id == household.id)
        )
    ).all()
    assert rows == []


async def test_one_household_cannot_read_or_change_another_s_members(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair
) -> None:
    created = await api_client.post(
        MEMBERS_URL, headers=household_headers(tenant_pair.household_a), json=ADULT
    )
    member_id = created.json()["id"]

    listed = await api_client.get(MEMBERS_URL, headers=household_headers(tenant_pair.household_b))
    patched = await api_client.patch(
        f"{MEMBERS_URL}/{member_id}",
        headers=household_headers(tenant_pair.household_b),
        json={"diet": "vegan"},
    )
    removed = await api_client.delete(
        f"{MEMBERS_URL}/{member_id}", headers=household_headers(tenant_pair.household_b)
    )

    assert listed.json() == []
    assert patched.status_code == 404, patched.text
    assert removed.status_code == 404, removed.text


async def test_no_error_body_ever_quotes_a_constraint(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A problem body is read by clients that log it. Health data must not be in it.

    The 404 for an unknown member is the one to watch: the tempting message
    echoes the row it could not find, and the row is somebody's allergy list.
    """
    household = await make_household()

    response = await api_client.get(
        f"{MEMBERS_URL}/../members", headers=household_headers(household)
    )
    missing = await api_client.patch(
        f"{MEMBERS_URL}/00000000-0000-7000-8000-000000000000",
        headers=household_headers(household),
        json={"diet": "vegan"},
    )

    assert missing.status_code == 404, missing.text
    for word in ("nuts", "celery", "vegetarian", "infant", "coriandre"):
        assert word not in missing.text
    assert response.status_code in (200, 404, 405)
