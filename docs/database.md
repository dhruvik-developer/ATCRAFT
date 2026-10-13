# Database & Module Contract

**Status:** binding. Every backend module is implemented against this document.
It exists so modules built in parallel agree on table names, foreign-key targets
and cross-module service signatures without reading each other's source.

This document records the repository's database contract. Where it and
`CLAUDE.md` disagree, `CLAUDE.md` wins.

---

## 1. Conventions

| Concern | Rule |
|---|---|
| Table names | `snake_case`, plural (`purchase_orders`, `inventory_transactions`) |
| Primary key | `id UUID` (UUIDv7 via `src.core.ids.new_id`) |
| Tenant column | `company_id UUID NOT NULL` on every table except `companies` |
| Money | `Numeric(18,4)` — `src.core.types.Money`. Never float |
| Unit price | `Numeric(18,6)` — `UnitPrice` |
| Quantity | `Numeric(18,4)` — `Quantity` |
| Percent / rate | `Numeric(9,4)` — `Percentage` |
| Timestamps | `TIMESTAMPTZ`, UTC, via `TimestampMixin` |
| Status columns | `String(24)`, values from a `StrEnum`, validated by a `StateMachine` |
| Business codes | unique **per company**: `UniqueConstraint("company_id", "code")` |
| Document numbers | `src.common.codes.next_document_number` — never `MAX(n)+1` |
| Soft delete | master data only. Never on ledger, audit, or posted financial rows |

### Base classes (`src.core.models`)

```
TenantEntity        id, company_id, created_at, updated_at, created_by_id, updated_by_id
TenantMasterEntity  TenantEntity + deleted_at            -> master data
ImmutableLedgerEntity  id, company_id, created_at, created_by_id  -> append-only history
```

Add `OptimisticLockMixin` to documents several users edit concurrently
(BOM, purchase order, sales order). Inventory relies on row locks instead.

### Relationship loading

Every `relationship()` is `lazy="raise"` unless a specific endpoint needs it
eagerly, in which case use `lazy="selectin"` **and** say why in a comment.
This makes an accidental N+1 a loud error in development rather than a silent
100-query page in production (master rules §32).

---

## 2. Module map and table ownership

| Module | Tables |
|---|---|
| `organization` | `companies`, `branches`, `plants`, `departments`, `cost_centres`, `financial_years` |
| `users` | `users`, `roles`, `user_roles`, `user_sessions`, `password_reset_tokens`, `login_attempts` |
| `audit` | `audit_logs`, `approval_requests` |
| `warehouses` | `warehouses`, `storage_locations` |
| `products` | `item_categories`, `units_of_measure`, `uom_conversions`, `item_attributes`, `manufacturers`, `brands`, `items`, `item_variants`, `item_prices`, `item_vendor_links` |
| `vendors` | `vendors`, `vendor_contacts`, `vendor_categories`, `transporters` |
| `customers` | `customers`, `customer_addresses`, `customer_contacts` |
| `inventory` | `inventory_transactions`, `inventory_balances`, `inventory_cost_layers`, `inventory_cost_consumptions`, `stock_batches`, `stock_serials`, `stock_transfers`, `stock_transfer_lines`, `stock_adjustments`, `stock_adjustment_lines`, `physical_counts`, `physical_count_lines` |
| `allocation` | `stock_reservations`, `reallocation_requests` |
| `bom` | `boms`, `bom_lines`, `bom_operations` |
| `routing` | `work_centers`, `machines`, `routings`, `routing_operations` |
| `sales` | `sales_orders`, `sales_order_lines` |
| `mrp` | `mrp_runs`, `mrp_demands`, `mrp_requirements`, `mrp_suggestions` |
| `procurement` | `purchase_requisitions`, `purchase_requisition_lines`, `rfqs`, `rfq_lines`, `vendor_quotations`, `vendor_quotation_lines`, `purchase_orders`, `purchase_order_lines`, `goods_receipts`, `goods_receipt_lines`, `purchase_returns`, `purchase_return_lines` |
| `production` | `production_orders`, `production_order_components`, `work_orders`, `material_issues`, `material_issue_lines`, `production_entries` |
| `assembly` | `assembly_orders`, `assembly_order_components` |
| `job_work` | `job_work_orders`, `job_work_order_lines`, `job_work_challans`, `job_work_challan_lines`, `job_work_receipts`, `job_work_receipt_lines` |
| `quality` | `qc_templates`, `qc_template_parameters`, `qc_inspections`, `qc_inspection_results` |
| `packing` | `packing_orders`, `packing_order_lines`, `cartons`, `carton_contents` |
| `dispatch` | `dispatches`, `dispatch_lines`, `dispatch_tracking_events` |
| `finance` | `ledger_accounts`, `ledger_entries`, `vendor_invoices`, `vendor_invoice_lines`, `payments`, `payment_allocations` |
| `reports` | `report_snapshots`, `inventory_summary` |
| `documents` | `documents` |
| `notifications` | `notifications`, `notification_preferences` |
| `common` | `document_sequences` |

