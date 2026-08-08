import { request, requestFile, requestText } from './client';
import type {
  Budget,
  BudgetHistory,
  BudgetPeriod,
  BudgetTarget,
  CreateInventoryItem,
  HouseholdMember,
  InventoryItem,
  InventoryItemPatch,
  InventoryPage,
  InventoryQuery,
  LocationDraft,
  MachineToken,
  MachineTokenCreated,
  MachineTokenDraft,
  MemberDraft,
  NewShoppingListItem,
  Product,
  PendingReceipts,
  ProviderCapabilities,
  Receipt,
  ReceiptConfirmLine,
  ReceiptConfirmResult,
  RecipeFeedbackState,
  RecipeFeedbackVerdict,
  RemovalReason,
  RemovalResult,
  ShoppingExportReceipt,
  ShoppingExportTarget,
  ShoppingExportTargetDraft,
  ShoppingImport,
  ShoppingImportConfirmLine,
  ShoppingImportResult,
  ShoppingItemPatch,
  ShoppingList,
  ShoppingListItem,
  StorageLocation,
  SuggestionQuality,
  SuggestRecipesRequest,
  SuggestRecipesResponse,
  UpdatedInventoryItem,
  WeeklyBalance,
} from './types';

export function getLocations(signal?: AbortSignal): Promise<StorageLocation[]> {
  return request<StorageLocation[]>('/locations', { signal });
}

/**
 * Creates a storage location. `409 location-name-taken` when the household
 * already has an active one with that name, compared without regard to case.
 *
 * A household is created with none at all, and nothing is seeded for it — see
 * the module docstring of `api/routers/locations.py` for why the empty state is
 * a decision. This is the call the empty state makes.
 */
export function createLocation(
  body: LocationDraft,
  signal?: AbortSignal,
): Promise<StorageLocation> {
  return request<StorageLocation>('/locations', { method: 'POST', body, signal });
}

export function getInventory(query: InventoryQuery, signal?: AbortSignal): Promise<InventoryPage> {
  return request<InventoryPage>('/inventory', {
    query: {
      location_id: query.location_id,
      q: query.q,
      expiring_within_days: query.expiring_within_days,
      limit: query.limit,
      offset: query.offset,
    },
    signal,
  });
}

export function createInventoryItem(
  body: CreateInventoryItem,
  signal?: AbortSignal,
): Promise<InventoryItem> {
  return request<InventoryItem>('/inventory', { method: 'POST', body, signal });
}

/**
 * Partial update of one item. Callers send only what changed: the unit the user
 * typed is preserved by *not* sending it, so no conversion can creep in
 * (contract v1.1 §6 — someone who wrote "1 L" must read back "1 L").
 *
 * The server logs the change in `stock_movement` with the `correction` reason;
 * a manual adjustment is not a consumption.
 */
export function updateInventoryItem(
  id: string,
  patch: InventoryItemPatch,
  signal?: AbortSignal,
): Promise<UpdatedInventoryItem> {
  return request<UpdatedInventoryItem>(`/inventory/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: patch,
    signal,
  });
}

/**
 * Removes a lot and reports what ran out.
 *
 * `200` with a body, not the `204` contract v1 froze: §6bis needs `depleted` on
 * this very response, and a `204` carries no body. `depleted` is always there,
 * `null` when there is nothing to propose — never absent, so the caller never has
 * to tell "no proposal" from "field forgotten".
 */
export function deleteInventoryItem(
  id: string,
  reason: RemovalReason = 'consumed',
  signal?: AbortSignal,
): Promise<RemovalResult> {
  return request<RemovalResult>(`/inventory/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    query: { reason },
    signal,
  });
}

/**
 * Reads pasted text into a **proposal** (§7.2). Nothing is written, and the
 * document is not stored server-side either.
 *
 * The deterministic parser answers on its own; a model is a second pass over
 * the lines it could not split (§7.1), so an instance with no provider
 * configured still imports a list. `parsed_by` says which of the two happened,
 * and the review screen shows it.
 */
