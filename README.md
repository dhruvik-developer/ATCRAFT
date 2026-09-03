# Atcraft Manufacturing ERP

Manufacturing ERP for architectural hardware: raw-material planning, multi-level
BOM, inventory with reservation and allocation, MRP, procurement, production,
assembly, job work, quality control, packing, dispatch and finance — on one
auditable ledger.

Built to the locked baseline in
[`ATCRAFT_MANUFACTURING_ERP_CLAUDE_MASTER.md`](./ATCRAFT_MANUFACTURING_ERP_CLAUDE_MASTER.md).
That document is the source of truth; this README tells you how to run the thing.

---

## Stack

| Layer | Choice |
|---|---|
| Frontend | React 18 · TypeScript · Vite · TanStack Query/Table · Tailwind · React Hook Form + Zod |
| Backend | Python 3.11 · FastAPI · Pydantic v2 · SQLAlchemy 2.x (async) · asyncpg · Alembic |
| Database | PostgreSQL 18 — the single source of truth |
| Cache / queue | Redis |
| Background jobs | Celery + Redis |
| Reverse proxy | Nginx |
| Containers | Docker + Docker Compose |
| Architecture | Modular monolith |

---

## Quick start

### Prerequisites

Docker Desktop, Node 20+, Python 3.11+, Git.

### 1. Configure

```bash
cp .env.example .env
```

Then set a real signing key — the API refuses to start in production without one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Paste the result into `JWT_SECRET`.

### 2. Hybrid development — the daily driver

Infrastructure in Docker, application code on your machine with hot reload.

**Windows**

```powershell
.\make.ps1 install     # one-off: create .venv, install backend + frontend deps
.\make.ps1 dev         # starts Postgres + Redis in Docker
# then, in separate terminals:
.\make.ps1 dev-api     # FastAPI on :8000
.\make.ps1 dev-web     # Vite on :5173
.\make.ps1 dev-worker  # optional: Celery worker
```

**macOS / Linux**

```bash
make install
make dev               # infra + API + web together
```

Apply the schema and load baseline data:

```bash
.\make.ps1 migrate     # or: make migrate
.\make.ps1 seed        # or: make seed
```

Optionally, fill the database with a working business so every screen has
something on it — items with stock, orders in flight, production part-finished,
invoices part-paid:

```bash
.\make.ps1 demo-data   # or: make demo-data
```

`demo-data` is for development and evaluation, never for a real deployment. It
creates every row through the module's own service rather than by `INSERT`, so
the ledger, balances, state machines and audit trail all agree; every record
carries a `DM-` code and the whole thing is idempotent, so it can be re-run and
will only fill in what is missing. `--only masters,engineering` runs a subset.

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Readiness | http://localhost:8000/health/ready |
| Metrics | http://localhost:8000/metrics |

### 3. Full Docker development

Everything containerised, reachable through Nginx on port 80. Use this for
onboarding, reproducing environment-specific bugs, and validating the Docker
configuration itself.

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# or:  make dev-docker  /  .\make.ps1 dev-docker
```

### 4. Production

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Work through [`docs/deployment.md`](./docs/deployment.md) first — TLS
certificates, secrets, backups and the pre-flight checklist.

---

## Repository layout

```
apps/
  api/                  FastAPI modular monolith
    src/core/           config, database, security, permissions, errors,
                        responses, pagination, cache, logging, deps, middleware
    src/common/         base repository, document numbering
    src/modules/<name>/ models · schemas · repository · service · router
    src/workers/        Celery app and tasks
    migrations/         Alembic
    tests/              unit · integration · api
  web/                  React + TypeScript + Vite
    src/app/            shell, routing, navigation
    src/components/     design system and shared components
    src/features/<name>/ one folder per business area
    src/lib/            API client, query client, formatting, permissions
infrastructure/
  docker/               Postgres init
  nginx/                edge configuration (prod + dev)
