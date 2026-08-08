"""Domain boundary: the values that cross it, the failures it can raise, and the
interfaces infrastructure must satisfy.

``services`` depends on this module and on nothing else from the outside world.
That is what keeps a use case testable with three dataclasses instead of a
database, and what lets the same service run against Open Food Facts, a local
dump or a stub without knowing which.

The enumerations are imported from :mod:`chaudron.domain.models` rather than
redeclared: they are the domain vocabulary, and a second copy would drift from
the database the day someone adds a member to one of them.

LLM ports are deliberately absent -- they live in ``domain/llm_ports.py``
(ADR-0005).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from chaudron.domain.models import (
    Allergen,
    AllergenDataState,
    ExpiryDateKind,
    FoodFamily,
    PnnsMarker,
    ProductSource,
    QuantityDimension,
    StockEntrySource,
    StockMovementKind,
    StorageKind,
)

__all__ = [
    "GTIN_STORAGE_LENGTH",
    "UNSET",
    "Allergen",
    "AllergenDataState",
    "BarcodeNotFoundError",
    "CatalogRecord",
    "DomainError",
    "ExpiryDateInconsistentError",
    "ExpiryDateKind",
    "FoodFamily",
    "HouseholdRepository",
    "InvalidBarcodeError",
    "InvalidQuantityError",
    "InventoryConflictError",
    "InventoryFilter",
    "InventoryItem",
    "InventoryItemNotFoundError",
    "InventoryPage",
    "InventoryRepository",
    "LocationDraft",
    "LocationNameTakenError",
    "LocationNotFoundError",
    "LocationRepository",
    "LocationSummary",
    "LocationView",
    "LotDraft",
    "LotState",
    "LotUpdate",
    "Maybe",
    "PnnsMarker",
    "ProductCatalog",
    "ProductCatalogUnavailableError",
    "ProductDraft",
    "ProductNotFoundError",
    "ProductRepository",
    "ProductSource",
    "ProductView",
    "QuantityDimension",
    "RetailerInternalBarcodeError",
    "StockEntrySource",
    "StockMovementKind",
    "StorageKind",
    "UnitInfo",
    "UnitRegistry",
    "UnknownUnitError",
    "UnsetType",
    "display_gtin",
    "is_retailer_internal",
    "normalize_gtin",
]


# --------------------------------------------------------------------------- #
# Partial updates
# --------------------------------------------------------------------------- #


class UnsetType:
    """Sentinel distinguishing "absent from the request" from "explicitly null".

    ``PATCH {"expires_on": null}`` clears the date; ``PATCH {}`` leaves it alone.
    Both are ``None`` without this type, and the difference is the whole point of
    the verb.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = UnsetType()

type Maybe[T] = T | UnsetType


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class DomainError(Exception):
    """Base class for every failure the domain can name.

    The API layer maps these onto RFC 9457 problems; anything not deriving from
    it is an unexpected fault and becomes an opaque 500.
    """


class HouseholdNotFoundError(DomainError):
    def __init__(self, household_id: uuid.UUID) -> None:
        super().__init__(f"no household with id {household_id}")
        self.household_id = household_id


class LocationNotFoundError(DomainError):
    def __init__(self, location_id: uuid.UUID) -> None:
        super().__init__(f"no storage location with id {location_id}")
        self.location_id = location_id