export function importShoppingList(text: string, signal?: AbortSignal): Promise<ShoppingImport> {
  return request<ShoppingImport>('/shopping-lists/import', {
    method: 'POST',
    body: { text },
    signal,
  });
}

/**
 * The same proposal, from a PDF or a `.txt`.
 *
 * One operation, two content types — the server refuses anything else with a
 * `415`, refuses an oversized document with a `413` naming which of the three
 * bounds was hit, and keeps the file no longer than the request (§7.3).
 */
export function importShoppingListFile(file: File, signal?: AbortSignal): Promise<ShoppingImport> {
  return requestFile<ShoppingImport>('/shopping-lists/import', file, signal);
}

/**
 * Writes the reviewed lines. **The only call of the import that writes** (§7.2).
 *
 * `lines` is what the human kept and corrected, not what the parser read: the
 * proposal was never stored, so this body is authoritative on its own and the
 * server re-validates every value in it.
 */
export function confirmShoppingListImport(
  importId: string,
  lines: ShoppingImportConfirmLine[],
  signal?: AbortSignal,
): Promise<ShoppingImportResult> {
  return request<ShoppingImportResult>(
    `/shopping-lists/import/${encodeURIComponent(importId)}/confirm`,
    { method: 'POST', body: { lines }, signal },
  );
}

/** The household's current list, created server-side on first read (§6bis). */
export function getCurrentShoppingList(signal?: AbortSignal): Promise<ShoppingList> {
  return request<ShoppingList>('/shopping-lists/current', { signal });
}

/**
 * Ticks, unticks, or corrects a quantity.
 *
 * Send only the keys that changed: an absent `quantity` leaves it alone while
 * `quantity: null` clears it, and the server tells the two apart. Returns the
 * item, not the list.
 */
export function updateShoppingListItem(
  itemId: string,
  patch: ShoppingItemPatch,
  signal?: AbortSignal,
): Promise<ShoppingListItem> {
  return request<ShoppingListItem>(`/shopping-lists/current/items/${encodeURIComponent(itemId)}`, {
    method: 'PATCH',
    body: patch,
    signal,
  });
}

