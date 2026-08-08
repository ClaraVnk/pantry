# Testing strategy

A scoping document. It describes what we test, at which level, and above all what we
do not test. The structuring decisions that constrain it are in the ADRs:
[0003](adr/0003-backend-stack.md) (PostgreSQL everywhere, never SQLite),
[0005](adr/0005-llm-provider-abstraction.md) (the adapter conformance suite is the
condition of the decision) and [0006](adr/0006-multi-tenant-from-day-one.md)
(isolation tests mandatory per resource).

The how-to — commands, Podman prerequisites, adding an adapter to the harness — is in
[`backend/tests/README.md`](../backend/tests/README.md), as is everything intended
for a contributor.

All technical identifiers quoted here are in English, in line with the project
convention.

---

## 1. Current state and purpose of this document

The project is at the scoping stage: documentation, ADRs, data schema. **There is no
feature code**, hence no feature to test. This document and the harness that goes
with it exist so that the first code written lands in a test environment that is
already ready — the only moment when a testing strategy is cheap, because it has no
existing code to catch up on.

What is already in place and runnable:

- the database fixtures (ephemeral PostgreSQL via Podman, transactional session
  rolled back, household and user factories);
- the multi-tenant isolation guards at schema level, which today pass on the 17
  declared tables;
- the LLM adapter conformance harness, parameterised over the five providers,
  entirely in `skip` as long as no adapter exists.

What is in `skip` is there with a legible reason, never with a silent `xfail`. **A
failing test is a signal; a test that passes without checking anything is a lie; a
motivated `skip` is a backlog item.** All three can be read in the output of
`pytest -ra`, and that is the only reason `skip`s are acceptable here.

---

## 2. The pyramid adopted

No dogmatic proportions: the shape follows from the nature of the system. Chaudron is
an application whose risk is essentially concentrated on three points — the quantity
rules, isolation between households, and behaviour in the face of heterogeneous model
providers. That is where the effort goes.

| Level | What is tested there | Cost | Expected volume |
|---|---|---|---|
| **Domain** (pure) | Unit conversions, FEFO allocation, lot merging, expiry computation, ingredient availability rules | µs, no I/O | The largest contingent |
| **Schema** (metadata) | Presence of the tenant, composite constraints, scoped uniqueness | ms, no I/O | A handful, but parameterised over **every** table |
| **Services + repositories** (real PostgreSQL) | Complete use cases, transactions, scoped queries, migrations | ~100 ms | One per use case, plus the error cases |
| **Adapter contract** (test doubles) | The five LLM adapters against the same contract | ms | 1 suite × 5 providers |
| **API** (in-process ASGI) | Status codes, input validation, authorisation, serialisation | ~100 ms | One nominal path and the refusals, per resource |
| **End to end** (browser) | Barcode scanning and receipt review, nothing else | seconds | A handful, never more |

### What we do not test

Just as important as the rest, and deliberately explicit so that nobody has to
re-decide it in review:

- **FastAPI, SQLAlchemy, Pydantic.** We do not test that Pydantic validation rejects
  an integer where a string is expected, nor that SQLAlchemy knows how to do an
  `INSERT`. Testing a third-party library produces tests that break on every version
  bump without ever having found anything.
- **Getters, DTOs, schemas with no logic.** Their coverage is obtained for free by
  the tests of the layers that use them.
- **HTML/CSS rendering.** End-to-end tests check that a journey works, not that a
  button is blue.
- **The intrinsic quality of model outputs in PR CI.** "Is the suggested recipe any
  good" is neither deterministic nor free: see §8.
- **The providers' SDKs.** We test our translation of their errors, never their
  behaviour. What *must* be checked against the real providers is framed in §5.4.
- **The EAN decoding pipeline in the browser.** It is a third-party library fed by a
  camera; we test what we do with the 13-character string it produces.
- **Combinations for the sake of combinations.** Five adapters × each feature × each
  capability is an explosive matrix (ADR-0005 says so: that is the real price of the
  decision). We cover it with a *single parameterised contract*, not with N
  copy-pasted suites.

