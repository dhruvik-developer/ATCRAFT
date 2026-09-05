# CLAUDE.md — working rules for this repository

Read this before changing anything. This file is the repository's authoritative
engineering baseline, together with the focused decisions under `docs/`.

## What this is

A manufacturing ERP for architectural hardware. It is not a CRUD app. The hard
parts are inventory correctness, multi-level BOM with common-part consolidation,
allocation and reallocation, MRP, and traceability across procurement,
production, job work, quality and dispatch.

## Before you write code

1. Read `docs/database.md` — the binding schema and cross-module service contract.
2. Read the module you are about to change. Do not replace working functionality
   to make an implementation simpler.
3. Identify the affected modules, the schema changes, the authorisation
   implications and the performance implications before typing.
4. Write the tests with the feature, not after.

## Layering — never violate this

```
router.py      thin. parse, authorise, delegate, return the envelope.
service.py     business rules, transaction boundaries, audit.
repository.py  queries and persistence, always tenant-scoped.
models.py      SQLAlchemy models, constraints, indexes.
```

Business logic in a route handler is a review rejection. Raw database access in
a route is a review rejection. Database access in a React component is not
possible and must never be simulated by fetching everything and filtering.

## Non-negotiables

- **Tenant scope.** `company_id` comes from `TenantContext`, derived from the
  verified token. Never from a request body, query parameter or header.
- **Inventory.** All stock movement goes through
  `src.modules.inventory.service.post_movement` / `post_movements`. Take the row
  lock, re-read after locking, write ledger + balance + audit in one transaction.
  Never update a balance directly. Never compute current stock by summing the
  ledger on a request path.
- **Reservations.** Go through `src.modules.allocation.service`. Reservations do
  not move on-hand quantity; they change `reserved_quantity`.
- **Money.** `Decimal` and `NUMERIC` only. Use `src.core.types.Money`,
  `UnitPrice`, `Quantity` and the `to_money` / `to_quantity` helpers. A `float`
  anywhere near a monetary value is a defect.
- **Audit.** Every create, update, delete, approval, rejection and status change
  writes an audit row via `record_audit` / `record_status_change` in the same
  transaction. Not a queue publish — the same transaction.
- **State machines.** Status columns are only changed through a `StateMachine`
  with `assert_transition`. No direct assignment.
- **Pagination.** Every list endpoint is paginated. Append-heavy tables
  (inventory ledger, audit log, notifications) use keyset cursors, not OFFSET.
- **No N+1.** No `await` on a database call inside a loop. Batch or join.
  Relationships default to `lazy="raise"` so accidents fail loudly.
- **Migrations.** Alembic only. Never edit a production schema by hand. Review
  every migration for locking impact, data migration, and rollback.
- **Secrets.** Environment variables. Never in source, never in an image, never
  committed.
- **Long work.** Celery. No HTTP request stays open for a long calculation.

## Conventions

| Thing | Convention |
|---|---|
| Primary keys | UUIDv7 via `src.core.ids.new_id` |
| Base classes | `TenantEntity`, `TenantMasterEntity`, `ImmutableLedgerEntity` |
| Codes | unique per company: `UniqueConstraint("company_id", "code")` |
| Document numbers | `src.common.codes.next_document_number` |
| Timestamps | UTC, via `src.core.clock.utcnow()` |
| Errors | raise from `src.core.errors`; never leak SQL or stack traces |
| Responses | `ApiResponse` / `PaginatedResponse` from `src.core.responses` |
| Permissions | `require_permission(Permission.X)` on every route |
| Shared constants | backend `src/core/constants.py` for schema-level decisions (`DEFAULT_CURRENCY`, code widths); frontend `lib/constants.ts` for page sizes, debounce, storage keys, formats and env — the only file that reads `import.meta.env` |
| Shared messages | backend `src/core/messages.py` for any sentence raised from two or more modules; frontend `lib/messages.ts` for the shared error headlines, empty states, prompts and the success builders for the standard verbs (created/updated/approved/...) — a bespoke confirmation that names its own action stays with its screen, and API messages are shown verbatim |
| Frontend data | TanStack Query only; keys from `queryKeys` in `lib/query-client.ts` |
| Frontend lists | `useListQuery` + `DataTable`, server-side everything |
| Lazy pages | `app/route-loaders.ts` is the one list of lazy pages; `lazy()` and the sidebar hover prefetch both read it |

## Assumptions

Business rules that were not fully specified are implemented conservatively,
marked with an `ASSUMPTION:` comment at the point of decision, and listed in
`docs/assumptions.md`. If you find one that is wrong for the business, change it
there — do not scatter a second interpretation elsewhere.

