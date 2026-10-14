# Architecture

## Shape: modular monolith

One FastAPI application, one PostgreSQL database, modules with real boundaries.

This is a deliberate choice, not a stepping stone we are embarrassed about. The
ERP's modules are densely coupled by nature — a sales order touches inventory,
BOM, allocation, MRP, procurement, production, QC, dispatch and finance before it
is done. Splitting that across services buys network calls, distributed
transactions, eventual-consistency bugs and a debugging story nobody wants, in
exchange for scaling properties this workload does not yet need.

The boundaries are still real: each module owns its tables, exposes a service
API, and could be lifted out later if measured load ever justifies it.

```
Users
  |
Nginx ── TLS · rate limit · security headers · compression
  |
  ├── /        → React SPA (static, content-hashed)
  └── /api/v1  → FastAPI
                    |
        ┌───────────┼───────────┐
        │           │           │
   PostgreSQL     Redis      Celery
   source of      cache      workers
   truth          + broker      │
        │                       │
        └───────────┬───────────┘
                    │
              File storage
```

## Layers

```
HTTP request
   │
   ▼
middleware      request id · access log · security headers · body limit · rate limit
                (pure ASGI, never BaseHTTPMiddleware — see below)
   │
   ▼
router          authorise, validate, delegate, return the envelope — and nothing else
   │
   ▼
service         business rules · transaction boundary · audit · cross-module calls
   │
   ▼
repository      queries, always tenant-scoped, pagination, no business decisions
   │
   ▼
models          SQLAlchemy: constraints and indexes that make bad states unrepresentable
   │
   ▼
PostgreSQL
```

### Middleware is pure ASGI

Every middleware in `src/core/middleware.py` implements the ASGI callable
directly rather than subclassing Starlette's `BaseHTTPMiddleware`.

`BaseHTTPMiddleware` is the comfortable spelling — it hands you a `Request` and
a `call_next` — and it pays for that by running the rest of the application in a
separate anyio task wired through a pair of memory object streams, for every
request. Measured on a minimal app: 16.6 µs/request bare, 1171 µs with five
no-op `BaseHTTPMiddleware` layers, 19.8 µs with five no-op pure-ASGI layers.
That is 231 µs per layer against 0.6 µs, and this application had five of them —
about 1.15 ms of overhead on every request before any application code ran.

The pure-ASGI form also makes one thing possible that the other does not.
`call_next` returns when the `http.response.start` message arrives — when the
*headers* are settled — so anything timing a request around it is measuring
time-to-first-byte. For JSON the difference is invisible; for the streaming
download route it was the entire transfer, so the access log and the
`atcraft_http_request_duration` histogram both reported the slowest route in the
ERP as the fastest. Wrapping `send` instead lets the timing run to the final
body message, which is what `is_final_body_message` is for.

Cost of the choice: a middleware here reads and writes ASGI messages rather than
request and response objects. That is the trade, and it is only worth making on
the layers every single request passes through.


Two rules keep this honest:

- A route that contains business logic is a review rejection.
- A repository that decides whether something is *allowed* belongs in a service.

### Where a transaction begins

Services own transactions, not routes and not repositories:

```python
async with transaction(session):
    order = await create_production_order(...)
    await reserve_material(...)
    await record_audit(...)
# one COMMIT, or one ROLLBACK — never a partial business state
```

`transaction()` nests safely, so a service can be called standalone or as part of
a larger operation without changing.

## Core (`src/core`)

Cross-cutting concerns every module depends on and none of them owns.