### What does not exist here

**No unit tests against a database double.** There is no SQLite mode, no in-memory
repository that "pretends". A query that has not been executed by PostgreSQL has not
been tested: partial indexes, deferred constraints, `jsonb`, `numeric` and
transactional behaviour are precisely what breaks on deployment (ADR-0003). The test
that enforces this rule is `tests/test_database_harness.py`: it checks the dialect
and the engine version, and fails if a substitute engine reappears on a Friday
evening.

---

## 3. Testing each layer without betraying the dependency rule

The rule `api → services → domain ← infra` is not a figure of speech: it is what
makes the domain testable without a database or a network. It is verified, not
assumed.

### 3.1 The domain is tested with nothing

A domain test is entitled to request **no fixture at all**. No `db_session`, no HTTP
client, no system clock. Concretely:

- Entities and rules are pure functions and objects; external dependencies are
  *ports* (interfaces) declared by the domain and passed as parameters.
- Time is a port like any other. An expiry rule that calls `datetime.now()` is not
  testable: it gives a different result tomorrow. The reference instant is passed in,
  and the linter already forbids naive datetimes (`DTZ`).
- The test doubles are hand-written in-memory implementations (`FakeRecipeSuggester`,
  `FakeClock`), not mocks with call assertions. A mock that checks "the method was
  called with these arguments" tests the current implementation, and breaks on the
  first refactor that changes nothing for the user.

**How we make sure of it concretely** — three nets, from weakest to strongest:

1. `ban-relative-imports` and `known-first-party` are already configured in `ruff`;
2. an architecture test (to be written as soon as `domain/` contains logic) that
   walks the modules of `chaudron.domain` and fails if one of them imports
   `sqlalchemy`, `fastapi`, `httpx` or a provider SDK. That is ten lines of `ast`,
   and it is the only way to make the rule enforceable rather than declarative;
3. slowness itself: `pytest -m "not integration"` must stay under one second. The day
   it drifts, a dependency has crossed a boundary.

### 3.2 Services are tested against a real database

A service orchestrates: it opens a transaction, calls repositories, applies a domain
rule, writes. Testing it on a simulated database proves nothing about what matters —
atomicity, scoping, uniqueness conflicts. It is therefore tested with `db_session`,
injecting test doubles **only** for what is outside the machine: the model provider,
Open Food Facts, the clock.

### 3.3 Infrastructure is tested by its contract

An adapter has no tests "of its own". It has a contract, common to every
implementation of the same port, and it either passes it or it does not (§5). That is
what keeps the cost of adding a provider bounded.

For the SQLAlchemy repositories, the contract is the real database; for outbound HTTP
clients (Open Food Facts), it is an `httpx.MockTransport` fed with **recorded**
responses, never hand-written ones.

### 3.4 The API is tested in-process

An ASGI client, with no server and no open port. What is checked there is what exists
only at that level: the status code, input validation at the boundaries, tenant
resolution from the authentication context, and the shape of the response. Not the
business rule, which has already been tested where it lives.

The `api_client` fixture already exists and **skips**: there is no application
factory, no session dependency to override, and no authentication context from which
to derive a `HouseholdScope`. Its docstring lists all three. Guessing at any one of
them would produce a fixture that tests something other than the application.

---

## 4. Multi-tenant isolation: a mechanism, not one-off tests

This is the most serious regression (one household reads another's stock — the
complete inventory of a home is the most sensitive data in the database) and the
easiest to introduce (a `select(Item)` with no `where` clause compiles, passes type
checking, and works perfectly in single-household development).

One-off tests are not enough: they cover only the resources somebody thought of, and
the leak always comes from the table nobody thought of. Four levels, three of which
are systematic by construction.

### 4.1 Schema guard — automatic, without a database, over every table

`backend/tests/tenancy/test_schema_tenant_guard.py` reads `Base.metadata` and checks,
table by table, index by index:

| Guard | What it catches |
|---|---|
| Every business table carries `household_id` | The table added next month with no tenant column |
| `household_id` is not null | A row belonging to nobody, invisible rather than shared |
| Every uniqueness constraint is scoped | `UNIQUE (barcode)`: prevents the second household from recording its row, **and** confirms to it that another household already holds that value |
| Every reference to a tenant table is composite | Otherwise a guessed identifier is enough to write into another household's data, without any application bug being needed |

The mechanism is parameterised over `metadata.sorted_tables`: there is nothing to
remember to add. The exceptions are named lists, each carrying its reason
(`GLOBAL_TABLES`, `NULLABLE_TENANT_TABLES`, `UNIQUE_CONSTRAINT_EXEMPTIONS`,
`SIMPLE_FOREIGN_KEY_EXEMPTIONS`). Adding an entry to one is a deliberate act, visible
in review — which is exactly the intended effect.

`SIMPLE_FOREIGN_KEY_EXEMPTIONS` is a **ratchet**, not an absolution: it freezes the
nine non-composite references that exist today (including the known hole on
`product`, documented in `data-model.md` §5.2) and prevents the tenth from arriving
unnoticed. It is meant to shrink. An entry that has become unnecessary triggers a
warning, not a failure: making the commit that *fixes* something fail is the surest
way to get the guard deleted.

### 4.2 Seeding fixture — the second tenant is never forgotten

`tenant_pair` provides two complete and unrelated households, each with its owner. An
isolation test that builds its own second household regularly ends up not building it
at all, and then proves that a household cannot read its own data from a household
that does not exist.

### 4.3 Per-resource isolation test — mandatory, review rejected otherwise

For each exposed resource: household A operates on household B's identifiers and
receives `404`. **Never `403`**, which would confirm the existence of the resource
and turn the API into an enumeration oracle. Applies to reads as well as writes
(ADR-0006).

These tests should be generated from a table of resources rather than written one by
one: as soon as three resources exist, a parameterisation
`(method, URL template, factory)` automatically covers each new entry, and an
oversight becomes a missing addition to a single table — visible — rather than a test
file that was never written.

### 4.4 Background jobs, a blind spot to be handled separately

Receipt parsing and notifications run **outside the HTTP request**, hence outside the
application scope resolved at the boundary. They are the ones that will leak first
(`data-model.md` §5.4). Every background task has its own isolation test: it loads
the household from the row being processed, never from an ambient context, and the
test proves it by processing a row of household B while a context of household A is
active.

### 4.5 What this mechanism does not prove

That the filtering is applied by the database. It is not, yet: RLS is deferred and
its trigger is explicit (ADR-0006 — the day an account is created by someone outside
the family circle). As long as it is deferred, isolation rests on an application
convention, and these tests are what holds it. On the day RLS is switched on, they
become the net that proves the policies break nothing — their value increases, it
does not disappear.

---

## 5. LLM adapter conformance

ADR-0005 accepts five adapters on the strength of one guarantee: *"adding a provider
means writing an adapter and making this suite pass"*. Without it, the ADR says so
itself, five adapters would be reckless for a solo project.
`backend/tests/contracts/test_llm_provider_contract.py` is that suite.

### 5.1 The contract

A conformant adapter honours four things, and the suite checks all four for each of
the five providers:

1. **Port signatures.** The implementation is substitutable for `RecipeSuggester` or
   `ReceiptExtractor`: the domain never knows the concrete implementation, and the
   method really is a coroutine (the whole stack is asynchronous).
2. **Error translation.** Each provider failure mode becomes the corresponding domain
   exception — `ProviderUnavailable` (connection refused, timeout, 5xx),
   `ProviderQuotaExceeded` (rate limit, quota exhausted), `ProviderResponseInvalid`
   (malformed payload, schema violated). The decisive assertion is not the type
   raised but **its module**: an exception whose module does not begin with
   `chaudron.` has crossed the boundary, and that is the stray
   `except AnthropicError` in a route that the ADR seeks to avoid. We also check that
   the message is not empty: support diagnosis needs the provider, the model and the
   failure mode.
