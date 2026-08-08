# API contract — extension v1.1: dietary constraints

Additive extension of the v1 contract (`docs/api-contract-v1.md`). Nothing that
already exists changes shape; the fields added to existing responses are optional
on the client side.

The decisions this document implements are in
[ADR-0009](adr/0009-dietary-constraints-and-weekly-balance.md). This file is
frozen **before** the code on either side, like the v1 contract, to allow work in
parallel.

---

## 1. Closed vocabularies

These lists are normative. No value outside them may appear in a response.

### 1.1 Allergens — the 14 subject to mandatory declaration (EU 1169/2011, Annex II)

| Code | Label | Open Food Facts tag |
|---|---|---|
| `gluten` | Cereals containing gluten | `en:gluten` |
| `crustaceans` | Crustaceans | `en:crustaceans` |
| `eggs` | Eggs | `en:eggs` |
| `fish` | Fish | `en:fish` |
| `peanuts` | Peanuts | `en:peanuts` |
| `soybeans` | Soybeans | `en:soybeans` |
| `milk` | Milk | `en:milk` |
| `nuts` | Nuts | `en:nuts` |
| `celery` | Celery | `en:celery` |
| `mustard` | Mustard | `en:mustard` |
| `sesame` | Sesame seeds | `en:sesame-seeds` |
| `sulphites` | Sulphur dioxide and sulphites | `en:sulphur-dioxide-and-sulphites` |
| `lupin` | Lupin | `en:lupin` |
| `molluscs` | Molluscs | `en:molluscs` |

> The tag mapping must be **checked against the Open Food Facts taxonomy** at
> implementation time, not copied from here: tags evolve and some have synonyms.
> This table is the starting point, not the proof.

Intolerances outside the regulatory list (lactose, FODMAP, histamine…) and any
other restriction go through the free-text field, which has a different status —
see §4.

### 1.2 Allergen state of a product

Three states, **never two**:

| State | Meaning |
|---|---|
| `contains` | Declared present |
| `may_contain` | Traces declared |
| `unknown` | **No data.** Treated as `may_contain` for filtering |

### 1.3 Diets

`omnivore`, `pescatarian`, `vegetarian`, `vegan`.

### 1.4 Infant textures

`smooth`, `soft_pieces`, `pieces`.

### 1.5 Age bands

`adult`, `child`, then for infants: `infant_4_6m`, `infant_6_9m`,
`infant_9_12m`, `infant_12_36m`.

Never a date of birth. The band is enough to apply the rules.

---

## 2. Household members

`GET /v1/members` →

```json
[{
  "id": "uuid",
  "display_name": "Camille",
  "age_band": "adult",
  "diet": "vegetarian",
  "allergens": ["nuts", "celery"],
  "free_text_restrictions": "no coriander",
  "infant_texture": null
}]
```

`POST /v1/members`, `PATCH /v1/members/{id}`, `DELETE /v1/members/{id}` — same
fields. `infant_texture` must be `null` if `age_band` is not an infant band, and
non-`null` if it is; the inconsistency is a `422`.

> These records carry **health data**, and for an infant that of a minor. They
> are never logged in clear, never included in an error message, and deleting a
> member deletes their constraints.

---

## 3. Recipe suggestion — added fields

`POST /v1/recipes/suggest`, extended body:

```json
{
  "location_ids": [],
  "max_suggestions": 3,
  "notes": "quick, no oven",
  "member_ids": ["uuid", "uuid"],
  "balance_mode": "weekly"
}
```

- `member_ids` — who we are cooking for. **The union** of their constraints
  applies. Empty or absent: all members of the household.
- `balance_mode` — `weekly` (default) or `off`.

Response, added fields:

```json
{
  "provider_mode": "ollama",
  "model": "qwen2.5:3b",
  "applied_constraints": {
    "members": [{"id": "uuid", "display_name": "Camille"}],
    "excluded_allergens": ["nuts", "celery"],
    "diet": "vegetarian",
    "infant_texture": "soft_pieces",
    "age_bands": ["adult", "infant_6_9m"],
    "products_withheld": 7,
    "products_unverified": 4
  },
  "balance": {
    "reference": "pnns-2019",
    "window_days": 7,
    "gaps": [
      {"marker": "fish", "label": "Fish", "target": "2 per week", "observed": 0, "shortfall": 2},
      {"marker": "legumes", "label": "Legumes", "target": "2 per week", "observed": 1, "shortfall": 1}
    ],
    "excesses": [
      {"marker": "red_meat", "label": "Red meat", "target": "500 g per week", "observed_grams": 780}
    ],
    "satisfiable_from_stock": false,
    "note": "Stock cannot close these gaps this week."
  },
  "suggestions": [{
    "id": "uuid",
    "title": "…",
    "allergen_assessment": {
      "declared_clear_of": ["nuts", "celery"],
      "unverified_product_count": 2,
      "statement": "No declared allergen among the identified products. 2 products have no allergen data."
    },
    "…": "…"
  }]
}
```

**`allergen_assessment.statement` is the only wording the interface is allowed to
display**, and it is produced by the server. It is negative and situated by
construction: "no declared allergen among the identified products", never "nut
free". The client does not compose its own sentence from the fields.

`products_withheld` and `products_unverified` exist so that the interface can
explain a short or empty list of suggestions as something other than a failure.

### New error cases

| Status | `type` | When |
|---|---|---|
| `409` | `…/no-suggestion-within-constraints` | The filtered stock allows no suggestion. **This is not a runtime error**: the interface explains which constraints emptied the inventory. |
| `422` | `…/member-not-in-household` | A `member_id` from another household |
| `502` | `…/constraint-violation-detected` | The post-call check found a forbidden ingredient. The suggestion is thrown away, never rendered partially. |

---

## 4. The free-text field

`free_text_restrictions` is passed to the model as a preference, **and only as a
preference**. It cannot be applied as a filter: the system does not know which
products contain coriander.

It must therefore never receive an allergen. The interface must say so explicitly
next to the field, and a text there resembling an allergen from the regulatory
list triggers a warning inviting the user to tick the corresponding box.

This field is also **user input reaching the prompt**: it goes through the same
neutralisation as product labels (`infra/untrusted_text.py`, cf. AUD-006).

---

## 4bis. Three classes of constraint, three mechanisms

The product now handles three natures of constraint, and conflating them is the
mistake to avoid. What decides the mechanism is not perceived importance, it is
**verifiability** and **the cost of failure**.

| Class | Examples | Mechanism | What happens if it fails |
|---|---|---|---|
| **Verifiable and serious** | Allergens, diet, infant texture, age-based prohibitions | **Filter**: removal before the call, invalidation after. The model is never consulted. | Physical consequence |
| **Computable by the application** | Near expiry, PNNS balance | **Ranking**: the application measures, the model only writes | Waste, imbalance |
| **Not verifiable** | Hot/cold, free-text restrictions | **Prompt instruction**, and nothing else is possible | Disappointment |

The third class is the one where a prompt instruction is the **right** mechanism,
precisely because no deterministic check exists and because failure hurts nobody.
It is the exact opposite of the reasoning applied to allergens, and this asymmetry
is deliberate: a mechanism is chosen not on principle but on consequence.

**No constraint of the third class may be presented as a guarantee.** The
interface announces it as a preference passed on, never as a filter applied.

---

## 4ter. Meal temperature

Field of the suggestion request:

```json
"meal_temperature": "any" | "hot" | "cold"
```

Default `any`. It is a **preference**, passed to the model, never a filter — we
cannot determine programmatically whether a recipe is cold.

To make the divergence visible anyway, the suggestion declares:

```json
"preparation": {
  "serving_temperature": "hot" | "cold" | "either",
  "requires_cooking": true,
  "requires_oven": false
}
```

These fields are **self-declared by the model**. They let the interface flag a
divergence — "you asked for cold, this suggestion is served hot" — without
pretending to prevent it. A divergence does not invalidate the suggestion: it is
displayed, and the user decides.

`requires_oven` deserves its own field for the same reason that motivates the
request: at 35 degrees, what one refuses is not only to eat hot, it is to switch
on the oven.

The mode is remembered per household as the last choice, with **no automatic
inference** from the season or the weather: that would require knowing the
household's hemisphere and location, that is, collecting data this product does
not need to know in order to suggest a salad.

---

## 5. Priority to near dates — a soft goal

