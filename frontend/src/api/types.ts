/**
 * Wire types for the Chaudron API v1.
 *
 * Mirrors docs/api-contract-v1.md exactly. The contract is written before both
 * sides of the code; anything that drifts from it here breaks integration, so
 * changes belong in the document first.
 */

export type LocationKind = 'fridge' | 'freezer' | 'pantry' | 'cellar' | 'other';

export interface StorageLocation {
  id: string;
  name: string;
  kind: LocationKind;
  item_count: number;
}

/** Locations are embedded in inventory items without their item count. */
export type EmbeddedLocation = Pick<StorageLocation, 'id' | 'name' | 'kind'>;

/** Body of `POST /v1/locations`. `kind` decides whether expiry is suspended. */
export interface LocationDraft {
  name: string;
  kind: LocationKind;
}

export interface Product {
  id: string;
  name: string;
  brand: string | null;
  gtin: string | null;
  image_url: string | null;
}

export interface Quantity {
  /**
   * A decimal string, never a number. JSON floats destroy exact decimals, and a
   * quantity wrong by a factor of ten in a food inventory is not a detail.
   */
  amount: string;
  unit: string;
}

export type ExpiryKind = 'use_by' | 'best_before';
export type ItemSource = 'manual' | 'barcode_scan' | 'receipt_import';
export type RemovalReason = 'consumed' | 'wasted' | 'correction';

export interface InventoryItem {
  id: string;
  product: Product;
  /**
   * `null` is a real answer, not a defect: `inventory_lot.storage_location_id`
   * is nullable, the API schema says `LocationRefOut | None`, and a lot whose
   * location was archived reads back without one. This field was typed
   * non-nullable, which is how the inventory screen came to crash on
   * `item.location.id` instead of grouping such a lot under a heading.
   */
  location: EmbeddedLocation | null;
  quantity: Quantity;
  expires_on: string | null;
  expiry_kind: ExpiryKind | null;
  opened_at: string | null;
  source: ItemSource;
  created_at: string;
}

export interface InventoryPage {
  total: number;
  items: InventoryItem[];
}

export interface InventoryQuery {
  location_id?: string;
  q?: string;
  expiring_within_days?: number;
  limit?: number;
  offset?: number;
}

/** Manual product creation, inline in a POST /v1/inventory body. */
export interface ProductDraft {
  name: string;
  brand: string | null;
  gtin: string | null;
  default_unit: string;
}

/**
 * `product_id` OR `product` must be supplied, never both — the union keeps that
 * rule in the type system rather than in a comment nobody reads.
 */
export type CreateInventoryItem = {
  location_id: string;
  amount: string;
  unit: string;
  expires_on: string | null;
  expiry_kind: ExpiryKind | null;
  source: ItemSource;
} & ({ product_id: string; product?: never } | { product: ProductDraft; product_id?: never });

/**
 * Sent back when an item's quantity reached zero (contract v1.1 §6bis). The
 * interface *proposes* a repurchase; it never writes one by itself.
 *
 * `reason` is the movement motive: `correction` never proposes — fixing a typo
 * is not finishing a product — and the server already applies that rule. The
 * client re-checks it anyway, because a shopping list polluted by typos is
 * exactly how this feature stops being used.
 */
export interface DepletedProduct {
  product_id: string;
  product_name: string;
  reason: RemovalReason;
  already_on_list: boolean;
  /** The household already refused this product once. Stay silent. */
  previously_declined: boolean;
}

/**
 * `DELETE /v1/inventory/{id}` — `200` with this body.
 *
 * Contract v1 froze the endpoint at `204`; §6bis then required `depleted` on the
 * same response, and the two cannot both hold. The status is `200` and the field
 * is always present, `null` when nothing is worth proposing.
 */
export interface RemovalResult {
  depleted: DepletedProduct | null;
}

/**
 * `PATCH /v1/inventory/{id}` — the item, plus `depleted` flat beside its fields.
 *
 * Only the mutations carry it. `GET /v1/inventory` returns the bare item, which
 * is why this is a separate type rather than a field on `InventoryItem`: a page
 * of 200 rows has no business carrying 200 nulls.
 */
export type UpdatedInventoryItem = InventoryItem & { depleted: DepletedProduct | null };