docs/                   architecture · database · api · security · performance
tests/e2e/              Playwright critical journeys
tests/load/             Locust profiles
storage/                runtime file storage (git-ignored)
```

Each backend module is self-contained so it could be extracted later if real
scale demands it — but the system starts, and should stay, a modular monolith.

---

## Commands

Every target exists in both `Makefile` and `make.ps1`.

| Command | What it does |
|---|---|
| `install` | Create the backend venv and install both dependency sets |
| `dev` | Hybrid mode: infra in Docker, app local with hot reload |
| `dev-docker` | Full containerised development stack |
| `up` / `down` / `restart` / `logs` / `ps` | Compose stack control |
| `migrate` / `migration "msg"` / `migrate-down` | Alembic |
| `seed` | Baseline roles, permissions, UoM, warehouse types |
| `test` / `test-unit` / `test-integration` / `test-cov` | Backend tests |
| `test-web` / `test-e2e` / `test-load` | Frontend, Playwright, Locust |
| `lint` / `format` / `typecheck` / `check` | Quality gates |
| `backup` | `pg_dump` to `./backups` |
| `shell-api` / `shell-db` | Shells inside the containers |

---

## Architecture in one page

```
                          Users
                            |
                          Nginx  (TLS, rate limit, security headers)
                            |
              +-------------+-------------+
              |                           |
        React + Vite                  FastAPI
                                          |
                       +------------------+------------------+
                       |                  |                  |
                  PostgreSQL            Redis             Celery
                 source of truth     cache + queue        workers
                       |                                     |
                       +------------------+------------------+
                                          |
                                    File storage
```

Read next:

- [`docs/architecture.md`](./docs/architecture.md) — layering, module boundaries, request lifecycle
- [`docs/database.md`](./docs/database.md) — schema and cross-module service contract
- [`docs/api.md`](./docs/api.md) — conventions, envelope, pagination, errors
- [`docs/security.md`](./docs/security.md) — authn, authz, tenancy, files, secrets
- [`docs/performance.md`](./docs/performance.md) — indexing, caching, background work
- [`docs/deployment.md`](./docs/deployment.md) — production checklist and runbook

---

## The rules that are not negotiable

These come straight from the master document and are enforced in code review:

1. **PostgreSQL is the source of truth.** Redis is a cache; every entry has a TTL.
2. **Inventory correctness beats everything.** Stock changes take a row lock,
   re-read after acquiring it, write ledger + balance + audit in one transaction.
3. **The ledger is append-only.** Current stock is never derived by summing
   history on a request path, and history is never rewritten.
4. **Nothing returns an unbounded result set.** Pagination is not optional, and
   append-heavy tables use keyset cursors.
5. **Authorisation is server-side.** Frontend permissions control affordances
   only. `company_id` always comes from the verified token, never the request.
6. **Money is `Decimal`/`NUMERIC`.** A float in a monetary path is a defect.
7. **Audit is written in the same transaction as the change it describes.**
8. **Status changes go through an explicit state machine.** Invalid transitions
   are rejected by the backend.
9. **Long work goes to Celery.** No HTTP request is held open for a calculation.
10. **Schema changes happen through Alembic.** Never by hand, never in production.

---

## Testing

```bash
make test-unit          # fast, no database
make test-integration   # needs Postgres + Redis
make test-e2e           # Playwright, needs a running stack
make test-load          # Locust
```

Integration tests expect the development infrastructure to be up
(`make dev` or `make up`). The concurrency tests are the important ones: they
assert that two simultaneous reservations against the same stock cannot both
succeed.

---

## Contributing

Branches: `main`, `develop`, `feature/*`, `bugfix/*`, `hotfix/*`.

Before opening a pull request:

```bash
make check              # lint + typecheck + tests
```

A feature is done when the business rules, migration, authorisation, validation,
audit, error handling, tests, pagination and documentation are all in place —
not when the screen renders. The full definition is section 90 of the master
document.
