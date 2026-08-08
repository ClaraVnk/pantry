"""Request and response models. The strict boundary of the application.

Two contract details are load-bearing and easy to undo by accident.

``quantity.amount`` is a **string**. JSON numbers are IEEE 754 doubles in every
mainstream parser, and a stock quantity wrong by a factor of ten in a food
inventory is not a rounding detail.

Timestamps end in ``Z``. The contract shows ``2026-08-03T18:20:00Z``; Python's
``isoformat`` writes ``+00:00``, which is the same instant and a different
string, and clients do compare strings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from chaudron.domain.models import (
    AgeBand,
    Allergen,
    Diet,
    InfantTexture,
    MachineTokenScope,
    PnnsMarker,
    PnnsUnit,
)
from chaudron.domain.ports import ExpiryDateKind, StockEntrySource, StorageKind

# The service owns the rule; the boundary refuses what it would refuse anyway, so
# an out-of-range value is a 422 naming the field rather than an exception raised
# three layers down. Imported rather than restated: two constants that must agree
# and are written twice eventually do not.
from chaudron.services.tokens import MAX_EXPIRY_DAYS as MAX_TOKEN_EXPIRY_DAYS

#: The three entry sources the v1 contract exposes. The database knows five; the
#: other two belong to flows that are not in this slice.
ContractSource = Literal["manual", "barcode_scan", "receipt_import"]

RemovalReason = Literal["consumed", "wasted", "correction"]

#: numeric(12,3): three decimals, so "1" is rendered "1.000" as in the contract.
_QUANTUM = Decimal("0.001")


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored.

    A client sending ``expiry_date`` instead of ``expires_on`` must be told, not
    silently given a lot without an expiry date.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessOut(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


class LocationOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: StorageKind
    item_count: int


class LocationRefOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: StorageKind


class LocationCreateIn(StrictModel):
    """A new storage location.

    ``max_length=80`` matches ``storage_location.name``: a name refused here is a
    sentence, the same name refused by the column is a 500. ``kind`` has no
    default -- it decides whether expiry is suspended (a freezer) or counted (a
    fridge), and guessing "other" for a user who meant "congélateur" would make
    the application silently wrong about dates rather than ask one question.
    """

    name: Annotated[str, Field(min_length=1, max_length=80)]
    kind: StorageKind

    @model_validator(mode="after")
    def _trimmed_name_is_not_empty(self) -> LocationCreateIn:
        """Trim here, once, so no layer below has to wonder whether it was done.

        ``min_length`` counts characters, and ``"   "`` is three of them. Left
        alone it becomes a location whose name is blank on every screen and whose
        uniqueness index has one slot for the entire household.
        """
        trimmed = self.name.strip()
        if not trimmed:
            raise ValueError("name cannot be blank")
        self.name = trimmed
        return self


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    brand: str | None
    gtin: str | None
    image_url: str | None


class ProductDetailOut(ProductOut):
    """The lookup response: the contract's fields plus what a prefill needs."""

    default_unit: str | None = None
    category_tag: str | None = None


class ProductCreateIn(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=300)]
    brand: Annotated[str | None, Field(default=None, max_length=200)]
    gtin: Annotated[str | None, Field(default=None, min_length=8, max_length=14)]
    default_unit: Annotated[str | None, Field(default=None, max_length=16)]


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


class QuantityOut(BaseModel):
    amount: Decimal
    unit: str

    @field_serializer("amount")
    def _as_string(self, value: Decimal) -> str:
        return str(value.quantize(_QUANTUM))


class InventoryItemOut(BaseModel):
    id: uuid.UUID
    product: ProductOut
    location: LocationRefOut | None
    quantity: QuantityOut
    expires_on: date | None
    expiry_kind: ExpiryDateKind
    opened_at: date | None
    #: When the lot actually becomes unfit: the printed date, shortened by the
    #: "consume within N days of opening" rule when the product was opened and
    #: its family carries such a figure (``docs/data-model.md`` 7.4). Equal to
    #: ``expires_on`` for everything unopened, which is most of an inventory.
    #:
    #: A separate field rather than a corrected ``expires_on``, because the two
    #: answer different questions and the client shows both: ``expires_on`` is
    #: what is written on the pack, this is what the sorting and the alerts use.
    #: It can be non-null while ``expires_on`` is null -- an opened pot with no
    #: printed date has an opening date and a family, which is a real answer.
    effective_expires_on: date | None
    source: StockEntrySource
    created_at: datetime

    @field_serializer("created_at")
    def _as_zulu(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class InventoryPageOut(BaseModel):
    total: int
    items: list[InventoryItemOut]


class InventoryCreateIn(StrictModel):
    product_id: uuid.UUID | None = None
    product: ProductCreateIn | None = None
    location_id: uuid.UUID | None = None
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))]
    unit: Annotated[str, Field(min_length=1, max_length=16)]
    expires_on: date | None = None
    expiry_kind: ExpiryDateKind | None = None
    opened_at: date | None = None
    source: ContractSource = "manual"

    @model_validator(mode="after")
    def _exactly_one_product(self) -> InventoryCreateIn:
        if (self.product_id is None) == (self.product is None):
            raise ValueError("provide exactly one of product_id or product")
        return self