export function removeShoppingListItem(itemId: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/shopping-lists/current/items/${encodeURIComponent(itemId)}`, {
    method: 'DELETE',
    signal,
  });
}

/**
 * The pending items as text, one per line. **Nothing leaves the instance.**
 *
 * The string is rendered by the server so that the share sheet, the clipboard
 * fallback and every third-party adapter write an item identically. The browser
 * then hands it to `navigator.share({ text })` — with no `url`, because iOS
 * rewrites a cross-domain one and strips the other's query string.
 */
export function getShoppingListText(shoppingListId: string, signal?: AbortSignal): Promise<string> {
  return requestText(`/shopping-lists/${encodeURIComponent(shoppingListId)}/export/text`, signal);
}

/**
 * Transmits the list to a third party under this household's stored credential.
 *
 * Personal data leaves the instance here, so the destination has to be one the
 * household registered and agreed to (ADR-0010). A `409` with
 * `export-target-not-configured` means there is none — which is the default,
 * and an explanation rather than a failure.
 */
export function exportShoppingList(
  shoppingListId: string,
  target: string,
  signal?: AbortSignal,
): Promise<ShoppingExportReceipt> {
  return request<ShoppingExportReceipt>(
    `/shopping-lists/${encodeURIComponent(shoppingListId)}/export/${encodeURIComponent(target)}`,
    { method: 'POST', signal },
  );
}

/**
 * Adds items to the household's current list, in **one** call (§6bis).
 *
 * The array is the contract's, not a convenience: emptying a fridge depletes
 * several products at once, and one gesture must not become five requests, five
 * transactions and five ways to half-succeed.
 *
 * Not the import path. §6bis defines this endpoint precisely because routing
 * "add one product" through `import` then `confirm` is a detour it calls absurd
 * — and the detour also loses `source`, which exists so that "is the repurchase
 * proposal actually used?" can be measured rather than assumed.
 */
export function addShoppingListItems(
  items: NewShoppingListItem[],
  signal?: AbortSignal,
): Promise<ShoppingList> {
  return request<ShoppingList>('/shopping-lists/current/items', {
    method: 'POST',
    body: { items },
    signal,
  });
}

/**
 * Records that these products must not be proposed for repurchase again.
 *
 * Perpetual by design (§6bis): a refusal that expired would re-propose exactly
 * what the household waved away, with nothing on screen to explain the return.
 * Revocable, because people change their minds — `DELETE
 * /v1/shopping-lists/declined/{product_id}`, not wired to a screen yet.
 */
export function declineRepurchase(productIds: string[], signal?: AbortSignal): Promise<void> {
  return request<void>('/shopping-lists/declined', {
    method: 'POST',
    body: { product_ids: productIds },
    signal,
  });
}

/**
 * Resolves a GTIN. Callers must screen out store-internal codes first (see
 * `isRestrictedCirculationNumber`): those are guaranteed 404s that burn the
 * shared Open Food Facts rate limit. The server's 422 is a safety net, not the
 * intended path.
 */
export function lookupProduct(gtin: string, signal?: AbortSignal): Promise<Product> {
  return request<Product>('/products/lookup', { query: { gtin }, signal });
}

export function suggestRecipes(
  body: SuggestRecipesRequest,
  signal?: AbortSignal,
): Promise<SuggestRecipesResponse> {
  return request<SuggestRecipesResponse>('/recipes/suggest', { method: 'POST', body, signal });
}

/**
 * Records — or changes — what the household thought of one suggestion.
 *
 * `PUT`, because there is exactly one opinion per suggestion and the latest wins:
 * tapping the same button twice must land where tapping it once did. The verdict
 * only ever reorders future suggestions; it never removes anything from them, and
 * it is never sent to the model.
 */
export function setRecipeFeedback(
  suggestionId: string,
  feedback: RecipeFeedbackVerdict,
  signal?: AbortSignal,
): Promise<RecipeFeedbackState> {
  return request<RecipeFeedbackState>(
    `/recipes/suggestions/${encodeURIComponent(suggestionId)}/feedback`,
    { method: 'PUT', body: { feedback }, signal },
  );
}

/** Withdraws the verdict. Answers with the resulting state, not `204`. */
export function clearRecipeFeedback(
  suggestionId: string,
  signal?: AbortSignal,
): Promise<RecipeFeedbackState> {
  return request<RecipeFeedbackState>(
    `/recipes/suggestions/${encodeURIComponent(suggestionId)}/feedback`,
    { method: 'DELETE', signal },
  );
}

/**
 * Feedback aggregated by provider and model, for this household.
 *
 * The one figure that turns "the small local model degrades gracefully" into
 * something readable. A server without the route simply shows nothing.
 */
export function getSuggestionQuality(signal?: AbortSignal): Promise<SuggestionQuality> {
  return request<SuggestionQuality>('/recipes/quality', { signal });
}

export function getProviderCapabilities(signal?: AbortSignal): Promise<ProviderCapabilities> {
  return request<ProviderCapabilities>('/providers/capabilities', { signal });
}

/**
 * Household members carry health data — an infant's age band included. Nothing
 * here puts a member in a URL path beyond its opaque id, and no caller stores a
 * member outside React state.
 */
export function getMembers(signal?: AbortSignal): Promise<HouseholdMember[]> {
  return request<HouseholdMember[]>('/members', { signal });
}

export function createMember(body: MemberDraft, signal?: AbortSignal): Promise<HouseholdMember> {
  return request<HouseholdMember>('/members', { method: 'POST', body, signal });
}

export function updateMember(
  id: string,
  body: MemberDraft,
  signal?: AbortSignal,
): Promise<HouseholdMember> {
  return request<HouseholdMember>(`/members/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body,
    signal,
  });
}