Do not invent a business rule silently. Prefer a named constant or a
configuration value over a hardcoded judgement.

## Traps this codebase has already hit

Each of these cost real debugging time. They are fixed; do not reintroduce them.

- **Server-side `onupdate` expires the attribute.** `updated_at` uses a
  Python-side `onupdate=utcnow`, not `func.now()`. A server-side one forces
  SQLAlchemy to expire the column after every UPDATE, and the next read —
  usually serialising the response — becomes an async lazy load that raises
  `MissingGreenlet`. The same applies to any new server-generated column.
- **Partial unique indexes are checked per statement, not at commit.** When
  promoting a new "one of these per parent" row (preferred vendor, default BOM),
  demote the old one *before* inserting the new one. Insert-then-demote is a
  valid end state that still violates the index mid-transaction.
- **A rolled-back savepoint expires the identity map.** After an expected
  failure inside `transaction(...)`, do not read attributes off an ORM object
  you loaded earlier — hold the id in a plain variable first.
- **Cache switches must cover invalidation.** Anything that talks to Redis
  checks `settings.cache_enabled`, including deletes. Otherwise disabling the
  cache still opens a connection on every write.
- **Association tables with two FKs to the same table need explicit joins.**
  `user_roles` references `users` twice (member, assigner), so `User.roles` and
  `Role.users` spell out `primaryjoin`/`secondaryjoin`.
- **Stock quantities are magnitudes; the transaction type carries the sign.**
  `post_movement(quantity=Decimal("80"), transaction_type=MATERIAL_ISSUE)`
  issues 80. Passing `-80` with an issue type also works (a re-posted ledger row
  stays idempotent), but a negative quantity on a *receipt* type is rejected
  outright — it is always a mis-keyed document. Choose an inflow by naming a
  receipt type, never by flipping the number.
- **Raw `INSERT` in a test bypasses Python-side column defaults.** Many NOT NULL
  columns default in Python, not the database (`warehouses.country`, every
  `items.can_*` flag, `users.is_active`). Build fixture rows through the ORM;
  a hand-maintained column list goes stale the moment a module adds one — or
  **removes** one. `tests/integration/test_quality_service.py` and
  `test_packing_service.py` still hand-write `INSERT INTO items (...)`, and
  dropping `items.valuation_method` broke 43 tests across them, twice: the column
  list and the `VALUES` clause are edited separately, and the same file holds
  several `VALUES` shapes, so fixing the ones you grep for leaves the others.
  Convert them to `tests/seed_support` when you next touch them.
- **Never hand-write another module's table in a test.** A `CREATE TABLE x (...)`
  or a `Table("x", Base.metadata, ...)` in a test file states a row shape its
  module has not defined yet, and collides with the real table the day that
  module lands (`DuplicateTableError: relation "goods_receipts" already exists`).
  `tests/schema_support.prepare_metadata()` imports every module that exists and
  registers a one-column stand-in for whatever foreign keys still dangle — it
  names no tables, so nothing in it can go stale. Same for behaviour: assert a
  missing module's absence by forcing its seam to resolve to nothing
  (`monkeypatch.setattr(service, "resolve_quality_port", lambda: None)`), never
  by asserting that the port really is unresolvable.
- **`created_at` ties within a transaction — always order by `(created_at, id)`.**
  `server_default=func.now()` is PostgreSQL's `now()`, which is transaction
  *start* time: every row written in one transaction carries the identical
  timestamp, so `ORDER BY created_at` alone returns them in arbitrary order.
  This bit the document audit trail, which came back delete-before-upload. The
  UUIDv7 primary key is the correct tiebreaker — it is monotonic, so it recovers
  true insertion order. `core/pagination.py` already does this for keyset paging;
  any hand-written `order_by` on a ledger or audit table must too. Transaction
  time is the right *business* timestamp, so do not reach for `clock_timestamp()`
  to dodge this.
- **Integration tests share one schema per module, not per test.** Building the
  full metadata per test took 20 s a test and grows with every module. Use the
  `session` fixture from `tests/conftest.py`, or the module-scoped schema
  pattern in `tests/integration/test_products_service.py`.
- **Never call `session.rollback()` inside a service.** `transaction()` runs its
  body in a SAVEPOINT and only the outermost frame COMMITs, precisely so a
  failure unwinds *that operation* and nothing else. A bare `rollback()`
  discards everything uncommitted on the session, including work the caller did
  before the call. Under the test `session` fixture — which joins the
  connection's transaction with `join_transaction_mode="create_savepoint"` — it
  reaches all the way back to the fixture's own savepoint and deletes the rows
  the test seeded, so an *expected* failure makes the next line fail with a
  bogus `NotFoundError`. The commit path is not symmetric with the rollback
  path: `commit()` is a savepoint release under that fixture and looks fine,
  which is why 2 000 unit tests missed it.
