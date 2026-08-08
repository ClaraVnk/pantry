# 0008. Open Food Facts integration strategy

## Status

Accepted — 2026-08-03.

## Context

Resolving an EAN code to a product record relies on Open Food Facts, a free
community service under the ODbL licence. The feasibility study
(`docs/technical-notes-scanning.md`) brought four facts to light that change the
nature of this integration.

**The rate limit applies per IP address, not per user.** The Open Food Facts
documentation states that the limit applies per user *when the requests come
directly from clients*. By centralising the calls in the backend — which is what
we do, and which remains the right choice — every request leaves from a single
IP: the limit becomes global to the whole instance. The order of magnitude
observed is fifteen requests per minute, with an IP ban on overrun. The
behaviour on overrun has been observed in practice: the API answers in HTML, not
JSON.

**The cache therefore cannot be scoped per household.** ADR-0006 mandates a
`household_id` on every business table. Applied mechanically to the product
cache, it would multiply outbound calls by the number of households, for
strictly identical content — exactly what the rate limit forbids.

**A substantial share of the items in a real cupboard has no usable code.**
Products absent from the reference dataset, crumpled packaging, fruit,
vegetables, butcher's cuts, loose goods. And above all the **store-internal
codes** with prefixes `02` and `20`–`29`: used for variable-weight items, they
embed the price, therefore change with every purchase, and will never appear in
a public reference dataset.

**API v2 is deprecated.** v3 is the current version and its error contract
differs: `result.id == "product_not_found"` with an HTTP 404, where v2 returned
`status: 0`.

## Decision

**The product catalogue is a shared external reference dataset, not household
data.** It is materialised by `product` with `household_id IS NULL`. Records
created or corrected by a household carry a non-null `household_id` and are
isolated. This is an explicit and bounded exception to ADR-0006's rule, and the
only one.

**The cache is a condition of operation, not an optimisation.** Every resolution
goes through the cache first. Failures are negatively cached: a code absent from
Open Food Facts must not trigger a call on every scan.

**Store-internal codes are detected client-side** from their prefix, and cause
no network call at all — neither to the backend nor to Open Food Facts. The user
goes straight to manual entry.

**Manual entry is a first-class feature**, not a degraded fallback. A locally
edited record wins over any later refresh coming from Open Food Facts: the field
that carries this precedence is planned from the first migration, because adding
it after the fact would mean working out which records had been corrected by
hand.

**Development is done against the staging environment**
(`world.openfoodfacts.net`), as the documentation requires, and API v3 is the
target.

**An honest caller identifier is sent** in the `User-Agent` header, in
accordance with the project's policy — application name, version, contact
address.

**In phase 2, importing the local dump becomes a prerequisite.** Serving
external users behind a limit of fifteen requests per minute is not viable. The
API then only serves to fill the dump's occasional gaps.

## Consequences

### Positive

- The application keeps resolving already-seen products when Open Food Facts is
  unavailable or has banned us.
- The shared cache makes the call cost independent of the number of households.
- Detecting store codes client-side saves a full round trip on items that will
  never be resolved anyway.
- No licensing cost: the qualitatively superior alternative, GS1 France's
  CodeOnline Food, requires a five-figure membership.

### Negative

- **The rate limit remains a hard limit in phase 1.** A household unpacking its
  shopping can scan faster than the allowed rate. That calls for a server-side
  queue, our own rate limiting and an honest message to the user — not a raw
  error.
- **The exception to ADR-0006 is a breach in a rule whose value lies in being
  absolute.** "`household_id` everywhere, except here" is harder to enforce than
  "`household_id` everywhere". The risk is that another developer invokes this
  precedent for a table that does hold household data.
- **The ODbL imposes share-alike.** As long as the reference dataset is only
  queried, the obligation stays theoretical; as soon as a dump is imported and
  enriched, it becomes real. It will have to be examined before phase 2, not
  during.
- **Importing the dump has an infrastructure cost** — disk space, periodic
  refresh, rebuild window — that does not exist today.
- **Coverage is not completeness.** Roughly 1.25 million products sold in France
  are present, but the completeness rate of the useful fields could not be
  measured, the rate limit having prevented the measurement. To be checked
  against a local dump before promising anything in the interface.

## Rejected alternatives

- **Calls made from the client's browser**, which would bring the limit back to
  one budget per user. Rejected: it exposes the call strategy, prevents any
  shared caching, and makes the application dependent on Open Food Facts being
  reachable from each user's network. The gain does not offset the loss of the
  shared cache.
- **A per-household cache**, consistent with ADR-0006 with no exception.
  Rejected: it multiplies outbound calls by the number of households for
  identical content, which the rate limit forbids.
- **CodeOnline Food (GS1 France)**, brand-supplied data of better quality.
  Rejected: five-figure GS1 membership, out of reach for a solo project.
- **Importing the dump from phase 1.** Rejected: immediate infrastructure cost
  for a single household whose scan volume sits comfortably under the limit.

## Revisiting

- Import the dump as soon as a second instance or a second active household
  exists, without waiting for the public opening — the limit is global, so it is
  shared.
- Re-examine the exception to ADR-0006 if a second table demands the same
  treatment: two exceptions are no longer an exception, they are a badly
  formulated rule.
- Examine the ODbL obligations before any dump import.
