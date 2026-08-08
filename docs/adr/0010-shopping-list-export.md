# 0010. Shopping list export

## Status

Accepted — 2026-08-04

## Context

The shopping list is produced in Chaudron, but it is *consumed* in the shop, on a phone, often by someone other than the person who filled it in. An application that forces you to open one more PWA in the chilled aisle loses against the notes app already on the phone. The question is therefore not "how do we store a list" but "how do we drop it where people are already looking".

The sourced study in `docs/technical-notes-ingestion.md` §4 (verified on 3 August 2026) settled the hard part, and this ADR does not replay it. Three of its findings shape the decision:

1. **Writing to iCloud Reminders from a server is impossible** — not hard, impossible. No public API, no CloudKit (which only grants access to our own containers), no CalDAV (Apple disabled reminder synchronisation as of iOS 13, corroborated by DAVx⁵, Tasks.org, BusyMac, python-caldav and Home Assistant).
2. **`navigator.share({ text })` gets ~90% of the benefit for ~5% of the effort**: 90.3% support, iOS ✅ Android ✅ desktop ✅ except Firefox, about one day of work, no dependency on a publisher verification programme. It is rank 1 in the §4.7 table, "do this first, no discussion".
3. **The `url` field must be omitted.** Two documented iOS bugs (query string stripped when sharing via Messages, cross-domain URL replaced by that of the current page) mean `url` is not an arbitrary data channel on iOS. Everything goes in `text`.

On top of that comes a constraint that is not technical: a shopping list says what identifiable people eat. In a household with an allergy, an intolerance or a religious practice, it carries health or belief data. Sending it to a third party is processing within the meaning of the GDPR, not an integration detail.

## Decision

### 1. Plain text is the format, and the domain produces it

`chaudron.domain.shopping_export.render_plain_text` renders the list as **one line per item, quantity and unit included**, with no title, no bullet, no checkbox. Each of those absences is a choice: a title becomes a spurious task the moment you paste the block into a task application, a `-` prefix survives inside the task name in several of them, a `[ ]` checkbox is redundant everywhere it lands.

The renderer is exposed in two ways, on purpose:

- as a reusable function, also called by the adapters — an item therefore reads identically in the share sheet and in Todoist;
- as a `GET /v1/shopping-lists/{id}/export/text` endpoint, `text/plain; charset=utf-8`.

**The `navigator.share` call itself belongs to the frontend** and is not delivered here. The backend supplies the string; that is the only split that keeps a single definition of the format.

Two rendering rules are worth writing down, because they look cosmetic and are not:

- **The decimal separator is the point.** This string gets pasted into spreadsheets and messaging apps we do not control, where the comma is a field separator. `1,5` turning into two cells is silent corruption; `1.5` is merely a little less idiomatic.
- **Whitespace is collapsed in the renderer, not only upstream.** A label typed by the household can contain a newline. An item that silently becomes two items is worse than a failure: nobody proofreads a list that looks plausible.

### 2. One domain port, adapters behind it

`ShoppingListExporter` is a domain `Protocol`, in the spirit of ADR-0005: the service that assembles the list does not know who receives it, exactly as `services/recipes.py` does not know whether Anthropic or Ollama answered. What crosses the boundary is a shopping list — names, quantities, units. No token, no header, no project identifier, no provider vocabulary.

`ShoppingExportFactory` resolves `(household, destination)` to an adapter, just as `infra/llm/factory.py` resolves a model provider.

### 3. A single adapter: Todoist, by personal token, via `/sync`

`POST https://api.todoist.com/api/v1/sync`, `application/x-www-form-urlencoded` body, `commands` field containing a JSON array of `item_add` commands. **The whole list goes in one call**, not one call per item. The exact shape of the request was re-read against the official reference on 4 August 2026 and is quoted in `infra/todo/todoist.py`; whatever could not be verified is flagged as such there.