3. **Well-formed capability declaration.** One boolean per capability
   (`structured_output`, `vision`, `prompt_caching`, `long_context`), a provenance of
   `static` or `probed`, and a probe date with a timezone if — and only if — the
   provenance is `probed`. Provenance is carried by the value because only a probed
   capability can go stale: the interface must be able to offer a refresh, which
   makes no sense for a static capability.
4. **Conformance to the degradation taxonomy.** For each capability declared absent,
   the adapter declares **exactly one** of the ADR's three cases, and the suite checks
   that the behaviour matches:
   - `unavailable` → the call fails with a **domain** error carrying a legible
     reason. Never a raw SDK error, never a JSON invented from an image no model has
     seen.
   - `emulated` → the call succeeds and returns a valid domain object; the loss is in
     the failure rate, not in the type.
   - `degraded` → the call succeeds **and says what it left out**, otherwise the
     persistent degraded-mode indicator has nothing to display.

   The absence of a declaration is a failure in itself: the goal is not that a
   strategy exists, it is that one was *chosen*. A missing capability with no declared
   case means the behaviour is whatever the code does by accident — precisely what the
   taxonomy exists to prevent.

A fifth guard covers completeness: the suite fails if the registry does not contain
the five keys of ADR-0005. Removing an adapter is an ADR revision, not a line quietly
deleted from a dictionary.

### 5.2 How the suite is parameterised

Two crossed parameterisation axes, and one dynamic discovery:

- a `provider_key` fixture parameterised over the five keys
  (`anthropic`, `openai`, `gemini`, `mistral`, `ollama`);
- an `adapter` fixture that resolves the key in the registry
  `chaudron.infra.llm.contract:CONTRACT_ADAPTERS`;
- parameterisations per port, per failure scenario and per capability.

Today the registry does not exist: the 145 cases are collected and **skipped** with
the reason. Each adapter added to the registry activates its column of the matrix,
without a single line of the test file being modified. An adapter that is registered
but does not respect the expected shape **fails** instead of being skipped: it chose
to enter the suite, it honours its contract.

The registry is discovered by structural import: the infrastructure never imports the
test package. That is the constraint that keeps the dependency rule intact right into
the tooling.

### 5.3 Without calling a real API, without spending money

The whole suite runs on **test doubles**. No network request, no credentials, no
cost, and a deterministic result.

The important point is *where* the double lives: **with the adapter, not in the
harness**. Only the Anthropic adapter knows what an Anthropic rate limit looks like;
only the Ollama adapter knows what an unreachable instance looks like. A harness that
manufactured the responses itself would be testing its own idea of the five
providers. The harness names *scenarios* (`rate_limited`, `malformed_payload`,
`missing_vision`…) and the adapter supplies, for each one, a transport that replays
it.

These doubles are built from **recorded responses** of the real providers
(`tests/contracts/recordings/<provider>/`), not hand-written: a double invented from
the documentation tests your reading of the documentation. The recordings are
scrubbed of every credential before being committed — API key, token, organisation
identifier, request headers. A recording is a committed artefact: it goes through the
same review and the same secret scan as everything else.

### 5.4 What must nevertheless be checked for real

The doubles rest on an assumption that decays: that the provider's behaviour has not
changed. Yet SDKs publish breaking versions, models are deprecated, error payloads
are reworked. ADR-0005 lists it as an accepted negative consequence: *five SDKs to
follow, and it has to be detected before the user does*. Nothing but real calls
detects it.

| | What is checked | When | Where |
|---|---|---|---|
| **Real smoke test** | The key authenticates, the model answers, the structured output validates against the schema, tokens and cost are reported | Nightly, on `main` | Dedicated job, outside PR CI |
| **Recording fidelity** | The real error shapes still match the replayed recordings (refreshing the captures) | Weekly | Dedicated job, with human review of the diff |
| **Ollama probe** | A real instance declares its capabilities as expected | Nightly, local CI instance | Dedicated job |
| **Extraction quality** | The receipt evaluation set (§8) | Weekly, and before any prompt change | Dedicated job |

Rules for this perimeter, non-negotiable:

- **Never on a pull request.** An external contributor has no keys, and a CI that
  spends money on every push is a CI that ends up being bypassed. It would also be
  non-deterministic: a red test because a provider is having an incident teaches you
  something about the provider, nothing about the code.