class InventoryPatchIn(StrictModel):
    """Every field optional. ``model_fields_set`` tells absent from explicitly null.

    ``amount`` carries the same bound as every other quantity on the boundary
    (``InventoryCreateIn``, ``ConfirmQuantityIn``, ``NewShoppingItemIn``) and not
    because the service below is unreliable: it checks the sign, and it is the only
    thing that did. A single guard with no second reading is how a value reaches
    ``ck_inventory_lot_quantity_positive`` the day that guard is refactored -- and a
    violated CHECK is answered by ``handle_unexpected``, which logs the exception
    PostgreSQL built by echoing the whole failing row back at us.
    """

    amount: Annotated[Decimal | None, Field(default=None, gt=0, le=Decimal("100000"))] = None
    unit: Annotated[str | None, Field(default=None, min_length=1, max_length=16)]
    location_id: uuid.UUID | None = None
    expires_on: date | None = None
    expiry_kind: ExpiryDateKind | None = None
    opened_at: date | None = None


class DepletedOut(BaseModel):
    """What just ran out, offered for repurchase and never added (contract 6bis).

    ``reason`` cannot hold ``correction`` and the type says so: fixing a mistyped
    500 g into 0 is not finishing a product, and a shape that could carry the
    third reason would be a shape somebody eventually fills with it.

    ``already_on_list`` spares the user a proposal for something they have
    already written down; ``previously_declined`` lets the client stay silent
    without keeping that memory itself -- it belongs to the household, on the
    server, and a second copy in a browser is a second answer.
    """

    product_id: uuid.UUID
    product_name: str
    reason: Literal["consumed", "wasted"]
    already_on_list: bool
    previously_declined: bool


class InventoryItemMutatedOut(InventoryItemOut):
    """The answer to a ``PATCH``: the item, plus what the change emptied.

    A subclass rather than a field on :class:`InventoryItemOut`, and that is the
    whole reason it exists. ``GET /v1/inventory`` returns up to 200 rows; giving
    every one of them a ``depleted: null`` would add a field that is null by
    construction on the busiest response of the application, on a mobile
    connection, forever. Listing and mutating are different answers and they now
    have different shapes.

    Flat rather than nested, as contract 6bis writes it: the field sits beside
    the item's own fields, not inside an envelope.
    """

    depleted: DepletedOut | None = None


class InventoryRemovalOut(BaseModel):
    """The answer to a ``DELETE``, and a deliberate break with contract v1.

    v1 froze this endpoint at ``204 No Content``. Contract v1.1 section 6bis then
    required the same response to carry ``depleted``, and a ``204`` carries no
    body at all -- the two cannot both hold. The status becomes ``200`` and the
    body is this object; the deviation is written down in both contracts rather
    than only in code.

    ``depleted`` is **always present**, ``null`` when there is nothing to
    propose. A client that has to tell "no proposal" from "field forgotten" is a
    client that will guess, and it will guess wrong on the removal that mattered.
    """

    depleted: DepletedOut | None


# --------------------------------------------------------------------------- #
# Provider capabilities
# --------------------------------------------------------------------------- #


class CapabilityFlagsOut(BaseModel):
    """The two capabilities the v1 contract exposes.

    The domain knows four; the other two (prompt caching, long context) change what
    a call *costs* or *covers*, not what the interface may offer, so they reach the
    user as a degradation reason rather than as a flag.
    """

    vision: bool
    structured_output: bool


