# Performance

Master rules sections 30, 57–65, 85, 86.

The framework is not what makes an application fast. What makes it fast is doing
less work: the right indexes, the right amount of data, expensive things moved
off the request path, and measurement instead of guessing.

---

## Targets

Engineering targets, not guarantees. Finalise them against real Atcraft data and
a load test.

| Path | Target |
|---|---|
| Normal API request | < 200 ms |
| Cache-served request | < 100 ms |
| Simple indexed query | < 100 ms |
| List endpoint (50 rows, filtered) | < 300 ms |
| Dashboard (summary tables) | < 500 ms |
| Large report | background job |
| Large MRP run | background job |

Heavy planning and reporting work is explicitly exempt. Forcing a genuine
five-minute calculation into a 200 ms budget produces a wrong answer quickly,
which is worse than a right answer slowly.

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

The indexes that carry the system:

```sql
-- current stock: the most-read table in the application
(company_id, item_id, warehouse_id)                          -- availability check
(company_id, warehouse_id, available_quantity)               -- "what can I promise?"

-- ledger: append-heavy, read by history and by cursor
(company_id, item_id, warehouse_id, occurred_at DESC)        -- item movement history
(company_id, occurred_at DESC, id DESC)                      -- keyset paging
(company_id, reference_type, reference_id)                   -- "what did this GRN post?"

-- reservations: read on every allocation decision
(company_id, item_id, warehouse_id, status)
(company_id, demand_type, demand_id)

-- documents: the list screens people live in
(company_id, status, created_at DESC)                        -- per document table
(company_id, vendor_id, status)                              -- purchase orders
(company_id, customer_id, status)                            -- sales orders

-- audit: unbounded, cursor-paginated
(company_id, created_at DESC, id DESC)
(company_id, entity_type, entity_id, created_at DESC)

-- search, without Elasticsearch
GIN (code gin_trgm_ops), GIN (name gin_trgm_ops)             -- items, partners
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

Reconciliation between the two runs as a scheduled job, not on demand.

---

## Caching

Redis is a cache. PostgreSQL is the truth. Every entry has a TTL, and keys are
tenant-scoped (`atcraft:{company_id}:{namespace}:{key}`) so cross-tenant bleed is
structurally impossible.

| Namespace | TTL | Contents |
|---|---|---|
| `ref` | 60 min | UoM, categories, warehouse types |
| `master` | 10 min | items, partners, warehouses |
| `perm` | 5 min | resolved permission sets — explicitly invalidated on role change |
| `list` | 2 min | filtered list pages |
| `dash` | 1 min | dashboard tiles |
| `report` | 5 min | completed report payloads |

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
- bulk import and export
- inventory valuation across the whole company
- notification fan-out
- scheduled summary refresh and stale-reservation release

```
POST /reports/…  →  202 {job_id, status_url}
                 →  Celery worker generates, stores, records a document row
                 ←  client polls; UI shows Generating… → Download
```

Queues are separated by shape (`mrp`, `reports`, `notifications`, `default`) so a
long planning run never blocks a short notification.

---

## Reporting

Dashboards read summary tables, not hot transactional tables. `inventory_summary`
is refreshed every 15 minutes by a beat task; the dashboard endpoint assembles
its tiles from a small number of indexed queries and is cached for 60 seconds.

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

**Application.** Prometheus metrics on `/metrics`: request latency by route
template, database query duration, cache hit ratio, Celery queue depth and task
duration, plus the business signals that mean the ERP is producing wrong answers
— lock conflicts, insufficient-stock refusals, blocked negative-balance writes.

Labels use the route *template* (`/items/{item_id}`), never the concrete path.
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