/** Where an item on the list came from. `depleted` is what §6bis wants counted. */
export type ShoppingItemSource = 'manual' | 'depleted' | 'import';

/** One item for `POST /v1/shopping-lists/current/items`: a product or free text. */
export interface NewShoppingListItem {
  product_id?: string;
  free_text?: string;
  amount?: string;
  unit?: string;
  source?: ShoppingItemSource;
}

/**
 * One item on the household's list — `GET /v1/shopping-lists/current` (§6bis).
 *
 * `product_id` and `free_text` are exclusive at the database level, and the
 * screens rely on it: a line the parser could not match to the catalogue is
 * free text and is shown as the text it is, never dressed up as a known product.
 */
export interface ShoppingListItem {
  id: string;
  product_id: string | null;
  product_name: string | null;
  free_text: string | null;
  quantity: Quantity | null;
  source: ShoppingItemSource;
  checked: boolean;
  sort_order: number;
}

export interface ShoppingList {
  id: string;
  name: string;
  items: ShoppingListItem[];
}

/**
 * `PATCH /v1/shopping-lists/current/items/{id}`.
 *
 * An absent key means "leave it alone" and `quantity: null` means "clear it" —
 * the server reads `model_fields_set`, so the two are genuinely different edits
 * and callers must not collapse them by spreading a partial object.
 */
export interface ShoppingItemPatch {
  checked?: boolean;
  quantity?: Quantity | null;
}

export type ImportConfidence = 'high' | 'medium' | 'low' | 'none';

export interface ShoppingImportLine {
  raw: string;
  quantity: Quantity | null;
  product_name: string;
  matched_product_id: string | null;
  confidence: ImportConfidence;
  needs_review: boolean;
  /**
   * Dietary signals, `allergen:<code>` (§7.4). **Flags, never a reason a line
   * was removed** — the household is allowed to buy what one of its members
   * cannot eat, and the server port has no method that could drop a line.
   *
   * Optional here because a server older than the flag field simply omits it;
   * empty is also the normal state, since flags stay empty until a dietary
   * screen is registered on the instance.
   */
  flags?: string[];
}

/** A proposal. Nothing is written until the confirm call. */
export interface ShoppingImport {
  import_id: string;
  source: string;
  parsed_by: 'deterministic' | 'deterministic+model';
  lines: ShoppingImportLine[];
  unparsed_line_count: number;
  /** More lines than the server reads in one document. Said on screen, always. */
  truncated: boolean;
}

/**
 * One reviewed line, on its way to the only call that writes.
 *
 * Deliberately not `ShoppingImportLine`: the proposal carries what the parser
 * *read*, this carries what the human *kept*. Sending the former back would
 * write `raw`, `confidence` and `needs_review` into a body that has no field
 * for them, and would quietly re-assert a match the user may have rejected.
 */
export interface ShoppingImportConfirmLine {
  product_id?: string;
  label?: string;
  quantity?: { amount: string; unit: string };
}

export interface ShoppingImportResult {
  shopping_list_id: string;
  shopping_list_name: string;
  created_item_count: number;
}

/** What a third-party destination accepted. Counts only, never the list itself. */
export interface ShoppingExportReceipt {
  target: string;
  export_id: string;
  exported_item_count: number;
  external_list_id: string | null;
}

/** Fields a manual adjustment may change. Everything omitted is left untouched. */
export interface InventoryItemPatch {
  amount?: string;
  unit?: string;
  location_id?: string;
  expires_on?: string | null;
  expiry_kind?: ExpiryKind | null;
  opened_at?: string | null;
}

/* -------------------------------------------------------------------------
 * v1.1 — dietary constraints, weekly balance, meal temperature.
 * Mirrors docs/api-contract-v1.1-dietary.md. Fields added to existing
 * responses are optional here, exactly as §"Extension additive" requires: a
 * server that has not shipped v1.1 yet must not break these screens.
 * ---------------------------------------------------------------------- */

/**
 * The 14 allergens of EU regulation 1169/2011 annex II, in contract order.
 * Closed vocabulary: nothing outside this list is an allergen for this product.
 */