- **An exemption can relocate a defect instead of closing it.** Three
  consecutive rounds of "fix" in the traceability rules each moved the failure
  somewhere new. The tell is a fix that makes one operation succeed without
  asking what the *next* operation on the resulting row will do. Round 4 let a
  transfer `receive()` complete and left the stock at a grain nothing could
  address; round 5 opened the departure and let serialised stock leave
  unidentified. Follow the row forward one more step before believing a fix.
- **`batch_id` is part of `BalanceGrain`; `serial_id` is not.** So the same
  direction gate is correct for batch and wrong for serial — see
  `docs/assumptions.md` §23. Anyone "tidying" those two rules into one gate
  reintroduces a critical defect;
  `test_the_batch_and_serial_rules_disagree_on_purpose` exists to fail loudly
  when they do.
- **Do not read a value while the loop that computes it is still mutating it.**
  MRP split a parent's supply at `bucket.earliest_date` inside the same loop
  that was still calling `note_date()` on that bucket, so an item both directly
  demanded and used as a component was phased at one date for its explosion and
  a different one for its own requirement. Settle the value first (`_settle_demand`
  iterates to a fixed point), and have both stages read *one* computed object so
  they cannot diverge again.
- **`if xs: stmt = stmt.where(col.in_(xs))` with no `else` inverts the filter.**
  An empty collection silently removes the restriction, so "nothing matches"
  becomes "everything matches". This shipped in the receivable ageing query,
  where a tenant with no control account saw every ledger entry. An empty set
  must produce an empty result.
- **SQLAlchemy shortens over-long identifiers itself, deterministically.** Names
  past 63 bytes come back as a truncation plus a 4-hex-char hash
  (`fk_goods_receipt_lines_purchase_order_line_id_purchase__e266`), and
  autogenerate applies the same shortening before comparing — so these do *not*
  cause drift, and 12 of them are fine. The 63-byte limit still matters for
  hand-written CHECK constraint names, which is what
  `tests/unit/test_schema_identifier_length.py` guards.

- **`clause or default` raises; a SQL clause has no truth value.** `search_page`
  defaulted with `order_by or Item.code.asc()`, so *every* request naming a sort
  column answered 500 with `TypeError: Boolean value of this clause is not
  defined` — while the unsorted path kept working, because `None` is falsy quite
  legally. 3 580 tests, a clean lint, a matching contract and a green build all
  passed over it: no test ever passed an `order_by`, and the frontend list sends
  `sort_by=code` by default. Use `x if x is None else y`, never `or`, anywhere a
  SQLAlchemy element can be the left operand.
- **`tsc --noEmit` on a solution-style tsconfig checks nothing.** `apps/web`'s
  root config is `"files": []` plus project references, so `npm run typecheck`
  passed on zero files and reported success for months. The real check is
  `tsc -b`, which only ever ran inside `npm run build`. The script is now
  `tsc -b --noEmit`; if you add a config, prove the checker fails on a
  deliberately broken file before believing it.
- **TypeScript 5.5+ infers type predicates, which defeats "wrap the guard".**
  `z.string().refine((v) => isDemandType(v))` used to keep zod's output as
  `string`; since 5.5 the compiler infers `v is DemandType` for that exact shape
  and narrows the field, so the empty string the select starts on stops being
  assignable. Annotate the predicate `(v): boolean` to state the intent in a way
  inference cannot undo.
- **A screen can gate an action on a right its endpoint does not enforce.** Five
  shipped: job-work Close on `job_work.issue`, sales Confirm on
  `sales_order.approve`, Reset password on `user.update`, packing Hand-over on
  `packing.manage`, and `/notifications` on nothing at all. It fails in both
  directions — the holder cannot see the control, and whoever sees it is
  refused — and nothing catches it, because both constants exist and both
  spellings are real. `tests/unit/test_frontend_permission_gates.py` now fails
  the build on it; `test_frontend_permission_catalogue.py` guards the vocabulary
  the gates are drawn from.
