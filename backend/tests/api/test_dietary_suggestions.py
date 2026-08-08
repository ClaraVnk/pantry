"""``POST /v1/recipes/suggest`` under dietary constraints -- the three-part device.

ADR-0009 describes a mechanism in three moments, and only two of them can be
tested by looking at a response body. All three are here:

1. **before the call** -- the product carrying the allergen is absent from the
   inventory that was sent, which the persisted ``stock_snapshot`` records
   exactly;
2. **after the call** -- an ingredient that cannot be matched back to that
   inventory discards the whole suggestion;
3. **never a prompt instruction** -- asserted by (1): the product is gone, not
   mentioned.

The case that matters most is not any of the three. It is the household with a
declared allergy and a pantry Open Food Facts has never documented: "no data" and
"no allergen" have to stay different statements all the way from the generated
column to the sentence on screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Allergen,
    AllergenDataState,
    Household,
    PnnsMarker,
    Product,
    ProductSource,
    RecipeSuggestion,
    StorageKind,
)
from tests.api.conftest import MakeLocation
from tests.api.test_providers import add_config
from tests.api.test_recipes import use_provider_double
from tests.conftest import MakeHousehold, household_headers

SUGGEST_URL = "/v1/recipes/suggest"
MEMBERS_URL = "/v1/members"

#: The two ingredients the provider double answers with. Every stock below is
#: built to make them resolve or not resolve on purpose.
DOUBLE_INGREDIENTS = ("Courgettes", "Crème")


async def add_product(
    session: AsyncSession,
    name: str,
    *,
    state: AllergenDataState = AllergenDataState.DECLARED,
    contains: tuple[Allergen, ...] = (),
    markers: tuple[PnnsMarker, ...] = (),
) -> Product:
    """A catalogue row with its allergen provenance stated, never defaulted.

    ``make_product`` in ``conftest`` leaves ``allergen_state`` at its column
    default of ``unknown``, which is correct for a scan and useless here: half
    these tests need a product that *declared* it has no peanuts, which is a
    different fact from one nobody has looked at.
    """
    product = Product(
        household_id=None,
        name=name,
        brand=None,
        source=ProductSource.OPEN_FOOD_FACTS,
        allergen_state=state,
        allergens_contains=list(contains),
        pnns_markers=list(markers),
    )
    session.add(product)
    await session.flush()
    return product


async def stock(
    api_client: httpx.AsyncClient,
    household: Household,
    location_id: uuid.UUID,
    product: Product,
    *,
    expires_on: str = "2099-01-31",
) -> str:
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "location_id": str(location_id),
            "amount": "200",
            "unit": "g",
            "expires_on": expires_on,
            "expiry_kind": "use_by",
            "source": "manual",
        },
    )
    assert created.status_code == 201, created.text
    item_id: str = created.json()["id"]
    return item_id


async def add_member(
    api_client: httpx.AsyncClient, household: Household, **overrides: object
) -> str:
    payload: dict[str, object] = {
        "display_name": "Camille",
        "age_band": "adult",
        "diet": "omnivore",
        "allergens": [],
        "free_text_restrictions": "",
        "infant_texture": None,
    }
    payload.update(overrides)
    created = await api_client.post(MEMBERS_URL, headers=household_headers(household), json=payload)
    assert created.status_code == 201, created.text
    member_id: str = created.json()["id"]
    return member_id


async def snapshot_items(session: AsyncSession, household: Household) -> list[str]:
    row = (
        await session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).first()
    assert row is not None
    return [item["name"] for item in row.stock_snapshot["items"]]


# --------------------------------------------------------------------------- #
# 1. Before the call
# --------------------------------------------------------------------------- #


async def test_a_product_carrying_a_declared_allergen_is_never_sent(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The model cannot propose what it has not seen.

    Asserted on the persisted snapshot rather than on the answer, because the
    answer comes from a double that would happily ignore any instruction. The
    snapshot is what actually crossed the boundary.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    risky = await add_product(db_session, "Cacahuètes grillées", contains=(Allergen.PEANUTS,))
    await stock(api_client, household, location.id, risky)
    member = await add_member(api_client, household, allergens=["peanuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assert response.status_code == 200, response.text
    assert set(await snapshot_items(db_session, household)) == set(DOUBLE_INGREDIENTS)
    assert response.json()["applied_constraints"]["products_withheld"] == 1


async def test_an_undocumented_product_is_withheld_from_a_household_with_an_allergy(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The property the whole feature rests on, end to end.

    Nobody has documented "Farine de blé". Its ``allergens_risk`` therefore
    carries all fourteen, and the exclusion query withholds it without the
    service having had to think about ``unknown`` at all. A screen written
    against ``allergens_contains`` would send it.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    silent = await add_product(db_session, "Farine de blé", state=AllergenDataState.UNKNOWN)
    await stock(api_client, household, location.id, silent)
    member = await add_member(api_client, household, allergens=["peanuts"])
    use_provider_double(api_app, household, "nominal")

    await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assert set(await snapshot_items(db_session, household)) == set(DOUBLE_INGREDIENTS), (
        "a product with no allergen data was sent to the model for a household "
        "that declared an allergy: 'unknown' is being read as 'contains nothing'"
    )


async def test_an_undocumented_product_is_still_sent_when_nobody_declared_an_allergy(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Over-exclusion is not free, and it is not paid by households with no allergy.

    Most fresh products on a wiki carry no allergen data. Withholding them from
    everybody would empty the average pantry and make the feature a regression
    for the people it does not protect.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    silent = await add_product(db_session, "Farine de blé", state=AllergenDataState.UNKNOWN)
    await stock(api_client, household, location.id, silent)
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )

    assert response.status_code == 200, response.text
    assert await snapshot_items(db_session, household) == ["Farine de blé"]

    assert response.json()["applied_constraints"]["products_unverified"] == 1


async def test_a_vegetarian_never_sees_the_meat_in_the_freezer(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household, kind=StorageKind.FREEZER)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    steak = await add_product(db_session, "Steak haché", markers=(PnnsMarker.RED_MEAT,))
    await stock(api_client, household, location.id, steak)
    member = await add_member(api_client, household, diet="vegetarian")
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assert response.status_code == 200, response.text
    assert set(await snapshot_items(db_session, household)) == set(DOUBLE_INGREDIENTS)


# --------------------------------------------------------------------------- #
# 2. After the call
# --------------------------------------------------------------------------- #


async def test_an_ingredient_that_cannot_be_matched_back_discards_the_suggestion(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The second control, and the reason it is a 502 rather than a partial answer.

    The double asks for courgettes this household does not own. Under a hard
    constraint, an ingredient this application cannot name is one it cannot vouch
    for -- so the suggestion is thrown away whole rather than served with a line
    removed or a caveat attached (ADR-0009).
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    cream = await add_product(db_session, "Crème fraîche épaisse")
    await stock(api_client, household, location.id, cream)
    member = await add_member(api_client, household, allergens=["peanuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assert response.status_code == 502, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/constraint-violation-detected"


async def test_a_suggestion_whose_ingredients_all_resolve_is_served(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The check has to be passable, or the feature is a denial of service.

    Same household, same allergy, same model answer -- with both ingredients
    actually in the fridge. If this test ever starts failing alongside the one
    above passing, the resolver has become strict enough to refuse everything.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    member = await add_member(api_client, household, allergens=["peanuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assert response.status_code == 200, response.text
    recipe = response.json()["suggestions"][0]
    assert [item["in_stock"] for item in recipe["ingredients"]] == [True, True]


async def test_an_unmatched_ingredient_is_tolerated_when_no_hard_constraint_applies(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Strictness is proportional to what is at stake, and here nothing is.

    A household of omnivorous adults with no declared allergy has nothing for the
    post-call check to protect. Refusing a recipe because the model wrote
    "quelques herbes" would cost every suggestion and prevent no harm.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    await stock(api_client, household, location.id, await add_product(db_session, "Crème"))
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )

    assert response.status_code == 200, response.text
    flags = {
        item["name"]: item["in_stock"] for item in response.json()["suggestions"][0]["ingredients"]
    }
    assert flags == {"Courgettes": False, "Crème": True}


# --------------------------------------------------------------------------- #
# When the constraints leave nothing
# --------------------------------------------------------------------------- #


async def test_an_inventory_emptied_by_the_constraints_is_a_409_and_says_why(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Not an execution failure (contract 3): the stock and the provider are fine.

    The interface has to be able to explain a blank screen by naming the *class*
    of constraint that emptied the pantry, without naming who at the table
    carries it.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    peanuts = await add_product(db_session, "Cacahuètes grillées", contains=(Allergen.PEANUTS,))
    await stock(api_client, household, location.id, peanuts)
    member = await add_member(api_client, household, allergens=["peanuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"member_ids": [member]}
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"] == "https://chaudron.dev/problems/no-suggestion-within-constraints"
    assert body["reasons"] == ["allergen"]
    assert body["products_withheld"] == 1
    # The class of constraint, never the allergen and never the person.
    assert "peanut" not in response.text.lower()
    assert "camille" not in response.text.lower()


async def test_a_member_from_another_household_is_a_422(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A partial selection is refused rather than silently narrowed.

    Quietly cooking for the members it recognised would drop somebody's allergy
    without anybody being told.
    """
    household = await make_household()
    stranger = await make_household()
    await add_config(db_session, household)
    elsewhere = await add_member(api_client, stranger, allergens=["nuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"member_ids": [elsewhere]}
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/member-not-in-household"


# --------------------------------------------------------------------------- #
# What the answer says about itself
# --------------------------------------------------------------------------- #


async def test_the_applied_constraints_report_the_union_and_the_counts(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    vegan = await add_member(api_client, household, display_name="Alix", diet="vegan")
    allergic = await add_member(
        api_client, household, display_name="Camille", allergens=["nuts", "celery"]
    )
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [vegan, allergic], "max_suggestions": 1},
    )

    assert response.status_code == 200, response.text
    applied = response.json()["applied_constraints"]
    assert {member["display_name"] for member in applied["members"]} == {"Alix", "Camille"}
    assert applied["diet"] == "vegan", "the strictest diet at the table wins"
    assert applied["excluded_allergens"] == ["celery", "nuts"]
    assert applied["age_bands"] == ["adult"]
    assert applied["products_withheld"] == 0
    assert applied["infant_texture"] is None


async def test_the_allergen_statement_is_negative_situated_and_server_written(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The sentence a household reads, and the sentence it must never read.

    "Aucun allergène déclaré parmi les produits identifiés" is a statement about
    records. "Sans fruits à coque" is a statement about food, and this
    application is not in a position to make it (ADR-0009).
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    member = await add_member(api_client, household, allergens=["nuts"])
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"member_ids": [member], "max_suggestions": 1},
    )

    assessment = response.json()["suggestions"][0]["allergen_assessment"]
    assert assessment["statement"].startswith("Aucun allergène déclaré parmi les produits")
    assert "sans" not in assessment["statement"].lower()
    # Always present, including at zero: absent and zero read identically.
    assert assessment["unverified_product_count"] == 0
    assert assessment["declared_clear_of"] == ["nuts"]