export const ALLERGEN_CODES = [
  'gluten',
  'crustaceans',
  'eggs',
  'fish',
  'peanuts',
  'soybeans',
  'milk',
  'nuts',
  'celery',
  'mustard',
  'sesame',
  'sulphites',
  'lupin',
  'molluscs',
] as const;

export type AllergenCode = (typeof ALLERGEN_CODES)[number];

export type Diet = 'omnivore' | 'pescatarian' | 'vegetarian' | 'vegan';

export type InfantTexture = 'smooth' | 'soft_pieces' | 'pieces';

/** Age band, never a date of birth — ADR-0009: the band suffices and is far less identifying. */
export type AgeBand =
  'adult' | 'child' | 'infant_4_6m' | 'infant_6_9m' | 'infant_9_12m' | 'infant_12_36m';

export interface HouseholdMember {
  id: string;
  display_name: string;
  age_band: AgeBand;
  diet: Diet;
  allergens: AllergenCode[];
  free_text_restrictions: string;
  /** Non-null if and only if `age_band` is an infant band — the server 422s otherwise. */
  infant_texture: InfantTexture | null;
}

export type MemberDraft = Omit<HouseholdMember, 'id'>;

/** Third-class constraint (§4bis): a preference sent to the model, never a filter. */
export type MealTemperature = 'any' | 'hot' | 'cold';

export type ServingTemperature = 'hot' | 'cold' | 'either';

/** Self-declared by the model. Never presented as verified fact. */
export interface RecipePreparation {
  serving_temperature: ServingTemperature;
  requires_cooking: boolean;
  requires_oven: boolean;
}

export interface AllergenAssessment {
  declared_clear_of: AllergenCode[];
  unverified_product_count: number;
  /**
   * Server-authored. The only allergen wording the interface may display; the
   * client never composes its own sentence from the other fields (ADR-0009).
   */
  statement: string;
}

export interface UrgentItem {
  inventory_item_id: string;
  product_name: string;
  expires_on: string | null;
  days_left: number;
}

export interface ExpiryPressure {
  items_used_expiring_within_days: number;
  urgent_items: UrgentItem[];
  /** What the suggestion did *not* save. */
  urgent_items_left_unused: number;
}

export interface AppliedConstraints {
  members: { id: string; display_name: string }[];
  excluded_allergens: AllergenCode[];
  diet: Diet | null;
  infant_texture: InfantTexture | null;
  age_bands: AgeBand[];
  products_withheld: number;
  products_unverified: number;
}

export interface BalanceGap {
  marker: string;
  label: string;
  target: string;
  observed: number;
  shortfall: number;
}

export interface BalanceExcess {
  marker: string;
  label: string;
  target: string;
  observed_grams: number;
  /**
   * The two PNNS ceilings expressed in grams are the only ones `observed_grams`
   * can carry. A ceiling counted in servings — sugary drinks — reports zero
   * there and its real count here, beside its unit; rendering the gram field
   * for it would print "7 g consommés" for seven glasses. Optional: a server
   * that predates the addition sends neither.
   */
  observed?: number;
  unit?: 'gram' | 'serving';
}

export interface WeeklyBalance {
  reference: string;
  window_days: number;
  gaps: BalanceGap[];
  excesses: BalanceExcess[];
  satisfiable_from_stock: boolean;
  note: string | null;
  /**
   * §7 requires the count of products whose category resolves to no marker to
   * be exposed — otherwise a badly categorised inventory yields an indisputable
   * and false "you are missing a fish". The frozen contract never names the
   * field, so the three plausible spellings are accepted and
   * `uncategorisedProductCount` normalises them. Absent means "not told", which
   * the interface says out loud rather than silently reading as zero.
   */
  uncategorised_product_count?: number;
  uncategorized_product_count?: number;
  products_uncategorised?: number;
}

export interface RecipeIngredient {
  name: string;
  amount: string | null;
  unit: string | null;
  in_stock: boolean;
}

export interface RecipeSuggestion {
  id: string;
  title: string;
  summary: string;
  duration_minutes: number | null;
  servings: number | null;
  ingredients: RecipeIngredient[];
  steps: string[];
  uses_expiring_soon: boolean;
  allergen_assessment?: AllergenAssessment;
  expiry_pressure?: ExpiryPressure;
  preparation?: RecipePreparation;
}