Using up what expires soon first is the reason this product exists: "throw in
whatever you have" means *before it goes in the bin*. It is a **soft goal**, never
a condition — a recipe that uses nothing urgent is still a valid recipe.

Two soft goals coexist and can contradict each other: what is expiring is
sometimes exactly what should not be eaten. The order is explicit and owned:

1. **Hard constraints** — allergens, diet, texture, age-based prohibitions.
   Nothing goes before them.
2. **Near expiry** — the **primary** soft goal. It is the application's first
   value, and waste is the problem it solves.
3. **Weekly balance** — the secondary soft goal. It steers the choice between
   several equally urgent recipes; it does not make anyone throw a product away
   to respect a benchmark.

The inventory passed to the model is **ordered by urgency** and each line carries
its deadline, rather than leaving the model to infer a priority from a date. The
suggestion exposes what actually drove its ranking:

```json
"suggestions": [{
  "uses_expiring_soon": true,
  "expiry_pressure": {
    "items_used_expiring_within_days": 3,
    "urgent_items": [
      {"inventory_item_id": "uuid", "product_name": "Plain stirred yoghurt", "expires_on": "2026-08-05", "days_left": 1}
    ],
    "urgent_items_left_unused": 2
  }
}]
```

`urgent_items_left_unused` is the honest field: it says what the suggestion
**did not** save. Without it, a list of recipes ignoring the yoghurt that expires
tomorrow looks like a deliberate choice.

The ranking of the returned suggestions follows this order. When none can use an
urgent product — because the hard constraints exclude it, for instance — the
response says so in `balance.note` rather than keeping quiet about it.

---

## 6. Manual quantity adjustment

`PATCH /v1/inventory/{id}` already exists in the v1 contract and accepts `amount`,
`unit`, `location_id`, `expires_on`, `expiry_kind`, `opened_at`. No change to the
contract is needed; what is missing is a screen that calls it.

Two requirements, both driven by what the data model already imposes:

**Every adjustment is logged in `stock_movement`.** Stock that changes without a
trace makes the weekly balance wrong, since it is computed from those movements
(ADR-0009). A manual adjustment carries the reason `correction` — it must **not**
count as consumption: correcting "500 g" to "300 g" because the entry was wrong is
not having eaten 200 g.

**The unit entered is kept as it is.** The model stores the entered pair and the
canonical pair separately (`docs/data-model.md` §6); a user who wrote "1 L" must
read back "1 L", not "1000 ml".

On the interface side, the adjustment must be reachable from the inventory row
without going through a full form: the common gesture is "there's half of it
left", not "I'm re-editing the record".

---

## 6bis. Item depletion → repurchase proposal

When the quantity of an item drops to zero, the application **proposes** adding it
to the shopping list. It does not add it.

### The movement's reason decides, and it already exists

`stock_movement` carries the reason of each outflow. It alone determines whether a
proposal makes sense:

| Reason | Propose? | Why |
|---|---|---|
| `consumed` | **Yes** | We ate it, we will probably buy it again |
| `wasted` | **Yes** | We no longer have it. That it went to waste does not change the fact |
| `correction` | **Never** | Correcting "500 g" to "0" because the entry was wrong is not having finished the product |

Conflating the three would give a shopping list polluted by typos, and it is the
same reason a manual adjustment does not count as consumption (§6).

### Propose, never do

Automatic adding would be a mistake: we stop buying things, and a list that fills
itself up becomes a list that gets ignored. The proposal is explicit, and its
refusal is **remembered** — a product that has been dismissed is not proposed
again every time it runs out.

### Group, do not interrupt

Emptying the fridge after a meal can deplete five items. Five dialogs in a row
would make people abandon the feature: the proposal is **grouped**,
non-blocking, and never gets in the way of the gesture under way.

### Shape

The body of `DELETE /v1/inventory/{id}` and of `PATCH /v1/inventory/{id}` answers
with:

```json
"depleted": {
  "product_id": "uuid",
  "product_name": "UHT semi-skimmed milk",
  "reason": "consumed",
  "already_on_list": false,
  "previously_declined": false
}
```

`already_on_list` avoids proposing what is already there. `previously_declined`
lets the client stay silent without having to hold that state itself.

### The initial shape was unworkable — what was settled

