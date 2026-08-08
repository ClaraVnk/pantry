"""``/v1/households`` -- the export a household may take away, and its erasure.

GDPR articles 15, 20 and 17. Three things are asserted here that a happy-path test
of either endpoint would not.

*The export is proved not to carry a credential by looking for the credential*,
not by reading the response model. A real AES-256-GCM ciphertext is stored, a real
machine token is minted through the API, and the serialised document is searched
for both in every encoding they could plausibly appear in. A schema-shaped
assertion would pass the day somebody adds a column.

*The erasure is proved by querying the tables in SQL*, table by table, with the
tenant identifier -- not by asking the ORM whether it still holds an object, which
it would answer from its identity map, and not by counting what the endpoint says
it deleted, which is the claim under test.

*The two neighbours that must survive are checked too.* An erasure that also took
the shared Open Food Facts catalogue, or the other household's stock, would pass
every "is it gone?" assertion in this file.
"""

from __future__ import annotations

import base64
import uuid
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import LargeBinary, Table
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    AgeBand,
    Allergen,
    Diet,
    ExpiryDateKind,
    Household,
    HouseholdPerson,
    InventoryLot,
    LlmProviderMode,
    MembershipRole,
    Product,
    ProductSource,
    QuantityDimension,
    Receipt,
    ReceiptStatus,
    StockEntrySource,
    StockMovement,
    StockMovementKind,
    StorageKind,
    StorageLocation,
    UserAccount,
)
from chaudron.services.privacy import TENANT_TABLES, WITHHELD_COLUMNS
from tests.api.test_providers import STORED_KEY, add_config
from tests.conftest import MakeHousehold, MakeMember, MakeUser, household_headers

pytestmark = pytest.mark.integration

EXPORT_URL: Final = "/v1/households/export"
ERASE_URL: Final = "/v1/households"

#: A column whose name ends in one of these can only hold credential material in
#: this schema. ``image_sha256`` is deliberately not covered: it is a digest of
#: bytes nobody stored (revision ``0012``) and reconstructs nothing.
_CREDENTIAL_SUFFIXES: Final = ("_ciphertext", "_hash", "_encryption_key_id")


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


async def seed_household(session: AsyncSession, household: Household) -> dict[str, Any]:
    """One row in each of the tables this file makes claims about.

    Not every tenant table: the ones below are the ones whose presence or absence
    the assertions turn on -- a health record, a movement ledger, a private
    catalogue entry, a reference to a shared one, and a provider configuration
    holding a real ciphertext.
    """
    location = StorageLocation(
        household_id=household.id, name="Congélateur", kind=StorageKind.FREEZER
    )
    session.add(location)

    public = Product(
        household_id=None,
        gtin=f"{uuid.uuid7().int % 10**13:013d}",
        name="Farine de blé T65",
        source=ProductSource.OPEN_FOOD_FACTS,
    )
    private = Product(
        household_id=household.id,
        name="Carottes du marché",
        source=ProductSource.MANUAL,
    )
    session.add_all([public, private])
    await session.flush()

    lot = InventoryLot(
        household_id=household.id,
        product_id=public.id,
        storage_location_id=location.id,
        quantity_value=Decimal(1000),
        quantity_unit_code="g",
        quantity_dimension=QuantityDimension.MASS,
        quantity_canonical=Decimal(1000),
        initial_quantity_canonical=Decimal(1000),
        best_before=None,
        date_kind=ExpiryDateKind.UNKNOWN,
        entry_source=StockEntrySource.MANUAL,
    )
    session.add(lot)
    await session.flush()

    session.add(
        StockMovement(
            household_id=household.id,
            inventory_lot_id=lot.id,
            kind=StockMovementKind.CONSUMPTION,
            delta_canonical=Decimal(-200),
            quantity_dimension=QuantityDimension.MASS,
        )
    )
    session.add(
        HouseholdPerson(
            household_id=household.id,
            display_name="Camille",
            age_band=AgeBand.ADULT,
            diet=Diet.OMNIVORE,
            allergens=[Allergen.PEANUTS],
            free_text_restrictions="intolérance au lactose",
        )
    )
    session.add(
        Receipt(
            household_id=household.id,
            image_sha256="a" * 64,
            status=ReceiptStatus.CONFIRMED,
            merchant_name="Coop",
        )
    )
    await session.flush()

    config = await add_config(
        session, household, mode=LlmProviderMode.BYOK, provider_code="anthropic", api_key=STORED_KEY
    )
    return {"public_product": public, "private_product": private, "config": config}