class ProviderCapabilitiesOut(BaseModel):
    """``configured: false`` with null identifiers is a normal state, not an error.

    ``degraded_reasons`` carries user-facing French sentences, not error codes: the
    PWA shows them in a permanent banner, before the user tries something rather
    than after it fails.
    """

    configured: bool
    mode: str | None
    provider: str | None
    model: str | None
    capabilities: CapabilityFlagsOut
    degraded: bool
    degraded_reasons: list[str]


# --------------------------------------------------------------------------- #
# Household members (api contract v1.1 section 2)
# --------------------------------------------------------------------------- #
#
# Every field below ``display_name`` is health data (GDPR article 9), and for an
# infant a minor's. Nothing here is logged, echoed in an error body or sent to a
# model; the schemas exist so the boundary refuses anything outside the closed
# vocabularies before a single row is written.

#: Longer than any name a household types, short enough that the column bound
#: (120) is never the thing that refuses the request.
MAX_MEMBER_NAME: Final = 60

#: Mirrors ``ck_household_person_free_text_length``. Enforced twice on purpose:
#: a bound held only by the API is a bound the next writer forgets.
MAX_FREE_TEXT: Final = 500

#: How many eaters one suggestion may be filtered by. Larger than any household
#: and bounded all the same, because the list becomes an ``IN`` clause.
MAX_MEMBER_FILTERS: Final = 32


class MemberRefOut(BaseModel):
    """A member as the suggestion response names them: an id and a display name.

    Deliberately nothing else. Echoing the allergens back inside every suggestion
    would put one person's medical fact on a screen anybody in the house can
    open, and the client already knows what it sent.
    """

    id: uuid.UUID
    display_name: str


class MemberOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: AgeBand
    diet: Diet
    allergens: list[Allergen]
    #: ``""`` rather than ``null`` when unset: the field is a text input on the
    #: other side, and a client that has to handle both writes one of them wrong.
    free_text_restrictions: str
    #: Non-null if and only if ``age_band`` is an infant band. The mismatch is a
    #: 422 here and a check constraint in the database (contract 2).
    infant_texture: InfantTexture | None


class MemberIn(StrictModel):
    """A whole member. Used by ``POST``; ``PATCH`` reuses it with every field optional.

    ``allergens`` is a closed list of the fourteen regulated codes and nothing
    else. An intolerance outside the regulation -- lactose, FODMAP -- has to go
    to ``free_text_restrictions``, because no catalogue column says which
    products contain it and a filter that cannot be computed must not look like
    one (contract 4).
    """

    display_name: Annotated[str, Field(min_length=1, max_length=MAX_MEMBER_NAME)]
    age_band: AgeBand = AgeBand.ADULT
    diet: Diet = Diet.OMNIVORE
    allergens: Annotated[list[Allergen], Field(default_factory=list, max_length=14)]
    free_text_restrictions: Annotated[str, Field(default="", max_length=MAX_FREE_TEXT)]
    infant_texture: InfantTexture | None = None


class MemberPatchIn(StrictModel):
    """Every field optional; an omitted one is left alone.

    ``infant_texture`` is the exception that proves the rule: it can be sent as
    ``null`` to clear it, and telling "absent" from "explicitly null" is the
    whole point of the verb. The service carries that distinction with ``UNSET``.
    """

    display_name: Annotated[str | None, Field(default=None, max_length=MAX_MEMBER_NAME)]
    age_band: AgeBand | None = None
    diet: Diet | None = None
    allergens: Annotated[list[Allergen] | None, Field(default=None, max_length=14)]
    free_text_restrictions: Annotated[str | None, Field(default=None, max_length=MAX_FREE_TEXT)]
    infant_texture: InfantTexture | None = None

    @model_validator(mode="before")
    @classmethod
    def _remember_what_was_sent(cls, data: Any) -> Any:
        """Record which keys the client actually sent.

        Pydantic gives this for free through ``model_fields_set``; the validator
        exists only to reject a body that changes nothing, which is otherwise a
        silent no-op the client reads as a successful edit.
        """
        if isinstance(data, dict) and not data:
            raise ValueError("send at least one field to change")
        return data


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #

#: A ceiling on a request that spends real money -- the household's or the
#: operator's. The contract's example asks for three.
MAX_SUGGESTIONS: Final = 5

#: More locations than any plausible home has. The bound exists so one request
#: cannot fan out into an unbounded number of inventory queries.
MAX_LOCATION_FILTERS: Final = 20