export function deleteMember(id: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/members/${encodeURIComponent(id)}`, { method: 'DELETE', signal });
}

export function getBalance(signal?: AbortSignal): Promise<WeeklyBalance> {
  return request<WeeklyBalance>('/balance', { signal });
}

/**
 * Grocery spending over one calendar period (contract §6ter).
 *
 * The server computes it from `receipt.total_amount` and never from the sum of
 * the receipt lines — the lines are the field vision models fabricate to make
 * the arithmetic land on the printed total. Nothing here is fetched until the
 * user has asked for it: budget tracking is proposed, not imposed.
 */
export function getBudget(period: BudgetPeriod, signal?: AbortSignal): Promise<Budget> {
  return request<Budget>('/budget', { query: { period }, signal });
}

export function getBudgetHistory(
  period: BudgetPeriod,
  count: number,
  signal?: AbortSignal,
): Promise<BudgetHistory> {
  return request<BudgetHistory>('/budget/history', { query: { period, count }, signal });
}

/** `null` when the household set no target, which is the normal state. */
export function setBudgetTarget(body: BudgetTarget, signal?: AbortSignal): Promise<BudgetTarget> {
  return request<BudgetTarget>('/budget/target', { method: 'PUT', body, signal });
}

export function clearBudgetTarget(period: BudgetPeriod, signal?: AbortSignal): Promise<void> {
  return request<void>('/budget/target', { method: 'DELETE', query: { period }, signal });
}

// ---------------------------------------------------------------------------
// Shopping-list export destinations (ADR-0010)
// ---------------------------------------------------------------------------

/**
 * Destinations this household may send its shopping list to **right now**.
 *
 * Consented destinations only: a destination whose agreement was withdrawn is
 * absent from this list, so what comes back is what a screen may offer. Always
 * an array — empty is the normal state and is never spelled `404`, which a
 * client would otherwise have to read as data.
 *
 * This is what replaced remembering a failed export in `localStorage`: the
 * question "does this household have a destination?" now has an endpoint.
 */
export function getExportTargets(signal?: AbortSignal): Promise<ShoppingExportTarget[]> {
  return request<ShoppingExportTarget[]>('/shopping-lists/export/targets', { signal });
}

/**
 * One destination, withdrawal included — the settings view.
 *
 * Unlike the list above this returns a destination whose agreement was
 * withdrawn, because ADR-0010 keeps the row precisely so a household can see
 * what it once authorised. `404` means nothing was ever registered, which is a
 * different thing to show.
 */
export function getExportTarget(
  target: string,
  signal?: AbortSignal,
): Promise<ShoppingExportTarget> {
  return request<ShoppingExportTarget>(
    `/shopping-lists/export/targets/${encodeURIComponent(target)}`,
    { signal },
  );
}

/**
 * Registers a destination, with the agreement that lets it be used.
 *
 * The token travels in the body and never in the path or the query string: a
 * credential in a URL lands in access logs, proxy logs and browser history.
 * Nothing in the response carries it back — `token_last4` is all a household
 * ever sees again. Calling this a second time is the rotation procedure.
 */
export function registerExportTarget(
  target: string,
  body: ShoppingExportTargetDraft,
  signal?: AbortSignal,
): Promise<ShoppingExportTarget> {
  return request<ShoppingExportTarget>(
    `/shopping-lists/export/targets/${encodeURIComponent(target)}`,
    { method: 'PUT', body, signal },
  );
}

/**
 * Withdraws the agreement. The row survives, so this answers with it.
 *
 * Not a deletion: the household keeps the record of what it authorised and
 * when. It takes effect at the next export rather than at some later cleanup.
 */
export function revokeExportTarget(
  target: string,
  signal?: AbortSignal,
): Promise<ShoppingExportTarget> {
  return request<ShoppingExportTarget>(
    `/shopping-lists/export/targets/${encodeURIComponent(target)}`,
    { method: 'DELETE', signal },
  );
}

/* -------------------------------------------------------------------------- */
/* Machine access tokens (contract v1.1 §10)                                   */
/* -------------------------------------------------------------------------- */

/**
 * This household's machine tokens. No response here carries a token value.
 *
 * Requires a browser session: the server refuses these three routes to any
 * bearer credential, so a leaked token cannot enumerate its neighbours, mint a
 * replacement, or revoke the ones an owner is relying on.
 */
export function getMachineTokens(signal?: AbortSignal): Promise<MachineToken[]> {
  return request<MachineToken[]>('/tokens', { signal });
}

/**
 * Issues a token. **The only response in this API that contains one.**
 *
 * The caller must show the value immediately and must not store it: there is no
 * endpoint that returns it again, by design. Losing it means issuing another and
 * revoking the one that was lost.
 */
export function createMachineToken(
  body: MachineTokenDraft,
  signal?: AbortSignal,
): Promise<MachineTokenCreated> {
  return request<MachineTokenCreated>('/tokens', { method: 'POST', body, signal });
}

/** Revokes a token. It stops working on the next request that presents it. */
export function revokeMachineToken(id: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/tokens/${encodeURIComponent(id)}`, { method: 'DELETE', signal });
}

