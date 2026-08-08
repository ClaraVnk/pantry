# 0006. Multi-tenant from day one

## Status

Accepted — 2026-08-03

## Context

In phase 1, Chaudron serves a single household. The cheapest modelling would therefore be single-tenant: `item`, `stock_entry`, `shopping_list_item` tables with no notion of owner, and authentication reduced to a single user.

The envisaged phase 2 is a public multi-user opening. Stock, shopping list and purchase history belong to a **household**, not to a person: two partners share the same fridge and must see the same stock. The natural unit of isolation is therefore the household (`household`), not the user.

The question is not whether multi-tenancy will be needed, but when to pay for it. And that cost is not linear over time: adding a tenant column to an existing schema is mechanical, but **retrofitting tenant filtering into application code that never had any is not**. Every query written without a tenant clause is a potential leak path, and they all have to be audited one by one, with no existing test failing if one is missed.

## Decision

Multi-tenancy is present from the first migration, even with a single row in `household`.

**Model.** A `household` table is the isolation root. Every business table carries a non-null `household_id` column, with a foreign key to `household`. Functional unique constraints are composite and always include `household_id` (for instance `UNIQUE (household_id, barcode)`, not `UNIQUE (barcode)`). Read indexes are prefixed by `household_id`. The user ↔ household link goes through a `household_member` association table carrying a role, which lets a person belong to several households without changing the model.

**Data access.** No business query runs without a tenant filter. The current `household_id` is resolved once, at the HTTP boundary, from the authentication context — **never read from the request body or parameters**. It is propagated explicitly by the application layer down to the repository. Repository functions take `household_id` as a required parameter, so that omitting it is a typing error caught by `mypy`, not a runtime defect.

**Tests.** Every exposed resource has an isolation test: two households are created with their own data, and household A's operations on household B's identifiers must return `404` (never `403`, which would confirm the resource exists). These tests are mandatory for every new resource; their absence is grounds for rejection in review.

**Strict isolation.** An identifier belonging to another household behaves exactly like a non-existent identifier. That holds for reads as well as writes.

## Consequences

### Positive

- Phase 2 requires neither a risky data migration nor an exhaustive audit of the access code.
- Sharing between members of the same household — needed from phase 1 for a couple — comes for free.
- The isolation tests are a permanent safety net: a leak-tightness regression fails CI instead of leaking in production.
- The `household_id`-prefixed indexes are the ones we need anyway, since every read is scoped.
- The model supports later scenarios (second home, flatshare, temporarily shared household) with no redesign.

### Negative

- **Every signature is heavier.** Each repository function carries one more parameter, each query one more clause. In single-household phase 1, that is pure ceremony: the value is always the same.
- Test fixtures are more verbose: create a household before creating an item, in every scenario.
- The discipline rests on a convention. Typing helps, but a `session.execute(select(Item))` with no filter is still writable and still compiles. A leak stays possible as long as filtering is not enforced by the database itself.
- The cost is paid immediately, the benefit only materialises in phase 2 — which may never come. It is a deliberate bet, not a certainty.
- Some cross-cutting analytical queries (global statistics, a shared product reference) will have to step out of tenant scope explicitly, which creates an exception to document and to protect separately.

## Rejected alternatives

- **Single-tenant now, migrate at phase 2** — the default choice. Rejected: the migration breaks down into adding a column (easy, `ALTER TABLE ... ADD COLUMN household_id`, backfill to a single value), reworking every unique constraint (moderately risky: going from `UNIQUE (barcode)` to `UNIQUE (household_id, barcode)` under load), and then **auditing every application query** (the real cost). That last step has no safeguard: nothing fails if a query is missed, the leak is discovered in production, at a user's expense, on personal data. Across a few dozen data-access files, that is several days of high-risk work, against a few hours of discipline spread out now.
- **One PostgreSQL database or schema per household** — the strongest isolation, enforced by the engine. Rejected: migrations then have to be applied to N schemas, connection pooling gets complicated, and operating this on a single VPS becomes disproportionate for the size of the data. It remains the reference option if a strict compliance requirement appears.
- **PostgreSQL Row-Level Security** — isolation is enforced by the database rather than by application convention, which removes the main weakness of the chosen decision. Rejected **for now**: RLS requires propagating the tenant into the session (`SET LOCAL`), which interacts badly with connection pooling and demands careful handling in an async context. This is the natural hardening, not a competing alternative: the chosen schema (`household_id` everywhere) is exactly RLS's prerequisite.
- **Tenant identified by subdomain or HTTP header** — convenient in B2B SaaS. Rejected: the tenant must derive from authentication alone. Any source the client controls is a privilege escalation handed over for free.

## Revisiting

Enable Row-Level Security on the business tables before phase 2's public opening: the schema is already compatible, and it moves the isolation guarantee from convention to engine.

Move to schema-level isolation if a compliance requirement forces physical data separation, or if a single household reaches a volume that justifies partitioning.