This section required an **optional** field on the body of the `DELETE`. The v1
contract freezes that same `DELETE` at `204 No Content` — and a `204` carries no
body, therefore no field, optional or not. The two texts were irreconcilable;
neither one could be applied as written.

What was decided, and why:

**`DELETE /v1/inventory/{id}` goes from `204` to `200`**, with
`{"depleted": {…} | null}` as its body. It is the most visible deviation, and it
is written in `api-contract-v1.md` too, at the `DELETE`. The alternative — keeping
the `204` and making a second call to find out what has just run out — adds a
round trip to a gesture repeated five times when emptying a fridge, and turns
information the server already holds into a question.

**The field is always present, `null` when there is nothing to propose.**
"Optional" would have left the client distinguishing absence from a null value,
and a client that has to guess will guess wrong precisely on the deletion that
mattered. Same principle as `uncategorised_product_count`.

**On the `PATCH`, `depleted` is flat**, alongside the item's own fields, as this
section writes it — not inside a wrapper object. It is carried by a response shape
distinct from that of the `GET`: `GET /v1/inventory` returns up to 200 rows, and
adding a structurally empty `depleted: null` to each of them would weigh down the
application's most frequent response, indefinitely.

**An adjustment never proposes anything today, and that is consistent.** The
server refuses a null or negative quantity on a `PATCH`: emptying an item goes
through the `DELETE`. And a manual adjustment carries the reason `correction`
(§6), which the table above excludes. The field is therefore present and is
`null` — the wiring exists so the rule stays true if a zero ever becomes
representable, it does not bypass the filter.

**The proposal cannot cost the mutation.** It is computed afterwards, inside a
`SAVEPOINT`: if it fails, the deletion or the correction stands and `depleted` is
`null`. The reverse — a courtesy that rolls back a deletion the user has seen
succeed — is acceptable in no direction.

### The write path — filled in after the fact

The initial version of this section said "adding goes through the shopping list's
normal write path". **That path existed nowhere**, and the agent in charge of the
interface reported it rather than inventing one: the only endpoints defined were
those of the import (§7), whose `import` → `confirm` detour to add one item is
absurd.

It is defined here.

| Method | Path | Role |
|---|---|---|
| `GET` | `/v1/shopping-lists/current` | The household's current list, created on the fly if absent |
| `POST` | `/v1/shopping-lists/current/items` | Adds one or more items **in a single call** |
| `PATCH` | `/v1/shopping-lists/current/items/{id}` | Ticks, unticks, corrects the quantity |
| `DELETE` | `/v1/shopping-lists/current/items/{id}` | Removes |

Adding accepts an **array**, not a single item: the repurchase proposal is grouped
by design, and an endpoint accepting only one item would force the client into
five calls for a single user gesture.

```json
POST /v1/shopping-lists/current/items
{"items": [
  {"product_id": "uuid", "amount": "1", "unit": "l", "source": "depleted"},
  {"free_text": "something for dessert"}
]}
```

`source` ∈ `manual` | `depleted` | `import`. It makes it possible to measure later
whether the repurchase proposal is actually good for anything, rather than
assuming it.

### Declining a proposal

`previously_declined` could never become true: nothing allowed a refusal to be
declared. Gap filled:

`POST /v1/shopping-lists/declined` — `{"product_ids": ["uuid", "uuid"]}`

`DELETE /v1/shopping-lists/declined/{product_id}` cancels the refusal, because
people change their minds and because a final decision taken in three seconds over
a bin has no business being irrevocable.

The refusal is **per household and per product**, with no expiry date. A refusal
that faded after a few months would propose again exactly what the user had
dismissed, and they would not know why.

It is stored in the `declined_repurchase` table (migration `0007`), which
deliberately has **no expiry column** — perpetuity is in the schema, not in an
application convention. Declaring the same refusal twice has no effect (`204` both
times): the same proposal dismissed on the phone and then on the tablet is one
refusal, not a conflict.

---

## 6ter. Grocery budget

When a till receipt or a click-and-collect order confirmation is imported, the
application **proposes** to track cumulative spending. It does not impose it: a
household may want to manage its stock without having its money counted.

### The printed total, never the sum of the lines

This is the point that decides the reliability of this whole feature, and it is
counter-intuitive.