class RecipeIngredientOut(BaseModel):
    """``in_stock`` is computed from the household's own stock, never from the model."""

    name: str
    amount: str | None
    unit: str | None
    in_stock: bool


class AllergenAssessmentOut(BaseModel):
    """``statement`` is the only allergen wording the interface may display.

    Written by the server, negative and situated by construction. A client that
    composed its own sentence out of ``declared_clear_of`` would put "sans
    arachide" on screen, which is the one thing ADR-0009 forbids: the truth is
    "none of the identified products declared it", and the two are not the same
    claim.
    """

    declared_clear_of: list[Allergen]
    #: Always present, including at zero. Absent and zero read identically to a
    #: naive client, and one of the two means "we could not check".
    unverified_product_count: int
    statement: str


class UrgentItemOut(BaseModel):
    """One lot the suggestion draws on before it spoils.

    ``expires_on`` is the effective date -- shortened by the after-opening rule
    when the lot was opened (``docs/data-model.md`` 7.4) -- so that it always
    equals ``today + days_left``. ``GET /v1/inventory`` is where the date printed
    on the packaging is found, under ``expires_on``, next to
    ``effective_expires_on``.
    """

    inventory_item_id: uuid.UUID
    product_name: str
    expires_on: date | None
    days_left: int


class ExpiryPressureOut(BaseModel):
    """What the suggestion saved, and -- the honest field -- what it did not."""

    items_used_expiring_within_days: int
    urgent_items: list[UrgentItemOut]
    urgent_items_left_unused: int


class PreparationOut(BaseModel):
    """Self-declared by the model (contract 4ter). ``null`` means it did not say.

    Never a verified fact, and never a reason to hide a suggestion: it lets the
    interface point out that a dish is served hot when cold was asked for, and
    the user decides.
    """

    serving_temperature: Literal["hot", "cold", "either"] | None
    requires_cooking: bool | None
    requires_oven: bool | None


class RecipeSuggestionOut(BaseModel):
    id: uuid.UUID
    title: str
    #: Never null in the contract: a model that returned no summary yields "".
    summary: str
    duration_minutes: int | None
    servings: int | None
    ingredients: list[RecipeIngredientOut]
    steps: list[str]
    #: Measured by the server from what the suggestion actually draws on, not
    #: read off the model's own claim.
    uses_expiring_soon: bool
    allergen_assessment: AllergenAssessmentOut
    expiry_pressure: ExpiryPressureOut
    preparation: PreparationOut


class AppliedConstraintsOut(BaseModel):
    """Which constraints the server applied, so a short list can be explained.

    ``products_withheld`` and ``products_unverified`` are why this object exists:
    without them, an inventory emptied by an allergy is indistinguishable from a
    model having a bad day.
    """

    members: list[MemberRefOut]
    excluded_allergens: list[Allergen]
    diet: Diet | None
    infant_texture: InfantTexture | None
    age_bands: list[AgeBand]
    products_withheld: int
    products_unverified: int


class BalanceGapOut(BaseModel):
    marker: PnnsMarker
    label: str
    target: str
    observed: int
    shortfall: int
    #: The official wording and the page it is on, carried so a household can
    #: check what this application asserts (ADR-0009). Additive to the contract's
    #: example, which is a floor and not a ceiling.
    statement: str
    source_url: str


class BalanceExcessOut(BaseModel):
    marker: PnnsMarker
    label: str
    target: str
    #: The contract's field name, and it only ever meant a mass -- the two PNNS
    #: ceilings expressed in grams. A serving-based ceiling reports zero here and
    #: carries its count in ``observed`` beside its ``unit``, rather than
    #: pretending a number of sugary drinks is a weight.
    observed_grams: int
    observed: int
    unit: PnnsUnit
    statement: str
    source_url: str


class WeeklyBalanceOut(BaseModel):
    """The weekly balance. Computed by the application; the model never sees a benchmark.

    ``uncategorised_product_count`` is **always present**, including at zero: a
    missing field and a zero say the same thing to a naive client -- "everything
    is categorised" -- and one of the two means "we do not know" (contract 8).
    """

    reference: str
    window_days: int
    uncategorised_product_count: int
    gaps: list[BalanceGapOut]
    excesses: list[BalanceExcessOut]
    satisfiable_from_stock: bool
    note: str | None