### Cost centres

`cost_centres` (organisation, `TenantMasterEntity`) is where money is charged
for management reporting: `code` (unique per company, retired codes included),
`name`, `parent_id` (a tree, at most `MAX_COST_CENTRE_DEPTH` = 6 levels counted
to the deepest descendant), `cost_centre_type`, `plant_id`, `head_user_id`,
`description`, `is_active`. It sits at the bottom of the dependency order, so
every module may reference it.

`cost_centre_type` (`PRODUCTION | SERVICE | ADMINISTRATION | SELLING`) is a
**reporting classification only**, the same rule as `items.item_group`: no rule
may branch on it.

Carriers - each a nullable `cost_centre_id`, FK `RESTRICT`, indexed:

| Table | What the centre means there |
|---|---|
| `departments` | the default for documents the department's people raise (used from WP B3) |
| `work_centers` | the cost pool the centre's machine and labour hours belong to |
| `production_orders` | the job's centre; **material issues carry none** - they reach it through `production_order_id` |
| `job_work_orders` | the centre the processing charges belong to |
| `stock_adjustments` | who bears the write-off; its `inventory_transactions` reach it through `reference_type` / `reference_id` (the movement table and `post_movement` carry no centre) |
| `vendor_invoices` | the bill's centre, and the default for its lines |
| `vendor_invoice_lines` | the line's own centre; NULL means "the invoice's" |
| `ledger_entries` | **the analytic source of truth**, set only by `post_journal` |

Rules (`docs/assumptions.md` §41 to §46):

* **NULL is "unallocated"**, never "the default centre". Nothing is backfilled.
* **Precedence is explicit value -> source document -> NULL.** A vendor invoice
  line resolves line -> header -> NULL at approval. Production and job-work
  orders take only an explicit value: their sources (MRP, a sales order, a
  production order behind a job-work order) carry none this release.
* **Every write validates through the organisation seam** -
  `require_cost_centre` / `require_cost_centres` (tenant, live, active) - because
  a foreign key to a UUID is satisfied by another company's row. A foreign id is
  404. Only a *new* assignment is checked: a document re-saving the centre it
  already holds keeps its reference after deactivation.
* **Only INCOME and EXPENSE ledger legs may carry a centre** (`post_journal`
  refuses any other, naming the leg). A new leg needs the centre active; all of
  a journal's centres are validated in one statement.
* **A reversal copies each leg's centre** and is exempt from the "active" check,
  so retiring a centre never makes its journals irreversible.
* **Approving a vendor invoice splits `PURCHASE_EXPENSE` per effective line
  centre** - one leg per centre, in line order, summing exactly to the subtotal
  (`split_by_cost_centre`). An invoice naming no centre posts the single
  unallocated leg it always did.
* **Two retirement levels.** Deactivating is refused while an active child, a
  department default or a work centre still hands the centre out. Archiving is
  refused while anything at all names it - a live child, a department, a work
  centre or any document row whatever its status - except `ledger_entries`:
  posted history keeps its centre. The archive guard probes tables above
  organisation with raw SQL (`COST_CENTRE_REFERENCE_PROBES`); a module adding a
  carrier adds it there, and `test_every_table_that_carries_a_cost_centre_is_probed`
  fails until it does.

The cost-centre statement (`/reports/cost-centre-expense`, report key
`COST_CENTRE_EXPENSE`) sums posted legs per (centre, account) over a period from
`ledger_entries`, through the partial index
`(company_id, cost_centre_id, entry_date) WHERE cost_centre_id IS NOT NULL`.

