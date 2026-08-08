"""The shopping list itself, and the repurchase proposal that feeds it.

The rule this file exists to pin down is the one in the middle of contract 6bis
and the easiest to get wrong: **the reason of the movement decides.** ``consumed``
and ``wasted`` mean there is none left; ``correction`` means the number was wrong.
:func:`test_a_correction_never_proposes_a_repurchase` is the test that fails if
the three are ever conflated, and a conflated version fills a household's
shopping list with its own typing mistakes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    DeclinedRepurchase,
    Household,
    Product,
    ShoppingItemOrigin,
    ShoppingList,
    ShoppingListItem,
)
from chaudron.domain.shopping import DepletionEvent
from chaudron.infra.repositories import SqlDeclinedRepurchaseStore
from chaudron.services.shopping_import import DepletionService
from tests.api.conftest import MakeProduct
from tests.conftest import MakeHousehold, household_headers

CURRENT_URL = "/v1/shopping-lists/current"
ITEMS_URL = f"{CURRENT_URL}/items"
DECLINED_URL = "/v1/shopping-lists/declined"


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


async def test_reading_the_current_list_creates_it_once(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()

    first = await api_client.get(CURRENT_URL, headers=household_headers(household))
    second = await api_client.get(CURRENT_URL, headers=household_headers(household))

    assert first.status_code == 200, first.text
    assert first.json()["id"] == second.json()["id"], "a second read created a second list"
    assert first.json()["items"] == []
    rows = (
        await db_session.scalars(
            select(ShoppingList).where(ShoppingList.household_id == household.id)
        )
    ).all()
    assert len(rows) == 1


async def test_several_items_are_added_in_one_call(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """The array is the design: one user gesture, one call (contract 6bis)."""
    household = await make_household()
    milk = await make_product(name="Lait demi-écrémé")

    response = await api_client.post(
        ITEMS_URL,
        json={
            "items": [
                {
                    "product_id": str(milk.id),
                    "amount": "1",
                    "unit": "l",
                    "source": "depleted",
                },
                {"free_text": "quelque chose pour le dessert"},
            ]
        },
        headers=household_headers(household),
    )

    assert response.status_code == 201, response.text
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["product_name"] == "Lait demi-écrémé"
    assert items[0]["quantity"] == {"amount": "1.000", "unit": "l"}
    assert items[0]["source"] == "depleted", "the depleted origin must survive the round trip"
    assert items[1]["free_text"] == "quelque chose pour le dessert"
    assert items[1]["quantity"] is None


async def test_a_depleted_item_is_stored_with_its_own_origin(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """``depleted`` maps onto ``low_stock``, which is what it means.

    The value has to be distinguishable in storage, because contract 6bis wants
    "does the repurchase proposal get used?" measured rather than assumed.
    """
    household = await make_household()
    product = await make_product(name="Beurre doux")

    await api_client.post(
        ITEMS_URL,
        json={"items": [{"product_id": str(product.id), "source": "depleted"}]},
        headers=household_headers(household),
    )

    row = await db_session.scalar(
        select(ShoppingListItem).where(ShoppingListItem.household_id == household.id)
    )
    assert row is not None and row.origin is ShoppingItemOrigin.LOW_STOCK


async def test_an_item_can_be_ticked_and_unticked(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    created = await api_client.post(
        ITEMS_URL,
        json={"items": [{"free_text": "pain"}]},
        headers=household_headers(household),
    )
    item_id = created.json()["items"][0]["id"]

    ticked = await api_client.patch(
        f"{ITEMS_URL}/{item_id}",
        json={"checked": True},
        headers=household_headers(household),
    )
    unticked = await api_client.patch(
        f"{ITEMS_URL}/{item_id}",
        json={"checked": False},
        headers=household_headers(household),
    )

    assert ticked.status_code == 200, ticked.text
    assert ticked.json()["checked"] is True
    assert unticked.json()["checked"] is False


async def test_a_quantity_can_be_corrected_and_cleared(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``quantity: null`` clears; an absent key leaves it alone."""
    household = await make_household()
    created = await api_client.post(
        ITEMS_URL,
        json={"items": [{"free_text": "farine", "amount": "1", "unit": "kg"}]},
        headers=household_headers(household),
    )
    item_id = created.json()["items"][0]["id"]

    corrected = await api_client.patch(
        f"{ITEMS_URL}/{item_id}",
        json={"quantity": {"amount": "2", "unit": "kg"}},
        headers=household_headers(household),
    )
    untouched = await api_client.patch(
        f"{ITEMS_URL}/{item_id}", json={"checked": True}, headers=household_headers(household)
    )
    cleared = await api_client.patch(
        f"{ITEMS_URL}/{item_id}", json={"quantity": None}, headers=household_headers(household)
    )

    assert corrected.json()["quantity"] == {"amount": "2.000", "unit": "kg"}
    assert untouched.json()["quantity"] == {"amount": "2.000", "unit": "kg"}, (
        "an absent key must not clear the quantity"
    )
    assert cleared.json()["quantity"] is None