class SuggestRecipesIn(StrictModel):
    location_ids: Annotated[
        list[uuid.UUID], Field(default_factory=list, max_length=MAX_LOCATION_FILTERS)
    ]
    max_suggestions: Annotated[int, Field(default=3, ge=1, le=MAX_SUGGESTIONS)]
    notes: Annotated[str, Field(default="", max_length=500)]
    #: Who is being cooked for. Empty or absent means every eater recorded in the
    #: household, whose constraints are then unioned -- never a subset picked by
    #: the server, which would silently drop somebody's allergy.
    member_ids: Annotated[
        list[uuid.UUID], Field(default_factory=list, max_length=MAX_MEMBER_FILTERS)
    ]
    balance_mode: Literal["weekly", "off"] = "weekly"
    meal_temperature: Literal["any", "hot", "cold"] = "any"


class TokenUsageOut(BaseModel):
    """What one suggestion request consumed, when the provider reported it.

    Tokens, and deliberately **not** a price. Converting these into euros needs a
    per-model rate card with a date on it: the rates move, they do not exist for a
    self-hosted Ollama, and under ``byok`` the household's own tier and currency --
    which this instance never sees -- are what actually bills. A stale amount in
    euros reads as authoritative and is a lie; a token count is a fact the household
    can price against their own invoice. See ``services/recipes.py`` for the
    corresponding decision on ``cost_micro``.

    Every field is nullable because "unknown" is a real answer and zero is not a
    substitute for it: an adapter that could not read a counter reports nothing
    rather than claiming a free call.

    ``input_tokens`` excludes what was served from cache; the whole prompt is
    ``input_tokens + cached_input_tokens``. That split is what makes the
    ``prompt_caching`` degradation reason measurable rather than rhetorical.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class SuggestRecipesOut(BaseModel):
    provider_mode: str
    model: str
    suggestions: list[RecipeSuggestionOut]
    #: Added after v1 froze, and additive by construction: a field the client may
    #: ignore entirely. ``null`` when the provider reported nothing, which is a
    #: state the interface must render as "not measured" rather than as "free".
    usage: TokenUsageOut | None = None
    applied_constraints: AppliedConstraintsOut | None = None
    #: ``null`` when the request asked for ``balance_mode: off``. Never null to
    #: mean "no gap": an empty ``gaps`` list is what says that.
    balance: WeeklyBalanceOut | None = None


# --------------------------------------------------------------------------- #
# Suggestion feedback
# --------------------------------------------------------------------------- #


class RecipeFeedbackIn(StrictModel):
    """Two answers and no scale.

    A five-star widget collects nothing from someone holding a phone over a hob;
    these two are one tap each and are exactly what the ranking and the
    per-model comparison consume (``domain/models.py``: ``RecipeFeedback``).
    Withdrawing an opinion is ``DELETE`` on the same path, not a third value --
    "no answer" is the absence of a verdict, not a verdict of its own.
    """

    feedback: Literal["cooked", "not_interested"]


class RecipeFeedbackOut(BaseModel):
    """The suggestion's current verdict, after the write.

    ``status`` is returned alongside because the two are one fact seen twice and
    the database refuses to let them disagree
    (``ck_recipe_suggestion_feedback_matches_status``). A client that showed a
    stale lifecycle next to a fresh opinion would be displaying a state the row
    cannot hold.
    """

    suggestion_id: uuid.UUID
    feedback: Literal["cooked", "not_interested"] | None
    feedback_at: datetime | None
    status: Literal["generated", "saved", "cooked", "discarded"]

    @field_serializer("feedback_at")
    def _as_zulu(self, value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ModelQualityOut(BaseModel):
    """One (provider mode, model) pair, in counts first and a rate only maybe.

    ``responses`` is never optional and ``cooked_rate`` often is. That asymmetry
    is the point: "2 avis sur 3" is a fact, "67 %" is the same fact wearing the
    costume of a measurement. Below ``min_responses`` answers the server sends
    ``cooked_rate: null`` and the client must show the counts alone -- it is not
    free to compute the division itself.
    """

    provider_mode: str
    model: str
    cooked: int
    not_interested: int
    responses: int
    #: ``null`` below the threshold. Never zero to mean "unknown".
    cooked_rate: float | None


class SuggestionQualityOut(BaseModel):
    """Feedback aggregated by provider and model.

    The one report that turns ADR-0007's "the small local model degrades
    gracefully" from an assertion into a number somebody can read. Scoped to the
    household like every other route here; the cross-tenant view is
    ``scripts/quality_report.py``, run by the maintenance identity, because
    nothing in this API is authenticated as an operator.
    """

    #: How many answers a pair needs before ``cooked_rate`` is populated. On the
    #: response so the client renders "not enough answers yet" with the real
    #: number instead of a hardcoded copy that can drift.
    min_responses: int
    models: list[ModelQualityOut]


# --------------------------------------------------------------------------- #
# Shopping-list import (api contract 7)
# --------------------------------------------------------------------------- #

#: Pasted text. Well above what a household writes by hand, and small enough that
#: it stays inside the global 256 KiB JSON body cap with room to spare -- the file
#: upload is the path with its own, larger, bound.
MAX_PASTED_TEXT: Final = 50_000

#: Lines accepted by the confirm endpoint, mirroring
#: ``services.shopping_import.MAX_CONFIRM_LINES``.
MAX_CONFIRM_LINES: Final = 500


class ImportTextIn(StrictModel):
    """The JSON form of the import: ``{"text": "…"}`` (contract 7.2)."""

    text: Annotated[str, Field(min_length=1, max_length=MAX_PASTED_TEXT)]


class ProposedQuantityOut(BaseModel):
    #: A string, for the reason at the top of this module.
    amount: Decimal
    unit: str

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> str:
        return str(value.quantize(_QUANTUM))


class ProposedLineOut(BaseModel):
    raw: str
    quantity: ProposedQuantityOut | None
    product_name: str
    matched_product_id: uuid.UUID | None
    confidence: Literal["high", "medium", "low", "none"]
    needs_review: bool
    #: Dietary signals, never a reason a line was removed (contract 7.4). Always
    #: present, so a client can render it without testing for the key; empty until
    #: a dietary model is registered on the instance.
    flags: list[str] = []


class ImportProposalOut(BaseModel):
    """Nothing in this response has been written (contract 7.2)."""

    import_id: uuid.UUID
    source: Literal["pdf", "txt", "text"]
    parsed_by: Literal["deterministic", "deterministic+model"]
    lines: list[ProposedLineOut]
    unparsed_line_count: int
    truncated: bool


class ConfirmQuantityIn(StrictModel):
    amount: Annotated[Decimal, Field(gt=0, le=Decimal("100000"))]
    unit: Annotated[str, Field(min_length=1, max_length=16)]


class ConfirmLineIn(StrictModel):
    product_id: uuid.UUID | None = None
    label: Annotated[str | None, Field(default=None, max_length=200)] = None
    quantity: ConfirmQuantityIn | None = None

    @model_validator(mode="after")
    def _needs_a_target(self) -> ConfirmLineIn:
        """``ck_shopping_list_item_target_present``, enforced at the boundary."""
        if self.product_id is None and not (self.label or "").strip():
            raise ValueError("a line needs either product_id or label")
        return self


class ConfirmImportIn(StrictModel):
    """The reviewed lines. This body is the only thing that writes (contract 7.2)."""

    shopping_list_id: uuid.UUID | None = None
    #: Only consulted when a list has to be created, which happens once per
    #: household at most.
    list_name: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    lines: Annotated[list[ConfirmLineIn], Field(min_length=1, max_length=MAX_CONFIRM_LINES)]


class ConfirmImportOut(BaseModel):
    shopping_list_id: uuid.UUID
    shopping_list_name: str
    created_item_count: int


# --------------------------------------------------------------------------- #
# The shopping list itself (api contract 6bis, "le chemin d'écriture")
# --------------------------------------------------------------------------- #

#: Items one ``POST`` may add, mirroring
#: ``services.shopping_lists.MAX_ITEMS_PER_CALL``.
MAX_ITEMS_PER_CALL: Final = 100

#: Products one refusal may cover. The grouped depletion proposal is a handful.
MAX_DECLINED_PRODUCTS: Final = 100

#: Where an item came from. Wider than the persisted enum on purpose: the value
#: exists so "does the repurchase proposal get used?" can be measured rather than
#: assumed (contract 6bis).
ItemSourceIn = Literal["manual", "depleted", "import"]


class ShoppingQuantityOut(BaseModel):
    amount: Decimal
    unit: str

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> str:
        return str(value.quantize(_QUANTUM))


class ShoppingItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID | None
    product_name: str | None
    free_text: str | None
    quantity: ShoppingQuantityOut | None
    source: ItemSourceIn
    checked: bool
    sort_order: int


class ShoppingListOut(BaseModel):
    id: uuid.UUID
    name: str
    items: list[ShoppingItemOut]


class NewShoppingItemIn(StrictModel):
    """One item to add. Either a catalogue product or free text, never neither."""

    product_id: uuid.UUID | None = None
    free_text: Annotated[str | None, Field(default=None, max_length=200)] = None
    amount: Annotated[Decimal | None, Field(default=None, gt=0, le=Decimal("100000"))] = None
    unit: Annotated[str | None, Field(default=None, min_length=1, max_length=16)] = None
    source: ItemSourceIn = "manual"

    @model_validator(mode="after")
    def _coherent(self) -> NewShoppingItemIn:
        if self.product_id is None and not (self.free_text or "").strip():
            raise ValueError("an item needs either product_id or free_text")
        if (self.amount is None) != (self.unit is None):
            # ``ck_shopping_list_item_quantity_triplet``: value, code and dimension
            # are all set or all null, so half a quantity has nowhere to go.
            raise ValueError("amount and unit are set together or not at all")
        return self


class AddShoppingItemsIn(StrictModel):
    """An **array**, and that is the design (contract 6bis).

    Emptying the fridge depletes several items at once; a single-item endpoint
    would turn one user gesture into five calls, five transactions and five ways
    to half-succeed.
    """

    items: Annotated[list[NewShoppingItemIn], Field(min_length=1, max_length=MAX_ITEMS_PER_CALL)]


class UpdateShoppingItemIn(StrictModel):
    """Tick, untick, or correct the quantity. Absent means "leave it alone".

    ``quantity: null`` is explicit and clears it, which is why the handler reads
    ``model_fields_set`` rather than testing for ``None``.
    """

    checked: bool | None = None
    quantity: ConfirmQuantityIn | None = None


class DeclineRepurchaseIn(StrictModel):
    product_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=MAX_DECLINED_PRODUCTS)]


# --------------------------------------------------------------------------- #
# Machine access tokens (contract v1.1 section 10)
# --------------------------------------------------------------------------- #

#: A name long enough to say "Home Assistant -- salon", short enough that the
#: column bound is not a surprise.
MAX_TOKEN_NAME: Final = 120

#: There are five scopes and a token cannot hold the same one twice, so nothing
#: legitimate sends more. The bound is here so a body carrying ten thousand
#: repetitions is refused by validation rather than by the database.
MAX_TOKEN_SCOPES: Final = 5


class MachineTokenOut(BaseModel):
    """One token as its household reads it back. **Carries no secret.**

    ``prefix`` and ``last4`` are what survives the creation response. They are
    enough to tell two tokens apart in a list and nowhere near enough to shorten
    a search against 256 bits of entropy.

    ``last_used_at`` is ``null`` until the token is first presented, and is then
    refreshed at most once an hour (``services/tokens.py``). It answers "is this
    integration still running?" before somebody deletes it -- a question nobody
    asks to the minute, and a per-request refresh would turn every authenticated
    read into a write.
    """

    id: str
    name: str
    scopes: list[str]
    prefix: str
    last4: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None

    @field_serializer("created_at", "last_used_at", "expires_at")
    def _as_zulu(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MachineTokenCreatedOut(MachineTokenOut):
    """The one response in this API that contains a token value.

    There is no route that reads ``token`` back, and there never will be: a
    credential a screenshot can steal is a credential a household cannot reason
    about. Losing it means issuing another, which is cheap; revoking the lost one
    is the gesture that should follow anyway.
    """

    token: str


class MachineTokenCreateIn(StrictModel):
    """What a household asks for when it wires up an integration.

    ``scopes`` has no default. That is the contract's "never implicit" made
    mechanical: a body that forgets the field is a ``422``, not a token that
    quietly turns out to grant something. The empty list is refused for the same
    reason -- a token that can do nothing has no use except to look like an
    integration exists.

    ``expires_in_days: null`` is a deliberate choice of "no expiry" and is
    allowed by the contract, which is why the field is required rather than
    defaulted: a household that wants a token forever should have typed so.
    """

    name: Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_NAME)]
    scopes: Annotated[list[MachineTokenScope], Field(min_length=1, max_length=MAX_TOKEN_SCOPES)]
    expires_in_days: Annotated[int | None, Field(ge=1, le=MAX_TOKEN_EXPIRY_DAYS)]