async def count_rows(session: AsyncSession, table: str, household_id: uuid.UUID) -> int:
    """Rows of *table* belonging to *household_id*, read in SQL.

    Deliberately raw SQL against the table name. The ORM would answer part of this
    from its identity map, and the question is what PostgreSQL holds.
    """
    result = await session.execute(
        sa.text(f"SELECT count(*) FROM {table} WHERE household_id = :household"),  # noqa: S608
        {"household": household_id},
    )
    return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Article 15 and 20: the export
# --------------------------------------------------------------------------- #


async def test_the_export_carries_the_household_and_its_rows(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    await seed_household(db_session, household)

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["export_version"] == 1
    assert body["household_id"] == str(household.id)
    assert body["household"]["name"] == household.name
    # Every tenant table has a key, present or empty: a recipient must be able to
    # tell "this household has no receipts" from "this export forgot receipts".
    assert set(body["tables"]) == {table.name for table in TENANT_TABLES}
    assert len(body["tables"]["inventory_lot"]) == 1
    assert len(body["tables"]["stock_movement"]) == 1
    # The health data is *in* the export. It is the household's, article 15 covers
    # it, and an export that quietly dropped the sensitive part would be the easy
    # mistake to make here.
    [person] = body["tables"]["household_person"]
    assert person["allergens"] == ["peanuts"]
    assert person["free_text_restrictions"] == "intolérance au lactose"


async def test_the_export_names_the_requesting_account_and_not_the_others(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
) -> None:
    """A co-member is a person, not a field of this household."""
    household = await make_household()
    other = await make_user(display_name="Autre membre", email="autre@example.test")
    await make_member(household, other, role=MembershipRole.MEMBER)

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    accounts = {entry["id"]: entry for entry in response.json()["accounts"]}
    assert set(accounts) == {str(signed_in_user.id), str(other.id)}
    assert "email" in accounts[str(signed_in_user.id)]
    assert accounts[str(other.id)]["display_name"] == "Autre membre"
    assert "email" not in accounts[str(other.id)]


async def test_the_export_separates_the_shared_catalogue_from_the_household_s_own(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """``product.household_id IS NULL`` is reference data, not this household's."""
    household = await make_household()
    seeded = await seed_household(db_session, household)

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))

    body = response.json()
    own = {row["id"] for row in body["tables"]["product"]}
    referenced = {row["id"] for row in body["referenced_public_products"]}
    assert own == {str(seeded["private_product"].id)}
    assert referenced == {str(seeded["public_product"].id)}
    # Reduced, and the raw upstream record is not in it.
    assert "off_payload" not in body["referenced_public_products"][0]


async def test_the_export_never_carries_a_credential(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Searched for in the document, not asserted from the response model.

    The provider key is stored as a real ciphertext and the machine token is minted
    through the real route, so both exist in the database in the form an attacker
    would want. What is asserted is that neither reaches the file, in any encoding
    it could have taken on the way -- while the four characters that let a
    household recognise its own key do.
    """
    household = await make_household()
    seeded = await seed_household(db_session, household)
    minted = await api_client.post(
        "/v1/tokens",
        json={"name": "Home Assistant", "scopes": ["inventory:read"], "expires_in_days": None},
        headers=household_headers(household),
    )
    assert minted.status_code == 201, minted.text

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))
    document = response.text

    ciphertext = seeded["config"].api_key_ciphertext
    assert ciphertext is not None
    for encoded in (
        ciphertext.hex(),
        base64.b64encode(ciphertext).decode(),
        repr(ciphertext),
        STORED_KEY,
        minted.json()["token"],
    ):
        assert encoded not in document, "the export carries credential material"

    body = response.json()
    [config] = body["tables"]["llm_provider_config"]
    assert "api_key_ciphertext" not in config
    assert "api_key_encryption_key_id" not in config
    assert config["api_key_last4"] == STORED_KEY[-4:]
    [token] = body["tables"]["machine_token"]
    assert "token_hash" not in token
    assert token["name"] == "Home Assistant"


async def test_the_export_says_what_it_withholds(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Article 15(1) is a right to know what is processed, omissions included."""
    household = await make_household()

    body = (await api_client.get(EXPORT_URL, headers=household_headers(household))).json()

    withheld = body["withheld"]
    assert "llm_provider_config.api_key_ciphertext" in withheld
    assert "household.calendar_feed_epoch" in withheld
    assert all(reason.strip() for reason in withheld.values())


async def test_a_viewer_may_not_export_the_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household(role=MembershipRole.VIEWER)

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))

    assert response.status_code == 403, response.text