`docs/technical-notes-ingestion.md` §3 reports, over a set of 10,656 receipts,
that models **fabricate or alter lines to force the sum onto the printed total**.
The "detail lines" field is the worst of all: 0.49 F1 at best. A receipt whose
arithmetic adds up is therefore **not** proof of accuracy — it is sometimes the
symptom of the opposite.

It follows that:

- **The budget is computed on `receipt.total_amount`**, a single number, printed
  large, which a human checks at a glance on the review screen.
- **Stock is built from the lines**, which are approximate by nature and go
  through human correction.

A consequence to own and to display: on the same photo, **the budget is more
reliable than the inventory**. It depends on one number instead of thirty.

A divergence between the total and the sum of the lines is **not** silently
corrected. It is exposed: it is the best indicator we have that a line has been
invented.

### Never add up what you do not know

The budget only knows what came through a receipt. An item entered by hand or
added by barcode scan has **no price**. Displaying "you spent €240 this month"
when half the shopping was never scanned is a lie by omission — the same flaw as
the one fixed on `uncategorised_product_count` (§8).

The response therefore always carries the coverage:

```json
"budget": {
  "period": "month",
  "period_start": "2026-08-01",
  "period_end": "2026-08-31",
  "currencies": [
    {
      "currency": "EUR",
      "spent": "182.47",
      "receipt_count": 4,
      "target": "400.00",
      "line_sum_mismatch_count": 1
    }
  ],
  "coverage": {
    "receipts_with_total": 4,
    "receipts_missing_total": 1,
    "stock_items_added_without_receipt": 12
  }
}
```

`stock_items_added_without_receipt` is the honest field: it says everything the
displayed spending **does not** count.

**Currencies are never mixed.** `receipt.currency` is per receipt; a period
containing several currencies returns an amount **per currency**, never a
conversion. Converting would assume a rate, a rate date, and a decision that does
not belong to this application.

> The first draft of this paragraph put `currency` and `spent` **flat**, which
> contradicted the sentence just above it: a single-currency shape has nowhere to
> put a second one, so it would have forced either adding up or hiding. The five
> fields kept their exact names and moved down into `currencies[]`. A
> single-currency household has one entry there.

A receipt counts as soon as it carries **a total and a currency** — it need not be
`confirmed`, which says that its *lines* have entered stock. §6ter separates the
two on purpose. A total without a currency counts as **missing**, never as
spending.

Periods are bounded in the **household's time zone**, not in UTC: a receipt from
the 31st at 11 p.m. must not slip into the following month.

`GET /v1/budget/history` returns **completed periods only**. A period in progress
plotted next to complete periods reads as a collapse every day except the last.
Nor does it carry a target: the history of targets does not exist — it would be
the record of a family's changing means — and stamping today's target onto last
May would assert something nobody wrote.

### Endpoints

| Method | Path | Role |
|---|---|---|
| `GET` | `/v1/budget?period=week\|month&at=YYYY-MM-DD` | Observed spending over the period containing `at` (default: today) |
| `GET` | `/v1/budget/history?period=month&count=6` | The N preceding periods, for a trend |
| `PUT` | `/v1/budget/target` | `{"period": "month", "amount": "400.00", "currency": "EUR"}` — optional |
| `DELETE` | `/v1/budget/target` | Stops tracking a target |

The month is **calendar**, not rolling: households think in pay months, and a
rolling window would make "what I have spent this month" incomparable from one day
to the next. The week starts on Monday.

A target is **optional** and triggers nothing beyond a display. No alert, no
blocking, no judgement: going over a food budget is information, not a fault, and
an application that scolds gets uninstalled.

### What this data is worth

A spending history with the retailer, the date and the amount says where a
household shops, at what rhythm, on roughly what income, and when it is away.
`docs/security-model.md` already classifies the inventory as an **A3** asset; the
budget is of the same order and adds to it.

There is therefore **no reason to keep the receipt image** after extracting the
total and the lines, and budget tracking must not become the pretext for keeping
it.

---

## 7. Importing a shopping list

Three inputs: a PDF, a `.txt`, or pasted text. All end up in the same place — a
proposed shopping list, **subject to review before writing**.

### 7.1 The model is optional, and that is structural

A shopping list is already half structured: one line, one item, sometimes a
quantity. A deterministic parser handles most of it.