The **personal token** is preferred over OAuth2. For a self-hosted product, OAuth would require *the operator* to register an application with Todoist, host a redirect URI and keep a client secret — per deployment, for a feature whose entire point is to cost one afternoon. A personal token is pasted once and revoked from the same settings page.

Each command's `uuid` is **derived** from `(export_id, index)` rather than drawn at random: Todoist documents that field as carrying idempotency, so a client replaying an already-sent export does not add the household's shopping twice.

### 4. Microsoft To Do is not shipped

A decision taken after checking, not for lack of time. The official page for `POST /me/todo/lists/{id}/tasks` gives, for the permissions table:

| Permission type | Least privileged |
|---|---|
| Delegated (work or school account) | `Tasks.ReadWrite` |
| Delegated (personal Microsoft account) | `Tasks.ReadWrite` |
| **Application** | **Not supported.** |

There is therefore **no static token mode**. Writing to Microsoft To Do imposes the full journey: registering an application in Entra ID by the operator of every instance, a public redirect URI, a client secret, an interactive authorisation flow, then storing and refreshing *refresh tokens* — that is to say a second credential system, with its own expiry cycle and its own silent failures. On top of that comes the need to resolve `todoTaskListId` through a prior call, and the absence of batching: one request per item.

The research ranks Todoist and Microsoft To Do at **rank 5**, "near-zero effort, narrow audience, good bonus candidate". For Todoist that assessment is right. For Microsoft To Do it underestimates the cost, because it did not weigh the absence of an application permission. **A half-done integration is worse than no integration**: we therefore ship Todoist alone, and reopen Microsoft To Do the day an OAuth flow exists in the product anyway (one will be needed for Google Tasks, see §Revisiting) — the marginal cost will then be real rather than foundational.

### 5. Bring! is refused, and the refusal is written down

Bring! is the dominant shopping app in Switzerland. That is this instance's audience. Technically, the integration works: `node-bring-api` and the Home Assistant integration use it every day.

**And the answer is no.**

Bring! Labs AG publishes no API. The existing integrations rest on a private endpoint obtained by reverse engineering, with an explicit warning that it is "*in no way endorsed by or affiliated with Bring! Labs AG*". That is **exactly** the reason ADR-0002 ruled out retailer drive accounts: a non-contractual surface that breaks without notice, without a status page and without a pinnable version, and whose use violates the service's terms.

The argument "but it's easy" is precisely the one ADR-0002 rejected for scraping, which was easy too. An architecture decision that only holds when it is convenient is not a decision. The refusal is here so that it can be cited against the next person who finds the endpoint and finds it handy — and `tests/todo/test_export_endpoints.py::test_bring_is_refused_by_name` makes it executable, because a decision that is only written down gets undone by a distracted pull request.

### 6. Tokens are encrypted with the existing mechanism

No cryptography is rewritten. `infra/todo/credentials.py` calls `CredentialCipher` (`infra/crypto.py`): AES-256-GCM, master key outside the database, AAD binding the ciphertext to `(household_id, target_id)`, rotation that fails loudly and names the environment variable.

**Deliberate, documented deviation**: `crypto.py` pins its AAD domain string to `chaudron/llm_provider_config/api_key/v1`, and its own docstring says that a second use of the master key "would carry its own prefix". Adding that prefix means modifying `crypto.py`, which belongs to a different change. In the meantime, cross-use replay is prevented by the row identifier rather than by the domain prefix: moving a ciphertext from an `llm_provider_config` row to an export row would require the two to share a UUID, independently drawn. The property holds — by arithmetic rather than by design. **Adding `chaudron/shopping_export_target/token/v1` is a prerequisite for the table below.**

### 7. The table is missing, and it is specified here rather than worked around

Per-household storage requires a table, a table requires a migration, and migrations belong to a different change. Following the precedent of `DeclinedRepurchaseStore` (`domain/shopping.py`), the `ShoppingExportTargetStore` port is written against its final shape and left without a SQL implementation.