- **A `live_provider` marker, disabled by default**, with an explicit opt-in via an
  environment variable and one key per provider read from the environment.
- **A spending cap** and the cheapest models of each provider: these tests check a
  protocol, not a quality.
- **A failure notifies, it does not block a deployment.** That distinction is what
  keeps the signal credible.

---

## 6. Test data and fixtures

**Factories, not shared datasets.** Each test builds what it needs via
`make_household`, `make_user`, `make_member`, all arguments optional with unique
default values. A common data file loaded for the whole suite creates invisible
coupling: a test ends up depending on a row that another test introduced for an
unrelated reason, and nobody dares touch it any more.

**Isolation by transaction, not by clean-up.** `db_session` opens a transaction on
the connection and attaches the session to it with
`join_transaction_mode="create_savepoint"`: the code under test can call `commit()`
freely, and the final rollback erases everything. No `TRUNCATE` between tests, no
recreated schema, no significant execution order — and
`test_previous_test_left_nothing_behind` fails the day this mechanism stops working.

**Global reference tables.** `unit` and `llm_provider` are outside the tenant and
populated by migration in production. Once a seed migration exists, the tests will
have to replay it rather than re-insert those rows by hand: a test seed that diverges
from the production seed is a permanent false positive.

**Two boundary values to wire in as soon as they make sense**, because they are the
domain's known traps (`data-model.md` §6): a quantity that must remain exact in
decimal (never a float), and a calendar expiry date in a household whose timezone is
not the server's.

**The test schema is temporary.** It is currently created by `metadata.create_all`,
for want of an Alembic revision. As soon as `migrations/versions` has content, the
fixture will have to run the migrations: otherwise the suite validates a schema that
no environment applies, and a broken migration reaches production with a green CI.

---

## 7. Coverage

**Threshold: 85% of lines and branches on `domain/` and `services/`, 70% overall.**
To be enabled in `[tool.coverage.report]` (`fail_under`) at the first implemented use
case — enabling it now, on a repository with no application code, would measure the
void and give a reassuring figure that means nothing. The measurement itself is
already in place (`--cov`, `--cov-branch`), so that the curve exists from the first
commit.

The thresholds are differentiated because the layers do not carry the same risk: the
domain concentrates the rules and has no coverage excuse; the API is mostly
declaration; the infrastructure is covered by the contracts, not by the line.

### What coverage does not tell you

- **That an executed line was verified.** A test that calls a function without
  asserting anything about its result produces exactly the same coverage as a good
  test. That is the metric's main weakness, and it is structural.
- **That the right cases were chosen.** 100% coverage of a unit conversion tested
  only on kilograms says nothing about millilitres, pieces, or the cross-dimension
  conversion that is the domain's real trap.
- **That the error paths are correct.** A covered `except` branch proves it was
  taken, not that the error raised is usable by the caller or legible to the user.
- **That isolation between households holds.** A query with no tenant filter is 100%
  covered by the test that uses it from a single household. That is exactly the
  reason §4 exists: coverage is blind to this class of defect.
- **That the system works.** Every unit can be covered and the system unusable,
  because the defect is in the assembly.

Consequently: the threshold is a floor that prevents drift, never a target. A module
below the threshold triggers a question, not a test written for the bar.

---

## 8. Non-deterministic paths

A language model returns a different answer on every call, and that answer is not
ours. The strategy fits in one sentence: **isolate the non-determinism in a segment as
thin as possible, and test everything else normally.**

### 8.1 Cutting the path in three

The "inventory → suggestions" flow decomposes into three segments, two of which are
perfectly deterministic:

1. **Building the request** — serialising the stock, assembling the prompt, placing
   the cut point for the cache. Deterministic: a reference (*golden*) test on the
   exact output. These tests catch the real risk of this segment — a prompt change
   that shifts the stable prefix and loses the cache, which multiplies the cost
   without changing a single visible result.
2. **The call** — non-deterministic, and the only segment concerned. Test doubles
   everywhere (§5.3), the real provider in a dedicated job (§5.4).
