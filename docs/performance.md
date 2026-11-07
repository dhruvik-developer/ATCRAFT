# Performance

Master rules sections 30, 57–65, 85, 86.

The framework is not what makes an application fast. What makes it fast is doing
less work: the right indexes, the right amount of data, expensive things moved
off the request path, and measurement instead of guessing.

---

## Targets

Engineering targets, not guarantees. Finalise them against real Atcraft data and
a load test.

| Path                              | Target         |
| --------------------------------- | -------------- |
| Normal API request                | < 200 ms       |
| Cache-served request              | < 100 ms       |
| Simple indexed query              | < 100 ms       |
| List endpoint (50 rows, filtered) | < 300 ms       |
| Dashboard (summary tables)        | < 500 ms       |
| Large report                      | background job |
| Large MRP run                     | background job |

Heavy planning and reporting work is explicitly exempt. Forcing a genuine
five-minute calculation into a 200 ms budget produces a wrong answer quickly,
which is worse than a right answer slowly.

---

## Load testing

`tests/load/locustfile.py` models read-heavy internal users with a distinct
login account per virtual user. The default profile is non-mutating: the MRP
kickoff task is disabled unless writes are explicitly enabled. Account files
contain test credentials and must be kept outside the repository:

```json
[
  { "email": "load-001@example.test", "password": "use-a-dedicated-test-password" },
  { "email": "load-002@example.test", "password": "use-another-test-password" }
]
```

For a single-process run, provide at least one account per virtual user. The
harness rejects duplicate email addresses and fails the run if the account pool
is too small or an account cannot log in. In distributed Locust, each worker
needs its own account file with a disjoint set of identities sized for the
users assigned to it; workers do not share or partition one account pool.

Run only against a populated, isolated load environment, never the shared
development database. For example, from PowerShell:

```powershell
$env:LOAD_ACCOUNTS_FILE = 'C:\secure\atcraft-load-accounts.json'
$env:LOAD_WRITE_ENABLED = 'false'
locust -f tests/load/locustfile.py --headless -u 20 -r 2 -t 10m --host http://127.0.0.1:8000
```

The optional `LOAD_WRITE_ENABLED=true` adds one write cycle per `WriteUser`
account: post a prepared stock adjustment, create and release a reservation,
raise and decide a stock-holding QC inspection, raise a consignment from a
prepared sales order, mark it ready, dispatch it, and issue its invoice. This is
intentionally opt-in and mutates stock and commercial records. Every account in
this mode needs a `write_fixtures` object; `tests/load/make_write_fixtures.py`
builds that object and rewrites the account file:

```powershell
$env:LOAD_ADMIN_EMAIL = 'admin@atcraft.test'       # may create masters and documents
$env:LOAD_ADMIN_PASSWORD = '...'                   # or omit to be prompted
$env:LOAD_BASE_URL = 'https://load-target.example'
python tests/load/make_write_fixtures.py `
    --accounts C:\secure\atcraft-load-accounts.json `
    --base-url $env:LOAD_BASE_URL `
    --confirm-target load-target.example `
    --apply
```

The script is a dry run without `--apply`: it prints the warehouse, unit, item,
customer and template codes it would create and makes no HTTP calls. With
`--apply` it requires a confirmed, non-loopback HTTPS API origin, then logs in
once as the admin, reads the company's adjustment approval
threshold, verifies every load account can log in, then creates, per account:

- a dedicated item, customer and FINAL QC template;
- posted opening stock and a draft write-down adjustment the load profile posts;
- a confirmed, fully allocated sales order the load profile raises a
  consignment against, marks ready and dispatches; and
- the reservation, inspection, decision, dispatch and invoice payloads.

A run tag (six random hex characters by default, `--run-tag` to choose one)
prefixes every code, so a run's records are identifiable afterwards. The account
file and the optional `--output` must stay outside the repository: they contain
passwords. The script refuses a file that already has `write_fixtures`, because
those documents are one-use. It also refuses an opening-stock or write-down value
above the company's adjustment approval threshold, since the write profile posts
directly and cannot run the approval workflow; lower the fixture quantities or
raise the threshold on the isolated target. The load accounts themselves still
need permission for the adjustment, reservation, quality, dispatch
(`DISPATCH_UPDATE` to mark ready, `DISPATCH_CREATE` to send) and invoice steps -
the admin only builds the fixtures.

Generate fixtures only against the dedicated load target (or a throwaway
database you will drop), never `atcraft_erp` or `atcraft_erp_test`. These
operations are not rolled back after the run. The Locust process exits nonzero
when the documented failure-rate or latency targets are exceeded, or when setup
or a write step fails; preserve the report and investigate before increasing
load. Once the accounts have run, generate a fresh fixture set (ideally from a
fresh account file) before the next write run.

