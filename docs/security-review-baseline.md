# Chaudron — baseline security review

> Audit of the existing state as of **3 August 2026**, before the first
> publication of the repository. The identifiers cited (files, columns,
> variables) are in English and are authoritative as written.
> Companion: [`security-model.md`](security-model.md), which describes the target.
> **This document reports. No fix has been applied.**

---

## 1. Method and scope

The repository is in the scoping phase: documentation, ADRs, data-model skeleton,
container units, CI. **No feature code.** The audit therefore covers the
**design** and the **baseline**, not an implementation.

Scope covered: the 37 files that will actually be published (list obtained by
applying `.gitignore`), namely `README.md`, `SECURITY.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `LICENSE`, `.gitignore`, `.editorconfig`, `.env.example`,
`.github/**`, `backend/**` (excluding caches and `.venv`), `docs/**`, `ops/**`.

Accepted design consequence: findings that concern a decision (RLS, retention,
webhook signature) are recorded as **design** defects. At this stage, fixing a
document costs an afternoon; fixing the corresponding schema will cost a
migration.

**Severity scale.** It qualifies the product impact **if it ships as designed**,
not exploitability today — nothing is exploitable today, there is no application.

| Level | Meaning |
|---|---|
| **Critical** | Compromises an asset belonging to a third party, or tenant isolation between households. Blocks phase 2. |
| **High** | Cancels or contradicts a security control announced by the project. |
| **Medium** | Missing hardening, or a divergence between two documents that will produce a defect. |
| **Low** | Real defect but with bounded impact, or operational friction. |
| **Informational** | Consistency, hygiene, documentation debt. |

---

## 2. Secret scan — result

### 2.1 Verdict

> ## **No real secret was found. The repository is clean. The scan is not a publication blocker.**

### 2.2 Tooling

`gitleaks`, `trufflehog` and `osv-scanner` **are not installed** on the machine
(`which` negative for all three). In accordance with the brief, the scan was done
**manually** with `grep -rInE`, over the whole tree, with `.git/`,
`backend/.venv/`, `backend/.mypy_cache/`, `backend/.ruff_cache/`,
`backend/.pytest_cache/` and `__pycache__/` excluded, then replayed **file by
file over the 37 actually publishable files**.

A favourable note: CI runs `gitleaks/gitleaks-action@v2` over the full history
(`fetch-depth: 0`, `.github/workflows/ci.yml:194-206`). The automated control
therefore exists in the pipeline; it is only missing on the machine.

### 2.3 Patterns searched for

`AKIA…`, `ASIA…`, `ghp_`, `gho_`, `ghs_`, `github_pat_`, `sk-`, `sk-ant-`,
`AIza…`, `xox[baprs]-`, `glpat-`, `-----BEGIN`, `PRIVATE KEY`, `AGE-SECRET-KEY`,
`eyJhbGciOi` (JWT), URLs with embedded credentials `://user:pass@`, and
assignments of the form `password|secret|token|api_key|credential = <value of 8+
characters>`. A complementary pass looked for base64/hex strings of 40 characters
or more.

### 2.4 Occurrences noted, and why none is a secret

| File:line | Value | Verdict |
|---|---|---|
| `.env.example:20` | `postgresql+asyncpg://user:password@host:5432/dbname` | **Template.** Literally `user` and `password`. |
| `CONTRIBUTING.md:130` | `postgresql+asyncpg://chaudron:<password>@127.0.0.1:5432/chaudron` | **Template.** The password is an angle-bracket placeholder. |
| `.github/workflows/ci.yml:111` | `postgresql+asyncpg://chaudron:chaudron@localhost:5432/chaudron_test` | **Credentials of an ephemeral containerised service**, created and destroyed within the job. No value outside CI. See SEC-025. |
| `.github/workflows/ci.yml:112` | `CHAUDRON_SECRET_KEY: ci-only-not-a-real-secret` | **Self-documenting value**, with no entropy. Correct. |
| `.env.example:61` | `CHAUDRON_LLM_DEFAULT_MODEL=claude-opus-5` | **Model name**, not a secret. See SEC-027. |
| `.env.example:89` | `CHAUDRON_OFF_BASE_URL=https://world.openfoodfacts.org` | Public URL. |

### 2.5 Complementary checks

- **Git history: empty.** `git log` returns no commit (`No commits yet on main`).
  There is therefore **no git object to rewrite or purge**: the first push will
  publish exactly the auditable state above. This is the most favourable
  situation possible, and it will not occur again.
- **`.git/config`:** contains only a `user.email`, no remote, no token, no
  credential helper. See SEC-029.
- **Ignored files present on disk** (`.venv/`, `.coverage`, `.pytest_cache/`,
  `.mypy_cache/`, `.ruff_cache/`, `__pycache__/`): **all correctly excluded** by
  `.gitignore`. None appears in the list of publishable files.
- **`.env`: absent from disk.** Nothing to leak.
- **`backend/uv.lock`:** no match on the key patterns; contains only SHA-256
  digests, which is its purpose.
- **No binary file** in the publishable set apart from `uv.lock` (text). No
  archive, no dump, no certificate, no private key.

**Conclusion:** publication is not blocked by a secret leak. It is blocked by the
findings in §3 below.

---

## 3. Findings

### Critical

---

#### SEC-001 — Tenant isolation between households rests on an application convention, with no engine-level guard rail

**Severity:** Critical
**Files:** `docs/adr/0006-multi-tenant-from-day-one.md:41,49,54` ·
`docs/data-model.md` §5.2 (composite foreign keys, `HouseholdScope`) and §5.3 (RLS) ·
`backend/src/chaudron/domain/models.py` → `HouseholdScopedMixin`, the
`ix_receipt_pending` index on `Receipt`, `Product.household_id` (nullable) ·
`CONTRIBUTING.md:373-377`

**Description.** The isolation setup has three layers. The first two are real but
do not cover the principal threat, and the third is deferred.

The **composite FKs** `(household_id, x_id) → parent(household_id, id)` are an
excellent control: they make a cross-household **write** impossible at the
database level, even with an application bug. On `llm_purpose_binding`, this is
what prevents spending another household's API key. This point is well designed
and must be kept.

But the leak that destroys this product is a **read**: a `SELECT` without
`WHERE household_id`. No FK filters a read. What remains, then, is:

1. code review — carried out by a single maintainer, who reviews their own code;
2. mandatory isolation tests — written by the same person, for the resources she
   thought of. **Nothing fails when you forget to write the test.** That is
   exactly the failure mode ADR-0006 holds against late migration, reproduced one
   level down.

Three concrete aggravating factors in the current schema:

- **Background jobs** (receipt parsing, expiry notifications, reconciliation) run
  **outside the HTTP request**, hence outside `HouseholdScope`.
  `docs/data-model.md` §5.4 says so explicitly: *"they are the ones that will
  leak first"*. The `ix_receipt_pending` index is **deliberately cross-household**:
  the worker reads a mixed queue and must re-scope itself by hand on every row.
- **`product_id` is a simple FK**, because `product.household_id` is nullable and
  therefore cannot serve as a composite target. Nothing at the database level
  prevents referencing another household's *private* product — which exposes
  identifying purchase habits (brands, diets, medical products). Known and
  accepted hole (`docs/data-model.md` §5.2, the "known and owned hole" paragraph
  on `inventory_lot.product_id`).
- **The trigger for enabling RLS is not observable**: "the day an account is
  created by a person outside the family circle". Neither CI nor a test can
  detect it. It will be crossed one evening, out of convenience.

**The deferral argument does not hold for the chosen stack.** RLS is deferred
because `SET LOCAL` would require transaction-mode pooling, and an error would
produce an inverted leak — a recycled connection retaining the previous
household. That reasoning is correct **in the presence of an external pooler**
(PgBouncer in session or statement mode). The chosen stack is asynchronous
SQLAlchemy 2.x + `asyncpg`, with an **in-process** pool that reserves a connection
for the duration of the transaction; `SET LOCAL` is reset by PostgreSQL itself at
`COMMIT`/`ROLLBACK`. The feared failure mode assumes either a session `SET`
instead of a `SET LOCAL`, or a component that Chaudron has not chosen. **The cost
invoked is that of an architecture which is not its own.**

**Concrete impact.** A legitimate user of another household reads a home's
complete inventory (`recipe_suggestion.stock_snapshot`), the till receipts, the
shopping list and a third party's provider configuration. It is the most likely
and most severe leak of this product, and the public repository will indicate
precisely where to look.

**Fix.** **Require RLS as of v1**, in the first Alembic migration:

1. Application role `chaudron_app`, **not the owner** of the tables, plus
   `ALTER TABLE … FORCE ROW LEVEL SECURITY` on every table carrying a
   `household_id` (without `FORCE`, the owner bypasses the policies).
2. `SET LOCAL app.household_id = …` emitted from **a single point** — the session
   factory — inside the transaction; a "one HTTP request = one transaction"
   discipline, already listed as a prerequisite to be paid immediately.
3. Policies with identical `USING` **and** `WITH CHECK`:
   `household_id = current_setting('app.household_id', true)::uuid`. An absent
   `current_setting` must make the query **fail**, not let it through.
4. A separate `chaudron_worker` role for the cross-household queue: a view or a
   `SECURITY DEFINER` function exposing only `(id, household_id)` of the pending
   receipts; the actual processing takes place only after the tenant has been set.
   `ix_receipt_pending` stays cross-household but no longer gives access to the data.
5. **Keep** the application layer and the isolation tests: RLS is a second
   barrier, not a replacement. A missing application filter will then give a slow
   query, not a wrong query.
6. Settle at the same time the hardening of `product_id`
   (`docs/data-model.md` §11, open question 4): a `household_id` sentinel on the
   public catalogue, or a function-based `CHECK`.

Update ADR-0006 through a **new ADR that supersedes it** (ADRs are immutable,
`CONTRIBUTING.md:351-356`), correcting the premise about pooling.

**Cost today: one migration and one session dependency.** There is no feature
code. Every hour of retrofit that ADR-0006 dreads is an hour that has not yet
been spent — that is ADR-0006's own argument, applied to its own conclusion.

---

### High

---

#### SEC-002 — The credential encryption key is provisioned by no Podman secret

**Severity:** High
**Files:** `ops/chaudron.container:32,40-42` · `ops/README.md:160-190` ·
`.env.example:38-58` · `docs/adr/0007-byok-and-local-inference.md:42` ·
`docs/data-model.md` §9.2 (`llm_provider_config.api_key_ciphertext`,
`api_key_encryption_key_id`)

**Description.** `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY` is declared **required**
(`.env.example:44`) and both ADR-0007 and the data model state that it comes from
the environment **via a Podman secret**, never from the database, so that a stolen
dump contains nothing usable.

Yet the quadlet declares only **three** secrets — `CHAUDRON_SECRET_KEY`,
`ANTHROPIC_API_KEY`, `CHAUDRON_INBOUND_EMAIL_WEBHOOK_KEY` (lines 40-42) — and the
procedure in `ops/README.md:167-181` creates only four, without this one. An
operator who follows the documentation has only one place to put it:
`EnvironmentFile=%h/chaudron/chaudron.env` (line 32), **a cleartext file, in the
same home directory as the backups** produced by `ops/README.md:257`.

The described control — "a stolen dump is not enough to decrypt" — rests entirely
on the key and the dump not travelling together. Following the documentation,
they travel together.

**Secondary divergence:** `OPENAI_API_KEY`, `GEMINI_API_KEY` and `MISTRAL_API_KEY`
(`.env.example:56-58`) are not provisioned either, whereas four providers are
targeted in v1 (ADR-0005). The quadlet has stayed in the era when Anthropic was
the only one.

**Fix.**

1. Add to the quadlet, after lines 40-42:

   ```ini
   Secret=chaudron-credential-encryption-key,type=env,target=CHAUDRON_CREDENTIAL_ENCRYPTION_KEY
   Secret=chaudron-openai-api-key,type=env,target=OPENAI_API_KEY
   Secret=chaudron-gemini-api-key,type=env,target=GEMINI_API_KEY
   Secret=chaudron-mistral-api-key,type=env,target=MISTRAL_API_KEY
   ```

2. Add the corresponding command to `ops/README.md` §2.3, in the same safe format
   as the others (masked entry, stdin, `unset`), to be run **on the server, under
   the `chaudron` account**:

   ```sh
   read -rs -p 'Credential encryption key: ' V && printf '%s' "$V" | podman secret create chaudron-credential-encryption-key - && unset V
   ```

3. Move the four provider keys out of the `.env.example` "instance_owner" section
   into a note pointing to the Podman secrets, so that the template no longer
   suggests writing them into a file.
4. Add to `ops/README.md` §4 the explicit warning: **the database backups and the
   encryption key must not be stored in the same place**, failing which encryption
   at rest no longer protects anything.

---

#### SEC-003 — The error columns can receive an API key in cleartext and display it

**Severity:** High
**Files:** `backend/src/chaudron/domain/models.py` → `LlmProviderConfig.last_error`,
`Receipt.parse_error`, `Receipt.raw_response` · `docs/data-model.md` §4.10
(`receipt.raw_response`, `receipt.parse_error`) and §4.13
(`llm_provider_config.last_error`) ·
`docs/adr/0007-byok-and-local-inference.md:44`

**Description.** ADR-0007 promises that keys are *"never logged"* and that
*"exception traces returned to the client are rewritten — an SDK that included the
key in an error message must not propagate it"*.

The schema contradicts that promise. `llm_provider_config.last_error` is a `text`
column intended to receive the upstream error message, and
`docs/data-model.md` §4.13 plans for it to feed the *"your key no longer works"*
banner — the one `ix_llm_provider_config_invalid` exists to serve —
**displayed on every page**. `receipt.parse_error` has the same
shape, and `receipt.raw_response` receives an endpoint's raw output.

No redaction is specified **at write time**. The path is direct: a provider, a
corporate proxy, or an endpoint under a third party's control that returns the
`Authorization` header or the key in its error message, and a
`last_error = str(exc)` written in the service. The key then ends up **in
cleartext in the database**, then **on screen**, then **in the `pg_dump` dump** —
that is, exactly where the whole at-rest encryption apparatus existed to keep it
out of. It is the only flaw in the project capable of cancelling §6.1 of the
threat model with a single line of code.

**Fix.**

1. These three columns **never** receive raw upstream text. The service
   translates the error into a domain code (`ProviderUnavailable`,
   `ProviderQuotaExceeded`, `ProviderResponseInvalid`, already defined by
   ADR-0005) and writes only that code plus a controlled message.
2. If an upstream excerpt absolutely must be kept for diagnostics: pass it
   through a single, tested redaction function that masks every known secret
   pattern (`sk-`, `sk-ant-`, `AIza`, `Bearer …`, the `x-api-key` header) **and**
   any high-entropy string longer than 20 characters; bound the stored length.
3. Add a `comment=` on `last_error` and `parse_error` in the model, just as the
   one already carried by `LlmProviderConfig.api_key_ciphertext`, which is a
   good example to generalise.
4. Add an explicit case to the test suite: a fake adapter returning an error
   containing a fake key, and the assertion that neither the database nor the HTTP
   response contains it.

---

#### SEC-004 — Two contradictory sources of truth for `instance_owner` authorisation

**Severity:** High
**Files:** `.env.example:52` (`CHAUDRON_INSTANCE_OWNER_HOUSEHOLD_ID`) ·
`backend/src/chaudron/domain/models.py` → `Household.is_instance_owner` and the
`uq_household_instance_owner` index ·
`docs/adr/0007-byok-and-local-inference.md:29` · `docs/data-model.md` §9.4
(`household.is_instance_owner`, `uq_household_instance_owner`)

**Description.** The `instance_owner` mode determines **who has the right to spend
the operator's money**. Two different mechanisms claim to decide it:

- ADR-0007: *"usable only by the household explicitly designated as the owner of
  the instance (**dedicated environment variable**)"* — hence
  `CHAUDRON_INSTANCE_OWNER_HOUSEHOLD_ID`;
- the data model: the `household.is_instance_owner` column, protected by a unique
  index guaranteeing there is **at most one**.

Nothing says which one is authoritative, nor how they are kept consistent. Any
divergence is an authorisation granted by mistake: a household marked in the
database but absent from the environment, or the reverse, and the operator pays
for a third party's inference. The data model also acknowledges that the rule is
cross-table, hence not expressible as a `CHECK`, and rests on the service alone.

**Fix.** Choose **one** source and make the other subordinate.

- Retain `household.is_instance_owner` as the **sole** authority — the unique
  index is a real guard rail, which the environment is not.
- Keep `CHAUDRON_INSTANCE_OWNER_HOUSEHOLD_ID` only as a **startup assertion**: at
  boot, if the variable is set and does not match the household marked in the
  database, **refuse to start** (consistent with the fail-fast rule of
  `.env.example:3`). If it is empty, the mode is disabled.
- Document the assignment procedure as an explicit and logged administrative act.
- Back it up with an RLS policy when SEC-001 is applied.

---

#### SEC-005 — Email webhook: replay not handled, household address guessable, no modelling

**Severity:** High
**Files:** `.env.example:93-101` · `docs/architecture.md` §3.4 ("Receiving an
order email") · `SECURITY.md:124-130` ·
`backend/src/chaudron/domain/models.py` → `Household` (no inbound address
column) · design note absent

**Description.** This is the only endpoint designed to be called by a stranger,
and it is the most under-specified sensitive surface in the repository. Four
distinct gaps:

1. **Replay.** The signature is planned (`CHAUDRON_INBOUND_EMAIL_WEBHOOK_KEY`),
   but nothing mentions a signed timestamp, a tolerance window, or a cache of
   message identifiers. A captured valid signature stays valid indefinitely and
   can be replayed N times.
2. **Non-constant-time comparison.** The algorithm is not specified. A `==` on a
   signature is vulnerable to timing; `hmac.compare_digest` is mandatory and must
   be written, not assumed.
3. **Guessability of the destination address — the most severe point.** The
   architecture attaches an email to a household *"by the destination address"*.
   That address is therefore, in effect, **an authorisation secret**: whoever
   knows it injects into that household. If it derives from the household name or
   from a counter, it is guessable and enumerable — and the public repository will
   say exactly how it is built. **No column of the data model carries it today**,
   so nothing guarantees that it will be random.
4. **Enumeration.** Nothing requires an identical response for a known and an
   unknown address. Without that, the webhook becomes an oracle for household
   existence.

**Aggravating factor:** the design note meant to cover all of this,
`docs/technical-notes-ingestion.md`, **does not exist** (see SEC-019).

**Fix.**

1. Add to `household` an `inbound_email_token` column: a random token of **at
   least 128 bits**, unique, indexed, regenerable, nullable (a household not using
   the feature does not have one). The address becomes
   `<token>@<CHAUDRON_INBOUND_EMAIL_DOMAIN>` and **never** derives from the
   household name nor from a sequential identifier.
2. Specify the signature: HMAC-SHA-256 over `timestamp + raw body`,
   `hmac.compare_digest`, a 5-minute tolerance window, a message-identifier
   deduplication table with purging.
3. **Identical** response and delay for an unknown address and a known one.
4. Attachment bounds: maximum count, image dimensions checked **before** decoding,
   MIME type determined by inspecting the content and not from the header, and
   **storage key derived from `(household_id, uuid)` — never from the received
   file name**.
5. Write `docs/technical-notes-ingestion.md` **before** implementing the feature.

---

#### SEC-006 — The described SSRF validation closes neither the DNS TOCTOU, nor the port, nor the alternative notations

**Severity:** High
**Files:** `.env.example:71-81` · `docs/adr/0007-byok-and-local-inference.md:47` ·
`docs/architecture.md` §4 ("The hard part: Ollama topology", the paragraph on the
user-supplied base URL as an SSRF primitive) · `SECURITY.md:99-109`

**Description.** The choice of an explicit allowlist is **correct**, and the
reasoning that leads to it (filtering private ranges is inoperative, since the
legitimate address of a co-located Ollama *is* private) is sound. What is missing
are the application details, and an allowlist is only safe if the allowed host is
exactly the host contacted.

Five gaps:

1. **DNS rebinding.** The announced control is *"DNS resolution performed at
   validation and before the call"*. Resolving twice does not close the window:
   the HTTP client re-resolves at connection time. The only control that holds is
   **resolve, validate the IP, then connect to that IP**, carrying the original
   name in the `Host` header.
2. **Free port.** `.env.example:76-79` accepts "hostnames **or** host:port". An
   allowed host without a port allows **all** of its ports: the server becomes an
   internal port scanner (`ollama:22`, `ollama:5432`, `ollama:6379`), response
   times alone being enough to map.
3. **Alternative notations.** No normalisation is described. `0x7f000001`,
   `2130706433`, `127.1`, `0.0.0.0`, `[::1]`, `[::ffff:127.0.0.1]`, `localhost.`
   (trailing dot), `127.0.0.1.nip.io` bypass any textual comparison on the host.
4. **`userinfo`.** `http://ollama@attacker.example/` is read as allowed by a naive
   parser.
5. **No refusal floor.** The allowlist being entirely in the operator's hands,
   nothing prevents `169.254.169.254` from appearing in it by mistake or by
   copy-paste.

**Fix.** A single, tested function, traversed by **all** outbound calls toward a
user-supplied host — including capability probing at registration:

```
resolve_and_validate(url) -> (ip, port, host_header)
```

- scheme strictly `http`/`https`; rejection if the URL contains `@`, a control
  character, or an encoded sequence in the host part;
- **mandatory port** in the allowlist; an entry without a port means "11434 only",
  never "all";
- resolution, then comparison on **the normalised IP** (not on the text), then
  connection to that IP with an explicit `Host`;
- **non-bypassable floor denylist**, applied even if the operator allows the host:
  `169.254.0.0/16`, `::ffff:169.254.0.0/112`, `fd00:ec2::254`, `0.0.0.0/8`,
  `::/128`;
- `follow_redirects=False` set explicitly on the `httpx` client;
- a response **size** bound — add `CHAUDRON_OLLAMA_MAX_RESPONSE_BYTES`, today there
  is only a time bound — and a depth bound on the deserialised JSON;
- a test set containing each of the notations above by name.

---

#### SEC-007 — The JWT algorithm is configurable and the signing secret is shared between two uses

**Severity:** High
**Files:** `.env.example`, the "Security" block (`CHAUDRON_SECRET_KEY`,
`CHAUDRON_JWT_TTL_MINUTES`, `CHAUDRON_JWT_ALGORITHM`) ·
`backend/src/chaudron/config.py` → `Settings.jwt_algorithm`,
`Settings.jwt_ttl_minutes`

> Both settings have since been **removed** rather than typed, from
> `.env.example` and from `Settings`: no JWT is issued or verified anywhere in
> the codebase, so nothing read them. Authentication landed as server-side
> sessions and hashed machine tokens (`docs/architecture.md` §8.1). The first
> half of this finding is therefore closed by deletion; the second half — one
> secret for two uses — no longer applies either, `CHAUDRON_SECRET_KEY` having
> ended up with a single use (the CalDAV feed key derivation).

**Description.** Two distinct defects in the same place.

`CHAUDRON_JWT_ALGORITHM` is an **environment variable**. Making the verification
algorithm configurable is the classic entry point for algorithm confusion:
`none`, or an RSA signature verified as an HMAC with the public key. An
application's algorithm is a property of the code, not of its deployment — an
operator has no legitimate reason to change it, and making it modifiable creates
only a risk.

`CHAUDRON_SECRET_KEY` is described as serving *"to sign sessions and JWTs"*: a
single secret for two mechanisms. Its leak compromises both, and its rotation
invalidates both.

**Fix.**

1. Remove `CHAUDRON_JWT_ALGORITHM` from `.env.example`. Freeze the algorithm in
   the code, and **enforce a list of accepted algorithms** at decoding
   (`algorithms=["HS256"]`), never the one announced by the token.
2. Separate the secrets: either two variables, or a master key and two subkeys
   derived by HKDF with distinct contexts.
3. Settle the authentication strategy (`docs/architecture.md` §8, then titled
   "What is not yet settled") **before**
   the first migration: transport (`Secure`, `HttpOnly`, `SameSite=Lax` or
   `Strict` cookie), duration, and **revocation mechanism** — a JWT with no
   revocation list cannot be withdrawn before expiry.

---

#### SEC-008 — No retention is defined, and the `CASCADE` does not delete the images

**Severity:** High
**Files:** `backend/src/chaudron/domain/models.py` → `Receipt.image_object_key`,
`Receipt.raw_response`, `RecipeSuggestion.stock_snapshot`, and the `CASCADE`
carried by `HouseholdScopedMixin` ·
`docs/data-model.md` §11, open question 5 (retention) · `docs/architecture.md` §6
(receipt images) and §8

> `Receipt.image_object_key` has since acquired a `COMMENT ON COLUMN` stating
> that it is **always** `NULL` — the image is read in memory and discarded
> (migration `0012`, `docs/architecture.md` §6). The storage half of this finding
> is answered by never writing an object rather than by purging one; the
> retention question on `raw_response` and `stock_snapshot`, and the fact that
> `CASCADE` covers only PostgreSQL, are untouched by that.

**Description.** Two problems that combine badly.

**No retention period is fixed**, and above all **no column allows tracking it**.
The architecture recommends *"purge after processing to be preferred"* and the
data model classes the question as one to settle before the first third-party
account. Without a column there is no purge: there is an intention. This concerns
the three most sensitive pieces of data in the system: the receipt images,
`receipt.raw_response`, and `recipe_suggestion.stock_snapshot` — which the model
itself calls the *"most sensitive data in the database"*, since it is **a home's
complete inventory, frozen**.

**The `ON DELETE CASCADE` covers only PostgreSQL.** It is described as answering
GDPR erasure, *"total and atomic, not a cleanup script that forgets a table"* —
and that is true for the database. But deleting a household erases the rows and
**leaves all the objects** in storage. A partial erasure presented as complete is
a non-compliance that looks like compliance.

**Fix.**

1. Settle the periods **before the first migration**, and record them in the
   schema. Proposal to be arbitrated: image purged on review confirmation (30 days
   maximum), `raw_response` 90 days, `stock_snapshot` **30 days**, `raw_label`
   retained after dissociation from the household, logs 30 days.
2. Add the columns that make the purge verifiable: `image_purged_at` on `receipt`,
   and an `expires_at` (or a policy derived from `created_at`) usable by a
   scheduled task.
3. Write household deletion as an **application-level operation**: enumeration and
   deletion of the objects **before** the `DELETE` of the `household` row, with
   verification and logging. The `CASCADE` remains useful for atomicity in the
   database; it must no longer be presented as complete GDPR erasure.
4. Strip the EXIF metadata (GPS, device, timestamp) **at ingestion**, before
   writing — otherwise the home's geolocation is retained and re-served.

---

#### SEC-009 — No rate limiting is designed, anywhere

**Severity:** High
**Files:** `.env.example` (no variable) · `backend/pyproject.toml:15-26`
(no dependency) · `docs/architecture.md` §6 (absent from the security
table) · `SECURITY.md:142-148`

**Description.** None of the four endpoints that need it has any rate limiting
described:

- **login** — credential stuffing and brute force, the default attack of an
  unauthenticated visitor against a self-hosted instance with no WAF and no
  fail2ban;
- **email webhook** — public endpoint, amplification and saturation;
- **receipt upload** — CPU cost, disk, and a **billed** model call;
- **recipe generation** — every call costs the household money, or the operator in
  `instance_owner` mode, where `CHAUDRON_LLM_MONTHLY_BUDGET_USD` is only a
  **global** ceiling: once reached, it cuts the feature off for everybody.

On top of that comes the Open Food Facts ceiling: 15 requests/minute **per IP**,
hence global to the instance, with a ban as the penalty — a single household
scanning in bursts can cut off product resolution for all the others. The
outbound client is limited to 10 req/min (ADR-0008), which protects Open Food
Facts from overload but does not protect the other households from monopolisation
of the quota.

**Fix.**

1. Decide the mechanism now (a PostgreSQL counter with a sliding window,
   sufficient at this scale and requiring no new dependency) and record it in the
   threat model.
2. Per-identity **and** per-IP limits on login, with progressive temporary
   lockout and logging.
3. Per-household quota on recipe generation and receipt import, configurable, with
   an honest message rather than a bare 429.
4. A fair per-household queue in front of the Open Food Facts client, so that one
   household cannot consume the whole instance quota.
5. Identical responses and delays at login for a known and an unknown email
   (anti-enumeration).

---

### Medium

---

#### SEC-010 — The README quick start publishes PostgreSQL on all interfaces

**Severity:** Medium
**File:** `README.md:166-169`

**Description.** The quick-start block contains `-p 5432:5432`, which publishes
the database on **all** of the host's interfaces. It is the most copy-pasted block
of a public repository, and it directly contradicts `ops/README.md:96`
(`-p 127.0.0.1:8000:8000`) and `ops/chaudron-db.container:26-28`, which rightly
insists: *"The database is never published to the host"*. The random password
(`openssl rand -hex 16`) limits the damage without removing the exposure
(scanning, version fingerprinting, future vulnerabilities in the daemon).

**Fix.** Replace with `-p 127.0.0.1:5432:5432`, and add the same one-line note as
in `ops/` explaining why loopback is the default.

---

#### SEC-011 — Third-party GitHub Actions are not pinned by digest

**Severity:** Medium
**File:** `.github/workflows/ci.yml:36,68,117,179` (`astral-sh/setup-uv@v7`),
`:204` (`gitleaks/gitleaks-action@v2`), `:33,65,114,148,176,198,218`
(`actions/checkout@v5`), `:134`, `:232`

**Description.** All the actions are referenced by **major version tag**. A tag is
mutable: its being moved by a compromised or malicious maintainer executes
arbitrary code in the runner, with read access to the repository and to the
dependency cache. The risk is higher on the two **third-party** actions
(`astral-sh/setup-uv`, `gitleaks/gitleaks-action`) than on those of the `actions`
organisation.

**Fix.** Pin by full commit SHA, with the tag in a comment:

```yaml
uses: astral-sh/setup-uv@<40-character-sha>  # v7.x.y
uses: gitleaks/gitleaks-action@<40-character-sha>  # v2.x.y
```

Add a `.github/dependabot.yml` with the `github-actions` ecosystem, which will
propose SHA bumps as pull requests rather than having them applied silently.

---

#### SEC-012 — Base images pinned by tag, and automatic database update from the registry

**Severity:** Medium
**Files:** `backend/Containerfile:14,36` · `ops/chaudron-db.container:19-20`

**Description.** Two divergences from the discipline applied everywhere else in
the project, where the Python dependencies are pinned **exactly** with a lockfile
and `UV_FROZEN`.

1. The base images (`ghcr.io/astral-sh/uv:python3.14-bookworm-slim`,
   `python:3.14-slim-bookworm`) are pinned by mutable tag. The header comment of
   the `Containerfile` claims *"bump deliberately, never implicitly"* — a tag does
   not allow keeping that promise.
2. `AutoUpdate=registry` on `docker.io/library/postgres:16` causes a new database
   image to be **pulled automatically** as soon as it is published, without
   review, without a maintenance window, and without a verified prior backup. It
   is an unplanned restart of the component that is hardest to restore, triggered
   by a third party. The API quadlet uses `AutoUpdate=local` (line 21), which is
   more prudent — the asymmetry is not justified.

**Fix.**

1. Pin the three images by `@sha256:…`, tag in a comment, explicit bumps. Hand the
   tracking to Dependabot (`docker` ecosystem, which handles `Containerfile`s).
2. Replace `AutoUpdate=registry` with `AutoUpdate=local` on
   `chaudron-db.container`, or remove it. A database version bump is a planned
   operation, preceded by a verified backup — `ops/README.md` §4 already describes
   the right procedure.

---

#### SEC-013 — `.gitignore` excludes neither backups, nor keys, nor the production environment file

**Severity:** Medium
**Files:** `.gitignore:14-21` · `ops/README.md:257` · `ops/chaudron.container:32`

**Description.** Three gaps, one of them directly induced by the documentation.

1. **No backup pattern.** `ops/README.md:257` proposes
   `pg_dump … > chaudron-$(date -I).dump` **in the current directory**. Run from
   the repository — which is the natural reflex — the command drops a complete
   copy of the database (A3, A4, encrypted A1) into a non-ignored git working
   directory.
2. **No cryptographic material pattern**: `*.pem`, `*.key`, `*.p12`, `*.pfx`,
   `id_rsa*`.
3. **`chaudron.env` is not covered.** The rules ignore `.env` and `.env.*`, but the
   production environment file is called `chaudron.env`
   (`ops/chaudron.container:32`) and matches no pattern.

**Fix.** Add to the "Secrets and local environment" section:

```gitignore
*.env
chaudron.env
*.dump
*.sql
*.sql.gz
*.pem
*.key
*.p12
*.pfx
id_rsa*
```

and modify `ops/README.md:257` to write the backup into a dedicated directory
outside the repository (`~/chaudron/backups/`), with a reminder about encrypting
it and keeping it separate from the encryption key (see SEC-002).

---

#### SEC-014 — Open Food Facts content is stored raw and rendered as if it were trustworthy

**Severity:** Medium
**Files:** `backend/src/chaudron/domain/models.py` → `Product` (`gtin`, `name`,
`brand`, `category_tag`, `image_url`, `off_payload`) ·
`docs/architecture.md` §5, the "model output treated as untrusted input" bullet

**Description.** The architecture states the right rule — *"model output treated as
untrusted input"* — but applies it only to models. Yet the fields coming from Open
Food Facts are **written by anonymous contributors**: `name`, `brand`,
`category_tag` are third-party free text, `off_payload` is a raw snapshot kept in
JSONB, and `image_url` is a third-party URL.

It is exactly the same risk class, on a path nobody watches because it does not
look like AI: stored XSS if a product name is rendered as HTML, loading of an
uncontrolled remote resource if `image_url` is used as is by the client. The
project has moreover already identified that Open Food Facts data is unreliable
(*"no assurances that the data is accurate"*), but on the **quality** side, not on
the **harmlessness** side.

**Fix.**

1. Explicitly add Open Food Facts data to the scope of the "untrusted input" rule
   in `docs/architecture.md` §5.
2. Strict Pydantic validation at the input: bounded lengths, controlled character
   set, `image_url` restricted to the `https` scheme and the
   `images.openfoodfacts.org` domain.
3. Rendering as **plain text** on the PWA side, never as HTML nor as Markdown with
   active links, plus a strict CSP without `unsafe-inline`.
4. Do not load `image_url` from the client: proxy and cache server-side, as
   already recommended for load reasons (`technical-notes-scanning.md` §3.5,
   point 6). The security reason adds to the courtesy reason.
5. Bound the size of `off_payload` before persisting it.

---

#### SEC-015 — CORS: no guard rail against pairing a wildcard origin with credentials

**Severity:** Medium
**File:** `.env.example:103-109`

**Description.** `CHAUDRON_CORS_ORIGINS` is an explicit list, which is correct. But
`CHAUDRON_CORS_ALLOW_CREDENTIALS` exists with no documented constraint. The pairing
of `*` + `allow_credentials=True` is the most common and most destructive CORS
misconfiguration: any site can then read the API's authenticated responses.

**Fix.**

1. Make startup **fail** — not produce a warning — if `CHAUDRON_CORS_ORIGINS`
   contains `*` while `CHAUDRON_CORS_ALLOW_CREDENTIALS=true`. This is consistent
   with the fail-fast rule already announced at the head of `.env.example`.
2. **Never** reflect the `Origin` header into `Access-Control-Allow-Origin`: only a
   value from the configured list is emitted.
3. Document it in the `.env.example` comment, where an operator will read it.

---

#### SEC-016 — The password hashing algorithm is neither decided nor tooled

**Severity:** Medium
**Files:** `backend/src/chaudron/domain/models.py` → `UserAccount.password_hash` ·
`backend/pyproject.toml:15-26`

**Description.** `user_account.password_hash` is a nullable `text`. No document
says what produces it, and **no hashing dependency is present** in
`pyproject.toml` (neither `argon2-cffi`, nor `passlib`, nor `bcrypt`). In the
absence of a decision, the first developer who implements login will choose under
pressure — and that is how you end up with hand-salted SHA-256.

Also missing: tracking of failed attempts, re-hashing at login when the parameters
change, and the maximum accepted length (an absent bound is a denial of service on
a slow algorithm).

**Fix.**

1. Settle on **Argon2id**, with explicit and versioned parameters, and add
   `argon2-cffi` as a pinned dependency.
2. Store the hash in PHC format (`$argon2id$v=19$m=…`), which carries its own
   parameters and makes progressive re-hashing possible without an extra column.
3. Re-hash on successful login when the stored parameters differ from the current
   ones.
4. Bound the accepted password length (for example 4096 bytes).

---

#### SEC-017 — The CI security controls block nothing and never run on their own

**Severity:** Medium
**File:** `.github/workflows/ci.yml:1-7,169-206` · absence of
`.github/dependabot.yml` · absence of `.github/CODEOWNERS`

**Description.** The two security jobs exist and are well chosen (`pip-audit
--strict` on the locked dependencies, `gitleaks` over the full history). Three
weaknesses make them less effective than they look:

1. **They are not declared required.** No `needs:` chains them to the other jobs,
   and nothing in the repository documents branch protection or a list of required
   checks. A scan that can be merged while failing protects nothing.
   `SECURITY.md:165-167` and `CONTRIBUTING.md:313-314` nonetheless assert that CI
   enforces them.
2. **No scheduled run.** The triggers are `push` on `main`, `pull_request` and
   `workflow_dispatch`. On a project with a low commit frequency, a CVE published
   the day after a merge sleeps until the next pull request — potentially months.
3. **No `dependabot.yml`, no `CODEOWNERS`.** Version bumps are entirely manual,
   and no review is required on the sensitive paths.

**Fix.**

1. Enable branch protection on `main`: mandatory pull request, `security-deps` and
   `security-secrets` as required checks, linear history.
2. Add `schedule: - cron: "0 6 * * 1"` to the workflow for a weekly run of the
   dependency audit.
3. Add `.github/dependabot.yml` covering `github-actions`, `uv` (or `pip`) and
   `docker`.
4. Add `.github/CODEOWNERS` on `ops/`, `.github/`, `docs/adr/` and
   `backend/src/chaudron/domain/`.
5. Check that `gitleaks/gitleaks-action@v2` does not require a licence for this
   repository: this point conditions the job's actual operation, and it is silent
   if it fails to initialise.

---

#### SEC-018 — No bound on HTTP uploads, against a 64 MB `/tmp`

**Severity:** Medium
**Files:** `ops/chaudron.container:60-62` · `.env.example:100-101` ·
`backend/pyproject.toml:25` (`python-multipart`)

**Description.** The container is `ReadOnly=true` with a single
`Tmpfs=/tmp:rw,size=64M`. That is good hardening. But `python-multipart` spills to
disk beyond a threshold, and **no size bound exists for the HTTP upload of a
receipt**: `CHAUDRON_INBOUND_EMAIL_MAX_BYTES` covers only the email path. A large
upload fills the 64 MB and makes everything that needs to write fail.

Also missing: a total volume ceiling per household, and mandatory pagination on
potentially long lists (`stock_movement`, `receipt_line`).

**Fix.**

1. Add `CHAUDRON_RECEIPT_MAX_UPLOAD_BYTES`, applied **before** reading the body
   (rejection on `Content-Length`, plus streaming verification), and refuse at the
   reverse proxy level as well.
2. Explicitly configure `python-multipart`'s spill threshold and the temporary
   directory it uses.
3. Size `Tmpfs` accordingly, or mount a volume dedicated to in-progress uploads.
4. Mandatory and capped pagination on every list, from the very first route.

---

#### SEC-019 — `docs/technical-notes-ingestion.md` is referenced twice and does not exist

**Severity:** Medium
**Files:** `README.md:206` · `docs/architecture.md` §3.4 (the closing link to the
note)

> The note has since been **written**. `docs/technical-notes-ingestion.md` exists,
> and both links resolve. The finding is kept for the record; the "Fix" below is
> what was done.

**Description.** The document is presented as covering *"Inbound email, receipt
OCR, shopping list export"*, and `docs/architecture.md` points to it for the
webhook details. It is absent. This is not merely a dead link in a public
repository: **it is the design note for the most sensitive and most
under-specified surface of the project** (see SEC-005). Its absence largely
explains why replay, address guessability and enumeration are handled nowhere.

**Fix.** Write the note before implementing the feature, addressing by name the
five points of SEC-005. Failing that, and in the very short term, remove the two
links rather than publishing a repository that promises a nonexistent document.

---

#### SEC-020 — No audit logging of accesses to sensitive assets

**Severity:** Medium
**Files:** `backend/src/chaudron/domain/models.py` (no audit table) ·
`docs/architecture.md` §7 ("Observability")

**Description.** The planned observability is good for operations: structured logs
from the first commit, `household_id` and request identifier on every line, three
well-chosen metrics. But nothing traces **accesses to the sensitive assets**:
decryption of a key, creation or modification of a provider configuration, export
of a household, deletion of a household, assignment of `instance_owner` mode,
access to a receipt image.

Direct consequence: in the event of a data breach, the operator can neither
delimit the incident nor demonstrate that there has not been one — while having to
notify within 72 hours. It is also the only way to detect abuse by a legitimate
user, who by definition triggers no authentication alert.

**Fix.**

1. Add an append-only `audit_event` table: `occurred_at`, `household_id` (nullable
   for instance-level events), `actor_user_id`, `action` (closed enum),
   `target_type`, `target_id`, `request_id`, `ip_hash`. No content, only
   references — an audit table must not become a second store of personal data.
2. Log at minimum the six events listed above.
3. A distinct retention period, longer than that of the application logs (12
   months), and explicit exclusion from the ordinary GDPR purge — an audit log is
   retained under legitimate interest, which must be written down.

---

### Low

---

#### SEC-021 — The production environment file is created with the wrong owner

**Severity:** Low
**File:** `ops/README.md:183-190`

**Description.** `install -m 0600 /dev/null ~chaudron/chaudron.env` appears in a
section whose preceding commands run as `root` (`useradd`, `install -d`). The file
will therefore be **owned by root in mode 0600**, and the `chaudron` account —
under which the quadlet runs — will not be able to read it: `EnvironmentFile=`
will fail at startup. Mode 0600 is the right reflex; the owner is not.

**Fix.** `install -o chaudron -g chaudron -m 0600 /dev/null ~chaudron/chaudron.env`,
and state under which account each block of section §2 must be run — the
distinction is already well made in §2.3, it is missing in §2.4.

---

#### SEC-022 — The repository URL is inconsistent between the quadlets and the rest of the project

**Severity:** Low
**Files:** `ops/chaudron.container:11` · `ops/chaudron-db.container:11`
(`https://github.com/stackops/chaudron`) · `README.md:11`, `SECURITY.md:35`,
`CONTRIBUTING.md:75`, `backend/pyproject.toml:29`,
`.github/ISSUE_TEMPLATE/config.yml:11` (`ClaraVnk/chaudron`)

**Description.** The two quadlet units point to `stackops/chaudron`, everything
else to `ClaraVnk/chaudron`. This is not just a typo: an operator who discovers a
security problem while reading the systemd unit on their server will follow the
`Documentation=` link and land on a repository that is not the project's. The
reporting channel described in `SECURITY.md` is then bypassed before it has even
been read.

**Fix.** Align on the definitive canonical URL **before** the first public push —
that is the moment when the choice is still free — and check all the occurrences
in one pass.

---

#### SEC-023 — `DAC_OVERRIDE` on the database container

**Severity:** Low
**File:** `ops/chaudron-db.container:52-53`

**Description.** The quadlet applies `DropCapability=ALL` then reintroduces
`CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID`. The approach is the right one, and
those capabilities are indeed necessary for the entrypoint of the official
`postgres` image, which adjusts permissions then drops its privileges.
`DAC_OVERRIDE` nonetheless remains the broadest of the list: it bypasses every
file permission check inside the container.

**Fix.** Optional and to be measured, not to be applied blindly: fix the ownership
of the data directory on the host
(`podman unshare chown -R 999:999 ~/chaudron/data/postgres`, the technique is
already documented for uploads in `ops/README.md:247`), then remove `DAC_OVERRIDE`
and `FOWNER` and check that `initdb` **and** a restart both pass. If either fails,
keep the current configuration and annotate it — a capability justified in writing
is better than a removed capability that breaks on the third start.

---

#### SEC-024 — CI builds an image from a `Containerfile` controlled by the pull request

**Severity:** Low
**File:** `.github/workflows/ci.yml:143-164`

**Description.** The `backend-build` job runs `podman build` on the branch's
`Containerfile`, including for a pull request coming from a fork. The `RUN`
instructions therefore execute on the runner with code controlled by an unknown
contributor.

The impact is **strongly bounded** — and that is why the severity stays low: the
trigger is `pull_request` and not `pull_request_target` (the classic trap is
**correctly avoided**), the token is read-only (`permissions: contents: read`), no
repository secret is exposed to forks, and a fork PR's caches are isolated from
those of the base branch. What remains is compute hijacking and outbound network
access from the runner.

**Fix.** Enable *"Require approval for all outside collaborators"* in the
repository's workflow run settings — one click, and it is the proportionate
control here. Do not add a secret to this job; if it should one day push an image
to a registry, do so in a **separate** workflow triggered only on `push` to `main`
or on a tag.

---

#### SEC-025 — Test database credentials in cleartext in the workflow

**Severity:** Low
**File:** `.github/workflows/ci.yml:96-99,111`

**Description.** `POSTGRES_PASSWORD: chaudron` and the corresponding DSN are
written in cleartext. This is **not a leak**: the containerised service is born
and dies with the job, it is reachable only from the runner, and line 108
documents it correctly. Two drawbacks remain: any secret scanner will flag this
line in perpetuity (noise that eventually masks a real signal), and the habit of
writing a cleartext password into a public workflow is the one that produces the
next leak.

**Fix.** Generate the value inside the job
(`echo "PGPW=$(openssl rand -hex 16)" >> "$GITHUB_ENV"`) and compose the DSN from
it, or add a named and commented exclusion to the `gitleaks` configuration. The
second option is the cheaper.

---

### Informational

---

#### SEC-026 — `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY` is absent from the test environment

**Severity:** Informational
**File:** `.github/workflows/ci.yml:107-112`

The test job provides `CHAUDRON_ENV`, `CHAUDRON_LOG_LEVEL`, `CHAUDRON_DATABASE_URL`
and `CHAUDRON_SECRET_KEY`, but not `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`, which is
nonetheless marked **REQUIRED** in `.env.example:38-44`. If the fail-fast
validation is honest, the application will not start in CI as soon as a test
instantiates it. Add an explicit test value (`ci-only-not-a-real-key`), which will
have the useful side effect of verifying that the variable really is mandatory.

---

#### SEC-027 — `.env.example` carries a value, contrary to its own rule

**Severity:** Informational
**Files:** `.env.example:61,89` · `CONTRIBUTING.md:84,366`

`CONTRIBUTING.md` asserts that `.env.example` *"never carries a value that
matters"* and makes adding a real value a ground for rejecting a pull request. Two
lines carry one: `CHAUDRON_LLM_DEFAULT_MODEL=claude-opus-5` and
`CHAUDRON_OFF_BASE_URL=https://world.openfoodfacts.org`. Neither is a secret, and
they are legitimate default values — but the rule as written is already violated
by the file it describes. Either refine the rule ("no **secret** value"), or move
these defaults into the configuration code, where they belong better.

---

#### SEC-028 — Scoping documents stale relative to the accepted ADRs

**Severity:** Informational
**Files:** `docs/architecture.md` §8 (then titled "What is not yet settled"), §2
(the "Ports defined by the domain" table) and §6 (the security table)

Section §8 "What is not yet settled" lists *"The project licence"*, whereas
`LICENSE` is AGPL-3.0 and `CONTRIBUTING.md` §7 details its consequences. It also
lists *"The Ollama topology chosen for v1"*, which ADR-0007 settled (co-located
case only). The `RecipeGenerator` row of the ports table mentions only Anthropic
and Ollama whereas ADR-0005 retains five, and the security table speaks of *"the
encryption key"* without naming it.

> Half of this is closed. `docs/architecture.md` §8 has been rewritten as
> "Authentication, and what is still open", and §8.2 now records that the licence
> and the Ollama topology were moved into the body of the document. The two
> remaining points stand as written: the ports table still names only two
> implementations for `RecipeGenerator`, and §6 still says "encryption key"
> without naming `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`.

A stale scoping document is read as if it were up to date, and that is how a
decision ends up relitigated. To be refreshed before publication — it is the first
thing an external reader will open (`README.md:28-29` sends them there).

---

#### SEC-029 — Git identity inconsistent with the project's declared author

**Severity:** Informational
**File:** `.git/config` (`user.email`, redacted — see below) ·
`backend/pyproject.toml:11` (`authors = [{ name = "ClaraVnk" }]`) ·
`CONTRIBUTING.md:440`

> The address originally quoted here has been removed. This finding is *about*
> address disclosure, and this document ships in a public repository: quoting the
> address to report the problem published it a second time. The finding stands
> without it — what matters is that the configured identity was not the declared
> author's, not which address it was.

The repository has **no commit**: the first push will freeze this identity in the
public history of every initial commit. The configured address is not that of the
declared author. This is not an application security problem, but it is an address
disclosure and an attribution inconsistency that cannot be cleanly corrected after
publication. Check `user.name` and `user.email` **before** the first commit, and
consider a `noreply`-style address if the exposure is not wanted.

---

#### SEC-030 — No signing policy, no SBOM, no image signature

**Severity:** Informational

Neither commit or tag signing, nor SBOM generation at build time, nor signing of
the published images. **None of this is necessary today** — the project publishes
no artefact — and adding it now would be ceremony. To be reassessed at the first
version tag: that is the moment when third parties will start executing binaries
produced by this repository, and when a verifiable chain stops being decorative.

---

#### SEC-031 — The allergen requirement has no mechanism

**Severity:** Informational
**File:** `docs/architecture.md` §6, the "Allergens" row of the security table

The security table contains a line that is correct and important: *"An error here
has physical consequences. Never present model-sourced allergen information as
authoritative"*. It is the project's only threat whose impact is bodily, and it
exists only as a table row — no mechanism, no schema constraint, no test.

A sentence in a document does not survive as far as the recipe screen. To be
turned into a constraint: allergens come from Open Food Facts' `allergens_tags` or
from human entry, **never** from a model-produced field; the model's output schema
contains no allergen field; and the display carries a warning that cannot be
dismissed. To be treated as a product requirement, not as a note.

---

## 4. Summary table

| ID | Severity | Finding | Main file |
|---|---|---|---|
| SEC-001 | **Critical** | Tenant isolation between households left to an application convention; RLS deferred on a premise that is false for the chosen stack | `docs/adr/0006-…:49` |
| SEC-002 | High | `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY` not provisioned as a Podman secret; ends up in cleartext next to the backups | `ops/chaudron.container:32,40-42` |
| SEC-003 | High | `last_error` / `parse_error` / `raw_response` can receive and display an API key in cleartext | `models.py` → `LlmProviderConfig.last_error` |
| SEC-004 | High | Two sources of truth for `instance_owner` authorisation | `.env.example:52` / `models.py` → `Household.is_instance_owner` |
| SEC-005 | High | Email webhook: replay, guessable household address, enumeration, no modelling | `docs/architecture.md` §3.4 |
| SEC-006 | High | SSRF: DNS TOCTOU, free port, alternative notations, no refusal floor | `docs/adr/0007-…:47` |
| SEC-007 | High | JWT algorithm configurable from the environment; signing secret shared | `.env.example:31-35` |
| SEC-008 | High | No retention defined; the `CASCADE` does not delete the images | `models.py` → `Receipt.image_object_key` |
| SEC-009 | High | No rate limiting designed (login, webhook, upload, generation) | `.env.example` (absent) |
| SEC-010 | Medium | The README quick start publishes PostgreSQL on all interfaces | `README.md:169` |
| SEC-011 | Medium | Third-party GitHub Actions pinned by mutable tag | `ci.yml:36,204` |
| SEC-012 | Medium | Base images by tag; `AutoUpdate=registry` on the database | `Containerfile:14,36` / `chaudron-db.container:20` |
| SEC-013 | Medium | `.gitignore` excludes neither dumps, nor keys, nor `chaudron.env` | `.gitignore:14-21` |
| SEC-014 | Medium | Open Food Facts content stored raw and rendered as trustworthy | `models.py` → `Product` |
| SEC-015 | Medium | CORS: no guard rail for wildcard origin + credentials | `.env.example:109` |
| SEC-016 | Medium | Password hashing neither decided nor tooled | `models.py` → `UserAccount.password_hash` |
| SEC-017 | Medium | Security jobs non-blocking, no scheduled scan, no Dependabot | `ci.yml:169-206` |
| SEC-018 | Medium | No HTTP upload bound against a 64 MB `/tmp` | `chaudron.container:62` |
| SEC-019 | Medium | `docs/technical-notes-ingestion.md` referenced twice, nonexistent | `README.md:206` |
| SEC-020 | Medium | No audit logging of accesses to sensitive assets | `models.py` (absent) |
| SEC-021 | Low | `chaudron.env` created with the wrong owner | `ops/README.md:189` |
| SEC-022 | Low | Repository URL inconsistent between quadlets and the rest of the project | `ops/*.container:11` |
| SEC-023 | Low | `DAC_OVERRIDE` on the database container | `chaudron-db.container:53` |
| SEC-024 | Low | `podman build` of a `Containerfile` controlled by a fork PR | `ci.yml:143-164` |
| SEC-025 | Low | Test database credentials in cleartext in the workflow | `ci.yml:96-99,111` |
| SEC-026 | Info | `CHAUDRON_CREDENTIAL_ENCRYPTION_KEY` absent from the test environment | `ci.yml:107-112` |
| SEC-027 | Info | `.env.example` carries values, contrary to its own rule | `.env.example:61,89` |
| SEC-028 | Info | Scoping documents stale relative to the accepted ADRs | `docs/architecture.md` §8 |
| SEC-029 | Info | Git identity inconsistent with the declared author | `.git/config` |
| SEC-030 | Info | No commit signing, no SBOM, no image signature | — |
| SEC-031 | Info | The allergen requirement has no mechanism | `docs/architecture.md` §6 |

**Breakdown:** 1 Critical · 8 High · 11 Medium · 5 Low · 6 Informational — **31
findings**.

---

## 5. To fix before the first public push

The ranking is by **cost of fixing after publication**, not by severity. A public
repository with no commit is in a situation that will not occur again: everything
fixed now has never existed.

### Blocking — do not push without these

These five points are either very cheap now and expensive later, or liable to
mislead a reader from the first hour of publication.

1. **SEC-029 — check `user.name` and `user.email`.** Five seconds. After the first
   push, the identity is in every commit of the public history, forever.
2. **SEC-013 — complete `.gitignore`** (`*.dump`, `*.sql`, `chaudron.env`, `*.pem`,
   `*.key`) **and** fix `ops/README.md:257` to write the backups outside the
   repository. It is the only fix that prevents a *future* leak by simple
   copy-paste of the documentation.
3. **SEC-010 — change `-p 5432:5432` to `-p 127.0.0.1:5432:5432`.** One line. It is
   the most copied block of a public repository, and it exposes a database.
4. **SEC-022 — align the repository URL.** A vulnerability reporting channel that
   points at the wrong repository is worse than none.
5. **SEC-019 / SEC-028 — either write the missing documents, or remove the dead
   links and refresh `docs/architecture.md` §8.** The README sends every external
   reader to these documents; it is the project's first impression.

### Before the first line of feature code

These decisions are taken once and are paid for in a migration if taken late. None
requires writing code — only settling the matter and recording it.

6. **SEC-001 — settle RLS**, and record it in an ADR superseding ADR-0006. The
   first Alembic migration must contain the non-owner role,
   `FORCE ROW LEVEL SECURITY` and the policies.
7. **SEC-008 — settle the retention periods** and add the corresponding columns to
   the first migration. Adding a column to an empty schema is free.
8. **SEC-005 — add `household.inbound_email_token`** to the model, and write
   `docs/technical-notes-ingestion.md` with replay, enumeration and attachment
   bounds.
9. **SEC-004 — choose the single source of truth** for `instance_owner`, and turn
   the environment variable into a startup assertion.
10. **SEC-007 / SEC-016 — settle authentication**: Argon2id, JWT algorithm frozen
    in the code, separate secrets, revocation mechanism.
11. **SEC-002 — complete the Podman secrets** in the quadlet and in the operating
    procedure. An incomplete quadlet, once published, becomes the template
    everybody copies.

### In the first few weeks

12. **SEC-003** — redaction rule and dedicated test, to be written **at the same
    time as** the first provider adapter, not after.
13. **SEC-006** — write `resolve_and_validate` and its test suite **before** the
    first outbound call toward a user-supplied host.
14. **SEC-009, SEC-018** — rate limiting and upload bounds, from the first route
    that accepts a body.
15. **SEC-011, SEC-012, SEC-017** — SHA pinning, `dependabot.yml`, branch
    protection with the security jobs as required checks.
16. **SEC-014, SEC-015, SEC-020** — external content treated as untrusted, CORS
    guard rail at startup, `audit_event` table.
17. **SEC-021, SEC-023, SEC-024, SEC-025, SEC-026, SEC-027, SEC-031** — cleanup as
    you go.

---

## 6. What this audit recognises as well done

A report that enumerates only defects gives a false picture of the baseline, and
it makes the next arbitration harder. The following points are above what one
usually sees at this stage, and **must be preserved** during the fixes above:

- **The composite FKs** `(household_id, x_id)` on every intra-household reference.
  It is cheap, it is enforced by the database, and it eliminates a whole class of
  leaks through identifier confusion. On `llm_purpose_binding`, it is explicitly a
  security control, and it is correct.
- **`api_key_ciphertext` declared `deferred=True`**, with a `COMMENT ON COLUMN` and
  a `CHECK` constraint for the consistency of the secret triplet. Three mechanisms
  that act on the developer who has read nothing — that is the right level of
  paranoia, and the model to generalise (see SEC-003).
- **`ck_llm_provider_config_mode_requirements`**, which makes the rule "the
  instance's key is never copied into the database" verifiable *by the database
  itself*.
- **Authenticated encryption with AAD** `(household_id, config_id)`: a ciphertext
  copied onto another row does not decrypt. Few projects at this stage think of it.
- **Container hardening**: rootless, fixed UID, `NoNewPrivileges`,
  `DropCapability=ALL`, `ReadOnly=true`, database never published, API on
  loopback.
- **The handling of SELinux**: `:Z` with its justification, explicit ban on
  `setenforce 0`, the `:U` pitfall documented with its remedy. This is operations
  documentation of rare quality.
- **The secret procedures**: masked entry, stdin, final `unset`, and the newline
  pitfall documented.
- **CI avoids `pull_request_target`**, restricts the token's permissions, uses
  `--locked` / `UV_FROZEN`, tests against a real PostgreSQL, and runs `gitleaks`
  over the full history.
- **`SECURITY.md`**: scope by surface, explicit out-of-scope, timelines announced
  as best-effort commitments rather than service levels, and the instruction never
  to include a real secret in a report. Declaring **design defects** to be in scope
  there is exactly the right posture at this stage of the project.
- **`CONTRIBUTING.md` §6**, several of whose grounds for rejection are directly
  security controls: no `household_id` removed, no tenant derived from a client
  input, no weakening of the SSRF allowlist. A written convention does not replace
  an engine-level control (SEC-001), but it is better than its absence.
- **Mandatory human review before any write to stock**, presented not as a
  protection against hallucinations but as the product itself. It is the project's
  most solid control against prompt injection, and it holds because it is aligned
  with the user's interest rather than against it.