**The import must therefore work with no model provider configured at all.** A
household in `byok` mode without a key, or without Ollama, must be able to import
its list. Making such a mundane feature depend on inference would contradict the
promise of ADR-0007, where having no provider is a normal state and not a failure.

The model comes in **second**, only on the lines the parser could not split, and
its absence degrades quality without blocking the feature. This is the use case
for ADR-0005's degradation taxonomy: lossy emulation, not unavailability.

### 7.2 Endpoints

`POST /v1/shopping-lists/import` — `multipart/form-data` with a file, or
`application/json` with `{"text": "…"}`.

Response `200`: a proposal, **nothing is written**.

```json
{
  "import_id": "uuid",
  "source": "pdf",
  "parsed_by": "deterministic",
  "lines": [{
    "raw": "2 kg of potatoes",
    "quantity": {"amount": "2", "unit": "kg"},
    "product_name": "potatoes",
    "matched_product_id": "uuid",
    "confidence": "high",
    "needs_review": false
  }, {
    "raw": "smth for dessert",
    "quantity": null,
    "product_name": "smth for dessert",
    "matched_product_id": null,
    "confidence": "none",
    "needs_review": true
  }],
  "unparsed_line_count": 1,
  "truncated": false
}
```

`parsed_by` ∈ `deterministic` | `deterministic+model`. The interface must be able
to tell the user whether a model touched their list.

`POST /v1/shopping-lists/import/{import_id}/confirm` — body carrying the retained
and corrected lines. This is **the only** call that writes.

### 7.3 The PDF is hostile input

A PDF supplied by the user is a complex binary format, and its parsers have a long
history of vulnerabilities. Requirements:

- **Text extraction only.** Never rendering, never execution of embedded
  JavaScript, never resolution of an external resource — a PDF can reference a
  remote URL, which replays AUD-005's SSRF from another angle.
- **Strict bounds** before parsing: file size, page count, volume of extracted
  text. Exceeding them gives a `413`, never a silent partial read.
- **Its own size limit**, distinct from the global request body cap (256 KiB) that
  exists for the JSON endpoints. This limit is a documented configuration
  variable.
- The file is **not kept** after extraction. A shopping list says what a household
  is going to eat; there is no reason to keep it any longer than the import.

### 7.4 All imported text is untrusted

The content of a PDF or of a `.txt` file reaches a prompt as soon as the model
gets involved on the unparsed lines. It therefore goes through the same
neutralisation as product labels (`infra/untrusted_text.py`, AUD-006) — a file is
an injection vector just as much as an Open Food Facts record, with the aggravating
factor that it is supplied directly.

The members' dietary constraints **do not filter** a shopping list: one is allowed
to buy what one will not eat oneself. A line matching a member's declared allergen
is however **flagged** at review, without being removed.

---

## 8. Balance — benchmarks and units

`GET /v1/balance` returns the same `balance` object as above, without a
suggestion, for a dashboard display.

The PNNS benchmarks retained, their unit and their window are carried by a
versioned reference table (`reference: "pnns-2019"`). The version travels with
every persisted suggestion: a revised benchmark must not rewrite history.

Markers are derived from the Open Food Facts categories of the consumed product. A
product whose category resolves to no marker counts towards none — and the number
of such products is exposed, failing which a badly categorised inventory would
produce an unarguable and false "you are one fish short".

The field carries this name, and no other:

```json
"balance": {
  "reference": "pnns-2019",
  "window_days": 7,
  "uncategorised_product_count": 3,
  "gaps": [],
  "excesses": []
}
```

`uncategorised_product_count` is **always present**, including at zero. The
absence of the field and a zero value would say the same thing to a naive client —
"everything is categorised" — when one of the two means "we do not know".

---

## 9. Acknowledged gaps between this contract and the implementation

This contract was frozen before the code. What follows notes what the
implementation added or made precise, so that the difference is written here
rather than discovered in the code. **Nothing is removed**: every field described
above exists and keeps its shape.

### 9.1 Additive additions