### Measured on this build (2026-11-06)

Two read-only runs against a throwaway database - created empty, migrated to
Alembic head, then populated with the demo dataset - served by a single Uvicorn
worker on `127.0.0.1:8000`, with PostgreSQL and Redis from the development
Compose stack and 30 dedicated load accounts:

| Run | Users | Duration | Requests | Failures | Median | p95    | p99    | Throughput |
| --- | ----- | -------- | -------- | -------- | ------ | ------ | ------ | ---------- |
| 1   | 10    | 3 min    | 328      | 1 (0.30%) | 60 ms | 1 300 ms | 2 800 ms | 1.8 req/s |
| 2   | 8     | 3 min    | 264      | 0        | 54 ms  | 710 ms   | 1 200 ms | 1.5 req/s |

Slowest endpoints in run 2 by p95: `GET /mrp/runs` and `GET /inventory/ledger`
(1 600 ms), `GET /sales/{id}/allocation-status` (1 200 ms),
`GET /inventory/balances` and `GET /notifications/unread-count` (1 100 ms),
`GET /reports/dashboard` (710 ms). Every other endpoint's p95 is under 700 ms,
and every endpoint's median is under 150 ms except `POST /auth/login` (430 ms,
one bcrypt verification per virtual user per run).

**These are indicative numbers, not capacity evidence.** They were taken on a
shared developer workstation - CPU at 88-91% from the IDE and an unrelated
Next.js dev server - with a single API worker, the demo dataset rather than
Atcraft's data volumes, and no write workload. The API logged 12
`Timeout reading from 127.0.0.1:6380` warnings during run 1, and the single
failure (a `RemoteDisconnected` on `GET /inventory/ledger`) falls inside that
window; run 2 saw no failures. The long tails track Redis and CPU contention on
the host, not a query that needs an index, so no tuning was attempted on this
evidence.

Production sign-off still requires the dedicated load target, the client's
agreed workload mix and thresholds, and one-use write fixtures described above.
Until then the engineering targets at the top of this document remain targets.

---

## Where ERP performance actually goes wrong

In rough order of how often it happens:

1. missing index on a filtered column
2. N+1 queries
3. `OFFSET 900000`
4. selecting columns nobody reads
5. aggregating a hot transactional table on every dashboard load
6. unbounded result sets
7. a cache with no invalidation strategy, quietly serving wrong numbers

Every one of these has a structural defence below.

---

## Indexing

Indexes are added for **observed query patterns**, not defensively. They speed
reads and cost writes and storage; on an append-heavy table like
`inventory_transactions` an unused index is a permanent tax on every movement.

The rules:

- index the columns you actually filter on;
- index foreign keys used in joins;
- composite indexes in the order the query uses them — equality columns first,
  then range, then sort;
- match the sort direction of the query for keyset pagination;
- partial indexes where the query always carries the predicate
  (`WHERE status = 'OPEN'`, `WHERE deleted_at IS NULL`);
- prove it with `EXPLAIN ANALYZE` before and after.

Representative implemented indexes are shown below. Check the owning models
and migrations for the full definitions; document indexes follow their actual
business dates rather than one universal status/creation-time shape.

```sql
-- current stock: the most-read table in the application
(company_id, item_id)                                       -- stock across warehouses
(company_id, warehouse_id, item_id)                          -- warehouse stock list
(company_id, item_id, warehouse_id) WHERE available_quantity > 0

-- ledger: append-heavy, read by history and by cursor
(company_id, item_id, warehouse_id, occurred_at DESC)        -- item movement history
(company_id, occurred_at DESC, id DESC)                      -- business-time reports
(company_id, created_at DESC, id DESC)                       -- keyset paging
(company_id, reference_type, reference_id)                   -- "what did this GRN post?"

-- reservations: read on every allocation decision
(company_id, item_id, warehouse_id, status)
(company_id, demand_type, demand_id)

-- documents: the list screens people live in
(company_id, status, required_date)                          -- sales orders
(company_id, customer_id, order_date)                        -- sales orders

-- audit: unbounded, cursor-paginated
(company_id, created_at, id)                                 -- also scans backwards
(company_id, entity_type, entity_id, created_at)

-- search, without Elasticsearch
GIN (code gin_trgm_ops), GIN (name gin_trgm_ops)              -- items
```

---

## Pagination

Nothing returns an unbounded result set. Two strategies, chosen per endpoint.