| Module | Responsibility |
|---|---|
| `config` | typed settings from the environment; refuses weak production secrets |
| `database` | async engine, bounded pool, session lifecycle, advisory locks |
| `models` | declarative base, mixins, constraint naming convention |
| `types` | `Money`, `UnitPrice`, `Quantity`, `GUID` — the numeric contract |
| `ids` | UUIDv7 |
| `clock` | one source of "now", always UTC, freezable in tests |
| `security` | Argon2id hashing, JWT issue/verify, password policy |
| `permissions` | the permission registry and baseline roles |
| `deps` | `Principal`, `TenantContext`, `require_permission` |
| `errors` | the exception hierarchy and stable error codes |
| `messages` | user-facing sentences raised from more than one module, so wording is decided once |
| `constants` | schema-level decisions shared by models and schemas (default currency, code widths) |
| `responses` | the response envelope |
| `pagination` | offset and keyset pagination |
| `cache` | tenant-scoped Redis cache-aside with explicit invalidation |
| `ratelimit` | sliding-window limits keyed on account, not just IP |
| `state_machine` | declarative transition graphs |
| `logging` | structured JSON logs with request-scoped context |
| `observability` | Prometheus metrics, OpenTelemetry, Sentry |
| `registry` | imports every module's models so Alembic sees the whole schema |

## Modules

Each module is a package:

```
src/modules/<name>/
  models.py      tables, constraints, indexes
  schemas.py     Pydantic request/response contracts
  repository.py  queries (extends BaseRepository — tenant scope is structural)
  service.py     business rules, transactions, audit
  router.py      an APIRouter named `router`, with its own prefix and tags
  engine.py      only where there is a real algorithm (BOM explosion, MRP)
```

`src/api/router.py` mounts them in a fixed order so the OpenAPI document groups
the way the business reads.

### Dependency direction

```
masters      warehouses · products · vendors · customers · organization · users
   ↓
engines      inventory  ·  bom  ·  routing
   ↓
demand       allocation · sales
   ↓
planning     mrp · procurement
   ↓
execution    production · assembly · job_work
   ↓
downstream   quality · packing · dispatch · finance · reports
   ↓
portals      vendor_portal · dealer_portal
```

Dependencies point downward only. A master-data module must never import from an
execution module; where it needs a fact from one (vendor rating needs QC
results), it takes it as an input rather than reaching upward. Where an upper
module needs a lower one's answer at run time - procurement raising or counting
an incoming inspection - it resolves it through a guarded import
(`resolve_quality_port`, `resolve_open_inspection_counter`) rather than importing
the module's types. `audit`, `notifications` and `documents` are services every
layer may call.

`vendor_portal` (WP C1a) owns no tables. It is the vendor's whole surface: a
facade that calls procurement's state machine and consignment rules, finance's
bill and payment rules, quality's inspection reads and the documents store, with
the vendor taken from the verified token, and returns allow-listed read models.
A vendor account cannot reach any other module: `core.deps` refuses every route
outside `/vendor-portal/**`, `/notifications/**` and auth self-service for a
principal with a `vendor_id` (docs/security.md section 4).

`dealer_portal` (WP C3a) sits downstream of sales and owns four tables:
`dealer_offers` (percentage schemes, also used as ERP sales-order defaults),
`dealer_carts` and `dealer_cart_lines` (quantities only - prices are read
live), and `dealer_wishlist_items`. It is the dealer's whole surface:
catalogue with channel pricing and availability bands, cart, wishlist,
checkout to a DRAFT sales order, orders, dealer-safe tracking, statement and
outstanding - a facade over sales, dispatch and finance with the customer
taken from the verified token, returning allow-listed read models. Sales
reaches the scheme matcher through a deferred import (the allocation seam
pattern), because the portal imports sales for checkout and a top-level import
would cycle. A dealer account cannot reach any other module: `core.deps`
refuses every route outside `/dealer-portal/**`, `/notifications/**` and auth
self-service for a principal with a `customer_id` (docs/security.md, Dealer
portal isolation).

## The two data patterns that matter

### Ledger + current state

```
inventory_transactions   append-only, explains WHY the balance changed
inventory_balances       current state, optimised for reads
```

The ledger gives auditability, traceability and historical reconstruction. The
balance gives the sub-100ms reads that allocation checks and dashboards need.
Neither can be dropped: summing the ledger per request does not scale, and a
balance table alone cannot answer "why is this number what it is?".