```
shopping_export_target
  id                      uuid        primary key
  household_id            uuid        not null references household(id) on delete cascade
  target_code             text        not null            -- 'todoist'
  token_ciphertext        bytea       not null            -- AES-256-GCM, AAD (household_id, id)
  token_last4             varchar(4)  not null
  token_encryption_key_id varchar(32) not null
  external_list_id        text                            -- project id; null = Inbox
  consented_at            timestamptz not null
  consent_revoked_at      timestamptz
  created_at              timestamptz not null default now()
  updated_at              timestamptz not null default now()
  unique (household_id, target_code)
  unique (household_id, id)
```

RLS like any tenant table (ADR-0006, migration `0004`). `consented_at` is `not null` **by legal obligation, not for convenience**: a nullable column would let a row exist with no consent, and the first `INSERT` that forgot it would be a violation rather than a bug. `consent_revoked_at` carries the withdrawal; the row survives so that the household can see what it authorised.

Until that table exists, an **instance-owned** destination is readable from the environment (`CHAUDRON_TODOIST_TOKEN`, `CHAUDRON_TODOIST_HOUSEHOLD_ID`), usable by **a single household**. This is ADR-0007's locked `instance_owner` door transposed to another secret, for the same reason: the token belongs to one person, and a token usable by every household would drop strangers' shopping into the operator's Todoist. Unset means nobody.

### 8. GDPR consent

Sending the list is refused without live consent. Two readings, both recorded:

- **stored destination**: `consented_at` set and `consent_revoked_at` null, checked on every send by `ShoppingExportFactory` — withdrawal takes effect at the next call, not at the next cleanup;
- **instance destination**: the operator wrote their own token into the environment of their own instance for their own household. The act is explicit, informed and revocable by deleting a line, which is what the rule requires.

The text export is out of scope: it answers the household's own browser, nothing leaves the instance. It is native sharing that decides next, and it is the user who triggers it.

What goes out is reduced to the minimum: a name and a quantity. No product identifier, no barcode, no household identifier, no expiry date, no allergen marker — even when the database knows them. Items already ticked off do not go.

### 9. Bounds on outbound calls

The destinations are compiled constants (`api.todoist.com`), not URLs supplied by a household: **there is therefore no SSRF allowlist here**, and copying one across would be a control that decides nothing while giving the impression of deciding. The rest of `infra/llm/http.py`'s bounds are kept, because a fixed hostname is not a promise of good behaviour: bounded timeout (15 s), bounded response (512 KiB, read as a stream), **redirects disabled** (following a `302` would send the bearer token to whichever host answered), and `https` checked at construction.

No `httpx` exception crosses the boundary: it renders the request that failed, and that request carries the `Authorization` header.

## Consequences

### Positive

- The main path covers iOS, Android and desktop in one day of work, with no dependency on a publisher verification programme, no audit, and no user cap.
- A single format renderer: what you share, what you copy and what you send to Todoist are the same string.
- No `client_secret`, no redirect URI, no token refresh cycle in the product.
- The service cannot name a destination or hold a token: that is not a convention, it is what its imports allow.
- A partial failure is reported as a failure, with its counters. A household doing its shopping from a list three items short is exactly what this feature exists to prevent.

### Negative

- **On desktop Firefox there is no native sharing.** The fallback is copying to the clipboard, and it is the frontend's job to supply it.
- **Native sharing ticks nothing.** On Apple Reminders, pasting a multi-line block creates **one single reminder** containing the whole list (a regression documented since January 2021, confirmed in November 2023). The iOS user therefore gets shared text, not a tickable list — that is the real limit of rank 1, and it is only fixed at rank 2 (CalDAV/VTODO).
- **Todoist covers only a minority of users**, and Bring!, which would cover the Swiss audience, is refused. This feature therefore disappoints precisely where it would be most useful.
- **Per-household storage does not exist yet.** As long as the table is missing, only the instance destination works, that is to say one household per instance.
- **The export token shares the provider keys' AAD domain string** (§6). No exploitable consequence today; a debt to settle with the migration.
- **The published limit on commands per request could not be read**: Todoist's "Request Limits" section did not render. The batch is therefore bounded by us at 100, a cautious choice but an uncited one. If the real limit is lower, a long list will fail batch by batch — visibly, with counters, but it will fail.
- **No rate limiting on the send endpoint.** The counters in `api/throttling.py` are wired into `api/deps.py` and `api/main.py`, outside the scope of this change. A household can therefore hammer its own destination's API and get rate-limited by it.
- Todoist is a US service: a European household's shopping list is transferred there. It is the household that decides so, and that is why consent is a `not null` column rather than a checkbox in an interface.