**Offset** — master data and filtered document lists, where the user jumps to a
page number and filters keep the set small. Capped at 200 rows per page.

**Keyset (cursor)** — append-heavy history: inventory ledger, audit log,
notifications. Constant cost at any depth, because there is no offset to skip.

```sql
-- keyset: a row-value comparison PostgreSQL can satisfy from a composite index
WHERE (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT 50
```

The `a < x OR (a = x AND b < y)` formulation usually cannot use the index; the
row-value form can.

Counting is itself a scan. `with_total=false` skips it, and even when asked for,
the count is bounded by `COUNT_CEILING` (10 000) so a broad filter cannot trigger
a full-table count. List queries fetch `limit + 1` rows to determine `has_next`
without a second query.

---

## N+1

Relationships are declared `lazy="raise"` by default. An accidental lazy load
raises in development instead of quietly issuing 100 queries in production.

Where related data is needed, it is loaded deliberately:

```python
# batch by id set, then map — never a query per row
stmt = select(Item).where(Item.id.in_(item_ids))

# controlled eager loading for a specific endpoint
stmt = select(PurchaseOrder).options(selectinload(PurchaseOrder.lines))
```

The BOM explosion engine loads every BOM at a given level in one query and then
descends. The MRP engine batches items, balances, BOMs and incoming quantities
before it starts netting. A per-node query in either would be a defect, not a
slow path.

Every list endpoint is reviewed for N+1 before merge.

---

## Ledger + balance

Current stock is never computed by summing history:

```
-- never on a request path
SELECT SUM(quantity) FROM inventory_transactions WHERE item_id = ...
```

`inventory_balances` holds current state and is updated in the same transaction
as the ledger row. `available_quantity` is a generated column, so it cannot drift
from its inputs and can be indexed directly.

The ledger repository exposes reconciliation queries for diagnostics; the beat
schedule does not currently run a full ledger/balance reconciliation. Stock
summary refreshes are separate from reconciliation and read the balance table.
Do not describe a summary refresh as evidence that historical postings reconcile.

---

## Caching

Redis is a cache. PostgreSQL is the truth. Every entry has a TTL, and keys are
tenant-scoped (`atcraft:{company_id}:{namespace}:{key}`) so cross-tenant bleed is
structurally impossible.

| Namespace    | TTL    | Contents                                                             |
| ------------ | ------ | -------------------------------------------------------------------- |
| `ref`        | 60 min | Tenant UoM conversion graph; invalidated on UoM/conversion writes    |
| `sessrevoke` | 30 sec | Refresh-session liveness; revoked sessions are invalidated on writes |
| `dash`       | 1 min  | dashboard tiles                                                      |
| `report`     | 5 min  | completed report payloads                                            |

These are the active server-side readers in `core/deps.py`, product services
and report services. `CacheNamespace.PERMISSIONS` remains an invalidation
compatibility namespace; permissions are currently resolved from database
roles and direct grants/denials on every request. General master/list caching
is not implemented on the server. The frontend has its own cache policy below.

**Never cached:** inventory availability, reservation state, and anything an
allocation decision reads. A stale available-quantity that lets two orders
reserve the same stock is a correctness bug, not a performance win. Cache the
item master around it instead.

Invalidation is explicit on write. `SCAN`, never `KEYS` — `KEYS` blocks the Redis
event loop for the whole scan and one call can stall every request in the system.

Cache failures degrade to a miss. A Redis outage must never become a 500.

---

## Background work

Work that can outlive a request does not run in one:

- MRP runs above the configured demand-line threshold
- Excel and PDF generation
- report exports
- inventory valuation across the whole company
- notification fan-out
- scheduled summary refresh and stale-reservation release

```
POST /reports/…  →  202 {job_id, status_url}
                 →  Celery worker generates, stores, records a document row
                 ←  client polls; UI shows Generating… → Download
```

Queues are separated by shape (`mrp`, `reports`, `notifications`, `default`).
The batch worker consumes `default,mrp,reports`; `celery-notify` consumes only
`notifications`, so a long planning run cannot occupy a notification child.
The host-run `dev-worker` target runs the batch queues; full Docker development
also starts the dedicated notification worker. Imports are not a general Celery
pipeline in the current implementation.

---

## Reporting

The dashboard reads stock value from `inventory_summary`, refreshed every 15
minutes by `maintenance.refresh_summary_views`. Its other operational tiles
use indexed document counts, assembled in one statement. The response is cached
for 60 seconds and includes the stock-summary timestamp. Other reports use their
own bounded queries and cache policy; live availability and valuation reads are
never served from the report cache.

