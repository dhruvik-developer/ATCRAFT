# CLAUDE.md — working rules for this repository

Read this before changing anything. This file is the repository's authoritative
engineering baseline, together with the focused decisions under `docs/`.
`docs/development-standards.md` maps the shared implementation points and
required completion checks across the existing application.

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
| Responses | `ApiResponse`, `PaginatedResponse` and `CursorPaginatedResponse` from `src.core.responses` |
| Permissions | `require_permission(Permission.X)` on every route |
| Shared constants | backend `src/core/constants.py` for schema-level decisions (`DEFAULT_CURRENCY`, code widths); frontend `lib/constants.ts` for page sizes, debounce, storage keys, formats and env — the only file that reads `import.meta.env` |
| Shared messages | backend `src/core/messages.py` for any sentence raised from two or more modules; frontend `lib/messages.ts` for the shared error headlines, empty states, prompts and the success builders for the standard verbs (created/updated/approved/...) — a bespoke confirmation that names its own action stays with its screen, and API messages are shown verbatim |
| Frontend data | TanStack Query only; keys from `queryKeys` in `lib/query-client.ts` |
| Frontend lists | `useListQuery` + `DataTable`, server-side everything |
| Lazy pages | `app/route-loaders.ts` is the one list of lazy pages; `lazy()` and the sidebar hover prefetch both read it |
| Colour | never a literal - a token from `styles/globals.css`, which `:root` and `.dark` both define. `lib/theme.ts` is the one place the light/dark/system decision is made, shared with the pre-React paint in `main.tsx` |

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
  Both files have since been converted to `tests/seed_support`, whose
  `seed_item` builds through the ORM with `**overrides` and so names no columns
  at all - which is why dropping `items.finish`, `size` and `colour` cost two
  test lines rather than forty-three. Keep it that way: the rule is the ORM, not
  the column list.
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
- **A transfer moves a unit on both legs, and both legs are traceable now.** The
  dispatch writes one `TRANSFER_OUT` per named unit, sets its serial
  `IN_TRANSIT` and creates the destination in-transit balance in the same
  transaction; receipt writes one `TRANSFER_IN` and sets `IN_STOCK` at the
  destination. Do not "simplify" either leg into a quantity-only movement, and do
  not defer the destination balance until receipt: the auto-consume check would
  see the lot at zero in between and close it while it is still on a lorry. A
  transfer line's `serial_numbers` are the labels an operator typed; the service
  resolves them to ids in one query (`_adopt_named_serials`) and refuses an
  unknown label rather than minting one. `serial_ids` is still the direct API
  form, and both are validated against quantity and duplicates.
- **A lot has a lifecycle, not just a label.** `ACTIVE -> QUARANTINED -> ACTIVE`
  and `ACTIVE | QUARANTINED -> EXPIRED` are audited transitions; any status
  becomes `CONSUMED` automatically only when that lot's stock across on-hand,
  in-transit, job-work and scrap is zero. Quarantine moves *available* stock to
  `QC_HOLD`; reactivating changes the status only, because releasing held stock is
  a quality decision. Expired and quarantined lots must stay transferable,
  returnable, adjustable and disposable — `BATCH_ISSUE_BLOCKED` names only the
  consuming movements. Adding a new transaction type to both the issue gate and
  the "can still get rid of it" set without thinking will trap stock.
- **FEFO never splits a line.** When an outbound movement names no lot, the
  engine picks the earliest-expiring `ACTIVE`, unexpired lot at the exact
  position whose available quantity covers the whole line; otherwise the line
  falls through to the ordinary shortfall. `MATERIAL_ISSUE`, `DISPATCH`,
  `ASSEMBLY_CONSUME`, `JOB_WORK_ISSUE` and `TRANSFER_OUT` are the five types that
  select. A caller needing two lots keys two lines, because one ledger row has
  one `batch_id` and the document must say what it consumed. A stock transfer
  document still requires its lot at create because both legs must key the same
  grain; the `TRANSFER_OUT` fallback is for a direct movement with no document
  lot to replay.
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
- **`BaseHTTPMiddleware` costs 231 us a layer, and times the wrong thing.**
  Starlette's convenient middleware base runs the rest of the application in a
  separate anyio task joined by two memory object streams, per request. Measured
  on a minimal app: 16.6 us bare, 1171 us with five no-op layers of it, 19.8 us
  with five no-op pure-ASGI layers - 231 us against 0.6 us. Five were stacked
  here, so every request paid about 1.15 ms before reaching a route. Worse, its
  `call_next` returns on `http.response.start` - when the *headers* are settled -
  so timing a request around it measures time to first byte. That is invisible
  for JSON and it is the whole transfer for `GET /documents/{id}/download`: the
  access log and the `atcraft_http_request_duration` histogram both reported the
  slowest route in the ERP as the fastest, and `request.slow` could never fire
  for a download. Middleware here is pure ASGI and wraps `send`; stop the clock
  on `is_final_body_message`, not on the start message.