---

## 3. Item master — one item, many roles

Master rules §1: a single item may be sold, purchased, manufactured, used in a
BOM, used in assembly, used as a spare, sent for job work, returned, or scrapped.
The model must never make one of those exclusive.

`items` therefore carries **independent boolean capability flags**, not a single
`item_type` discriminator:

```
can_purchase, can_manufacture, can_sell, can_be_bom_component,
can_be_assembly_component, can_be_spare, can_job_work, can_return, can_scrap
```

`item_group` (`RAW_MATERIAL | COMPONENT | SEMI_FINISHED | FINISHED_GOOD |
CONSUMABLE | PACKING_MATERIAL | SPARE | SCRAP | SERVICE`) is a **reporting and
default-behaviour classification only**. No business rule may read it to decide
whether an operation is permitted — read the capability flag instead.

### Item attributes

`item_attributes` holds the permitted finishes, sizes and colours, one row per
value, discriminated by an `attribute` column (`FINISH | SIZE | COLOUR`) and
unique per `(company_id, attribute, code)` — a finish and a colour may both be
called BLACK. `items` and `item_variants` reference it by
`finish_id` / `size_id` / `colour_id`.

These were free text until `4c1d70b8ea92`, which is why the table exists:
nothing branched on the values, but nothing governed them either, so "Brushed",
"brushed" and "Brushed Steel" were three different finishes and no report could
group by finish.

Three rules follow from it being a client-editable list:

* **Nothing may branch on a value.** That property is what makes the list safe to
  hand to an administrator, and it is the reason this is a master table rather
  than an enum. A rule that reads a finish by name belongs in code, and the
  finish then belongs in an enum.
* **The kind cannot change after creation.** `ItemAttributeUpdate` does not carry
  `attribute`; moving a value between kinds would retarget every item pointing at
  it. Renaming, by contrast, is free and is the point.
* **Assignment is validated for tenant and kind, not only for existence.** A
  foreign key to a globally unique UUID is satisfied by another company's row,
  and by a colour's id in `finish_id`. `_require_attribute` in the products
  service reads through the tenant-scoped repository and checks the kind; see
  `test_a_colour_is_refused_where_a_finish_belongs`.

Retirement is the `is_active` flag and is refused while an item or a variant
still carries the value — otherwise an item would display a value its own edit
form could no longer offer, and saving that form would silently drop it. There
is no delete.

### Brands and manufacturers

`manufacturers` records who makes a part (`code`, `name`, `legal_name`,
`country`, `website`, `notes`); `brands` records the name a product is sold
under (`code`, `name`, `description`, `sort_order`) and, optionally, the one
manufacturer that owns it (`brands.manufacturer_id`). Both are
`TenantMasterEntity` tables, unique per `(company_id, code)`, with a partial
picker index over live, active rows. An item carries `brand_id`,
`manufacturer_id` and a free-text `manufacturer_part_number`; an
`item_vendor_links` row carries `manufacturer_id` and `manufacturer_part_number`
for the maker whose part that source supplies. A manufacturer is deliberately
**not** a vendor: a maker's part is routinely bought through a distributor, and
a vendor carries payables, approval and a portal login that a maker does not.

**The rule: nothing may branch on a brand or a manufacturer value.** They are
client-editable classification and traceability masters. A screen or a report
may *filter* or *group* by them — `ix_items_company_brand` exists for the item
list's brand filter and a future dealer catalogue — but no price, discount,
permission, posting, approval or stock rule may read one to decide anything. A
rule that needs a brand belongs in code, and the brand then belongs in an enum.

What the products service does enforce:

* **Assignment is validated for tenant and status.** `_require_brand` and
  `_require_manufacturer` read through the tenant-scoped repositories on item
  create and update, on a brand's owner and on a vendor link, so another
  company's id is "not found" and an inactive or retired value is refused on the
  way in. (Item creation now validates `finish_id` / `size_id` / `colour_id` the
  same way; before this it did not.)
* **Lifecycle.** `BRAND_LIFECYCLE` and `MANUFACTURER_LIFECYCLE` are the
  organisation masters' machine — ACTIVE ↔ INACTIVE, ARCHIVED (soft-deleted)
  terminal. Every `is_active` change goes through `assert_transition` and
  `record_status_change`; retirement (`DELETE`, with a reason) is audited as a
  DELETE status change.