Escalation order — do not skip steps:

1. correct SQL
2. correct indexes
3. summary tables
4. materialised views
5. Redis
6. Celery-generated artefacts
7. ClickHouse, only when measured analytical volume proves the need

"The project is large" is not evidence.

---

## Frontend

The UI must feel immediate even when the backend is doing real work.

- server-side pagination, filtering, sorting and search — the browser never
  receives a whole table;
- search debounced (`SEARCH_DEBOUNCE_MS` in `lib/constants.ts`, 300 ms), so a
  request does not follow every keystroke;
- `keepPreviousData`, so the table does not collapse to a skeleton on page change;
- route-level code splitting — a store keeper never downloads the finance bundle;
- vendor chunks split from app code so a deploy does not invalidate everything;
- route chunks warmed on intent: hovering, focusing or touching a sidebar link
  starts that page's download (`app/route-loaders.ts`), and the login page warms
  the dashboard while the password is being typed, so the first authenticated
  paint comes from cache;
- bundle budgets enforced at build time (`scripts/check-bundle-budget.mjs`):
  the gzipped entry, each vendor chunk, every route chunk and the total first
  load are ratchets that fail `npm run build` when a heavy import lands on the
  first-paint path;
- row virtualisation on wide report tables (opt-in: on a 50-row page it costs
  more than it saves);
- skeletons shaped like the content they replace, so nothing jumps on load;
- TanStack Query cache tuned per domain — reference data for 30 minutes,
  inventory availability never.

---

## Measuring

**Application.** `core/observability.py` registers HTTP request counts and
latency by route template, Celery task counts/duration, inventory lock conflicts,
insufficient-stock refusals, MRP counts/duration, queue depth and responding
worker count. The container entrypoint prepares a private multiprocess metrics
directory before starting the process. `/metrics` aggregates all Gunicorn
workers in the backend container; each Celery worker container exposes its
aggregated task metrics on port `9100`.

Scrape these targets from a monitor attached to the deployment's private
Compose network:

| Target | Metrics |
| --- | --- |
| `backend:8000/metrics` | API request/business metrics and broker-backed queue status |
| `celery-worker:9100/metrics` | Batch worker task counts and durations |
| `celery-notify:9100/metrics` | Notification worker task counts and durations |

Worker exporter ports are internal only; Compose publishes no additional host
ports. Give each target a distinct Prometheus `job` label. Queue gauges are
updated by the backend target; `atcraft_queue_monitoring_up` is `0` when the
broker or worker probe cannot complete, `atcraft_celery_workers_online` is `0`
when no worker answers, and `atcraft_celery_queue_depth` reports ready tasks
waiting in each queue. Redis list depth excludes tasks already reserved or
running and ETA-scheduled tasks, so alert on sustained backlog together with
worker availability rather than interpreting it as total outstanding work.

The multiprocess collector aggregates counters and histograms written by API
and Celery child processes within each container. The entrypoint clears only
that container's temporary metrics files on container start; process restarts
within a running container remain part of its cumulative counters.

Database query duration, cache hit ratio and a dedicated negative-balance
counter are not currently exported. Add instrumentation and collection together
before building alerts on them. Optional OpenTelemetry instruments FastAPI,
SQLAlchemy and Redis when the `otel` dependency extra and collector are
configured; this is separate from Prometheus collection.

Labels use the route _template_ (`/items/{item_id}`), never the concrete path.
Raw paths create one time series per record and will eventually take Prometheus
down.

**Database.** `pg_stat_statements` is enabled; `log_min_duration_statement` is
200 ms in development and 500 ms in production. A `statement_timeout` of 30 s and
`idle_in_transaction_session_timeout` of 60 s stop a runaway query pinning a
backend.

**Requests.** Anything over 1 000 ms is logged as `request.slow` with its
correlation id, so a slow endpoint surfaces before a user complains.

---

## When something is slow

Do not change the framework. Do not add infrastructure. Find the bottleneck:

```
frontend → network → API → business logic → Redis → PostgreSQL → SQL → index → disk
```

Then, for the query:

1. reproduce it
2. measure it
3. `EXPLAIN (ANALYZE, BUFFERS)`
4. check joins, row estimates versus actuals, index usage, sorts
5. check for N+1
6. reduce selected columns
7. add or fix an index — only if justified
8. consider a summary table
9. consider Redis, if the result is safely cacheable
10. consider Celery, if it is inherently expensive
11. re-measure
12. record the result if the change was significant

Every optimisation is measured. An unmeasured optimisation is a guess with extra
steps.