- **A per-IP rate limit is per *site*, and a plant is one site.** Every user
  behind the office NAT shares one `$binary_remote_addr`, so nothing keyed on it
  can be a fair per-user budget - and two limits were set as though it could.
  Nginx's `api_general` at 120 r/m refused a single operator working properly (a
  detail page is ~5 requests, a dialog on it ~5 more), and
  `RATE_LIMIT_AUTH_PER_MINUTE` at 4/min refused the twenty-first person at a
  shift change, *before* their password was checked - which from the floor is
  indistinguishable from the ERP being down. Fairness belongs to the application
  limiter, which keys on the authenticated user; the Nginx layers are flood
  guards and are sized well above any real office. Credential stuffing is stopped
  per account by `LOGIN_PER_EMAIL` and the lockout, which are fixed constants -
  raising the per-address budget does not weaken it. Sizing table in
  `docs/security.md` section 8.
- **jsdom answers `:modal` by asking itself, and swallows the overflow.** One
  `user.click` on a Radix Select trigger cost 36-46 s; the query that found the
  trigger cost 32 ms and everything after the menu opened cost 130 ms. jsdom
  uses nwsapi as a library and never calls its `install()`, so nwsapi's internal
  `_matches` is never assigned and its `matchesNative` falls back to
  `node.matches` - which is nwsapi. `isModal` is
  `matchesNative(node, ':modal') || isFullscreen(node)`, so the query re-enters
  itself; nwsapi's own try/catch swallows the `RangeError` from the blown stack
  and every unwinding frame restarts the other branch, making it depth-squared
  rather than a fast failure. One `document.body.matches(':modal')` costs
  1,717,414 internal calls and ~616 ms and returns `false`; `matches(':hover')`
  costs 0.2 ms. Floating UI's `isTopLayer` asks `:popover-open` then `:modal`,
  and Radix positions Select, DropdownMenu, Popover and Tooltip at
  `strategy: 'fixed'` - which walks every ancestor on every measurement - so 46
  honest probes became ~61 million calls. Four files paid it, 14 tests, ~644 s,
  and three of the four open a *DropdownMenu*, so it was never a Select problem.
  `src/test/setup.ts` now answers those three selectors `false` for everything,
  which is the answer jsdom eventually gives anyway; `src/test/top-layer-shim.test.ts`
  fails if the recursion returns. Do NOT widen it to `:open` / `:closed`, which
  nwsapi answers from a real `<details>`/`<dialog>` branch, and do not "fix" this
  by changing `SelectContent` to `position="item-aligned"` - that is a 61,000x
  speedup bought by shipping different popover behaviour to users to make a test
  fast. Measured: the reported test 44,985 ms -> 1,554 ms, the whole frontend
  suite 489 s -> 165 s.
- **The fix existing in 15 files is not the fix existing.** Every one of those
  four slow files was slow because a shim 15 of their 16 siblings already
  carried had never been copied into them - and two of the copies say in their
  own comments that it "belongs in `src/test/setup.ts`", which nobody had done
  because the file was outside whatever change was in front of them at the time.
  A workaround pasted into the file you are touching is a note that something
  belongs one level up; put it there, or it becomes a 644-second tax collected
  from whoever writes the next screen test.
- **A bind-mounted Vite dev server serves what it cached at startup.** Docker
  Desktop on Windows and macOS does not deliver inotify events across a bind
  mount, so Vite's watcher never fires: the file on disk is plainly correct, the
  container can `cat` it, and the module the browser receives is the transform
  Vite made hours ago. Nothing errors. This is worse than a build failure,
  because every check still passes - a full Playwright run went green against a
  frontend that did not contain the change it was meant to be testing, and the
  only tell was a control that was simply absent from the page. `vite.config.ts`
  has had the switch (`VITE_USE_POLLING`) and a comment saying exactly why since
  before this happened; nothing set it for `docker-compose.dev.yml`, the one
  stack whose entire purpose is that bind mount. It is set now. When a browser
  check disagrees with a unit test, confirm the browser is running your code -
  `curl http://127.0.0.1/src/<path>.tsx | grep <YourNewSymbol>` answers it in one
  line - before believing either.
- **A foreign key to a UUID is not a tenant check, and not a kind check.**
  `items.finish_id` references `item_attributes.id`. A UUID is globally unique,
  so the constraint is perfectly satisfied by one company's item pointing at
  another company's finish: nothing complains, the wrong company's word simply
  appears on the screen. It is also satisfied by a COLOUR row's id sitting in
  `finish_id`, which the database cannot tell apart and a person reading the item
  can. Both are closed by reading the row through the tenant-scoped repository
  and checking `attribute`, in `_require_attribute` - never by trusting the FK.
  `update_item` matters most here because it applies its payload with a blind
  `setattr` loop, so any id it does not look up is an id nobody checked.
  `tests/unit/test_products_reference_probes.py` exists because this class
  already shipped once.