## Rejected alternatives

- **Writing to iCloud Reminders from the server** — impossible, not expensive: no public API (EventKit is strictly local), no CloudKit (our containers only), no CalDAV (reminders disabled since iOS 13). No amount of effort changes that result.
- **Bring!** — refused. Unofficial API obtained by reverse engineering, with no connection to the vendor. Same reason as ADR-0002 on drive accounts; the coherence of that decision depends on this one. See §5.
- **The Google Keep API** — ideal data model (`body.list`, `ListItem{text, checked}`), unreachable authorisation path: the only two documented modes are variants of Workspace domain-wide delegation, and the quickstart requires a super-administrator account. A `@gmail.com` account has no domain, no admin console and no super-admin. No sentence from Google forbids it in black and white — which is one more reason not to build on it: it would be betting on undocumented behaviour.
- **Google Tasks** — the only viable Google route, but a cap of **100 cumulative users over the life of the project** without verification, a scope that is most likely *sensitive* (up to 10 days of review, verified domain, video of the OAuth flow), and above all: "Google Tasks is not where people do their shopping". Rank 4.
- **Microsoft To Do** — see §4. Rejected for this iteration, not on principle.
- **A `.ics` / `webcal://` feed of VTODOs** — no mainstream mobile client is proven to consume it. Decisive industry signal: Todoist, which has exactly this need, does not emit VTODOs in its iCal feed.
- **Serving a CalDAV/VTODO endpoint** — this is not a rejection, it is sequencing. It is rank 2 and the only path that lands in iOS's native Reminders app (proven in April 2026 on iOS 26.4.1, Vikunja ticket #2658) and covers Android via DAVx⁵/Tasks.org, with nothing to install on the phone. Its contract is an RFC, not an API that can shut down. High effort: CalDAV server, authentication, ETags, sync tokens. To be done next.
- **An iOS Shortcut distributed by iCloud link** — rank 3, excellent on iPhone, two on-device tests required first (the `Headers` parameter is undocumented by Apple, network privacy prompt). iOS only.
- **On-phone Android automation** (Tasker, HTTP Shortcuts) — the HTTP call is a solved and free problem, but **nothing allows writing to Keep or Tasks from the phone**: Android has no standard contract for tasks. The effort falls back on the user, which is the worst place to put it.
- **One REST call per item** (`POST /api/v1/tasks`) rather than `/sync` — simpler to write, one request per item. Rejected: a thirty-item list becomes thirty requests, thirty chances to half-fail, and thirty times the rate-limit budget.

## Revisiting

Reopen the decision if one of these signals appears:

- **The `shopping_export_target` table ships**: per-household storage becomes possible, and the instance destination goes back to being what it should be, a special case.
- **An OAuth flow already exists in the product** for another reason: the marginal cost of Microsoft To Do and Google Tasks collapses, and §4 must be reassessed.
- **Bring! publishes an official API** — and only in that case. A private endpoint that is more convenient, better documented by third parties or more stable than before changes nothing: the reason for the refusal is contractual, not technical.
- **The rank 2 CalDAV endpoint ships**: the negative consequence "native sharing ticks nothing" disappears for iOS as well as Android, and text sharing goes back to being a fallback rather than the main path.
- **Todoist publishes a per-request command limit below 100**: adjust `MAX_COMMANDS_PER_REQUEST` and remove the caveat from §Negative.