The same shape appears in `ledger_entries` (finance) and `audit_logs`.

### Snapshot at commitment

A production order snapshots its exploded BOM into
`production_order_components` when it is released. A revised BOM must not
retroactively change what an in-flight order requires. The same reasoning governs
price capture on order lines and cost capture on inventory movements.

## Concurrency

The one scenario the master rules single out:

> Available = 100. Order A requests 80, order B requests 50.
> The system must never allow both.

The answer is PostgreSQL, not application code:

1. `SET LOCAL lock_timeout` — fail fast rather than queueing behind a lock.
2. `SELECT ... FOR UPDATE` on the affected balance rows, **ordered by primary
   key** so two multi-line documents cannot deadlock each other.
3. Re-read quantities *after* the lock. A value read before it is worthless.
4. Validate, write the ledger, update the balance, write the audit row.
5. One COMMIT.

Plus a defence that does not depend on anyone remembering: `CHECK` constraints on
`inventory_balances` enforcing `on_hand >= 0`, `reserved <= on_hand` and
`allocated <= reserved`, and `available_quantity` as a generated column that
cannot drift from its inputs.

A Redis lock is not a substitute. It cannot make a stock update atomic; it can
only decide who *tries*.

## Background work

Anything that can outlive a request goes to Celery: MRP runs above the
configured demand-line threshold, exports, PDF and Excel generation, bulk
imports, notification fan-out, and the scheduled jobs that refresh summary tables
and release stale reservations.

```
POST /mrp/runs
   → create the run row (QUEUED)
   → dispatch to Celery
   → 202 with a job id and a status URL
   ← client polls; the UI shows Generating… → Completed → Download
```

Queues are separated by shape — `mrp`, `reports`, `notifications`, `default` —
so a four-minute planning run never sits in front of a 200 ms notification.

Web Push shares the notifications queue. Business transactions stage email and
push tasks until the outer commit; nested SAVEPOINT commits do not publish.
Workers claim under a notification lock, close the transaction before transport,
then update devices in a second transaction. If every device needs retry, a
compare-and-release UPDATE makes the notification available again. Permanent
failures keep the claim; GONE removes a device. Collapse preserves delivery
markers and never re-sends an already-delivered unread notification. A 120-second
sweep requeues up to 100 eligible unclaimed email/push rows less than 24 hours old,
recovering broker outages. It filters inactive users, disabled channels, and push
recipients without a recent session or an unexpired device before limiting the
batch, so abandoned devices cannot block later deliveries. Claimed rows intentionally
retain the existing at-most-once crash
window: a process killed after claiming may require operator replay.

Auth and user-admin repositories call the notification device cleanup helper in
their revocation transaction. Quality's inspection pull facade accepts a batch
of receipt references so portal receipt pages avoid N+1 reads.

## Request lifecycle

```
Nginx mints X-Request-ID (or honours an inbound one)
   → RequestContextMiddleware binds it to the execution context
   → every log line, audit row and Celery task carries it
   → the response returns it in X-Request-ID
```

One identifier spans nginx, the API, the database logs and the worker. A support
ticket quoting it finds the whole story.

## Frontend

```
src/app/         shell: layout, routing, navigation, route-loaders (the one list of lazy pages)
src/components/  design system + DataTable + shared building blocks (PageHeader, DateRangeFilter, ConfirmDialog)
src/features/    one folder per business area
src/lib/         api-client · query-client · format · permissions · constants · messages
src/hooks/       useListQuery and friends
```

Server state lives in TanStack Query and is not duplicated into a global store.
List state (page, size, sort, search, filters) lives in the URL, so a filtered
view is shareable and survives the back button.

`lib/api-client.ts` is the only place that calls `fetch`. Token refresh, the
response envelope, error shaping and the correlation id are handled there once.