- **A health check that cannot fail is worse than none.** Six containers
  reported the wrong status on the first real deployment, and every cause was
  in the check rather than the service. `wget http://localhost/` resolves to
  `::1` while nginx listens on IPv4 only, so two containers reported unhealthy
  while serving every request correctly. `celery-beat` inherited the api
  image's `HEALTHCHECK` - an HTTP probe - and serves no HTTP, so it was
  permanently unhealthy; a service that overrides its image's command usually
  needs to override the health check too. The worker's used exec-form `CMD`, so
  `$HOSTNAME` reached celery as nine literal characters and it pinged a node
  that cannot exist - `CMD-SHELL` is required for any expansion. And in the
  other direction: `/health/live` and `/health/ready` are served at the app
  root, not under `/api/v1`, so a probe through nginx fell through to
  `location /` and was answered by the SPA with 200 and an HTML page. Point a
  probe at something that goes wrong when the service is broken, then break the
  service and watch it go red.
- **Counting directories up from `__file__` works in a checkout and not in an
  image.** `config.py` located the repository root with `parents[4]`, which is
  right at `<repo>/apps/api/src/core/config.py` and raises `IndexError` at
  `/app/src/core/config.py`, where `src/` is copied straight to `/app`. It
  raised during import, before any logger existed, so gunicorn reported only
  "Worker failed to boot" above a pathlib traceback mentioning neither
  configuration nor a file. 3 600 tests, a clean lint and a green build all
  passed over it because every one of them runs from a checkout: the layout is
  an input, and only one value of it was ever supplied.
  `tests/unit/test_config_roots.py` supplies both.
- **nginx resolves an upstream hostname once and caches it forever.** A name in
  an `upstream ... server` directive is resolved at configuration load, and
  container addresses change every time a container is recreated - which is what
  a deploy does. Compose does not restart dependents when a dependency is
  replaced, so after a rebuild nginx proxied to an address nothing answered on:
  502 for every API request, while nginx's own `/healthz` stayed 200 because it
  touches no upstream. Total outage on an ordinary deploy, cured by an
  `nginx -s reload` nobody knew to run. The fix is `resolver 127.0.0.11` plus a
  *variable* in `proxy_pass`, which is re-resolved; it costs upstream keepalive
  and buys correctness under any restart order.
- **`protected-mode yes` on Redis means loopback only, whatever `bind` says.**
  Redis 7 applies protected mode whenever no password is set, and a `bind`
  directive does not exempt it. Every client here is a different container, so
  it rejected all of them - and in the worst shape: the TCP connection is
  accepted and the first command returns `-DENIED`, which client libraries
  report as "Connection reset by peer", a network error for an authorisation
  decision. Meanwhile `redis-cli ping` inside the container succeeds, because
  that is loopback, so Redis reports healthy while being unusable. What
  protected mode defends against is handled here by publishing no host port.
- **`chown -R` does not traverse symlinks.** The frontend image ran
  `chown -R nginx:nginx /var/run`, which is a symlink to `/run`: it retargeted
  the link and left `/run` owned by root, so nginx logged its version and worker
  method and then died on the last step before serving with
  `open() "/run/nginx.pid" failed (13: Permission denied)`. Chown the file, not
  the symlinked directory.
- **A directive in an included file plus the same directive in the including
  block is a duplicate, not an override.** `proxy_params.conf` set
  `proxy_read_timeout` and was included inside locations that set it again, so
  nginx refused to load at all. Defaults meant to be overridden belong one
  context up - at `server` level - where a location value legally wins. Both
  config sets had this and neither had ever been loaded by anything.
- **A never-executed test accumulates drift silently.** `tests/e2e` had a
  297-line journey that could not pass its own login: it used
  `admin@atcraft.local`, the address `seed.py` documents at length as one
  `EmailStr` refuses, and `getByLabel('Password')` which substring-matches the
  field's own "Show password" toggle. Past those, its item payload predated
  `base_uom_id`, and it skipped the submit step on both the BOM and the purchase
  order. None of that is visible until it runs. Run it.
- **`model_validate(orm_row)` reads every field the schema declares, including
  the `lazy="raise"` one.** `StockTransferRead` and `StockAdjustmentRead` both
  declare `lines`, and both list services cleared it with `read.lines = []`
  *after* validating - too late, because the read happened inside
  `model_validate` and raised `InvalidRequestError`. Both list endpoints
  answered 500 for every row they found, and were clean only while the table was
  empty, which is every automated run: the demo data raises no adjustments and
  no transfers. It surfaced the first time a person raised one through the UI -
  the document was created and the screen meant to list it broke. Declare the
  relationship loaded *before* validating (`set_committed_value(row, "lines",
  [])`), which costs no query, and pin it with a test that lists a row it
  actually created.