* **Refused while in use** (`REFERENCED_RECORD_IN_USE`). A brand cannot be
  deactivated or retired while a live item carries it; a manufacturer, while a
  live item, a live item-vendor link or a live brand names it. "Live" is
  `deleted_at IS NULL` — an inactive item still shows its brand.
* **Assignment and retirement are serialised on the master row.** Both are
  check-then-act, and the foreign key cannot referee them: its check takes
  `FOR KEY SHARE`, which an `UPDATE` of `is_active` or `deleted_at` does not
  conflict with. So an assignment reads the brand or maker `FOR SHARE` and a
  deactivation or retirement reads it `FOR NO KEY UPDATE`; whichever arrives
  second waits, then decides on the first one's committed result. Without that,
  a deactivation's "still in use?" probe cannot see an item whose INSERT has not
  committed, and the item lands carrying a value nothing offers
  (`tests/integration/test_brand_manufacturer_concurrency.py`).
* **Codes are never reused**, retired rows included (`docs/assumptions.md`
  §39).

Other required columns: `code`, `name`, `category_id`, `base_uom_id`,
`purchase_uom_id`, `sale_uom_id`, `hsn_code`, `gst_rate`, `standard_cost`,
`sale_price`, `dealer_price`, `finish_id`, `size_id`, `colour_id`, `brand_id`,
`manufacturer_id`, `manufacturer_part_number`, `production_type`
(`MAKE | BUY | MAKE_OR_BUY | JOB_WORK`), `lead_time_days`, `reorder_level`,
`reorder_quantity`, `min_order_quantity`, `safety_stock`, `shelf_life_days`,
`is_batch_tracked`, `is_serial_tracked`, `default_warehouse_id`, `is_active`.

There is deliberately **no `valuation_method` column**. Stock is valued **FIFO**,
for every item, by `inventory.service` against `inventory_cost_layers` — see
`docs/assumptions.md` section 31. The column existed briefly as
`FIFO | WEIGHTED_AVERAGE | STANDARD`, defaulting to `WEIGHTED_AVERAGE`, while the
engine costed everything FIFO regardless: it described a choice nothing honoured,
and for most rows it described the wrong one. It was dropped in revision
`7e8eb4bdd0ed`. If per-item valuation is ever wanted, the column returns
**together with** the costing dispatch that reads it, never before.

---

## 4. Inventory engine — the contract other modules depend on

Master rules §10, §61, §83.

### 4.1 Two tables, two jobs

```
inventory_transactions   append-only ledger — WHY the balance changed
inventory_balances       current state      — FAST reads
```

`inventory_balances` is **derived**, and it is never the only historical source.
Current stock is never computed by summing the ledger on a request path.

### 4.2 `inventory_transactions` (`ImmutableLedgerEntity`)

| Column | Type | Notes |
|---|---|---|
| `transaction_type` | `String(32)` | see vocabulary below |
| `item_id` | FK `items.id` | |
| `warehouse_id` | FK `warehouses.id` | |
| `location_id` | FK `storage_locations.id` nullable | |
| `batch_id` | FK `stock_batches.id` nullable | required when `item.is_batch_tracked` |
| `serial_id` | FK `stock_serials.id` nullable | |
| `quantity` | `Quantity` | **signed**: `+` increases on-hand, `−` decreases |
| `unit_cost` | `UnitPrice` | valuation at movement time |
| `total_cost` | `Money` | |
| `balance_after` | `Quantity` | on-hand after this movement, for reconciliation |
| `stock_state` | `String(24)` | `AVAILABLE / QC_HOLD / DAMAGED / IN_TRANSIT / JOB_WORK / SCRAP / RETURN` |
| `reference_type` | `String(48)` | e.g. `goods_receipt`, `material_issue` |
| `reference_id` | `UUID` | not an FK — the ledger outlives its source |
| `reference_code` | `String(64)` | human-readable, e.g. `GRN/2026-27/00012` |
| `reason` | `String(255)` | never null in practice — §83 |
| `occurred_at` | `TIMESTAMPTZ` | business time, may differ from `created_at` |

Transaction-type vocabulary (§83), each with a fixed sign:

```
+ PURCHASE_RECEIPT  PRODUCTION_RECEIPT  JOB_WORK_RECEIPT  MATERIAL_RETURN
  ASSEMBLY_RECEIPT  CUSTOMER_RETURN     TRANSFER_IN       ADJUSTMENT_INCREASE
  OPENING_BALANCE   QC_RELEASE

- MATERIAL_ISSUE    JOB_WORK_ISSUE      DISPATCH          PURCHASE_RETURN
  ASSEMBLY_CONSUME  TRANSFER_OUT        ADJUSTMENT_DECREASE
  SCRAP             QC_HOLD
```

Reservation and allocation changes do **not** move on-hand quantity; they change
`reserved_quantity` on the balance and are recorded in `stock_reservations` plus
the audit log.

Indexes:
```
(company_id, item_id, warehouse_id, occurred_at DESC)   -- item movement history
(company_id, occurred_at DESC, id DESC)                 -- keyset ledger paging
(company_id, reference_type, reference_id)              -- "what did this GRN post?"
(company_id, transaction_type, occurred_at DESC)
```

### 4.3 `inventory_balances` (`TenantEntity`)

Grain: **one row per (company, item, warehouse, location, batch)**, with a
unique constraint on exactly that tuple. `location_id` and `batch_id` use a
sentinel-free `COALESCE`-based unique index so NULLs do not create duplicates.

| Column | Meaning |
|---|---|
| `on_hand_quantity` | physically present |
| `reserved_quantity` | promised to a specific demand, still physically present |
| `allocated_quantity` | reserved **and** picked/committed to a document |
| `in_transit_quantity` | dispatched from a source, not yet received |
| `qc_hold_quantity` | present but not usable pending inspection |
| `damaged_quantity` | |
| `job_work_quantity` | issued to a vendor, still owned by the company |
| `scrap_quantity` | |
| `available_quantity` | **generated column**: `on_hand − reserved − qc_hold − damaged` |

`available_quantity` is a PostgreSQL `GENERATED ALWAYS AS (...) STORED` column.
It cannot drift from its inputs, and it can be indexed for "what can I promise?"
queries. Application code never writes it.

Check constraints — these are the correctness floor, not decoration:

```sql
CHECK (on_hand_quantity   >= 0)
CHECK (reserved_quantity  >= 0)
CHECK (allocated_quantity >= 0)
CHECK (qc_hold_quantity   >= 0)
CHECK (reserved_quantity  <= on_hand_quantity)      -- cannot promise what is not there
CHECK (allocated_quantity <= reserved_quantity)     -- allocation is a subset of reservation
```

### 4.4 Concurrency — mandatory pattern

Master rules §10: *"Available = 100; Order A requests 80; Order B requests 50 —
the system must never allow both."*

Every stock-changing operation follows this, inside one transaction:

1. `SET LOCAL lock_timeout` (fail fast rather than pile up).
2. `SELECT ... FOR UPDATE` the affected `inventory_balances` row(s),
   **ordered by primary key** so two transactions touching the same set cannot
   deadlock each other.
3. Re-read the quantities *after* acquiring the lock. A value read before the
   lock is worthless.
4. Validate against the freshly-read values; raise `InsufficientStockError` if
   short.
5. Write the ledger row and update the balance in the same transaction.
6. Write the audit entry in the same transaction.
7. `COMMIT` — or `ROLLBACK` everything.

Never solve this in application memory, and never with a Redis lock: only
PostgreSQL arbitrates the row.

Where a balance row may not exist yet, use
`INSERT ... ON CONFLICT (…) DO UPDATE … RETURNING` so two concurrent first
movements cannot both insert.

### 4.5 Public service API — call these, do not touch the tables

`src.modules.inventory.service` exports (all async, all take an `AsyncSession`
and a `TenantContext`, all must be called inside a `transaction(...)` block):