async def test_a_member_may_not_export_the_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Owner-only, and ``routers/privacy.py`` argues what that costs a member."""
    household = await make_household(role=MembershipRole.MEMBER)

    response = await api_client.get(EXPORT_URL, headers=household_headers(household))

    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------- #
# Article 17: the erasure
# --------------------------------------------------------------------------- #


async def test_erasure_removes_every_row_of_the_household(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The claim, checked in SQL against every tenant table rather than sampled."""
    household = await make_household()
    await seed_household(db_session, household)
    before = {
        table.name: await count_rows(db_session, table.name, household.id)
        for table in TENANT_TABLES
    }
    assert sum(before.values()) > 5, f"the fixture seeded almost nothing: {before}"

    response = await api_client.delete(ERASE_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    after = {
        table.name: await count_rows(db_session, table.name, household.id)
        for table in TENANT_TABLES
    }
    assert after == dict.fromkeys(after, 0), f"rows survived the erasure: {after}"
    remaining = await db_session.execute(
        sa.text("SELECT count(*) FROM household WHERE id = :household"), {"household": household.id}
    )
    assert remaining.scalar_one() == 0

    body = response.json()
    assert body["household_id"] == str(household.id)
    # The receipt has to describe what was there, not what is there now.
    assert {table: count for table, count in body["rows_erased"].items() if count} == {
        table: count for table, count in before.items() if count
    } | {"household": 1}


async def test_erasure_leaves_the_shared_catalogue_and_the_accounts(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    signed_in_user: UserAccount,
) -> None:
    """What is not the household's to erase stays, and the receipt says so."""
    household = await make_household()
    seeded = await seed_household(db_session, household)

    response = await api_client.delete(ERASE_URL, headers=household_headers(household))
    assert response.status_code == 200, response.text

    public = await db_session.execute(
        sa.text("SELECT count(*) FROM product WHERE id = :id"),
        {"id": seeded["public_product"].id},
    )
    assert public.scalar_one() == 1, "the erasure took a shared catalogue entry with it"
    account = await db_session.execute(
        sa.text("SELECT count(*) FROM user_account WHERE id = :id"), {"id": signed_in_user.id}
    )
    assert account.scalar_one() == 1, "the erasure took an account that belongs to no household"
    assert set(response.json()["not_erased"]) == {"product (public catalogue)", "user_account"}


async def test_erasure_leaves_another_household_alone(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The assertion the cascade cannot make for itself."""
    erased = await make_household(name="À effacer")
    kept = await make_household(name="À garder")
    await seed_household(db_session, erased)
    await seed_household(db_session, kept)

    response = await api_client.delete(ERASE_URL, headers=household_headers(erased))
    assert response.status_code == 200, response.text

    assert await count_rows(db_session, "inventory_lot", kept.id) == 1
    assert await count_rows(db_session, "household_person", kept.id) == 1


async def test_erasure_is_refused_while_a_receipt_still_names_a_stored_image(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """A refusal, not a partial erasure reported as a complete one.

    ``receipt.image_object_key`` is NULL on every path this application takes
    (revision ``0012``), so this is a deployment that reintroduced retention. There
    is no object-storage client here to delete the object with, and deleting the
    row while the object survives is the non-compliance ``docs/security-model.md``
    section 8.5 describes.
    """
    household = await make_household()
    db_session.add(
        Receipt(
            household_id=household.id,
            image_sha256="b" * 64,
            status=ReceiptStatus.CONFIRMED,
            image_object_key="households/x/receipts/y.jpg",
        )
    )
    await db_session.flush()

    response = await api_client.delete(ERASE_URL, headers=household_headers(household))

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/erasure-incomplete-by-construction")
    assert response.json()["retained_images"] == 1
    # And it really refused: the household is still there.
    assert await count_rows(db_session, "receipt", household.id) == 1


async def test_a_member_may_not_erase_the_household(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Erasure destroys data belonging to every member, so it belongs to the owner."""
    household = await make_household(role=MembershipRole.MEMBER)
    await seed_household(db_session, household)

    response = await api_client.delete(ERASE_URL, headers=household_headers(household))

    assert response.status_code == 403, response.text
    assert await count_rows(db_session, "inventory_lot", household.id) == 1


async def test_erasure_is_logged_with_counts_and_nothing_else(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole audit record. There is no audit table, and migration 0017 says why.

    What matters is as much what is *not* in the record: the household name, the
    member, the merchant and the product are all in the database this call just
    emptied, and none of them belongs in a log line that outlives it.
    """
    household = await make_household(name="Maison Témoin")
    await seed_household(db_session, household)

    with caplog.at_level("INFO", logger="chaudron.services.privacy"):
        response = await api_client.delete(ERASE_URL, headers=household_headers(household))
    assert response.status_code == 200, response.text

    [record] = [entry for entry in caplog.records if entry.message == "household_erased"]
    counts = record.rows_erased  # type: ignore[attr-defined]
    assert counts["household"] == 1
    assert counts["inventory_lot"] == 1
    assert all(isinstance(value, int) for value in counts.values())
    assert "Maison Témoin" not in caplog.text
    assert "Camille" not in caplog.text
    assert "Coop" not in caplog.text


# --------------------------------------------------------------------------- #
# The deny-list, read against the model
# --------------------------------------------------------------------------- #
#
# The export is generated from ``Base.metadata``: every column of every tenant
# table goes out unless it is denied. That is what makes it complete, and it is
# also what makes a new secret column an incident. These three read the model and
# need no database, in the same spirit as
# ``tests/tenancy/test_schema_tenant_guard.py``.


def _tenant_table_ids() -> list[str]:
    return [table.name for table in TENANT_TABLES]


@pytest.mark.parametrize("table", TENANT_TABLES, ids=_tenant_table_ids())
def test_every_binary_column_of_a_tenant_table_is_withheld(table: Table) -> None:
    """``LargeBinary`` in this schema means "a ciphertext", both times it appears."""
    offenders = [
        column.name
        for column in table.c
        if isinstance(column.type, LargeBinary)
        and (table.name, column.name) not in WITHHELD_COLUMNS
    ]
    assert not offenders, (
        f"{table.name}: binary columns reaching the export: {offenders}. Every one of "
        f"them in this schema is credential material; add it to WITHHELD_COLUMNS in "
        f"services/privacy.py with the reason, or argue in review why a household "
        f"should receive raw bytes."
    )


@pytest.mark.parametrize("table", TENANT_TABLES, ids=_tenant_table_ids())
def test_every_credentially_named_column_is_withheld(table: Table) -> None:
    """A digest and a key identifier are not secrets, and neither is exported."""
    offenders = [
        column.name
        for column in table.c
        if column.name.endswith(_CREDENTIAL_SUFFIXES)
        and (table.name, column.name) not in WITHHELD_COLUMNS
    ]
    assert not offenders, (
        f"{table.name}: columns named like credentials and exported anyway: {offenders}. "
        f"An export is a file that gets mailed and backed up."
    )


def test_the_withheld_list_has_no_stale_entries() -> None:
    """An entry naming a column that no longer exists is an entry nobody reads."""
    declared = {(table.name, column.name) for table in TENANT_TABLES for column in table.c}
    stale = sorted(key for key in WITHHELD_COLUMNS if key not in declared)
    assert not stale, f"WITHHELD_COLUMNS names columns that do not exist any more: {stale}"
    assert all(reason.strip() for reason in WITHHELD_COLUMNS.values())


def test_the_deny_list_is_not_a_projection() -> None:
    """A guard on the guard: a deny-list that grew to cover the schema exports nothing.

    The export's completeness comes from denying the exception rather than listing
    the rule. If this number ever climbs, somebody has started using
    ``WITHHELD_COLUMNS`` as a way to shape the document, and the shape of an
    article 15 answer is not a matter of taste.
    """
    total = sum(len(table.c) for table in TENANT_TABLES)
    assert len(WITHHELD_COLUMNS) < total // 10, (
        f"{len(WITHHELD_COLUMNS)} of {total} columns are withheld. The deny-list is "
        f"for credentials; anything else a household holds, it may have."
    )


def test_the_export_covers_every_tenant_table() -> None:
    """The floor the whole file rests on: the walk found the schema.

    A ``TENANT_TABLES`` that resolved to nothing would make every assertion above
    vacuous and every export an empty document that looked well-formed.
    """
    assert len(TENANT_TABLES) >= 15, f"only {len(TENANT_TABLES)} tenant tables were found"
    names = {table.name for table in TENANT_TABLES}
    assert {"inventory_lot", "household_person", "receipt", "llm_provider_config"} <= names