- **A hand-written frontend type is compared with nothing.** `ItemDetail` in
  `features/items/item-model.ts` is declared by hand rather than as
  `SchemaOf<'...'>`, so `tsc` checks it against no contract at all. When
  `items.valuation_method` was dropped from the API (now part of baseline
  migration `20261021_0100`; every item is FIFO) - the interface kept declaring
  it and
  `item-detail-page.tsx` kept drawing a "Valuation" row from it. `humanise`
  returns `'-'` for `undefined`, so every item in the ERP showed a labelled row
  with a dash in it for months, on a screen people read daily, with a green
  build the whole time. The reason the shape is hand-written at all is worth
  knowing: 27 endpoints set `response_model=None` because the response varies by
  permission (`get_item` returns `ItemDetailWithCost` to a principal who may see
  costs), and an endpoint with no response model emits an empty `schema: {}`
  into `openapi.json` - so the generated types have nothing to offer and the
  frontend has to write the shape itself. `tests/unit/test_frontend_contract_types.py`
  now compares each hand-written mirror against the *Pydantic* model, which
  exists whatever the router declares. Register a new mirror there, or derive it
  from the generated types and need no entry.
- **A button offered on a status the state machine cannot leave strands the
  document.** The job-work list offered Close on `QC_PENDING`, and the machine
  has no `QC_PENDING -> CLOSED` edge - that status has exactly one way out,
  `QC_PENDING -> COMPLETED` by recording the inspection verdict. Nothing in the
  application called that endpoint either, so an order whose output went to QC
  hold could not be completed, could not be closed, and its output stayed on
  hold: real stock, owned and valued, that nothing could release. Press the
  button on every status a predicate admits, or read the transition table -
  `isClosable` and its friends are assertions about the machine, and an
  optimistic one shows a control that can only fail.
- **A sortable column the server does not allow empties the table.** The MRP
  shortage report marked `available_quantity` and `incoming_quantity` sortable;
  the repository's `sortable_fields` listed neither, so clicking either heading
  answered 422 and the rows vanished - the two questions a planner most wants to
  ask that screen. Both were real indexed columns; the whitelist had simply not
  kept up. A column with `enableSorting: true` is a claim about the endpoint
  behind it.
- **A status with no entry in the badge map reads as a draft.** `STATUS_VARIANTS`
  in `components/shared.tsx` falls back to the neutral `secondary`, so `POSTED` -
  which every goods receipt and every stock adjustment in a live tenant carries -
  rendered the same grey as `DRAFT`. The fallback is what hides it: nothing
  errors, the badge just says the wrong thing on every row of two daily screens.
  Add the key when a status reaches the UI.
- **Two release paths for one hold strands the document or moves the stock twice.**
  A goods receipt, an assembly completion and a job-work receipt each put
  their output on QC hold and raise an inspection, and each also offered its
  own verdict endpoint that released the same hold. Deciding the inspection
  moved the stock but left the receipt line PENDING, the PO in QC_PENDING for
  ever and the assembly and job-work orders unable to leave QC_PENDING; the
  owner-side verdict moved it again, so whichever ran second either failed for
  want of held stock or released another lot held at the same grain. The rule
  now: quality owns the hold and the one movement (`quality.owners`
  registry), the owner learns the verdict through a callback in the same
  transaction and never moves stock, and the owner-side endpoints serve only
  holds no inspection covers - refusing with a message that names the open
  inspection, or completing from a decided inspection's own figures.
- **A dev-server proxy default that is right on the host is wrong in the
  container.** `vite.config.ts` sends `/api` to `http://localhost:8000`, which is
  correct for `make dev` - Vite and the API both run on the host there. The
  Docker dev stack runs Vite *inside a container*, where `localhost` is the
  frontend itself and nothing listens on :8000, so the proxy answered
  `ECONNREFUSED` and Vite reported that to the browser as a **500**. The failure
  shape is the trap: :5173 serves the SPA perfectly, the sign-in screen paints,
  and only the submit fails - with a status that blames the API, while the
  backend log shows the request never arrived. The port is published in the dev
  overlay and `tests/e2e/.env` names it as the default `E2E_BASE_URL`, so it is a
  door the project invites you through; it had never carried a request. Set
  `VITE_API_PROXY_TARGET` per stack. When a 500 has no matching line in the
  server log, suspect the thing between them.

