# CLAUDE.md — working rules for this repository

Read this before changing anything. The authoritative specification is
`ATCRAFT_MANUFACTURING_ERP_CLAUDE_MASTER.md`; this file is the operational
summary plus the conventions this codebase has actually settled on.

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
| Frontend data | TanStack Query only; keys from `queryKeys` in `lib/query-client.ts` |
| Frontend lists | `useListQuery` + `DataTable`, server-side everything |

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
- **A never-executed test accumulates drift silently.** `tests/e2e` had a
  297-line journey that could not pass its own login: it used
  `admin@atcraft.local`, the address `seed.py` documents at length as one
  `EmailStr` refuses, and `getByLabel('Password')` which substring-matches the
  field's own "Show password" toggle. Past those, its item payload predated
  `base_uom_id`, and it skipped the submit step on both the BOM and the purchase
  order. None of that is visible until it runs. Run it.

## Things not to do

- Do not introduce Kafka, Kubernetes, Elasticsearch, ClickHouse, a second
  database, or microservices. Not without measured evidence and an update to the
  master document first.
- Do not add a dependency without justifying it in the PR.
- Do not disable authorisation, validation or a constraint to make development
  easier.
- Do not cache inventory availability.
- Do not use soft deletion on ledger, audit or posted financial records.
- Do not "fix" a failing type check or lint rule by loosening the configuration.

## Useful commands

```bash
make dev            # hybrid development (Windows: .\make.ps1 dev)
make migrate        # apply migrations
make test           # unit + integration
make check          # lint + typecheck + tests — what CI runs
```