| Object | Fields added | Why |
|---|---|---|
| `balance.gaps[]` | `statement`, `source_url` | ADR-0009's argument for benchmarks expressed as frequencies rather than a score is that a household can go and read them. The §3 object carried neither the official wording nor the URL, so the interface could not show them. |
| `balance.excesses[]` | `observed`, `unit` | `observed_grams` can only describe the two benchmarks expressed in grams. A cap counted in portions (sugary drinks) would return `0` there, or worse a number of glasses displayed as grams. It therefore carries `observed` with its `unit`, and `observed_grams` is `0`. |
| `suggestions[].preparation` | — | **Always present**, with its three fields at `null` when the model declared nothing. `null` reads as "not declared", never as "does not need an oven". |
| `applied_constraints` | — | **Always present**, including for a household with no registered member (`members: []`, `diet: null`). |
| `balance` | — | `null` **only** if `balance_mode: "off"`. An absent gap is said with `gaps: []`. |
| `409 no-suggestion-within-constraints` | `reasons`, `products_withheld` | The class of constraint that emptied the inventory (`allergen`, `diet`, `infant_rule`) and the number of products withheld. Never the allergen, never the person. |

### 9.1bis Usage feedback on a suggestion

The contract described no feedback mechanism; the table has provided for it since
migration `0005` (`recipe_suggestion.feedback`, `feedback_at`,
`feedback_by_user_id`, plus the index `(provider_mode, model, feedback)`). Three
routes make it usable, all scoped to the household:

| Route | Effect |
|---|---|
| `PUT /v1/recipes/suggestions/{id}/feedback` | Body `{"feedback": "cooked" \| "not_interested"}`. One response per suggestion, **the last one wins** — hence `PUT` and not `POST`. `status` moves with it: the constraint `ck_recipe_suggestion_feedback_matches_status` refuses to let the life cycle and the measurement contradict each other. |
| `DELETE /v1/recipes/suggestions/{id}/feedback` | Removes the response. Answers `200` with the resulting state, not `204`. Idempotent. `status` goes back to `cooked` if `cooked_at` exists, otherwise to `generated` — removing a response is not throwing the recipe away. |
| `GET /v1/recipes/quality` | `{"min_responses": 10, "models": [{provider_mode, model, cooked, not_interested, responses, cooked_rate}]}`. |

Three rules frame the use of the signal, and they are restrictions:

**The response ranks, it never filters.** It is the fourth and *last* sort key,
below the three of §5: hard constraints, expiry, balance. A dismissed recipe drops
among those already tied on everything that matters more, and stays on offer. A
signal that filtered would fold in on itself — each refusal narrows the space the
next refusal is drawn from — until the household sees only three dishes, with
nothing on screen to explain it. Nothing *promotes* either: a dish cooked on
Sunday is not proof that it is wanted on Monday, and raising it would narrow the
offering just as much, in the flattering direction where nobody notices.

**The response never goes back into the prompt.** "Computable by the application"
class of §4bis: the application measures, the model writes. Sending it back would
make the ranking unverifiable (a model is entitled to ignore an instruction) and
would publish the household's habits in a third party's request body.

**A response is about a suggestion**, not about a product nor about a person. No
per-eater profile is derived from it, and `feedback_by_user_id` stays `NULL` as
long as there are no user accounts.

**The rate is only published above 10 responses** for a (provider, model) pair —
`MIN_RESPONSES_FOR_RATE`, in `services/recipe_feedback.py`. Below that,
`cooked_rate` is `null` and only the counts are returned: "2 responses out of 3"
is a fact, "67%" is the same fact dressed up as a measurement, which one more tap
moves by seventeen points. The threshold travels on the response
(`min_responses`) so that no client-side copy can drift.

**Cross-household reading is not a route.** The aggregate that answers "does the
small local model produce recipes we actually cook?" (ADR-0007) is
`backend/scripts/quality_report.py`, run under the maintenance identity: no
operator identity exists in this slice, and a route that served other households'
counters to whoever guesses a UUID would be the application's biggest hole. The
policies of migration `0004` prevent it engine-side anyway.

### 9.2 Readings adopted where the contract was ambiguous

**`items_used_expiring_within_days`** (§5) is read as *the number of urgent
products the suggestion uses* — hence the size of `urgent_items`. The urgency
threshold is **3 days**, a server constant.

**The post-call check only runs if a hard constraint applies** — at least one
excluded allergen, a non-omnivore diet, or an applicable infant rule. With no hard
constraint there is nothing to violate, and invalidating a suggestion because the
model wrote "a few herbs" would cost every suggestion without protecting anyone.