export interface SuggestRecipesRequest {
  location_ids: string[];
  max_suggestions: number;
  notes: string;
  /** Empty means the whole household. The union of their constraints applies. */
  member_ids: string[];
  balance_mode: 'weekly' | 'off';
  meal_temperature: MealTemperature;
}

export interface SuggestRecipesResponse {
  provider_mode: string;
  model: string;
  suggestions: RecipeSuggestion[];
  applied_constraints?: AppliedConstraints;
  balance?: WeeklyBalance;
}

/**
 * Two answers and no scale. A five-star widget collects nothing from someone
 * holding a phone over a hob; these are one tap each, and they are the two facts
 * the ranking and the per-model comparison actually consume.
 *
 * `null` is the absence of an answer, never a third opinion.
 */
export type RecipeFeedbackVerdict = 'cooked' | 'not_interested';

export interface RecipeFeedbackState {
  suggestion_id: string;
  feedback: RecipeFeedbackVerdict | null;
  feedback_at: string | null;
  /** Kept in step with `feedback` by a database CHECK; shown by nothing yet. */
  status: 'generated' | 'saved' | 'cooked' | 'discarded';
}

/**
 * One provider/model pair's record, in counts first.
 *
 * `cooked_rate` is `null` below `min_responses` answers and the client **must
 * not** compute the division itself: "100 %" built on one tap is the exact
 * misreading the threshold exists to prevent. Below it, show `cooked` and
 * `responses` as they are.
 */
export interface ModelQuality {
  provider_mode: string;
  model: string;
  cooked: number;
  not_interested: number;
  responses: number;
  cooked_rate: number | null;
}

export interface SuggestionQuality {
  /** Carried by the response so no copy of the threshold can drift here. */
  min_responses: number;
  models: ModelQuality[];
}

/** Null when the server did not report it — never silently zero. */
export function uncategorisedProductCount(balance: WeeklyBalance): number | null {
  const candidates = [
    balance.uncategorised_product_count,
    balance.uncategorized_product_count,
    balance.products_uncategorised,
  ];
  for (const value of candidates) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return null;
}

/**
 * The contract states degraded_reasons "lists in plain language what is reduced
 * and why" and shows an empty array. Accepting an object shape as well costs
 * nothing and keeps the banner from crashing if the backend sends structured
 * reasons; `formatDegradedReason` normalises both.
 */
export type DegradedReason =
  string | { code?: string; title?: string; detail?: string; message?: string; reason?: string };

export interface ProviderCapabilities {
  configured: boolean;
  mode: string | null;
  provider: string | null;
  model: string | null;
  capabilities: {
    vision: boolean;
    structured_output: boolean;
  };
  degraded: boolean;
  degraded_reasons: DegradedReason[];
}

/** RFC 9457 problem details. */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  [extension: string]: unknown;
}

export function formatDegradedReason(reason: DegradedReason): string {
  if (typeof reason === 'string') return reason;
  return (
    reason.detail ??
    reason.message ??
    reason.title ??
    reason.reason ??
    reason.code ??
    'Raison non précisée.'
  );
}

// ---------------------------------------------------------------------------
// Shopping budget (contract §6ter)
// ---------------------------------------------------------------------------

export type BudgetPeriod = 'week' | 'month';

/**
 * What the displayed amount does not count.
 *
 * Rendered next to the figure and never behind a disclosure control: a coverage
 * number a user has to click to discover is a coverage number that does not
 * exist. Every field is always present, including at zero — an absent field and
 * a zero say the same thing to a naive reader, and one of the two means "we do
 * not know".
 */
export interface BudgetCoverage {
  receipts_with_total: number;
  receipts_missing_total: number;
  /** Items put into stock by hand or by scan: they carry no price at all. */
  stock_items_added_without_receipt: number;
}

/**
 * One currency's spending over the period. Two currencies stay two entries:
 * converting would need a rate, a rate date, and a decision that belongs to a
 * bank rather than to this application.
 *
 * `spent` and `target` are decimal strings, like `quantity.amount`, and are
 * never parsed into a JavaScript number outside `features/budget/money.ts`.
 */