class LocationNameTakenError(DomainError):
    """An active location of this household already carries that name.

    The uniqueness is the partial index ``uq_storage_location_name`` -- per
    household, case-insensitive, and only over rows that are not archived. Two
    active "Frigo" is a typo the user wants told about; one active and one
    archived is history, and stays legal.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"a storage location named {name!r} already exists in this household")
        self.name = name


class ProductNotFoundError(DomainError):
    def __init__(self, product_id: uuid.UUID) -> None:
        super().__init__(f"no product with id {product_id}")
        self.product_id = product_id


class InventoryItemNotFoundError(DomainError):
    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(f"no inventory item with id {item_id}")
        self.item_id = item_id


class UnknownUnitError(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(f"unknown unit {code!r}")
        self.code = code


class InvalidQuantityError(DomainError):
    """A quantity that the schema would refuse, caught before it reaches it."""


class ExpiryDateInconsistentError(DomainError):
    """An expiry date without a kind, or a kind of ``unknown`` carrying a date."""


class MissingProductReferenceError(DomainError):
    def __init__(self) -> None:
        super().__init__("provide exactly one of product_id or product")


class UnsupportedRemovalReasonError(DomainError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"unknown removal reason {reason!r}")
        self.reason = reason


class InventoryConflictError(DomainError):
    """Two concurrent writes produced the same merge key.

    The database arbitrates (``uq_inventory_lot_merge_key``); the loser is told
    to retry rather than being served a fabricated result.
    """


# -- Dietary constraints (ADR-0009, contract v1.1) ------------------------- #
#
# None of the four below interpolates anything about a person. An eater's
# allergies, diet and age band are health data (GDPR article 9); a message that
# quoted them would put them in an HTTP body, a log line and a support ticket at
# once. The only identifier any of them carries is an opaque row id the client
# already sent.


class MemberNotFoundError(DomainError):
    def __init__(self, member_id: uuid.UUID) -> None:
        super().__init__(f"no household member with id {member_id}")
        self.member_id = member_id


class MemberNotInHouseholdError(DomainError):
    """A ``member_id`` that belongs to another household, or to nobody."""

    def __init__(self, member_id: uuid.UUID) -> None:
        super().__init__(f"member {member_id} does not belong to this household")
        self.member_id = member_id


class InfantTextureInconsistentError(DomainError):
    """A texture outside an infant band, or an infant band without one.

    The database refuses it too (``ck_household_person_infant_texture_band``).
    Raised here so the client gets the contract's 422 with a sentence it can
    show, rather than a constraint name.
    """


class NoSuggestionWithinConstraintsError(DomainError):
    """The screen emptied the inventory. Not an execution failure.

    Carries the *reasons* that emptied it -- allergen, diet, infant rule -- and
    the counts, so the interface can explain a blank screen without naming who
    at the table caused it.
    """

    def __init__(self, *, reasons: tuple[str, ...], withheld: int) -> None:
        super().__init__("no product survived the dietary screen")
        self.reasons = reasons
        self.withheld = withheld


class ConstraintViolationDetectedError(DomainError):
    """The post-call check refused every suggestion the model produced.

    Thrown away rather than served with a warning: a suggestion whose
    ingredients this application cannot name is one it cannot vouch for, and
    ADR-0009 forbids returning it partially.
    """

    def __init__(self, *, examined: int) -> None:
        super().__init__("every suggestion failed the post-generation check")
        self.examined = examined


class RecipeSuggestionNotFoundError(DomainError):
    """No suggestion with this identifier belongs to this household.

    One error for "never existed" and "belongs to somebody else" alike. Two
    distinguishable answers would turn the feedback endpoint into an oracle for
    the existence of another household's rows, which is the same reasoning that
    made ``get_household_id`` answer 401 identically in all three of its failure
    cases (audit AUD-013).
    """

    def __init__(self, suggestion_id: uuid.UUID) -> None:
        super().__init__(f"no recipe suggestion with id {suggestion_id}")
        self.suggestion_id = suggestion_id


class BarcodeNotFoundError(DomainError):
    def __init__(self, gtin: str) -> None:
        super().__init__(f"no product matches GTIN {gtin}")
        self.gtin = gtin


class RetailerInternalBarcodeError(DomainError):
    """A variable-weight in-store code (prefix ``02`` or ``20``--``29``).

    It encodes a price, changes on every purchase, and will never appear in a
    public reference. Resolving it is not slow, it is impossible (ADR-0008).
    """

    def __init__(self, gtin: str) -> None:
        super().__init__(f"{gtin} is a retailer-internal barcode")
        self.gtin = gtin


class ProductCatalogUnavailableError(DomainError):
    """The external catalogue is unreachable, or has rate-limited us.

    ``retry_after`` is seconds, and is surfaced verbatim as the HTTP header: the
    client is the only party able to slow down, so it needs the number.
    """

    def __init__(self, reason: str, retry_after: int) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Values crossing the boundary
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class UnitInfo:
    """One row of the ``unit`` reference table, as the domain needs it."""

    code: str
    dimension: QuantityDimension
    factor_to_canonical: Decimal
    symbol: str


@dataclass(frozen=True, slots=True)
class ProductView:
    id: uuid.UUID
    name: str
    brand: str | None
    gtin: str | None
    image_url: str | None
    default_unit_code: str | None = None
    category_tag: str | None = None
    is_public: bool = True


@dataclass(frozen=True, slots=True)
class LocationView:
    id: uuid.UUID
    name: str
    kind: StorageKind


@dataclass(frozen=True, slots=True)
class LocationSummary:
    id: uuid.UUID
    name: str
    kind: StorageKind
    item_count: int


@dataclass(frozen=True, slots=True)
class LocationDraft:
    """A storage location about to be created. Name already trimmed by the API."""

    name: str
    kind: StorageKind


@dataclass(frozen=True, slots=True)
class InventoryItem:
    id: uuid.UUID
    product: ProductView
    location: LocationView | None
    quantity_value: Decimal
    quantity_unit_code: str
    best_before: date | None
    date_kind: ExpiryDateKind
    opened_at: date | None
    #: When this lot actually becomes unfit -- ``min(best_before, opened_at +
    #: shelf_life_guideline.opened_days)``, computed by the query
    #: (``domain/shelf_life.py``). Kept beside ``best_before`` rather than
    #: replacing it: the printed date is what a human read off the packaging and
    #: what a ``PATCH`` round-trips, and overwriting it with a derived value
    #: would make the application look like it had invented a label.
    effective_expiry: date | None
    entry_source: StockEntrySource
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InventoryPage:
    total: int
    items: Sequence[InventoryItem]


@dataclass(frozen=True, slots=True)
class InventoryFilter:
    location_id: uuid.UUID | None = None
    query: str | None = None
    expiring_within_days: int | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class LotDraft:
    """A lot ready to be written: every derived value is already computed.

    Conversion to the canonical unit happens in the service layer, on write and
    exactly once (``docs/data-model.md`` section 6.1). A repository that
    recomputed it would be a second implementation of the same rule.
    """

    product_id: uuid.UUID
    storage_location_id: uuid.UUID | None
    quantity_value: Decimal
    quantity_unit_code: str
    quantity_dimension: QuantityDimension
    quantity_canonical: Decimal
    best_before: date | None
    date_kind: ExpiryDateKind
    opened_at: date | None
    entry_source: StockEntrySource
    #: The receipt line this lot was created from, when one was (contract 6ter).
    #: Written only on creation: when the draft merges into an existing lot the
    #: older row keeps its own provenance, because a lot that grew has more than
    #: one origin and this column can only hold one. The budget reads it to count
    #: what went into the cupboard *without* a receipt, so an approximation here
    #: would be an approximation in the one figure that exists to be honest.
    source_receipt_line_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LotState:
    """What a service needs to know about an existing lot to change it."""

    id: uuid.UUID
    #: The product this lot holds. Carried so that a removal can say *what* ran
    #: out without loading the display projection again (contract 6bis).
    product_id: uuid.UUID
    quantity_value: Decimal
    quantity_unit_code: str
    quantity_dimension: QuantityDimension
    quantity_canonical: Decimal
    storage_location_id: uuid.UUID | None
    best_before: date | None
    date_kind: ExpiryDateKind
    opened_at: date | None


@dataclass(frozen=True, slots=True)
class LotUpdate:
    """A partial change. ``UNSET`` leaves a column alone, ``None`` clears it."""

    quantity_value: Maybe[Decimal] = UNSET
    quantity_unit_code: Maybe[str] = UNSET
    quantity_dimension: Maybe[QuantityDimension] = UNSET
    quantity_canonical: Maybe[Decimal] = UNSET
    storage_location_id: Maybe[uuid.UUID | None] = UNSET
    best_before: Maybe[date | None] = UNSET
    date_kind: Maybe[ExpiryDateKind] = UNSET
    opened_at: Maybe[date | None] = UNSET


@dataclass(frozen=True, slots=True)
class ProductDraft:
    """A product created here rather than fetched. Private to its household."""

    name: str
    brand: str | None = None
    gtin: str | None = None
    default_unit_code: str | None = None
    #: How the row came about. ``manual`` is somebody typing a name; the receipt
    #: import passes ``receipt_import`` so that "where did this product come
    #: from?" stays answerable a year later, when the label is the only clue
    #: left as to why the catalogue holds ``pomme de terre nouvelle 1 kg``.
    source: ProductSource = ProductSource.MANUAL


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """A product as an external catalogue describes it.

    ``payload`` is kept verbatim: Open Food Facts changes its schema without
    notice, and a field we did not anticipate is unrecoverable once dropped.
    """

    gtin: str
    name: str
    brand: str | None = None
    image_url: str | None = None
    category_tag: str | None = None
    net_content_value: Decimal | None = None
    net_content_unit_code: str | None = None
    payload: dict[str, Any] | None = None

    # Normalisation of the catalogue's own taxonomies, done once at the
    # boundary. The defaults are the safe ones: a record built by a caller that
    # knows nothing about allergens declares nothing, and `UNKNOWN` withholds
    # the product from anybody with a declared allergy rather than clearing it.
    allergen_state: AllergenDataState = AllergenDataState.UNKNOWN
    allergens_contains: tuple[Allergen, ...] = ()
    allergens_may_contain: tuple[Allergen, ...] = ()
    #: Empty means *unresolved*, never "belongs to no food group".
    pnns_markers: tuple[PnnsMarker, ...] = ()
    #: ``None`` means unresolved, and unresolved means no expiry date is
    #: proposed at all -- see :class:`~chaudron.domain.models.FoodFamily`.
    food_family: FoodFamily | None = None


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


class HouseholdRepository(Protocol):
    async def exists(self, household_id: uuid.UUID) -> bool: ...


class UnitRegistry(Protocol):
    async def get(self, code: str) -> UnitInfo | None: ...


class LocationRepository(Protocol):
    async def list_with_counts(self, household_id: uuid.UUID) -> Sequence[LocationSummary]: ...

    async def exists(self, household_id: uuid.UUID, location_id: uuid.UUID) -> bool: ...

    async def create(self, household_id: uuid.UUID, draft: LocationDraft) -> LocationSummary:
        """Add an active location, or raise :class:`LocationNameTakenError`."""


class ProductRepository(Protocol):
    async def get_visible(
        self, household_id: uuid.UUID, product_id: uuid.UUID
    ) -> ProductView | None:
        """A product the household may reference: its own, or the public catalogue."""

    async def create_private(self, household_id: uuid.UUID, draft: ProductDraft) -> ProductView: ...

    async def find_cached(self, gtin: str) -> tuple[ProductView | None, datetime | None] | None:
        """Return the cached public entry for a GTIN, if any.

        The result distinguishes three states the caller must not conflate:
        ``None`` (never looked up), ``(None, synced_at)`` (looked up and absent
        -- the negative cache), and ``(product, synced_at)`` (a hit).
        """

    async def upsert_public(self, record: CatalogRecord) -> ProductView: ...

    async def remember_absent(self, gtin: str) -> None:
        """Record that the external catalogue does not know this GTIN."""


class InventoryRepository(Protocol):
    async def list_items(
        self, household_id: uuid.UUID, criteria: InventoryFilter
    ) -> InventoryPage: ...

    async def get_state(self, household_id: uuid.UUID, item_id: uuid.UUID) -> LotState | None:
        """Load the mutable state of a lot, locking it against concurrent writers."""

    async def get_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> InventoryItem | None:
        """Load the display projection of a lot."""

    async def find_mergeable(self, household_id: uuid.UUID, draft: LotDraft) -> LotState | None: ...

    async def add(self, household_id: uuid.UUID, draft: LotDraft) -> InventoryItem: ...

    async def apply(
        self, household_id: uuid.UUID, item_id: uuid.UUID, update: LotUpdate
    ) -> InventoryItem: ...

    async def deplete(self, household_id: uuid.UUID, item_id: uuid.UUID) -> None: ...

    async def record_movement(
        self,
        household_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        kind: StockMovementKind,
        delta_canonical: Decimal,
        dimension: QuantityDimension,
        reason: str | None,
    ) -> None:
        """Append to the ledger. Never called with a zero delta (``ck_delta_nonzero``)."""


# --------------------------------------------------------------------------- #
# Barcodes
# --------------------------------------------------------------------------- #
#
# Pure rules, kept in the domain because both sides need them: services decide
# whether to call out at all, infrastructure stores and echoes the result, and a
# second implementation of "what is a GTIN" would disagree with the first within
# a month.

#: Lengths a GTIN is allowed to have once the padding is removed.
_GTIN_DISPLAY_LENGTHS: Final = (8, 12, 13, 14)

#: Storage form: GTIN-14, left-padded. EAN-13 ``3033490004743`` and GTIN-14
#: ``03033490004743`` designate the same article, and storing both would defeat
#: ``uq_product_gtin_global`` (docs/data-model.md, section 4.5).
GTIN_STORAGE_LENGTH: Final = 14


class InvalidBarcodeError(DomainError):
    def __init__(self, raw: str) -> None:
        super().__init__("not a valid GTIN")
        self.raw = raw


def normalize_gtin(raw: str) -> str:
    """Return the GTIN-14 storage form, or raise :class:`InvalidBarcodeError`."""
    digits = "".join(character for character in raw if not character.isspace() and character != "-")
    if not digits.isdigit() or not 8 <= len(digits) <= GTIN_STORAGE_LENGTH:
        raise InvalidBarcodeError(raw)
    return digits.rjust(GTIN_STORAGE_LENGTH, "0")


def display_gtin(stored: str) -> str:
    """Undo the storage padding, back to the shortest standard length.

    The API contract shows barcodes the way they are printed on the packaging
    (``3033490004743``); returning the padded form would make every client
    compare strings that never match what its scanner produced.
    """
    trimmed = stored.lstrip("0")
    for length in _GTIN_DISPLAY_LENGTHS:
        if len(trimmed) <= length:
            return trimmed.rjust(length, "0")
    return stored  # pragma: no cover - unreachable for a normalised GTIN


def is_retailer_internal(gtin: str) -> bool:
    """True for a variable-weight in-store code (ADR-0008).

    Evaluated on the EAN-13 form, which is where the prefixes are defined; the
    storage padding would otherwise shift every one of them by a digit.
    """
    ean13 = normalize_gtin(gtin)[-13:]
    prefix = ean13[:2]
    return prefix == "02" or prefix.startswith("2")


class ProductCatalog(Protocol):
    async def lookup(self, gtin: str) -> CatalogRecord | None:
        """Resolve a barcode against the external reference.

        Returns ``None`` when the catalogue answered and does not know the code
        -- a fact worth caching. Raises :class:`ProductCatalogUnavailableError` when
        it did not answer, which is not.
        """