`lib/constants.ts` holds every value that used to be repeated across feature
folders - page sizes, the search debounce, browser-storage keys, date formats -
and is the only place `import.meta.env` is read. `lib/messages.ts` holds the
shared error headlines, empty-state sentences, confirmation prompts and the
success builders for the standard verbs (`success.created('Purchase order')`);
a confirmation whose wording is specific to one action ("Carton sealed") stays
with its screen. Messages the API composes are shown verbatim, because the
server knows why it refused. `app/route-loaders.ts` is the single list of lazily-loaded
pages: `React.lazy` in the route table and the sidebar's hover prefetch both
read it, so a chunk warmed on hover is the one the click resolves.

Light, dark and system. The palette is entirely CSS custom properties in
`styles/globals.css` - a `:root` set and a `.dark` set - and Tailwind is
configured `darkMode: ['class']`, so a screen never writes a `dark:` variant and
never names a colour: it names a token and the theme swaps underneath. That is
why the whole ERP themes correctly with no per-screen work, charts included
(`fill: 'hsl(var(--primary))'`). The few literal `text-white` / `bg-black/50`
uses are deliberate - the sidebar is dark in both themes, and a modal scrim is
black in both.

`lib/theme.ts` owns the decision and is shared with `main.tsx`, which paints
before React mounts so a dark operator never sees a white flash while
`/auth/me` is in flight; two implementations of "which theme is this" would
otherwise change colour a moment after the page loads. `app/theme-context.tsx`
owns it from then on, and it is mounted at the root rather than inside
`AppLayout` so the login page - the one screen you see before signing in - can
change theme too.

The preference has three states, not two, and `system` is the default. It is
stored by *removing* the key, so an absent preference and an explicit "follow
the system" are one state rather than two spellings of one intent. Storage is
written only when a person chooses: persisting from a mount effect, which is
what the first implementation did, silently opted every operator out of
following their OS on their first visit and left no way back. While the
preference is `system` the provider subscribes to `prefers-color-scheme`, so a
laptop switching to dark at sunset is followed; while it is pinned it does not
subscribe at all.

Every screen is responsive down to a 360px phone. The shared primitives carry
the safety nets - `DialogContent` scrolls its own overflow, `TabsList` wraps,
`PageHeader` actions wrap, `DataTable` scrolls horizontally inside a `min-w-0`
main - so a feature page cannot forget them; toolbars stack their controls with
`flex-wrap` and `w-full sm:w-[..]` widths, and the sidebar's "collapsed"
preference only applies at `lg`, where the control that undoes it exists.

`Select` carries a safety net of the same kind. Inside a `<form>`, Radix keeps a
hidden native `<select>` and re-dispatches every change of the controlled value
through it; when the new value's `<option>` has not rendered yet - a form reset
to a saved record while its picker's list is still loading - the native element
can only hold `""`, and that arrived as `onValueChange('')`. Every form wired to
it cleared the field: an amended job-work order lost its warehouse, a document
its cost centre, and the next save wrote the loss. `""` is never a choice a
person can make - Radix refuses an item whose value is empty, which is why every
"none" option is a sentinel - so `components/ui/select.tsx` drops it, once, for
every select (`select.test.tsx`).

Permissions in the UI control affordances only. The API authorises every request
independently — anyone can edit the browser's idea of their permissions, and the
server does not care.

## What we deliberately did not build

| Not present | Why | When to revisit |
|---|---|---|
| Microservices | modules are too coupled; the overhead buys nothing yet | organisational scale, or one module genuinely outgrowing the box |
| Kafka | Celery + PostgreSQL cover every current async need | cross-system event streaming with real replay requirements |
| Elasticsearch | `pg_trgm` handles item and partner search | demonstrated relevance/fuzzy needs beyond PostgreSQL |
| ClickHouse | summary tables and materialised views are enough | measured analytical volume, not "the project is large" |
| Kubernetes | one Compose stack on one server fits the load | multi-node, real HA requirements |
| Event sourcing | the ledger already gives history where it matters | — |

Scale in this order: SQL and indexes → Redis → Celery → bigger server → separate
database host → read replicas → split the genuinely hot component. Only then
consider anything above.
