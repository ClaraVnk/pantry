# API contract — v1 (vertical slice)

This document freezes the interface between the backend and the PWA for the first
functional slice. It is written **before** the code on either side, so that both
can be built in parallel without diverging.

Deliberately narrow scope: inventory, locations, barcode resolution, recipe
suggestions. Receipt parsing and inbound email are not in this slice.

Base: `/v1`. Everything is JSON. Errors follow RFC 9457 (`application/problem+json`).

---

## Authentication (slice 1)

No user accounts in this slice. The current household is resolved from a header:

```
X-Household-Id: <uuid>
```

Absent or unknown → `401`. A demonstration household is created by the seed.

> This mechanism is **provisional and documented as such**. It exists so that the
> slice is testable end to end without building authentication first. The shape
> of the contract does not change when a real session arrives: the household
> stays resolved server-side, never sent by the client.

---

## Health

| Method | Path | Response |
|---|---|---|
| `GET` | `/healthz` | `200 {"status":"ok"}` — the process is alive. Does not touch the database. |
| `GET` | `/readyz` | `200 {"status":"ready","checks":{"database":"ok"}}` or `503` with the detail |

---

## Storage locations

`GET /v1/locations` →

```json
[{"id":"uuid","name":"Fridge","kind":"fridge","item_count":12}]
```

`kind` ∈ `fridge` | `freezer` | `pantry` | `cellar` | `other`.

`POST /v1/locations` →

```json
{"name":"Fridge","kind":"fridge"}
```

`201` with the created location (`item_count` at `0`). `name`: 1 to 80
characters, edge whitespace stripped. `409 location-name-taken` if the household
already owns an active location with that name, compared case-insensitively.

> **Added after this contract was frozen.** v1 provided for no creation at all:
> locations came from the demonstration seed, and the only household that existed
> had some. Since authentication arrived, a freshly registered household has
> **zero** and had no way to obtain one — the first screen after registration was
> a dead end.
>
> **Nothing is seeded at registration**, and that is a choice: there is neither
> deletion nor archiving of a location on the API side, so a default that does
> not fit the household stays there forever. The full argument is in the
> docstring of `api/routers/locations.py`. The client compensates by offering the
> three common names in one gesture from its empty state — the convenience of a
> seed, without the row nobody chose.

---

## Inventory

`GET /v1/inventory` — parameters: `location_id`, `q`, `expiring_within_days`,
`limit` (default 50), `offset`.

```json
{
  "total": 37,
  "items": [{
    "id": "uuid",
    "product": {"id":"uuid","name":"Semi-skimmed milk","brand":"Lactel","gtin":"3033490004743","image_url":null},
    "location": {"id":"uuid","name":"Fridge","kind":"fridge"},
    "quantity": {"amount":"1.000","unit":"L"},
    "expires_on": "2026-08-12",
    "expiry_kind": "use_by",
    "opened_at": null,
    "source": "barcode_scan",
    "created_at": "2026-08-03T18:20:00Z"
  }]
}
```

`quantity.amount` is a **string**, not a number: JSON floats destroy exact
decimals, and a quantity wrong by a factor of ten in a food inventory is not a
detail.

`source` ∈ `manual` | `barcode_scan` | `receipt_import`.

`location` is **nullable**: `inventory_lot.storage_location_id` is nullable in
the database, and a lot whose location has been archived reads back without one.
The client must render that state, not assume it impossible — it had typed the
field non-nullable, which crashed the inventory screen on `item.location.id`.

`POST /v1/inventory` →

```json
{"product_id":"uuid","location_id":"uuid","amount":"1.5","unit":"kg",
 "expires_on":"2026-08-20","expiry_kind":"best_before","source":"manual"}
```

`201` with the created item. `product_id` **or** `product` (a manual product
creation object) must be provided, not both.

`PATCH /v1/inventory/{id}` — editable fields: `amount`, `unit`, `location_id`,
`expires_on`, `expiry_kind`, `opened_at`.

`DELETE /v1/inventory/{id}` — ~~`204`~~ **`200`** (see the note below).
Parameter `reason` ∈ `consumed` | `wasted` | `correction` (default `consumed`),
logged in `stock_movement`.

> **Deliberate deviation from this frozen contract.** This version said
> `204 No Content`. Extension v1.1 §6bis requires the body of that same `DELETE`
> to carry a `depleted` field (the repurchase proposal), and a `204` carries no
> body: the two requirements cannot hold together. The endpoint therefore
> answers `200` with:
>
> ```json
> {"depleted": {"...": "..."} }
> ```
>
> The `depleted` field is **always present**, `null` when there is nothing to
> propose — a client that has to tell "no proposal" from "forgotten field" will
> guess, and will guess wrong on the day it matters. Same principle as
> `uncategorised_product_count`. The full reasoning is in §6bis of
> `api-contract-v1.1-dietary.md`.
>
> `PATCH /v1/inventory/{id}` gains the same field, flat alongside the item's own
> fields. `GET /v1/inventory` **does not carry it**: a page of 200 rows has no
> business carrying 200 `null`s.

---

## Products and barcodes

`GET /v1/products/lookup?gtin=3033490004743` →

- `200` with the product record (from the cache, otherwise Open Food Facts, then cached)
- `404` `{"type":"...product-not-found","gtin":"..."}` if not found — the client
  then falls back to manual entry
- `422` if the code is an **in-store internal code** (prefix `02`, `20`–`29`):
  variable weight, it will never be in a public reference. The client must detect
  this case itself and not call the API at all; the server-side check is a net.
- `503` if Open Food Facts is unreachable or has rate-limited us, with `Retry-After`

`POST /v1/products` — manual creation: `{"name":"...","brand":null,"gtin":null,
"default_unit":"g"}` → `201`. The product is private to the household.

---

## Recipes

`POST /v1/recipes/suggest` →

```json
{"location_ids": [], "max_suggestions": 3, "notes": "quick, no oven"}
```

Response `200`:

```json
{
  "provider_mode": "instance_owner",
  "model": "claude-opus-5",
  "suggestions": [{
    "id":"uuid","title":"Courgette gratin","summary":"...",
    "duration_minutes":35,"servings":4,
    "ingredients":[{"name":"Courgettes","amount":"600","unit":"g","in_stock":true}],
    "steps":["...","..."],
    "uses_expiring_soon": true
  }]
}
```

`409` `{"type":"...provider-not-configured"}` if the household has no usable
provider. The client shows the configuration screen, not a raw error.

---

## Provider capabilities

`GET /v1/providers/capabilities` →

```json
{
  "configured": true,
  "mode": "instance_owner",
  "provider": "anthropic",
  "model": "claude-opus-5",
  "capabilities": {"vision": true, "structured_output": true},
  "degraded": false,
  "degraded_reasons": []
}
```

When `degraded` is `true`, `degraded_reasons` states in plain language what is
reduced or unavailable and why. **The PWA displays this state permanently, not at
the moment of failure**: the user must know the limit before trying, not after.

---

## Error shape

```json
{"type":"https://chaudron.dev/problems/product-not-found",
 "title":"Product not found","status":404,
 "detail":"No product matches GTIN 3033490004743.","gtin":"3033490004743"}
```

No error returns an exception trace, and none leaks a provider key — including in
`detail`.