3. **Validation and integration** — the model's output is treated **as hostile
   input**, on the same footing as a form posted by a stranger (architecture §5).
   Deterministic: we test the validation with malformed outputs — truncated JSON,
   missing field, negative quantity, unknown unit, invented currency, an apology text
   instead of the JSON, prose wrapping the JSON, a numeric value as a string. This
   segment is the one that protects the user, and it is the most testable of the
   three.

### 8.2 Never assert on the text, always on the invariants

Where a real model output is at stake, the assertions bear on properties that must
hold whatever the answer: the schema validates, quantities are strictly positive,
units belong to the reference table, ingredients reference existing stock, the
currency is three uppercase letters, no field is an empty string. Never "the answer
contains the word *poêle*".

Zero temperature and a fixed seed are used when the provider offers them, but **never
as the basis of an assertion**: they reduce variance, they do not remove it, and two
of the five providers guarantee nothing on the subject.

### 8.3 Evaluation set, separate from the test suite

Receipt extraction quality is a **measurement**, not an assertion. A set of about
thirty photos of real receipts (anonymised: no name, no card number, no address) with
the expected line items, scored on recall and precision over the labels and the
quantities. We track the curve between two prompt versions or two models, and
compare; we do not fail a PR because a model read "PDT NOUV 1KG" differently from
last week.

This set also serves as a product safeguard: it is what says whether the review
screen is still necessary — and the answer is yes, which the human correction rate
measured in production (architecture §7) will confirm or not.

### 8.4 What human review changes for the tests

Nothing enters stock without human review (architecture §3.2). That is what shifts
the risk: a model that gets it wrong produces a line to correct, not a false stock.
The tests must therefore bear first and foremost on **the review path** — that an
unreviewed line can never reach stock, that a correction is retained, that a refusal
writes nothing. That is deterministic, it is critical, and it depends on no model.

A special case: **allergens**. Allergen information coming from a model is never
presented as authoritative (architecture §6). A dedicated test must check that any
allergen data of model origin carries its provenance and its warning all the way to
the API output — an error here has physical consequences.

---

## 9. Execution and continuous integration

The existing CI (`.github/workflows/ci.yml`) chains lint → format → mypy → pytest on
a `postgres:16` service → Podman image → dependency audit and secret scan. The
strategy slots into it without modifying it: `CHAUDRON_DATABASE_URL` is already set
by the test job, and the fixtures prefer it to starting any container.

Locally, no variable is needed: an ephemeral PostgreSQL 16 is started via
testcontainers on the rootless Podman socket. Two traps of this combination are
handled in `conftest.py` and documented in `tests/README.md` — Ryuk, the clean-up
container that does not start under rootless and fails the session with a message
that blames PostgreSQL; and the `testcontainers.postgres` module, a deprecated
adapter whose wait strategy probes the database from the host with a synchronous
driver that is absent, and reports the lack as a connection refusal from Podman.

Expected separation of jobs as the project grows:

| Job | Content | Trigger |
|---|---|---|
| PR | Lint, types, deterministic tests (database included), contracts on doubles | Every push |
| Nightly | Real provider smoke tests, Ollama probe | `main`, scheduled |
| Weekly | Recording fidelity, evaluation set | Scheduled |

A test with an unbounded duration or one depending on the public network never enters
the PR job. A slow or flaky feedback loop ends up bypassed, and a bypassed CI no
longer protects anything.

---

## 10. What remains open

- The architecture test that forbids infrastructure imports in `domain/` (§3.1): to
  be written as soon as `domain/` contains logic.
- Moving from `metadata.create_all` to Alembic migrations in the fixtures (§6).
- Enabling `fail_under` (§7), at the first implemented use case.
- Parameterising the isolation tests from a resource table (§4.3), from the third
  exposed resource onwards.
- The end-to-end tooling on the PWA side — outside the scope of this document, which
  covers only the backend.
- The authentication strategy is not settled (architecture §8); the authorisation
  tests are waiting on that decision, and the `api_client` fixture with them.