- **`localhost` is not `127.0.0.1`, and a cancelled Redis command never heals.**
  Two faults, one symptom: `/health/ready` answering `{"redis": false}` while
  `redis-cli ping` answers instantly. First, the dev overlay publishes every
  port on `127.0.0.1`, but `.env` said `localhost` - which resolves to `::1`
  first on Windows. redis-py tries that address alone, waits out
  `socket_connect_timeout`, retries, and fails after four seconds; asyncpg does
  fall back to IPv4, but the dead attempt still costs about two seconds, which
  is the entire readiness budget, so the database read false at startup too.
  Host-side settings must say `127.0.0.1`; inside Docker the names `postgres`
  and `redis` resolve to IPv4 only and are fine. Second, and worse: the probe
  bounds each check with `asyncio.wait_for`, and a timeout cancels the `ping`
  *mid-command*. redis-py returns that connection to the pool with its reply
  unread and hands it to the next command, so after one slow moment the probe
  answers "down" for the life of the process - the container reports unhealthy
  for ever, `up --wait` never returns, and the application is serving traffic
  perfectly the whole time. `check_redis_ready` now discards the cached client
  on any failure, cancellation included, so the next probe reconnects.
- **The dependency locks are resolved on Linux, and pin `uvloop`.** pip-compile
  evaluates environment markers for the platform it runs on, so
  `requirements-dev.lock` names `uvloop` unconditionally and a plain
  `pip install --require-hashes -r` on Windows fails before installing anything
  ("uvloop does not support Windows"). The Windows quick start could not
  succeed. Regenerating the lock on Windows is not the fix: CI regenerates it
  on Linux and diffs, and the image would lose its event loop. Install through
  `python -m scripts.install_requirements <lock>`, which drops the pins the
  platform cannot build and keeps every remaining hash.
- **Idempotence keyed on "today" is not idempotent tomorrow.** The demo price
  list looked up an existing slab by `valid_from == anchor - 180 days`, and
  `anchor` is the day the loader runs. A second run a day later found nothing,
  inserted, and was refused by `create_price` for overlapping the open-ended
  row the first run wrote - the whole stage failed. Key an existence check on
  what identifies the row (item, channel, slab, still-open), never on a value
  derived from the clock.
- **A subprocess in a test needs a timeout, and stdin closed.** The migration
  round-trip test ran `alembic` with `subprocess.run(...)` and no timeout; on a
  four-core laptop under load the initial-schema upgrade took 2m17s (about 10 s
  on CI), the run sat at 27% for an hour, and nothing said why. `timeout=` turns
  a hang into a failure with output; `stdin=DEVNULL` stops a child that reads
  stdin from waiting on the test runner's.
- **`shrink-0` on a group that is itself `flex-wrap` defeats the wrap.** The
  `PageHeader` actions box was `flex shrink-0 flex-wrap`; `shrink-0` pins the
  box at its single-line max-content width, so the inner wrap never fires and
  five buttons ran off a phone. A wrapping group must be allowed to shrink.
  Likewise a `max-h-[90vh]` dialog with no `overflow-y-auto` of its own clips
  its footer the moment its body outgrows a phone - put the scroll on the
  primitive (`DialogContent`), not on each consumer.

## Things not to do

- Do not introduce Kafka, Kubernetes, Elasticsearch, ClickHouse, a second
  database, or microservices. Not without measured evidence and an update to
  this repository's architecture decision records first.
- Do not add a dependency without justifying it in the PR.
- Do not disable authorisation, validation or a constraint to make development
  easier.
- Do not cache inventory availability.
- Do not use soft deletion on ledger, audit or posted financial records.
- Do not "fix" a failing type check or lint rule by loosening the configuration.

## Useful commands

Every development target exists in both `Makefile` and `make.ps1`
(`.\make.ps1 <target>` on Windows); the `deploy-*` targets are Makefile and
Git Bash only.

```bash
make install        # venv + deps + git hooks; uses scripts.install_requirements because
                    # the locks are Linux-resolved and pin uvloop, which has no Windows build
make dev            # hybrid development (Windows: .\make.ps1 dev, then dev-api / dev-web)
make migrate        # apply migrations
make test           # unit + integration
make contract       # regenerate openapi.json and the frontend types after an API change
make check          # contract check + lint + typecheck + tests — what CI runs
make hooks          # install the git pre-commit + pre-push hooks (done by `make install`)
make hooks-run      # run every hook on the whole tree, exactly as a push would
```

Branches: `main` is deployment 1 (the client's environment), `dev` is
deployment 2 (integration testing); feature, bugfix and hotfix branches come off
`dev`.

The hooks are defined in `.pre-commit-config.yaml`. `pre-commit` runs the
formatters, linters and file-hygiene checks on the staged files; `pre-push`
runs mypy, the unit tests, vitest, the type check, the OpenAPI contract check
and a Compose config parse on the whole tree. Never bypass them with
`--no-verify`; fix what they report.