export interface BudgetCurrency {
  currency: string;
  spent: string;
  receipt_count: number;
  /** Receipts whose lines contradict their printed total. Reported, never fixed. */
  line_sum_mismatch_count: number;
  target: string | null;
}

export interface Budget {
  period: BudgetPeriod;
  period_start: string;
  period_end: string;
  currencies: BudgetCurrency[];
  coverage: BudgetCoverage;
}

/** Complete periods only, oldest first: the one in progress is not a trend point. */
export interface BudgetHistory {
  period: BudgetPeriod;
  periods: Budget[];
}

export interface BudgetTarget {
  period: BudgetPeriod;
  amount: string;
  currency: string;
}

// ---------------------------------------------------------------------------
// Shopping-list export destinations (ADR-0010)
// ---------------------------------------------------------------------------

/**
 * Where a household has agreed its shopping list may be sent.
 *
 * **There is no token field, and there is not going to be one.** The server
 * never returns the stored credential in any form; `token_last4` is the whole
 * of what comes back, and it exists so someone with two Todoist accounts can
 * tell which key is installed.
 *
 * `is_consented` is what a client branches on. Deriving it from the two dates
 * instead is one inverted condition away from sending personal data on an
 * agreement that was withdrawn, which is why the server computes it.
 */
export interface ShoppingExportTarget {
  /** Stable code of the destination, e.g. `todoist`. */
  target: string;
  token_last4: string;
  /** The container items land in. `null` means the recipient's own inbox. */
  external_list_id: string | null;
  consented_at: string;
  /** When the household withdrew its agreement. The row survives it. */
  consent_revoked_at: string | null;
  is_consented: boolean;
  /**
   * Who pasted the token and ticked the box. `null` for a destination registered
   * before the server started recording it — which is "we do not know", not
   * "nobody".
   */
  registered_by: string | null;
  /**
   * Whether that person is still a member of the household.
   *
   * `false` means the list would be filed into the account of somebody who has
   * left, so the export refuses until an owner registers again or withdraws. It
   * is deliberately separate from `is_consented`: the agreement was never
   * withdrawn, and the remedies are different.
   */
  registrant_is_member: boolean;
}

/**
 * What registering a destination sends.
 *
 * `consent_granted` is required and separate from the token on purpose: pasting
 * a credential says "I have an account there", agreeing says "send my
 * household's shopping list to it". A form that inferred one from the other
 * would be a pre-ticked box.
 */
export interface ShoppingExportTargetDraft {
  token: string;
  consent_granted: boolean;
  external_list_id?: string;
}

/* -------------------------------------------------------------------------- */
/* Machine access tokens (contract v1.1 §10)                                   */
/* -------------------------------------------------------------------------- */

/**
 * The five scopes, in the order the contract lists them.
 *
 * Closed, additive, and never implicit: `inventory:write` does not grant
 * `inventory:read`, and holding all five is not an administrator. There is
 * deliberately no scope for recipe suggestions (the only endpoint that spends
 * money) and none for household members (allergens and infant age bands are
 * health data). Adding one is a contract change, not a constant.
 */
export const MACHINE_TOKEN_SCOPES = [
  'inventory:read',
  'inventory:write',
  'shopping:read',
  'shopping:write',
  'budget:read',
] as const;
export type MachineTokenScope = (typeof MACHINE_TOKEN_SCOPES)[number];

/** One token as the household reads it back. Carries no secret. */
export interface MachineToken {
  id: string;
  name: string;
  scopes: MachineTokenScope[];
  /** The fixed marker every Chaudron token starts with, e.g. `chdr_`. */
  prefix: string;
  /** The last four characters, enough to tell two tokens apart and nothing more. */
  last4: string;
  created_at: string;
  /** `null` until first used; refreshed at most once an hour, server-side. */
  last_used_at: string | null;
  /** `null` means the household chose no expiry, which the contract allows. */
  expires_at: string | null;
}

/**
 * The creation response, and the **only** one that ever carries `token`.
 *
 * There is no route that reads the value back. A screen that showed it twice
 * would make a screenshot enough to steal it.
 */
export interface MachineTokenCreated extends MachineToken {
  token: string;
}