```python
async def post_movement(session, tenant, *, item_id, warehouse_id, quantity,
                        transaction_type, reference_type, reference_id,
                        reference_code, reason, location_id=None, batch_id=None,
                        serial_id=None, unit_cost=None, stock_state="AVAILABLE",
                        occurred_at=None) -> InventoryTransaction
    """Single signed movement. Locks the balance row, validates, writes ledger
    + balance + audit. THE only way stock changes."""

async def post_movements(session, tenant, movements: list[MovementRequest]) -> list[InventoryTransaction]
    """Batch form. Locks all affected rows in primary-key order — use this for
    multi-line documents (GRN, material issue) so lines cannot deadlock."""

async def get_balance(session, tenant, item_id, warehouse_id, *, location_id=None, batch_id=None) -> InventoryBalance | None
async def get_available(session, tenant, item_id, warehouse_id=None) -> Decimal
    """Sum of available_quantity. Never cached — §34."""

async def transfer_stock(session, tenant, *, item_id, from_warehouse_id,
                         to_warehouse_id, quantity, reference_code, reason, ...) -> tuple[InventoryTransaction, InventoryTransaction]
    """Two-legged TRANSFER_OUT / TRANSFER_IN."""

async def change_stock_state(session, tenant, *, item_id, warehouse_id, quantity,
                             from_state, to_state, reason, ...) -> InventoryTransaction
    """QC_HOLD -> AVAILABLE, AVAILABLE -> DAMAGED, etc."""
```

`src.modules.allocation.service` exports:

```python
async def reserve(session, tenant, *, item_id, warehouse_id, quantity,
                  demand_type, demand_id, demand_line_id, reservation_type,
                  priority, required_date, reason) -> StockReservation
async def release(session, tenant, *, reservation_id, reason, quantity=None) -> StockReservation
async def consume(session, tenant, *, reservation_id, quantity, reference_type,
                  reference_id, reference_code) -> StockReservation
    """Convert a reservation into an actual issue/dispatch movement."""
async def suggest_reallocation(session, tenant, *, item_id, warehouse_id,
                               quantity_needed, for_demand_type, for_demand_id,
                               required_date) -> list[ReallocationCandidate]
async def reallocate(session, tenant, *, request_id, approved_by) -> ReallocationRequest
async def available_to_promise(session, tenant, *, item_id, warehouse_id=None) -> Decimal
```

`demand_type` ∈ `SALES_ORDER | PRODUCTION_ORDER | ASSEMBLY_ORDER | JOB_WORK_ORDER | SPARE | TRANSFER`.
`reservation_type` ∈ `HARD | SOFT`. Hard reservations are never taken by
reallocation without an approved `ReallocationRequest`.

---

## 5. BOM engine

`boms`: `item_id`, `version` (int), `code`, `revision`, `status`
(`DRAFT | PENDING_APPROVAL | APPROVED | OBSOLETE`), `is_default`,
`bom_type` (`PRODUCTION | ASSEMBLY | PHANTOM | ALTERNATE`), `output_quantity`,
`base_uom_id`, `effective_from`, `effective_to`, `parent_bom_id` (alternates),
`approved_by_id`, `approved_at`, `total_material_cost`, `total_operation_cost`.

Unique: `(company_id, item_id, version)`. Partial unique index enforcing **at
most one `is_default` APPROVED BOM per item**.

`bom_lines`: `bom_id`, `line_number`, `component_item_id`, `quantity_per`,
`uom_id`, `scrap_percentage`, `is_optional`, `substitute_for_line_id`,
`operation_sequence`, `notes`.
Unique `(bom_id, line_number)`. Self-reference on `component_item_id` is blocked
by a check constraint.

### Explosion engine — `src.modules.bom.engine`

```python
@dataclass(frozen=True)
class ExplodedComponent:
    item_id: uuid.UUID
    item_code: str
    total_quantity: Decimal      # consolidated across ALL branches
    level: int                   # shallowest level at which it appears
    is_purchased: bool
    is_manufactured: bool
    source_paths: tuple[tuple[str, ...], ...]   # traceability: every path here
    scrap_adjusted_quantity: Decimal

async def explode(session, tenant, item_id, quantity, *, bom_id=None,
                  max_depth=20, as_of=None) -> BomExplosion
```

Two behaviours are non-negotiable (master rules §11, §72):

1. **Common-part consolidation.** If `SCREW-M4` appears in `Rose Handle` (2 per)
   and in `Lock Body` (4 per), exploding 100 `Premium Lock Kit` yields **one**
   `SCREW-M4` row for 600 — not two rows. Consolidation is keyed on
   `item_id`, and `source_paths` retains where each contribution came from so
   the merge stays auditable.