- **Smart App Control blocks unsigned launchers, and its verdict drifts.** On the
  Windows development box Smart App Control runs in enforce mode and judges
  native binaries by reputation. It refused `greenlet` 3.5.5's DLL - which
  breaks every async database call, so integration tests error in setup and
  the hybrid API cannot serve while unit tests stay green - and, an hour into
  a session that had been committing normally, it began refusing the
  `end-of-file-fixer.exe` and `check-json.exe` console launchers pip writes
  into the pre-commit environment, so every commit failed with WinError 4551.
  Neither was a file problem. The venv now carries greenlet 3.2.4 (the locks
  still pin 3.5.5 for Linux), and `.pre-commit-config.yaml` runs every
  pre-commit-hooks hook as `python -m pre_commit_hooks.<module>` through the
  signed interpreter. When a tool fails with "An Application Control policy
  has blocked this file", read `Microsoft-Windows-CodeIntegrity/Operational`
  before suspecting the code, and route the tool through `python -m` rather
  than skipping it.
- **`shrink-0` on a group that is itself `flex-wrap` defeats the wrap.** The
  `PageHeader` actions box was `flex shrink-0 flex-wrap`; `shrink-0` pins the
  box at its single-line max-content width, so the inner wrap never fires and
  five buttons ran off a phone. A wrapping group must be allowed to shrink.
  Likewise a `max-h-[90vh]` dialog with no `overflow-y-auto` of its own clips
  its footer the moment its body outgrows a phone - put the scroll on the
  primitive (`DialogContent`), not on each consumer.
- **A Radix Select inside a form clears a value whose option has not loaded.**
  Radix keeps a hidden native `<select>` in a form and re-dispatches every change
  of the controlled value through it. Reset a form to a saved record while the
  picker's list is still loading and that `<option>` does not exist yet, so the
  native element can only hold `""` - which arrived as `onValueChange('')`. An
  amended job-work order lost its warehouse whenever the warehouse list was not
  cached, the select showed its placeholder once the list came back, and the
  next save wrote the loss. `""` is never a choice a person can make (Radix
  refuses an item whose value is empty), so `components/ui/select.tsx` drops it
  once for every select. An optional select offers "none" as the `SELECT_NONE`
  sentinel and maps it to `null`; never design one whose "none" is `""`.
- **A sweep that flags every route flags nothing.** The browser sweep opened
  each route with `page.goto`. A full load drops the in-memory access token,
  and the first `GET /auth/me` answers 401 twice before the refresh cookie
  restores the session - so all 42 routes were flagged `HTTP`, and a real
  failure in that column looked like the rest. Its sibling, the responsive
  sweep, matched dialog openers with `run|post`, so it pressed "Run MRP" on
  every pass and wrote three real MRP runs into the database it was measuring.
  A sweep opens routes client-side after one sign-in and clicks only verbs
  that open a form.

## Things not to do

- Do not create a second copy of this repository. There is one working copy,
  `C:\Project\Other\ATCRAFT`; work directly on `develop`. Keep only `main` and
  `develop`, and synchronize their verified revisions locally and remotely. Do
  not create feature, temporary or backup branches. No `git worktree add`,
  clone or copied folder - beside it, under a tool's data directory or through
  a tool's worktree mode - and no clone databases or extra Compose projects left
  behind. `AGENTS.md` carries the same rule for tools that read it instead of
  this file. The copies made before this rule linked `.venv` and `node_modules`
  back here as junctions, which Windows PowerShell 5.1's `Remove-Item -Recurse`
  follows into the target: unlink the junctions before deleting any copy.
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
make seed-local     # migrate and populate a local database, including demo PDFs
make test           # unit + integration
make contract       # regenerate OpenAPI, frontend types and Postman after an API change
make check          # contract + lint + typecheck + tests + production web build
make hooks          # install the git pre-commit + pre-push hooks (done by `make install`)
make hooks-run      # run every hook on the whole tree, exactly as a push would
make clean          # remove generated outputs; preserve source, local data and dependencies
```

Install `pre-commit` as a tooling prerequisite before `make install`. GNU Make
uses commands from PATH: activate `apps/api/.venv` after installation before
running the local development and checking targets. The PowerShell wrapper uses
the backend interpreter explicitly for its backend commands.

All development, fixes and integration work use `develop` in this checkout.
Keep the verified revision synchronized to `main`. The deployment guard maps
deployment `main` to branch `main` and deployment `dev` to branch `develop`;
deployment names do not require additional Git branches.

The hooks are defined in `.pre-commit-config.yaml`. `pre-commit` runs the
formatters, linters and file-hygiene checks on the staged files; `pre-push`
runs mypy, backend unit tests, Vitest, the frontend type check, OpenAPI/Postman
and generated-type checks, and a Compose config parse on the whole tree. Never bypass them with
`--no-verify`; fix what they report.