// ---------------------------------------------------------------------------
// Receipt import (contract §6ter and §7)
// ---------------------------------------------------------------------------

/**
 * Read a till receipt photograph or a drive order recap.
 *
 * **This one writes**, unlike the shopping-list import: it returns `201` and a
 * stored proposal with a URL. That is deliberate — a photograph costs a model
 * call the household pays for, so losing the reading to a page reload would
 * charge them twice — and it is why `discardReceipt` exists: a parsed receipt
 * already counts towards the budget (§6ter counts a receipt as soon as it has a
 * total and a currency, before its lines are accepted), so an import abandoned
 * mid-review must be deletable.
 *
 * A PDF needs no model at all: the server reads the text the document already
 * carries. A photograph needs one that can see, and the server answers `409`
 * with the reason when there is none, rather than failing at the click.
 */
export function importReceipt(file: File, signal?: AbortSignal): Promise<Receipt> {
  return requestFile<Receipt>('/receipts/import', file, signal);
}

/** One stored reading, so a reload does not lose a review in progress. */
export function getReceipt(receiptId: string, signal?: AbortSignal): Promise<Receipt> {
  return request<Receipt>(`/receipts/${encodeURIComponent(receiptId)}`, { signal });
}

/**
 * Receipts read but not yet accepted into stock.
 *
 * The list is what keeps an abandoned import findable — and therefore
 * discardable. Without it, money would sit in the budget with nothing on any
 * screen pointing at it.
 */
export function getPendingReceipts(signal?: AbortSignal): Promise<PendingReceipts> {
  return request<PendingReceipts>('/receipts', { signal });
}

/**
 * Write the reviewed lines into stock. **The only call that touches the
 * inventory.**
 *
 * `lines` is what the human kept and corrected. A line left out is marked
 * `ignored` and keeps its price: removing it from a cupboard is not a claim that
 * it was never on the receipt, and the sum shown beside the total has to stay
 * the sum of what was printed.
 */
export function confirmReceipt(
  receiptId: string,
  lines: ReceiptConfirmLine[],
  locationId: string | null,
  signal?: AbortSignal,
): Promise<ReceiptConfirmResult> {
  return request<ReceiptConfirmResult>(`/receipts/${encodeURIComponent(receiptId)}/confirm`, {
    method: 'POST',
    body: { lines, location_id: locationId },
    signal,
  });
}

/**
 * Throw a reading away, and the spend it was counting with it.
 *
 * Refused once confirmed: those lines are lots in a cupboard, and deleting the
 * receipt would leave them with a dangling provenance while the money they were
 * bought with silently left the budget.
 */
export function discardReceipt(receiptId: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/receipts/${encodeURIComponent(receiptId)}`, {
    method: 'DELETE',
    signal,
  });
}