2. **Cycle detection.** A component that reaches its own ancestor raises
   `BomCycleError` with the full path. Depth beyond `max_depth` raises
   `BOM_DEPTH_EXCEEDED`. Both are checked *during* traversal, not after.

Traversal loads all BOMs for the involved items in **batched queries by level**
— never one query per node (§32).

---

## 6. MRP engine

Flow (master rules §14):

```
Sales orders (+ manual demand)
  -> net against finished-goods available
  -> explode BOM for the shortfall
  -> MERGE common parts across all demand lines and all branches
  -> net against inventory available
  -> net against confirmed incoming POs (by expected date)
  -> net against in-progress production
  -> shortage
  -> suggestions
```

`mrp_runs`: `code`, `status` (`QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED`),
`mode` (`SYNC | ASYNC`), `horizon_days`, `warehouse_ids`, `celery_task_id`,
`started_at`, `completed_at`, `demand_line_count`, `suggestion_count`,
`error_message`, `parameters` (JSONB), `result_summary` (JSONB).

`mrp_suggestions`: `run_id`, `item_id`, `suggestion_type`
(`MANUFACTURE | PURCHASE | JOB_WORK | TRANSFER`), `required_quantity`,
`suggested_quantity` (after min-order-qty / multiple rounding),
`required_date`, `suggested_order_date` (required date − lead time),
`shortage_quantity`, `available_quantity`, `incoming_quantity`, `source_demands`
(JSONB), `status` (`OPEN | CONVERTED | IGNORED`), `converted_to_type`,
`converted_to_id`.

**Execution rule (§14):** a run whose demand-line count exceeds
`settings.mrp_sync_threshold_demand_lines` is dispatched to Celery and the API
returns `202` with a job id. A normal HTTP request is never held open for a
long calculation.

---

## 7. Document state machines (§73)

Each is declared as a `StateMachine` in its module and is the *only* thing
permitted to change a status column.

```
Sales order       DRAFT -> CONFIRMED -> ALLOCATED -> PARTIALLY_DISPATCHED -> DISPATCHED -> INVOICED -> CLOSED
                  CONFIRMED -> PARTIALLY_DISPATCHED (reserved portion may ship)
                  ALLOCATED -> CONFIRMED (quantity increase restores a shortage)
                  (CANCELLED reachable from DRAFT/CONFIRMED/ALLOCATED only)

Purchase          DRAFT -> PENDING_APPROVAL -> APPROVED -> CONVERTED -> CLOSED
requisition       (REJECTED from PENDING_APPROVAL; CANCELLED from DRAFT, PENDING_APPROVAL,
                  APPROVED; CLOSED from APPROVED or CONVERTED via POST .../close)

Purchase order    DRAFT -> PENDING_APPROVAL -> APPROVED -> SENT -> ACKNOWLEDGED
                        -> PARTIALLY_RECEIVED -> RECEIVED -> QC_PENDING -> QC_COMPLETED
                        -> GRN_COMPLETED -> INVOICED -> PAID -> CLOSED
                  (REJECTED from PENDING_APPROVAL; CANCELLED before receipt)

Production order  DRAFT -> APPROVED -> RELEASED -> MATERIAL_ISSUED -> IN_PROCESS
                        -> COMPLETED -> CLOSED   (CANCELLED before IN_PROCESS)

QC inspection     PENDING -> IN_PROGRESS -> (ACCEPTED | REWORK | REJECTED | HOLD | SCRAP) -> CLOSED

Job work order    DRAFT -> APPROVED -> ISSUED -> PARTIALLY_RECEIVED -> RECEIVED
                        -> QC_PENDING -> COMPLETED -> CLOSED

Dispatch          NOT_DISPATCHED -> READY_FOR_DISPATCH -> DISPATCHED -> IN_TRANSIT
                        -> DELIVERED -> QC_PENDING -> RECEIVED -> QC_COMPLETED -> GRN_COMPLETED
```

Master records with no status column - branch, plant, department, cost centre,
financial year, brand and manufacturer - project `is_active` / `deleted_at` onto
one derived lifecycle of their own (`organization.service.master_lifecycle`),
through the same kind of machine. There is no status column to disagree with
them:

