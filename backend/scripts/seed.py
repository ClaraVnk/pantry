"""Populate a demonstration household with credible French pantry data.

Run from ``backend/`` against a migrated database::

    uv run alembic upgrade head
    CHAUDRON_ENV=local uv run python scripts/seed.py

**It refuses to run anywhere but ``local``**, and :func:`main` is where that is
enforced. The reason is the account below: this script creates a sign-in with a
password written in the source of a public repository, into whatever
``CHAUDRON_DATABASE_URL`` names.

Every identifier is fixed, so the script is idempotent and so a screenshot, a
bookmark or a frontend fixture keeps working across re-seeds. Re-running updates
the demonstration rows in place and touches nothing else in the database.

That includes :data:`DEMO_HOUSEHOLD_ID`, and it stays fixed on purpose even
though it is a constant in a public repository. Two reasons, in that order.
Every other identifier here is a ``uuid5`` of a fixed namespace, so a household
drawn afresh per run would leave the people, locations, products and lots
pointing at the *previous* household -- the primary keys would already exist and
be attached elsewhere, and each run would deposit another orphaned demonstration
household that nothing ever deletes. And since authentication landed (audit
AUD-001) this value is not a credential: ``X-Household-Id`` selects among the
households a signed-in account already belongs to, so publishing it grants
nothing. The secret in this file is the password, and the environment refusal is
what guards it.

The data is deliberately realistic and in French: these rows end up in
screenshots and in the frontend's development loop, and ``foo``/``bar`` in an
inventory screen makes the product impossible to judge. Expiry dates are
computed relative to the run date -- a fixed date would show a demonstration
entirely made of expired food within a fortnight.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chaudron.config import ConfigurationError, get_settings
from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    BudgetPeriod,
    BudgetTarget,
    Diet,
    ExpiryDateKind,
    FoodFamily,
    Household,
    HouseholdMember,
    HouseholdPerson,
    InfantTexture,
    InventoryLot,
    MembershipRole,
    PnnsMarker,
    Product,
    ProductSource,
    QuantityDimension,
    ShoppingItemOrigin,
    ShoppingList,
    ShoppingListItem,
    StockEntrySource,
    StockMovement,
    StockMovementKind,
    StorageKind,
    StorageLocation,
    Unit,
    UserAccount,
)
from chaudron.infra.db import set_transaction_household
from chaudron.infra.passwords import Passwords

logger = logging.getLogger("chaudron.seed")

# A fixed UUIDv7-shaped identifier. Not random: screenshots, bookmarks and
# frontend fixtures all point at it, and a value that changed per run would make
# every one of them stale every morning.
DEMO_HOUSEHOLD_ID = uuid.UUID("01991000-0000-7000-8000-000000000001")
DEMO_USER_ID = uuid.UUID("01991000-0000-7000-8000-000000000002")

#: The demonstration account. It is printed at the end of the run, because a
#: household identifier is no longer something you can paste into a header --
#: since authentication landed, the way in is a sign-in form (audit AUD-001).
DEMO_EMAIL = "demo@chaudron.test"

#: **A development credential, and it looks like one.** Written in the source
#: rather than generated so the local loop is reproducible, and deliberately not
#: guessable-in-the-abstract: an instance that runs this script and is then
#: exposed has published this account, which is why :func:`main` refuses to run
#: anywhere but ``local``.
DEMO_PASSWORD = "chaudron-demo-password"  # noqa: S105 - a seeded local credential, by design

_PERSON_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000050")
_SHOPPING_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000060")
_LOCATION_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000100")
_PRODUCT_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000200")
_LOT_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000300")


def _stable_id(namespace: uuid.UUID, name: str) -> uuid.UUID:
    """A deterministic identifier for a named demonstration row."""
    return uuid.uuid5(namespace, name)


@dataclass(frozen=True, slots=True)
class SeedPerson:
    """One eater of the demonstration household.

    Health data even here: these are invented people, but the shape of the row
    is the shape of a real one, and a screenshot taken from this seed is a
    screenshot of the layout that will one day hold somebody's actual allergies.
    """

    key: str
    display_name: str
    age_band: AgeBand
    diet: Diet
    allergens: tuple[Allergen, ...] = ()
    infant_texture: InfantTexture | None = None
    free_text_restrictions: str | None = None
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class SeedLocation:
    key: str
    name: str
    kind: StorageKind
    sort_order: int


@dataclass(frozen=True, slots=True)
class SeedProduct:
    key: str
    name: str
    brand: str | None
    gtin: str | None
    default_unit: str
    category_tag: str | None = None
    #: Defaults to ``UNKNOWN`` on purpose, and most rows below leave it there.
    #: That is what a freshly scanned pantry actually looks like -- Open Food
    #: Facts is a wiki -- and a demonstration where every product is documented
    #: would hide the state the interface most needs to render honestly.
    allergen_state: AllergenDataState = AllergenDataState.UNKNOWN
    allergens_contains: tuple[Allergen, ...] = ()
    allergens_may_contain: tuple[Allergen, ...] = ()
    pnns_markers: tuple[PnnsMarker, ...] = ()
    food_family: FoodFamily | None = None


@dataclass(frozen=True, slots=True)
class SeedShoppingItem:
    """One line of the standing shopping list.

    Either a catalogue product or free text, never both -- the same rule the
    ``shopping_list_item`` check constraint enforces.
    """

    product_key: str | None
    label: str | None
    amount: str | None
    unit: str | None
    origin: ShoppingItemOrigin
    sort_order: int
    checked_hours_ago: int | None = None


@dataclass(frozen=True, slots=True)
class SeedLot:
    product_key: str
    location_key: str
    amount: str
    unit: str
    #: Days from today. ``None`` means no expiry date at all, which is the normal
    #: case for flour, rice and anything decanted into a jar.
    expires_in_days: int | None
    expiry_kind: ExpiryDateKind
    source: StockEntrySource = StockEntrySource.MANUAL
    opened_days_ago: int | None = None


# A household that exercises every branch of the constraint machinery at once,
# because a demonstration where everyone eats everything proves nothing. Camille
# carries a real allergy, Nino is an infant with an age band and a texture, and
# Sofia is vegetarian -- so the suggestion panel has something to withhold, a
# hard age rule to apply and a diet to union, on the first screenshot.
PEOPLE: tuple[SeedPerson, ...] = (
    SeedPerson(
        key="camille",
        display_name="Camille",
        age_band=AgeBand.ADULT,
        diet=Diet.OMNIVORE,
        allergens=(Allergen.NUTS, Allergen.CELERY),
        free_text_restrictions="pas de coriandre",
        sort_order=10,
    ),
    SeedPerson(
        key="sofia",
        display_name="Sofia",
        age_band=AgeBand.ADULT,
        diet=Diet.VEGETARIAN,
        sort_order=20,
    ),
    SeedPerson(
        key="lou",
        display_name="Lou",
        age_band=AgeBand.CHILD,
        diet=Diet.OMNIVORE,
        free_text_restrictions="n'aime pas les courgettes",
        sort_order=30,
    ),
    SeedPerson(
        key="nino",
        display_name="Nino",
        age_band=AgeBand.INFANT_9_12M,
        diet=Diet.OMNIVORE,
        # Nine to twelve months is where ANSES puts the move away from purées;
        # the texture is what makes the infant rules visible in the interface,
        # and the honey rule still applies to him for another few weeks.
        infant_texture=InfantTexture.SOFT_PIECES,
        sort_order=40,
    ),
)

LOCATIONS: tuple[SeedLocation, ...] = (
    SeedLocation("fridge", "Frigo", StorageKind.FRIDGE, 10),
    SeedLocation("freezer", "Congélateur", StorageKind.FREEZER, 20),
    SeedLocation("pantry", "Placard", StorageKind.PANTRY, 30),
)

_DECLARED = AllergenDataState.DECLARED

PRODUCTS: tuple[SeedProduct, ...] = (
    SeedProduct(
        "lait",
        "Lait demi-écrémé UHT",
        "Lactel",
        "3033490004743",
        "l",
        "en:milks",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.MILK,),
        pnns_markers=(PnnsMarker.DAIRY,),
        food_family=FoodFamily.FRESH_DAIRY,
    ),
    SeedProduct(
        "beurre",
        "Beurre doux 250 g",
        "Président",
        "3155250353518",
        "g",
        "en:butters",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.MILK,),
        pnns_markers=(PnnsMarker.ADDED_FATS,),
        food_family=FoodFamily.FRESH_DAIRY,
    ),
    SeedProduct(
        "yaourt",
        "Yaourt nature brassé",
        "Danone",
        "3033491401503",
        "piece",
        "en:yogurts",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.MILK,),
        pnns_markers=(PnnsMarker.DAIRY,),
        food_family=FoodFamily.FRESH_DAIRY,
    ),
    SeedProduct(
        "comte",
        "Comté affiné 12 mois",
        None,
        None,
        "g",
        "en:cheeses",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.MILK,),
        pnns_markers=(PnnsMarker.DAIRY,),
        food_family=FoodFamily.CHEESE,
    ),
    SeedProduct(
        "oeufs",
        "Œufs plein air calibre moyen",
        "Loué",
        "3268840001008",
        "piece",
        "en:chicken-eggs",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.EGGS,),
        pnns_markers=(PnnsMarker.EGGS,),
        food_family=FoodFamily.EGGS,
    ),
    SeedProduct(
        "poulet",
        "Filets de poulet fermier",
        None,
        None,
        "g",
        "en:chicken-breasts",
        pnns_markers=(PnnsMarker.POULTRY,),
        food_family=FoodFamily.FRESH_MEAT,
    ),
    SeedProduct(
        "saumon",
        "Pavés de saumon surgelés",
        "Findus",
        "3045140105502",
        "piece",
        "en:salmon-fillets",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.FISH,),
        pnns_markers=(PnnsMarker.FISH, PnnsMarker.OILY_FISH),
        food_family=FoodFamily.FROZEN,
    ),
    SeedProduct(
        "petits-pois",
        "Petits pois extra-fins surgelés",
        "Bonduelle",
        None,
        "g",
        "en:peas",
        pnns_markers=(PnnsMarker.FRUITS_VEGETABLES,),
        food_family=FoodFamily.FROZEN,
    ),
    SeedProduct(
        "courgettes",
        "Courgettes du marché",
        None,
        None,
        "g",
        "en:courgettes",
        pnns_markers=(PnnsMarker.FRUITS_VEGETABLES,),
        food_family=FoodFamily.PRODUCE,
    ),
    SeedProduct(
        "tomates",
        "Tomates grappe",
        None,
        None,
        "g",
        "en:tomatoes",
        pnns_markers=(PnnsMarker.FRUITS_VEGETABLES,),
        food_family=FoodFamily.PRODUCE,
    ),
    SeedProduct(
        "pommes",
        "Pommes Gala",
        None,
        None,
        "piece",
        "en:apples",
        pnns_markers=(PnnsMarker.FRUITS_VEGETABLES,),
        food_family=FoodFamily.PRODUCE,
    ),
    SeedProduct(
        "pates",
        "Penne rigate 500 g",
        "Barilla",
        "8076809513692",
        "g",
        "en:pastas",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.GLUTEN,),
        pnns_markers=(PnnsMarker.STARCHY_FOODS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    SeedProduct(
        "riz",
        "Riz basmati",
        "Taureau Ailé",
        "3016570100016",
        "g",
        "en:rices",
        pnns_markers=(PnnsMarker.STARCHY_FOODS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    SeedProduct(
        "farine",
        "Farine de blé T55",
        "Francine",
        "3242272371207",
        "g",
        "en:flours",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.GLUTEN,),
        pnns_markers=(PnnsMarker.STARCHY_FOODS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    SeedProduct(
        "huile",
        "Huile d'olive vierge extra",
        "Puget",
        "3021690043051",
        "ml",
        "en:olive-oils",
        allergen_state=_DECLARED,
        pnns_markers=(PnnsMarker.ADDED_FATS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    SeedProduct(
        "tomates-pelees",
        "Tomates pelées entières",
        "Mutti",
        "8005110120404",
        "g",
        "en:canned-tomatoes",
        allergen_state=_DECLARED,
        pnns_markers=(PnnsMarker.FRUITS_VEGETABLES,),
        food_family=FoodFamily.CANNED,
    ),
    # Deliberately left `unknown`: bought loose at the market, no barcode, and
    # therefore no allergen data at all. It is the product that must never be
    # described as free of anything, and the one that gets withheld from
    # Camille even though lentils contain none of the fourteen.
    SeedProduct(
        "lentilles",
        "Lentilles vertes du Puy",
        None,
        None,
        "g",
        "en:lentils",
        pnns_markers=(PnnsMarker.LEGUMES,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    # Camille's allergy has to have something to bite on, or the filter is
    # invisible in every screenshot.
    SeedProduct(
        "noix",
        "Cerneaux de noix",
        None,
        "3276651000012",
        "g",
        "en:walnuts",
        allergen_state=_DECLARED,
        allergens_contains=(Allergen.NUTS,),
        pnns_markers=(PnnsMarker.NUTS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
    # And so does the infant rule: honey before twelve months is the one
    # deterministic prohibition everybody recognises.
    SeedProduct(
        "miel",
        "Miel de fleurs",
        None,
        "3564700010013",
        "g",
        "en:honeys",
        allergen_state=_DECLARED,
        pnns_markers=(PnnsMarker.SUGARY_FOODS,),
        food_family=FoodFamily.DRY_GOODS,
    ),
)

# A believable mid-week fridge: a couple of things about to go off, a lot of
# things with room to spare, and a few staples with no date at all.
LOTS: tuple[SeedLot, ...] = (
    SeedLot("lait", "fridge", "1", "l", 2, ExpiryDateKind.USE_BY, StockEntrySource.BARCODE_SCAN),
    SeedLot("lait", "fridge", "1", "l", 9, ExpiryDateKind.USE_BY, StockEntrySource.BARCODE_SCAN),
    SeedLot("beurre", "fridge", "250", "g", 21, ExpiryDateKind.BEST_BEFORE),
    SeedLot(
        "yaourt",
        "fridge",
        "4",
        "piece",
        1,
        ExpiryDateKind.USE_BY,
        StockEntrySource.BARCODE_SCAN,
    ),
    SeedLot("comte", "fridge", "220", "g", 12, ExpiryDateKind.BEST_BEFORE, opened_days_ago=3),
    SeedLot("oeufs", "fridge", "6", "piece", 11, ExpiryDateKind.BEST_BEFORE),
    SeedLot("poulet", "fridge", "450", "g", 2, ExpiryDateKind.USE_BY),
    SeedLot("courgettes", "fridge", "600", "g", 4, ExpiryDateKind.BEST_BEFORE),
    SeedLot("tomates", "fridge", "500", "g", 3, ExpiryDateKind.BEST_BEFORE),
    SeedLot("saumon", "freezer", "4", "piece", 180, ExpiryDateKind.BEST_BEFORE),
    SeedLot("petits-pois", "freezer", "750", "g", 240, ExpiryDateKind.BEST_BEFORE),
    SeedLot("pommes", "pantry", "6", "piece", None, ExpiryDateKind.UNKNOWN),
    SeedLot(
        "pates",
        "pantry",
        "500",
        "g",
        400,
        ExpiryDateKind.BEST_BEFORE,
        StockEntrySource.BARCODE_SCAN,
    ),
    SeedLot("riz", "pantry", "1", "kg", 500, ExpiryDateKind.BEST_BEFORE),
    SeedLot("farine", "pantry", "1", "kg", 120, ExpiryDateKind.BEST_BEFORE),
    SeedLot("huile", "pantry", "750", "ml", 300, ExpiryDateKind.BEST_BEFORE),
    SeedLot(
        "tomates-pelees",
        "pantry",
        "800",
        "g",
        600,
        ExpiryDateKind.BEST_BEFORE,
        StockEntrySource.RECEIPT_IMPORT,
    ),
    SeedLot("lentilles", "pantry", "500", "g", None, ExpiryDateKind.UNKNOWN),
    SeedLot("noix", "pantry", "200", "g", 90, ExpiryDateKind.BEST_BEFORE),
    SeedLot("miel", "pantry", "250", "g", None, ExpiryDateKind.UNKNOWN),
)


# The list a household actually walks around with: a couple of things the stock
# screen noticed were running out, a couple typed in on the way to the door, and
# one already ticked off. An empty list makes the screen impossible to judge.
SHOPPING_ITEMS: tuple[SeedShoppingItem, ...] = (
    SeedShoppingItem("lait", None, "2", "l", ShoppingItemOrigin.LOW_STOCK, 10),
    SeedShoppingItem("yaourt", None, "8", "piece", ShoppingItemOrigin.LOW_STOCK, 20),
    SeedShoppingItem(None, "Pain de campagne", None, None, ShoppingItemOrigin.MANUAL, 30),
    SeedShoppingItem(None, "Pommes de terre", "2", "kg", ShoppingItemOrigin.MANUAL, 40),
    SeedShoppingItem(None, "Papier cuisson", None, None, ShoppingItemOrigin.MANUAL, 50),
    SeedShoppingItem(
        "huile", None, "1", "l", ShoppingItemOrigin.LOW_STOCK, 60, checked_hours_ago=3
    ),
)

#: A target, and no receipts to measure it against. That is the honest state of
#: this feature: the arithmetic is built, receipt import is not, so a demonstration
#: household shows a budget with nothing spent against it -- which is exactly what
#: a real instance shows today.
BUDGET_TARGET_AMOUNT = Decimal("650.00")


async def _load_units(session: AsyncSession) -> dict[str, Unit]:
    units = {unit.code: unit for unit in await session.scalars(select(Unit))}
    if not units:
        raise RuntimeError(
            "the unit reference table is empty: run `uv run alembic upgrade head` first"
        )
    return units


async def _seed_household(session: AsyncSession) -> Household:
    household = await session.get(Household, DEMO_HOUSEHOLD_ID)
    if household is None:
        household = Household(id=DEMO_HOUSEHOLD_ID)
        session.add(household)
    household.name = "Foyer de démonstration"
    household.timezone = "Europe/Zurich"
    household.default_currency = "CHF"
    household.archived_at = None
    await session.flush()
    return household


async def _seed_account(session: AsyncSession) -> UserAccount:
    """The account somebody actually signs in with, and its ownership of the demo.

    Before authentication existed this script printed an ``X-Household-Id`` to
    paste into a header. That header is no longer a credential -- it selects
    among the households the *signed-in* account belongs to -- so a seed that
    stopped at the household would leave a developer with data they cannot reach.

    The password is hashed with the same :class:`Passwords` the application uses,
    with the same parameters. A fixture that invented its own format would be a
    fixture the real code cannot read.
    """
    user = await session.get(UserAccount, DEMO_USER_ID)
    if user is None:
        user = UserAccount(id=DEMO_USER_ID)
        session.add(user)
    user.email = DEMO_EMAIL
    user.display_name = "Camille (démo)"
    user.password_hash = Passwords().hash(DEMO_PASSWORD)
    user.disabled_at = None
    await session.flush()

    # `household_member` is row-level-security protected and its WITH CHECK reads
    # the transaction-local tenant, so the household has to be posted first.
    await set_transaction_household(session, DEMO_HOUSEHOLD_ID)
    membership = await session.get(HouseholdMember, (DEMO_HOUSEHOLD_ID, DEMO_USER_ID))
    if membership is None:
        session.add(
            HouseholdMember(
                household_id=DEMO_HOUSEHOLD_ID,
                user_id=DEMO_USER_ID,
                role=MembershipRole.OWNER,
            )
        )
    else:
        membership.role = MembershipRole.OWNER
    await session.flush()
    return user


async def _seed_people(session: AsyncSession) -> dict[str, HouseholdPerson]:
    """The eaters. None of them has a ``user_account``, and that is the point.

    A demonstration household made of accounts would quietly suggest that
    cooking for somebody requires them to have a login -- which is exactly the
    modelling mistake ``household_person`` exists to avoid. Nino is nine months
    old.
    """
    people: dict[str, HouseholdPerson] = {}
    for spec in PEOPLE:
        person_id = _stable_id(_PERSON_NAMESPACE, spec.key)
        person = await session.get(HouseholdPerson, person_id)
        if person is None:
            person = HouseholdPerson(id=person_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(person)
        person.display_name = spec.display_name
        person.age_band = spec.age_band
        person.diet = spec.diet
        person.allergens = list(spec.allergens)
        person.infant_texture = spec.infant_texture
        person.free_text_restrictions = spec.free_text_restrictions
        person.sort_order = spec.sort_order
        people[spec.key] = person
    await session.flush()
    return people


async def _seed_locations(session: AsyncSession) -> dict[str, StorageLocation]:
    locations: dict[str, StorageLocation] = {}
    for spec in LOCATIONS:
        location_id = _stable_id(_LOCATION_NAMESPACE, spec.key)
        location = await session.get(StorageLocation, location_id)
        if location is None:
            location = StorageLocation(id=location_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(location)
        location.name = spec.name
        location.kind = spec.kind
        location.sort_order = spec.sort_order
        location.archived_at = None
        locations[spec.key] = location
    await session.flush()
    return locations


async def _seed_products(session: AsyncSession) -> dict[str, Product]:
    products: dict[str, Product] = {}
    for spec in PRODUCTS:
        product_id = _stable_id(_PRODUCT_NAMESPACE, spec.key)
        product = await session.get(Product, product_id)
        if product is None:
            product = Product(id=product_id)
            session.add(product)
        # Private to the demonstration household, even for products that carry a
        # real barcode: seeding them into the public catalogue would put
        # never-verified rows in the shared cache that real scans then read back.
        product.household_id = DEMO_HOUSEHOLD_ID
        product.name = spec.name
        product.brand = spec.brand
        product.gtin = None if spec.gtin is None else spec.gtin.rjust(14, "0")
        product.category_tag = spec.category_tag
        product.default_unit_code = spec.default_unit
        product.source = ProductSource.MANUAL
        product.allergen_state = spec.allergen_state
        product.allergens_contains = list(spec.allergens_contains)
        product.allergens_may_contain = list(spec.allergens_may_contain)
        product.pnns_markers = list(spec.pnns_markers)
        product.food_family = spec.food_family
        product.archived_at = None
        products[spec.key] = product
    await session.flush()
    return products


async def _seed_lots(
    session: AsyncSession,
    units: dict[str, Unit],
    products: dict[str, Product],
    locations: dict[str, StorageLocation],
) -> int:
    today = date.today()  # noqa: DTZ011 - a calendar date, matching the column type
    count = 0
    for index, spec in enumerate(LOTS):
        unit = units[spec.unit]
        amount = Decimal(spec.amount)
        canonical = (amount * unit.factor_to_canonical).quantize(Decimal("0.001"))
        lot_id = _stable_id(_LOT_NAMESPACE, f"{spec.product_key}:{spec.location_key}:{index}")

        lot = await session.get(InventoryLot, lot_id)
        is_new = lot is None
        if lot is None:
            lot = InventoryLot(id=lot_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(lot)

        lot.product_id = products[spec.product_key].id
        lot.storage_location_id = locations[spec.location_key].id
        lot.quantity_value = amount
        lot.quantity_unit_code = unit.code
        lot.quantity_dimension = QuantityDimension(unit.dimension)
        lot.quantity_canonical = canonical
        lot.initial_quantity_canonical = canonical
        lot.best_before = (
            None if spec.expires_in_days is None else today + timedelta(days=spec.expires_in_days)
        )
        lot.date_kind = spec.expiry_kind
        lot.opened_at = (
            None if spec.opened_days_ago is None else today - timedelta(days=spec.opened_days_ago)
        )
        lot.entry_source = spec.source
        lot.depleted_at = None

        if is_new:
            # The ledger is the historical truth; a lot that appeared without a
            # movement would make the reconciliation job report drift on a
            # freshly seeded database.
            session.add(
                StockMovement(
                    id=_stable_id(_LOT_NAMESPACE, f"intake:{lot_id}"),
                    household_id=DEMO_HOUSEHOLD_ID,
                    inventory_lot_id=lot_id,
                    kind=StockMovementKind.INTAKE,
                    delta_canonical=canonical,
                    quantity_dimension=QuantityDimension(unit.dimension),
                    occurred_at=datetime.now(UTC),
                    reason="seed",
                )
            )
        count += 1
    await session.flush()
    return count


async def _seed_shopping_list(
    session: AsyncSession,
    units: dict[str, Unit],
    products: dict[str, Product],
) -> int:
    """The household's one default list, and what is currently on it."""
    # The API creates the default list on first read, so an instance a developer
    # has already opened owns one under an identifier this script never chose.
    # Adopt it: a partial unique index allows exactly one default per household,
    # and insisting on a fixed id would make the second run fail.
    shopping_list = await session.scalar(
        select(ShoppingList).where(
            ShoppingList.household_id == DEMO_HOUSEHOLD_ID,
            ShoppingList.is_default.is_(True),
            ShoppingList.archived_at.is_(None),
        )
    )
    if shopping_list is None:
        shopping_list = ShoppingList(
            id=_stable_id(_SHOPPING_NAMESPACE, "default"), household_id=DEMO_HOUSEHOLD_ID
        )
        session.add(shopping_list)
    shopping_list.name = "Courses"
    shopping_list.is_default = True
    shopping_list.archived_at = None
    await session.flush()
    list_id = shopping_list.id

    now = datetime.now(UTC)
    for spec in SHOPPING_ITEMS:
        item_id = _stable_id(_SHOPPING_NAMESPACE, f"item:{spec.product_key or spec.label}")
        item = await session.get(ShoppingListItem, item_id)
        if item is None:
            item = ShoppingListItem(id=item_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(item)
        item.shopping_list_id = list_id
        item.product_id = None if spec.product_key is None else products[spec.product_key].id
        item.label = spec.label
        if spec.amount is None or spec.unit is None:
            item.quantity_value = None
            item.quantity_unit_code = None
            item.quantity_dimension = None
        else:
            unit = units[spec.unit]
            item.quantity_value = Decimal(spec.amount)
            item.quantity_unit_code = unit.code
            item.quantity_dimension = QuantityDimension(unit.dimension)
        item.origin = spec.origin
        item.sort_order = spec.sort_order
        item.checked_at = (
            None
            if spec.checked_hours_ago is None
            else now - timedelta(hours=spec.checked_hours_ago)
        )
    await session.flush()
    return len(SHOPPING_ITEMS)


async def _seed_budget_target(session: AsyncSession, household: Household) -> None:
    """A monthly target in the household's own currency.

    Nothing is spent against it, and that is not an oversight: ``budget_target``
    is measured against ``receipt.total_amount``, and receipt import does not
    exist yet. Seeding fake receipts would draw a screen the product cannot
    currently produce.
    """
    target_id = _stable_id(_SHOPPING_NAMESPACE, "budget:month")
    target = await session.get(BudgetTarget, target_id)
    if target is None:
        target = BudgetTarget(id=target_id, household_id=DEMO_HOUSEHOLD_ID)
        session.add(target)
    target.period = BudgetPeriod.MONTH
    target.amount = BUDGET_TARGET_AMOUNT
    target.currency = household.default_currency
    await session.flush()


async def seed(session: AsyncSession) -> None:
    household = await _seed_household(session)
    await _seed_account(session)
    units = await _load_units(session)
    people = await _seed_people(session)
    locations = await _seed_locations(session)
    products = await _seed_products(session)
    lots = await _seed_lots(session, units, products, locations)
    shopping_items = await _seed_shopping_list(session, units, products)
    await _seed_budget_target(session, household)
    # Counts only. The people are health records: their names, bands, diets and
    # allergies never reach a log line, here or anywhere else.
    logger.info(
        "seed_complete",
        extra={
            "household_id": str(DEMO_HOUSEHOLD_ID),
            "people": len(people),
            "locations": len(locations),
            "products": len(products),
            "lots": lots,
            "shopping_items": shopping_items,
        },
    )


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    # This script writes an account whose password is in the source file of a
    # public repository. Anywhere that is not a developer's own machine, that is a
    # published administrator credential on a live database -- so the refusal is
    # here, where the environment is actually known, rather than in a comment
    # nobody reads at the moment they paste a DSN into a terminal.
    #
    # `local` and nothing else. `ci` used to be allowed and no pipeline ever used
    # it (``.github/workflows/ci.yml`` runs migrations and pytest, never this
    # script); an environment that is permitted but unused is a door left open for
    # nobody. The database URL is not inspected: an operator who exported a
    # production DSN *and* set CHAUDRON_ENV=local has defeated a hostname check
    # too, and a check that guesses which hosts are "local" would refuse the
    # container-network names a normal development stack uses.
    if settings.env != "local":
        logger.error(
            "refusing to seed a %s instance. This script creates %s with a password "
            "written in the source of a public repository, and it writes to whatever "
            "CHAUDRON_DATABASE_URL points at. If this really is a throwaway "
            "development database, set CHAUDRON_ENV=local for the command: "
            "`CHAUDRON_ENV=local uv run python scripts/seed.py`",
            settings.env,
            DEMO_EMAIL,
        )
        return 2

    engine = create_async_engine(settings.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await seed(session)
    finally:
        await engine.dispose()

    # What a developer actually needs now: something to type into the sign-in
    # form. The household identifier is printed too, because it is still the
    # value that *selects* a household for an account that belongs to several --
    # but on its own it opens nothing.
    logger.info("sign in with: %s / %s", DEMO_EMAIL, DEMO_PASSWORD)
    logger.info("household (selector only, not a credential): %s", DEMO_HOUSEHOLD_ID)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
