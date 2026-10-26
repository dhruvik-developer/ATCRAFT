# Atcraft Manufacturing ERP

Manufacturing ERP for architectural hardware: raw-material planning, multi-level
BOM, inventory with reservation and allocation, MRP, procurement, production,
assembly, job work, quality control, packing, dispatch and finance — on one
auditable ledger.

Repository-wide engineering rules live in [`CLAUDE.md`](./CLAUDE.md); the
architecture and operational decisions are recorded under [`docs/`](./docs).
The shared implementation points and completion checks are mapped in
[`docs/development-standards.md`](./docs/development-standards.md).

The [current scope checkpoint](docs/implementation/production-scope-20261025.md)
records the implemented portals and serial workflows, verification, and remaining
full-scope packages. Deployment still requires a server/domain and the
[go-live checks](docs/deployment.md#go-live-gate); this checkout is not a
production sign-off.

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

Docker Desktop, Node 22.12+, Python 3.11+, Git, and `pre-commit` on your PATH.
Install the Git hook tool before `make install`, for example with
`python -m pip install pre-commit` in your chosen tooling environment.

### 1. Configure

```bash
cp .env.example .env
```

Then set a real signing key — the API refuses to start in production without one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Generate two independent values. Paste one into `JWT_SECRET` and the other into
`CREDENTIAL_ENCRYPTION_KEY`; rotating the JWT signing key must not invalidate
encrypted MFA credentials.

Outbound email stays `EMAIL_PROVIDER=disabled` on a checkout, so a password
reset requested locally is acknowledged but no link is sent. A deployment must
set `EMAIL_PROVIDER=smtp`, the `SMTP_*` values and an HTTPS `PUBLIC_WEB_URL` —
the API refuses to start otherwise (`.env.example` documents each key).

### 2. Hybrid development — the daily driver

Infrastructure in Docker, application code on your machine with hot reload.

**Windows**

```powershell
.\make.ps1 install     # one-off: create .venv, install backend + frontend deps, git hooks
.\make.ps1 dev         # starts Postgres + Redis in Docker
# then, in separate terminals:
.\make.ps1 dev-api     # FastAPI on :8000
.\make.ps1 dev-web     # Vite on :5173
.\make.ps1 dev-worker  # optional: Celery worker
```

**macOS / Linux**

```bash
make install
source apps/api/.venv/bin/activate
make dev               # infra + API + web together
```

`install` runs `scripts.install_requirements` rather than `pip install`
directly: the lock files are resolved on Linux and pin `uvloop`, which has no
Windows build, so the installer drops the pins the platform cannot use and
installs everything else hash-verified.

For a complete local testing dataset, use the single seed entry point:

```powershell
.\make.ps1 seed-local  # macOS/Linux: make seed-local
```

It applies migrations, loads baseline reference data and all demonstration
stages, and generates the demo invoice/credit-note PDFs. It adds missing demo
records without resetting the database and can be run again. The initial
administrator password is generated and displayed once; sign in as
`admin@atcraft.example` and choose your own password. Existing passwords are
preserved. See [local seed data](docs/local-seed-data.md) for configuration,
fixture coverage and custom administrator options.

To load only the baseline instead:

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
make dev-docker        # Windows: .\make.ps1 dev-docker
```

The bare equivalent needs the project name — `docker-compose.yml` refuses to
start without one, so two deployments can never share a project by accident:

```bash
docker compose -p atcraft-erp -f docker-compose.yml -f docker-compose.dev.yml up --build
```

### 4. Production

```bash
./scripts/deploy.sh main init       # first deployment only; then set real hosts
./scripts/deploy.sh main doctor
./scripts/deploy.sh main up         # build, backup, migrate, start, verify
```

Never run production with a bare `docker compose up`. Work through
[`ONE-BOX-RUNBOOK.md`](./ONE-BOX-RUNBOOK.md) first — TLS, secrets, backups,
the two isolated deployments, and the go-live checklist.

---

## Install as an app

Staff, vendors and dealers can install the application from a supported browser
on HTTPS (or localhost for development). Use the browser's **Install app** action;
on iPhone or iPad, use Safari's **Share → Add to Home Screen**, then open the
installed app. Enable notifications from **Profile → This device** or
**Notifications → Preferences** when the deployment has Web Push configured.
Installation does not provide offline access to ERP data.

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

Every development target exists in both `Makefile` and `make.ps1`
(`make <target>` / `.\make.ps1 <target>`). The `deploy-*` targets are
Makefile and Git Bash only: they wrap `scripts/deploy.sh`, which holds the
guards that keep the two deployments apart, and `make.ps1` deliberately does
not duplicate them.

| Command | What it does |
|---|---|
| `install` | Create the backend venv, install both dependency sets, install the git hooks |
| `dev` | Hybrid mode: infra in Docker, app local with hot reload (on Windows, infra only — then `dev-api`, `dev-web`, `dev-worker`) |
| `dev-docker` | Full containerised development stack |
| `up` / `down` / `restart` / `logs` / `ps` | Compose stack control |
| `migrate` / `migration m="msg"` (Windows: `.\make.ps1 migration "msg"`) / `migrate-down` | Alembic |
| `seed` | Baseline roles, permissions, UoM, warehouse types |
| `seed-local` | Migrate a local database, load the complete demo dataset and generate its invoice/credit PDFs |
| `demo-data` | Fill a development database with a working business |
| `db-fresh` / `db-handover` | Destroy and rebuild the dev database with demo data / baseline only |
| `test` / `test-unit` / `test-integration` / `test-cov` | Backend tests |
| `test-web` / `test-e2e` / `test-load` | Frontend, Playwright, Locust |
| `build-web` | Production frontend build, bundle budgets and bundled secret scan |
| `lint` / `format` / `typecheck` | Quality gates |
| `contract` / `contract-check` | Regenerate the OpenAPI contract, the frontend types and the Postman collection / fail if any is stale |
| `postman` | Regenerate only the Postman collection from the committed contract |
| `check` | `contract-check` + `lint` + `test-cov` + `test-web` + `build-web`; application checks shared with CI |
| `hooks` / `hooks-run` | Install the git hooks / run every hook on the whole tree |
| `lock` | Regenerate the hash-locked Python requirements |
| `backup` | `pg_dump` to `./backups` |
| `shell-api` / `shell-db` | Shells inside the containers |
| `deploy-init` / `deploy-doctor` / `deploy-up` / `deploy-down` / `deploy-migrate` / `deploy-health` / `deploy-backup` / `deploy-verify` | `scripts/deploy.sh` wrappers, `D=main` or `D=dev` (Makefile only) |
| `clean` | Remove generated builds, test outputs and caches; preserve data, configuration and dependencies |

Preview cleanup with `python scripts/clean.py --dry-run` (Windows:
`.\make.ps1 clean --dry-run`). Both cleanup entry points use the same bounded
helper. It preserves tracked files, environment files, database backups, runtime
storage, installed dependencies and generated handbook deliverables. Save any
test reports you want to retain before cleaning; run `build-web` to regenerate
the production frontend bundle.

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

These are the repository's core invariants and are enforced in code review:

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

Current development integrates on `develop`; base new `feature/*`, `bugfix/*`
and `hotfix/*` branches there, in this one checkout. Do not create git
worktrees, clones or copied folders of the repository; [`AGENTS.md`](AGENTS.md)
states the rule for coding agents. Deployment names remain `main` and `dev`:
`scripts/deploy.sh` deliberately requires the corresponding `main` or `dev`
branch and its matching remote HEAD. Promoting `develop` into a deployment
branch is a separate release step; passing `dev` selects that deployment.

`make install` installs the git hooks from `.pre-commit-config.yaml`
(`make hooks` on its own does the same). The pre-commit hook formats and lints
what you are committing; the pre-push hook runs the type checks, unit tests,
vitest, the OpenAPI contract check and a Compose config parse. Do not bypass
them with `--no-verify`.

Before opening a pull request:

```bash
make check              # contracts + lint/types + backend coverage + frontend tests + production build
make hooks-run          # every hook on the whole tree, as a push would run it
```

Both exist as `.\make.ps1 check` and `.\make.ps1 hooks-run` on Windows.

A feature is done when the business rules, migration, authorisation, validation,
audit, error handling, tests, pagination and documentation are all in place —
not when the screen renders. The full definition of done is in `CLAUDE.md`.