```
Branch, plant, department, cost centre, financial year, brand, manufacturer
                  ACTIVE <-> INACTIVE;  ACTIVE | INACTIVE -> ARCHIVED (terminal; retire = soft delete)
```

Every transition writes an audit row via `record_status_change`.

---

## 8. Cross-module integration points

| Event | Must also do |
|---|---|
| GRN posted | `inventory.post_movements` (`PURCHASE_RECEIPT`), advance PO status, create incoming QC inspection when the item requires it |
| Material issue | `allocation.consume` the reservation, then `post_movements` (`MATERIAL_ISSUE`) |
| Production completion | `post_movements` (`PRODUCTION_RECEIPT`) for output, `ASSEMBLY_CONSUME`/`MATERIAL_ISSUE` for components |
| Job-work issue | `post_movements` (`JOB_WORK_ISSUE`) moving stock to `stock_state=JOB_WORK` — still company-owned |
| QC rejection | `change_stock_state` to `DAMAGED`/`SCRAP`, raise return/rework |
| Dispatch | `allocation.consume`, then `post_movements` (`DISPATCH`) |
| Sales order confirmed | `allocation.reserve` per line against available stock |
| Stock arrives for a short sales order | `maintenance.allocate_short_orders` finds live shortfalls, then calls `sales.allocate_order` per order in its own transaction; default cadence 900 seconds, at most 200 orders per company |
| Requisition converted | One DRAFT purchase order per (vendor, procurement category, warehouse) through `procurement.service._create_purchase_order` (duplicate check included); `_link_source_documents` adds to each line's `ordered_quantity` and moves the requisition APPROVED → CONVERTED; one CREATE audit row per order and one "Split into …" row per requisition - all in one transaction, or nothing |
| Vendor invoice approved | `finance.post_journal`: one `PURCHASE_EXPENSE` debit per effective line cost centre (line -> header -> none), input tax, payable |
| Any write naming a cost centre | `organization.service.require_cost_centre` / `require_cost_centres` (tenant, live, active) before the row is written |
| Any status change | `audit.record_status_change` in the same transaction |

### Requisition conversion and the purchase-order category

`purchase_orders.item_category_id` (nullable, FK `item_categories` RESTRICT,
indexed with `(company_id, vendor_id, item_category_id)`) is the procurement
category an order was raised for. It is **derived, never accepted from a
client**: the group's category when a requisition is split, and on a buyer's own
order the single category every line resolves to against the order's vendor, or
NULL when the lines differ. A line's procurement category is the nearest
ancestor-or-self of its item's category that the vendor is qualified for in
`vendor_categories`, else the item's own (docs/assumptions.md §50).

The vendor portal reads it: `PurchaseOrderRepository._scoped` shows a vendor
user only sent orders, and, when the user's `allowed_category_ids` is not empty,
only orders whose category sits at or beneath one of them - matched on the
materialised `item_categories.path` - plus orders assigned to that user by name
(§56). `users.allowed_category_ids` must name live categories of the tenant
that the user's vendor is qualified for.

Every path that adds to a requisition's `ordered_quantity` - a conversion, a
buyer's own order naming requisition lines, an edit rewriting such an order's
lines - takes its locks in one order: requisition headers by id, then
requisition lines by id, then the `po-duplicate` advisory locks sorted by
(vendor, warehouse). A conversion takes all of its advisory keys before raising
its first order. Giving quantity back on cancel or reject locks only the lines,
never a header, so it cannot close a cycle with that order.

---

## 9. Reporting

Dashboards and reports never query hot transactional tables with wide
aggregations on every request (§25). The order of escalation is fixed:

1. correct SQL with the right indexes
2. summary tables refreshed by Celery (`maintenance.refresh_summary_views`)
3. PostgreSQL materialised views
4. Redis caching of safe, non-authoritative results
5. Celery-generated report artefacts for anything large
6. ClickHouse only when measured volume proves the need — not before

Inventory availability is **never** served from cache (§34).


### Sales fulfilment and issued tax invoices

A SALES dispatch naming an order must name a matching `sales_order_line_id` on
 every line. Dispatchable quantity is `max(0, min(live reservation remaining,
ordered - dispatched - other open dispatch quantities))`; closed reservations
are zero. Planning and departure both validate it under the order lock. A SALES
consignment without an order retains the direct free-stock movement path.