/**
 * What creating a token sends.
 *
 * `scopes` has no default and the empty array is refused: a token that can do
 * nothing has no use except to look like an integration exists. `expires_in_days`
 * is required rather than optional so that "no expiry" is a choice somebody made
 * rather than a field they forgot.
 */
export interface MachineTokenDraft {
  name: string;
  scopes: MachineTokenScope[];
  expires_in_days: number | null;
}

// ---------------------------------------------------------------------------
// Receipt import (contract §6ter and §7)
// ---------------------------------------------------------------------------

export type ReceiptStatus = 'parsed' | 'confirmed' | 'failed';

/**
 * How the lines were obtained, and it is shown on screen.
 *
 * `text` means a drive recap's own text was read: no model was called, nothing
 * was billed, and nothing could have been invented because nothing generated
 * it. `model` means a vision model transcribed a photograph, which
 * `docs/technical-notes-ingestion.md` §3.4 measures at 0.49 F1 on line items.
 * The two do not deserve the same trust and the interface says so.
 */
export type ReceiptReadBy = 'text' | 'model';

export type ReceiptLineMatchStatus = 'pending' | 'suggested' | 'confirmed' | 'rejected' | 'ignored';

export interface ReceiptLine {
  id: string;
  line_no: number;
  /** What the till printed. Never overwritten, and shown whenever it differs. */
  raw_label: string;
  /** The readable form: an expansion when one was trusted, `raw_label` otherwise. */
  label: string;
  /**
   * A reading the abbreviation lexicon produced but declined to trust —
   * ambiguous, low-confidence, or leaving a token it could not expand. Offered
   * as a one-tap correction, never applied. `null` is the common case: recall is
   * 0.412 by measurement (`docs/label-lexicon.md`).
   */
  suggested_label: string | null;
  quantity: string | null;
  unit: string | null;
  unit_price: string | null;
  total_price: string | null;
  matched_product_id: string | null;
  match_status: ReceiptLineMatchStatus;
  /** A claim about the *label expansion* only. No provider exposes logprobs. */
  match_confidence: string | null;
}

/**
 * A stored, unconfirmed reading of one receipt.
 *
 * `total_amount` and `line_sum` are both here and are **never** reconciled by
 * either side. §6ter: models fabricate lines to force their sum onto the printed
 * total, so a gap between the two is the best signal available that a line was
 * invented, and closing it would delete the signal to tidy a screen.
 * `line_sum_delta` is that gap, pre-computed by the server.
 *
 * Decimal strings throughout, like every amount in this contract: a stock
 * quantity or a price wrong by a factor of ten because it went through an IEEE
 * 754 double is not a rounding detail.
 */
export interface Receipt {
  id: string;
  source: 'photo' | 'pdf';
  read_by: ReceiptReadBy;
  status: ReceiptStatus;
  merchant: string | null;
  /** The chain the abbreviation lexicon was scoped to, when it was recognised. */
  retailer: string | null;
  purchased_at: string | null;
  total_amount: string | null;
  currency: string | null;
  lines: ReceiptLine[];
  line_sum: string | null;
  line_sum_delta: string | null;
  truncated: boolean;
  degradation_notice: string | null;
}

/** One row of "receipts read but not yet accepted into stock". */
export interface ReceiptSummary {
  id: string;
  source: 'photo' | 'pdf';
  status: ReceiptStatus;
  merchant: string | null;
  purchased_at: string | null;
  total_amount: string | null;
  currency: string | null;
  line_count: number;
  created_at: string;
}

export interface PendingReceipts {
  receipts: ReceiptSummary[];
}

/**
 * One reviewed line, on its way to the only call that writes stock.
 *
 * Addressed by `id` because the proposal *is* stored server-side, unlike the
 * shopping-list one. A line **absent** from the body is a line the reviewer
 * removed: it is marked `ignored` and keeps its price, because "do not put this
 * in my cupboard" is not "this was not on the receipt".
 */
export interface ReceiptConfirmLine {
  id: string;
  product_id?: string;
  label?: string;
  quantity?: { amount: string; unit: string };
}

export interface ReceiptConfirmResult {
  receipt_id: string;
  created_lot_count: number;
  ignored_line_count: number;
  created_product_count: number;
}
