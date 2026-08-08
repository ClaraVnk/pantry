# 0003. Backend stack: FastAPI, SQLAlchemy 2.x, PostgreSQL, uv

## Status

Accepted — 2026-08-03

## Context

Chaudron exposes an API consumed by a separate React PWA (see ADR-0004), and has to handle heterogeneous workloads: short CRUD requests on the stock, slow and unpredictable outbound calls to language models (see ADR-0005), and calls to Open Food Facts for EAN resolution. Those outbound calls dominate response time and are mostly network wait.

The developer works alone and knows Python. The target deployment is a Rocky Linux 10 VPS in SELinux Enforcing, via systemd quadlets and Podman containers.

The data model is multi-tenant from the start (see ADR-0006), with a `household_id` carried by every business table — which demands an ORM that can express composite constraints and partial indexes cleanly.

## Decision

**Runtime**: Python 3.14, the latest stable version, pinned in `pyproject.toml` (`requires-python = ">=3.14"`) and `.python-version`.

**HTTP framework**: FastAPI. Native ASGI, hence concurrent I/O without a thread pool on outbound calls; input validation by Pydantic at the boundaries; OpenAPI generated from the types, which gives the frontend a usable contract with no hand-maintained documentation.

**Persistence**: SQLAlchemy 2.x in typed declarative style (`Mapped[...]`, `mapped_column`), with `asyncpg` as the driver. Migrations by Alembic, each migration providing both `upgrade` and `downgrade`.

**Database**: PostgreSQL 16, in a dedicated container, named volume, password as a Podman secret, volume mounted with the `:Z` suffix under SELinux.

**Tooling**: `uv` for the toolchain and dependencies (`uv.lock` versioned); `ruff` for lint and format; `mypy` in strict mode; `pytest` with `pytest-cov`.

**Tests**: PostgreSQL is the only engine exercised, locally and in CI alike. CI starts a dedicated `postgres:16` service. There is no SQLite mode, not even for unit tests.

## Consequences

### Positive

- A single type chain, from the HTTP request body down to the columns: Pydantic at the boundaries, `Mapped[...]` in the database, `mypy --strict` in between.
- ASGI absorbs LLM call latency without multiplying workers: a handler blocked on a model response does not block stock requests.
- PostgreSQL brings the types the domain needs: `timestamptz` (expiry dates with time zone), `numeric` (quantities), indexable `jsonb` (raw payload of a parsed receipt), composite unique constraints on `(household_id, ...)`.
- A versioned `uv.lock` makes builds reproducible; `uv sync --frozen` in CI guarantees that the production container installs exactly what was tested.
- Testing against the production engine removes a whole class of bugs that only surface at deployment.

### Negative

- **Async code spreads.** An `async` function can only be called cleanly from an `async` context: a well-chosen synchronous library will have to be wrapped in a thread, or replaced. That is a structural cost, not an occasional annoyance.
- Async SQLAlchemy sessions are a silent N+1 trap: lazy loading raises in an async context, which forces every `selectinload` to be written explicitly. Better for performance, more verbose to write.
- The test loop is heavier: every run assumes a reachable PostgreSQL. No `pytest` on a bare machine with no container running.
- `mypy --strict` over async SQLAlchemy code costs annotations and the occasional `cast()`; the friction is real on complex queries.
- Python 3.14 is recent: some dependencies may not ship a prebuilt wheel, which lengthens builds or forces a compilation.
- Two codebases (backend, frontend) mean two pipelines, two dependency sets, and an OpenAPI contract to keep in sync.

## Rejected alternatives

- **Django + Django REST Framework** — admin included, mature ORM, complete ecosystem, and phase 2's multi-tenant auth would be wired up faster there. Rejected for two reasons: Django's async support remains partial exactly where it counts (the ORM, precisely the path taken by handlers waiting on an LLM), and the monolith encourages putting business logic in the views, which is exactly the boundary we want to hold. We are giving up real comfort here — the Django admin would have covered part of the back office for free.
- **Litestar** — faster than FastAPI on benchmarks, cleaner DI. Rejected: a markedly smaller ecosystem and body of answers, for a gain that never shows on a workload dominated by network wait.
- **Flask + SQLAlchemy** — familiar and minimal. Rejected: synchronous WSGI, validation to wire by hand, OpenAPI to maintain separately.
- **Go or Node** — Go for deployment robustness, Node to share the language with the frontend. Both rejected: the Python ecosystem for multimodal models and image processing is markedly richer, and it is the language the developer knows best.
- **SQLite** — zero operations, a single file, tempting for family use. Explicitly rejected, and this is the most important decision in this ADR:
  - Writes are serialised database-wide. A background job (receipt parsing, LLM call, e-mail import) blocks a user request.
  - There is no real typing: booleans as `0`/`1`, naive timestamps with no time zone, JSON stored as text. An expiry date with no time zone is a bug you discover at the daylight-saving switch.
  - The backup is a file copy, not an archive restorable table by table.
  - Migrating PostgreSQL → SQLite late would have to go through the ORM models, not through a rewritten dump, since the two engines agree on almost no type.
- **SQLite for tests only, PostgreSQL in production** — the common compromise. Rejected: the production engine then never gets tested. The divergences (types, deferred constraints, `jsonb`, partial indexes, transactional behaviour) show up in production, where they cost the most.

## Revisiting

Reassess PostgreSQL 16 against a higher major version once that version is under long-term support and available as a stable official image. Reassess the choice of FastAPI if the project adopts a server-rendered frontend, in which case the split codebases lose their justification.