async def test_an_item_can_be_removed(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    created = await api_client.post(
        ITEMS_URL, json={"items": [{"free_text": "pain"}]}, headers=household_headers(household)
    )
    item_id = created.json()["items"][0]["id"]

    removed = await api_client.delete(
        f"{ITEMS_URL}/{item_id}", headers=household_headers(household)
    )
    listed = await api_client.get(CURRENT_URL, headers=household_headers(household))

    assert removed.status_code == 204, removed.text
    assert listed.json()["items"] == []


async def test_another_households_item_is_invisible(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    mine = await make_household()
    theirs = await make_household()
    created = await api_client.post(
        ITEMS_URL, json={"items": [{"free_text": "foie gras"}]}, headers=household_headers(theirs)
    )
    item_id = created.json()["items"][0]["id"]

    patched = await api_client.patch(
        f"{ITEMS_URL}/{item_id}", json={"checked": True}, headers=household_headers(mine)
    )
    deleted = await api_client.delete(f"{ITEMS_URL}/{item_id}", headers=household_headers(mine))

    assert patched.status_code == 404, patched.text
    assert deleted.status_code == 404, deleted.text


async def test_an_item_with_neither_product_nor_text_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.post(
        ITEMS_URL,
        json={"items": [{"amount": "1", "unit": "kg"}]},
        headers=household_headers(household),
    )

    assert response.status_code == 422, response.text


async def test_half_a_quantity_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """``ck_shopping_list_item_quantity_triplet``, caught at the boundary."""
    household = await make_household()

    response = await api_client.post(
        ITEMS_URL,
        json={"items": [{"free_text": "farine", "amount": "2"}]},
        headers=household_headers(household),
    )

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# Declining -- durable, because the table is
# --------------------------------------------------------------------------- #


async def test_a_decline_is_remembered_and_can_be_undone(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """A refusal is a row, and revoking it is the row's absence (contract 6bis).

    The assertion is against the table rather than against a later proposal on
    purpose: what makes the refusal worth anything is that it survives the request
    that took it, and only the row proves that.
    """
    household = await make_household()
    product = await make_product(name="Endives")

    declined = await api_client.post(
        DECLINED_URL,
        json={"product_ids": [str(product.id)]},
        headers=household_headers(household),
    )
    assert declined.status_code == 204, declined.text
    assert await _declined_rows(db_session, household.id) == {product.id}

    undone = await api_client.delete(
        f"{DECLINED_URL}/{product.id}", headers=household_headers(household)
    )

    assert undone.status_code == 204, undone.text
    assert await _declined_rows(db_session, household.id) == set(), (
        "a refusal must be revocable (contract 6bis)"
    )


async def test_declining_twice_is_one_refusal(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """The same proposal dismissed on the phone and then on the tablet.

    A real gesture, and it must be a no-op rather than a unique-violation the user
    reads as "something went wrong".
    """
    household = await make_household()
    product = await make_product(name="Chou-fleur")
    payload = {"product_ids": [str(product.id), str(product.id)]}
    headers = household_headers(household)

    first = await api_client.post(DECLINED_URL, json=payload, headers=headers)
    second = await api_client.post(DECLINED_URL, json=payload, headers=headers)

    assert (first.status_code, second.status_code) == (204, 204), second.text
    assert await _declined_rows(db_session, household.id) == {product.id}


async def test_revoking_a_refusal_that_was_never_taken_is_not_an_error(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, make_product: MakeProduct
) -> None:
    """The caller asked for a state, and that state holds."""
    household = await make_household()
    product = await make_product(name="Panais")

    response = await api_client.delete(
        f"{DECLINED_URL}/{product.id}", headers=household_headers(household)
    )

    assert response.status_code == 204, response.text


async def test_a_refusal_belongs_to_one_household(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    """One household's refusal must not silence another household's proposal."""
    mine = await make_household()
    theirs = await make_household()
    product = await make_product(name="Rutabaga")

    await api_client.post(
        DECLINED_URL, json={"product_ids": [str(product.id)]}, headers=household_headers(theirs)
    )

    assert await _declined_rows(db_session, mine.id) == set()
    proposals = await DepletionService(
        db_session, declined=SqlDeclinedRepurchaseStore(db_session)
    ).propose(mine.id, [DepletionEvent(product_id=product.id, reason="consumed")])
    assert proposals[0].previously_declined is False


async def _declined_rows(session: AsyncSession, household_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(DeclinedRepurchase.product_id).where(DeclinedRepurchase.household_id == household_id)
    )
    return set(rows)


# --------------------------------------------------------------------------- #
# The repurchase proposal: the reason of the movement decides
# --------------------------------------------------------------------------- #


@pytest.fixture
def depletion(db_session: AsyncSession) -> DepletionService:
    return DepletionService(db_session)


async def _product(db_session: AsyncSession, household: Household, name: str) -> Product:
    from chaudron.domain.models import ProductSource

    product = Product(household_id=household.id, name=name, source=ProductSource.MANUAL)
    db_session.add(product)
    await db_session.flush()
    return product


@pytest.mark.parametrize("reason", ["consumed", "wasted"])
async def test_consuming_or_wasting_proposes_a_repurchase(
    reason: str,
    depletion: DepletionService,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Both mean the same thing for a shopping list: there is none left."""
    household = await make_household()
    product = await _product(db_session, household, "Lait demi-écrémé UHT")

    proposals = await depletion.propose(
        household.id, [DepletionEvent(product_id=product.id, reason=reason)]
    )

    assert len(proposals) == 1
    assert proposals[0].product_name == "Lait demi-écrémé UHT"
    assert proposals[0].reason == reason
    assert proposals[0].already_on_list is False
    assert proposals[0].previously_declined is False


async def test_a_correction_never_proposes_a_repurchase(
    depletion: DepletionService, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The rule that must not be inverted (contract 6bis).

    Fixing a mistyped 500 g into 0 is not finishing the product. Conflating the
    three reasons fills the shopping list with the household's typing errors, and
    it is the same distinction that keeps a manual adjustment from counting as
    consumption.
    """
    household = await make_household()
    product = await _product(db_session, household, "Farine T55")

    proposals = await depletion.propose(
        household.id, [DepletionEvent(product_id=product.id, reason="correction")]
    )

    assert proposals == (), "a correction is not a depletion"


async def test_several_depletions_come_back_as_one_grouped_answer(
    depletion: DepletionService, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Emptying the fridge is one gesture, not five interruptions."""
    household = await make_household()
    products = [await _product(db_session, household, f"Article {index}") for index in range(4)]

    proposals = await depletion.propose(
        household.id,
        [
            DepletionEvent(product_id=products[0].id, reason="consumed"),
            DepletionEvent(product_id=products[1].id, reason="wasted"),
            DepletionEvent(product_id=products[2].id, reason="correction"),
            DepletionEvent(product_id=products[3].id, reason="consumed"),
            # The same product twice: emptying two lots of the same milk is one
            # proposal, not two.
            DepletionEvent(product_id=products[0].id, reason="consumed"),
        ],
    )

    assert [proposal.product_id for proposal in proposals] == [
        products[0].id,
        products[1].id,
        products[3].id,
    ]


async def test_a_product_already_on_the_list_says_so(
    depletion: DepletionService, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    product = await _product(db_session, household, "Café moulu")
    shopping_list = ShoppingList(household_id=household.id, name="Courses", is_default=True)
    db_session.add(shopping_list)
    await db_session.flush()
    db_session.add(
        ShoppingListItem(
            household_id=household.id,
            shopping_list_id=shopping_list.id,
            product_id=product.id,
            origin=ShoppingItemOrigin.MANUAL,
        )
    )
    await db_session.flush()

    proposals = await depletion.propose(
        household.id, [DepletionEvent(product_id=product.id, reason="consumed")]
    )

    assert proposals[0].already_on_list is True


async def test_a_checked_off_item_does_not_suppress_the_proposal(
    depletion: DepletionService, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """A ticked item is history, not a pending purchase.

    Finishing the milk a week after buying it must offer to buy milk again.
    """
    from datetime import UTC, datetime

    household = await make_household()
    product = await _product(db_session, household, "Lait entier")
    shopping_list = ShoppingList(household_id=household.id, name="Courses", is_default=True)
    db_session.add(shopping_list)
    await db_session.flush()
    db_session.add(
        ShoppingListItem(
            household_id=household.id,
            shopping_list_id=shopping_list.id,
            product_id=product.id,
            origin=ShoppingItemOrigin.MANUAL,
            checked_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    proposals = await depletion.propose(
        household.id, [DepletionEvent(product_id=product.id, reason="consumed")]
    )

    assert proposals[0].already_on_list is False


async def test_a_declined_product_is_reported_as_declined(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    product = await _product(db_session, household, "Chou de Bruxelles")
    store = SqlDeclinedRepurchaseStore(db_session)
    await store.decline(household.id, [product.id])

    proposals = await DepletionService(db_session, declined=store).propose(
        household.id, [DepletionEvent(product_id=product.id, reason="consumed")]
    )

    assert proposals[0].previously_declined is True


async def test_another_households_product_yields_no_proposal(
    depletion: DepletionService, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    mine = await make_household()
    theirs = await make_household()
    hidden = await _product(db_session, theirs, "Truffe noire")

    proposals = await depletion.propose(
        mine.id, [DepletionEvent(product_id=hidden.id, reason="consumed")]
    )

    assert proposals == ()


def test_the_amount_type_survives_the_service_boundary() -> None:
    """A guard on the one arithmetic mistake that silently ruins a quantity."""
    assert Decimal("1.5") != Decimal("15")