**Infant texture is not a product filter.** No column of the catalogue says
whether a product can be pureed: the texture therefore travels as a *preparation
requirement* in the prompt and in `applied_constraints.infant_texture`. What is
actually filtered on the infant side, before the call as after, are the
**age-band prohibitions** of `infant_food_restriction`, which are verifiable.

**Diet filters on positive proof.** A product whose categories resolve no marker
is not withheld — unlike allergens, no generated column makes the unknown
maximally excluding, and discarding every badly categorised product would empty
the stock of any vegetarian household. The guarantee comes from the post-call
check: an ingredient absent from the transmitted inventory does not resolve, and
the suggestion falls.

**A portion is a stock movement.** `stock_movement` records quantities and dates,
not meals; a portion is therefore read as *a consumption event*. It is an
approximation, and it is stated as such. The two caps in grams, for their part,
are weighed exactly.

### 9.3 Not honoured

**"The mode is remembered per household as the last choice" (§4ter)** is not
honoured server-side: no table carries a household preference, and adding one
required a migration. The last choice is remembered by the client
(`localStorage`, keyed by household). Consequence: the setting does not follow the
device.

---

## 10. Machine access tokens — a prerequisite for any integration

**This section is frozen before the code, like the rest of this document.**

The authentication that shipped is made for a browser: `__Host-` cookie,
`HttpOnly`, CSRF token on every unsafe method. A machine client — a Home Assistant
integration, a script, a dashboard — cannot use it without storing **an account's
password** and replaying a login. That is the failure mode to name up front: an
integration that asks for a password gives third-party software an access its
owner can neither restrict nor revoke without changing that password everywhere.

A second means of authentication is therefore needed, and it is different in
nature.

### What a machine token is, and is not

| | Browser session | Machine token |
|---|---|---|
| Carried by | `__Host-` cookie, `HttpOnly` | `Authorization: Bearer` header |
| CSRF | Required | **Not applicable** — a header does not travel on its own |
| Lifetime | Short, renewed | Long, explicitly chosen |
| Revocation | Logout | Individual, without touching the password |
| Scope | The whole account | **Restricted, and a single household** |

A token is **bound to a household**, not to an account. An account belonging to
two households issues two tokens. Otherwise installing an integration for the
family home would grant access to the flat-share.

### Scopes

Closed, additive, and **never implicit** — a token with no scope can do nothing.

| Scope | Allows |
|---|---|
| `inventory:read` | Read the stock, the locations, the expiry dates |
| `inventory:write` | Add, correct, remove |
| `shopping:read` | Read the current list |
| `shopping:write` | Add, tick, remove |
| `budget:read` | Read the spending and the target |

**No scope grants access to recipe suggestions.** It is the only endpoint that
spends money, and a long-lived token sitting in home automation software is
exactly the wrong place to hold that power. Same for household members: infants'
allergies and age bands do not leave through a machine token.

### Storage and display

The token is **hashed with SHA-256** like a session token — 256 bits of entropy do
not call for a memory-hard hash, and imposing one on every request would be a
self-inflicted denial of service.

It is shown **only once**, at creation. Afterwards the API returns its prefix and
its last four characters, never more. A token that can be read back is a token a
screenshot is enough to steal.

### Endpoints

| Method | Path | Role |
|---|---|---|
| `GET` | `/v1/tokens` | The household's tokens: name, scopes, prefix, `last4`, last use, expiry |
| `POST` | `/v1/tokens` | `{"name": "Home Assistant", "scopes": [...], "expires_in_days": 365 \| null}` — **the only response that contains the token** |
| `DELETE` | `/v1/tokens/{id}` | Immediate revocation |

Creating or revoking a token requires a **browser session**, never a token.
Otherwise a compromised token issues others, and revocation becomes whack-a-mole.

`last_used_at` exists so that an owner can answer "is this token still in use?"
before deleting it. It is written at most once an hour: to the request, it would
turn every read into a write.

### What does not change

The existing routes change neither shape nor semantics. A token is a **second door
into the same house**: the household is resolved at issuance instead of by a
selector, RLS arms identically, and the route census in
`tests/api/test_route_authentication.py` remains the guarantee that no route is
open.