async def test_the_statement_counts_the_products_nobody_documented(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Without the count, "no allergen declared" reads as a clean bill of health."""
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    await stock(
        api_client,
        household,
        location.id,
        await add_product(db_session, "Courgettes", state=AllergenDataState.UNKNOWN),
    )
    await stock(api_client, household, location.id, await add_product(db_session, "Crème"))
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )

    assessment = response.json()["suggestions"][0]["allergen_assessment"]
    assert assessment["unverified_product_count"] == 1
    assert "1 produit n'a pas de données allergènes." in assessment["statement"]


async def test_a_recipe_reports_the_urgent_products_it_left_behind(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Contract 5's honest field. Silence here reads as a deliberate choice.

    The yoghurt expires tomorrow and no suggestion touches it. Reporting only
    what a recipe *saved* would make a list of recipes that all ignore it look
    like considered advice.
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    await stock(
        api_client,
        household,
        location.id,
        await add_product(db_session, "Yaourt nature brassé"),
        expires_on=tomorrow,
    )
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )

    pressure = response.json()["suggestions"][0]["expiry_pressure"]
    assert pressure["urgent_items"] == []
    assert pressure["urgent_items_left_unused"] == 1
    assert response.json()["suggestions"][0]["uses_expiring_soon"] is False
    assert response.json()["balance"]["note"] is not None


@pytest.mark.parametrize("mode", ["weekly", "off"])
async def test_the_balance_is_returned_or_switched_off_but_never_faked(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    mode: str,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"max_suggestions": 1, "balance_mode": mode},
    )

    balance = response.json()["balance"]
    if mode == "off":
        assert balance is None
    else:
        assert balance is not None
        assert balance["reference"] == "pnns-2019"
        assert balance["window_days"] == 7
        assert balance["uncategorised_product_count"] == 0


async def test_the_meal_temperature_is_a_preference_and_the_answer_says_so(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Contract 4ter. Asking for cold cannot filter anything, and does not.

    The double declares nothing about its recipe, so ``preparation`` comes back
    with three nulls -- which is the state the interface renders as "not stated"
    rather than inventing a default of "served cold".
    """
    household = await make_household()
    await add_config(db_session, household)
    location = await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, location.id, await add_product(db_session, name))
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"max_suggestions": 1, "meal_temperature": "cold"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["suggestions"][0]["preparation"] == {
        "serving_temperature": None,
        "requires_cooking": None,
        "requires_oven": None,
    }
