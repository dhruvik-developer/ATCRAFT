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
| `inventory` | `inventory_transactions`, `inventory_balances`, `inventory_cost_layers`, `inventory_cost_consumptions`, `stock_batches`, `stock_serials`, `stock_transfers`, `stock_transfer_lines`, `stock_adjustments`, `stock_adjustment_lines`, `physical_counts`, `physical_count_lines`, `bin_moves`, `bin_move_lines`, `scrap_disposals`, `scrap_disposal_lines`, `internal_issues`, `internal_issue_lines` |
| `allocation` | `stock_reservations`, `reallocation_requests` |
| `bom` | `boms`, `bom_lines`, `bom_operations` |
| `routing` | `work_centers`, `machines`, `routings`, `routing_operations`, `shifts`, `shift_assignments` |
| `sales` | `sales_orders`, `sales_order_lines` |
| `mrp` | `mrp_runs`, `mrp_demands`, `mrp_requirements`, `mrp_suggestions` |
| `procurement` | `purchase_requisitions`, `purchase_requisition_lines`, `rfqs`, `rfq_lines`, `vendor_quotations`, `vendor_quotation_lines`, `purchase_orders`, `purchase_order_lines`, `goods_receipts`, `goods_receipt_lines`, `purchase_returns`, `purchase_return_lines`, `purchase_order_shipments`, `purchase_order_documents` |
| `production` | `production_orders`, `production_order_components`, `work_orders`, `material_issues`, `material_issue_lines`, `production_entries` |
| `assembly` | `assembly_orders`, `assembly_order_components` |
| `job_work` | `job_work_orders`, `job_work_order_lines`, `job_work_challans`, `job_work_challan_lines`, `job_work_receipts`, `job_work_receipt_lines` |
| `quality` | `qc_templates`, `qc_template_parameters`, `qc_inspections`, `qc_inspection_results`, `qc_inspection_decisions`, `qc_rework_records` |
| `packing` | `packing_orders`, `packing_order_lines`, `cartons`, `carton_contents` |
| `dispatch` | `dispatches`, `dispatch_lines`, `dispatch_tracking_events` |
| `finance` | `ledger_accounts`, `ledger_entries`, `vendor_invoices`, `vendor_invoice_lines`, `sales_invoices`, `sales_invoice_lines`, `payments`, `payment_allocations` |
| `reports` | `report_snapshots`, `inventory_summary` |
| `documents` | `documents` |
| `notifications` | `notifications`, `notification_preferences`, `push_subscriptions` |
| `vendor_portal` | none - a facade over procurement, finance, quality and documents, scoped by the token's vendor |
| `dealer_portal` | `dealer_offers`, `dealer_carts`, `dealer_cart_lines`, `dealer_wishlist_items` - a facade over sales, dispatch and finance, scoped by the token's customer, plus its own schemes, carts and wishlists |
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
  line resolves line -> header -> NULL at approval, except on a job-work bill,
  where the job-work order's centre sits between the line and the header
  (line -> order -> header -> NULL). Production and job-work
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
  unallocated leg it always did. A `JOB_WORK` invoice splits `JOB_WORK_EXPENSE`
  instead, with the job-work order's centre between the line and the header.
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

### Shifts and the roster

Routing owns the immutable-code `shifts` master (local start/end times,
midnight flag, break minutes, ordering and active lifecycle). `shift_assignments`
records a local start date, shift, cell, optional machine and internal operator.
UTC `starts_at`/`ends_at` are plain snapshots computed in the company timezone;
changing the timezone never moves a stored assignment. Two partial GiST
exclusions prevent overlapping PLANNED operator or machine windows. A composite
(machine, work centre) foreign key keeps machines in their cell. Writes hold a
company roster advisory lock, batch references, and return every conflict.

Roster lists require a date range of at most 31 inclusive days. Bulk writes and
copy-day writes are atomic, capped at 200 assignments. Cancelling is irreversible
and audited. Future PLANNED assignments block shift re-timing/deactivation/retirement
and asset retirement or movement. Times include the whole window; breaks reduce
reported rostered capacity. Maintenance and breakdown are warnings.

`work_orders.shift_id` and `production_entries.shift_id` reference a shift; their
existing `shift` text remains a read-only code snapshot. New inputs accept only
shift ids. Existing text is retained without inventing times or backfilling ids.
The roster is advisory: assignment/bookings outside the plan warn and remain
valid facts. Active internal operators have a production-readable paginated
directory, independent of `user.view`. `SHIFT_PRODUCTION` groups bookings by
local entry date, code snapshot, cell, machine and operator. Machine utilisation
retains calendar capacity and adds net PLANNED roster capacity beside it.

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

Two further flags drive quality holds rather than capabilities:
`requires_production_qc` (good output of this item is booked into QC hold with
an in-process inspection) and `requires_final_qc` (packing a line of this
item raises a FINAL inspection). They default off and are edited on the item
form.

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
`is_batch_tracked`, `is_serial_tracked`, `default_warehouse_id`,
`default_location_id`, `is_active`. The optional default bin must be live,
active, tenant-owned and in the item's default warehouse. Assignment takes
`FOR SHARE` on the bin; retirement takes the conflicting `FOR NO KEY UPDATE`
lock and refuses while a live item names it.

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
  SCRAP             QC_HOLD            SCRAP_DISPOSAL
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

### 4.6 Putaway, scrap and internal consumption

All three documents and their lines are tenant-owned, numbered, audited and
warehouse-scoped. Each line identifies an item, quantity, optional batch and
bin. Lists are paginated. Posted quantities and costs are immutable.

`bin_moves` pairs `TRANSFER_OUT` with `TRANSFER_IN` in the same warehouse and
transaction. The source and destination bins must differ; NULL is the unbinned
position. `carries_cost_of` carries the exact FIFO draws between positions.
There is no transit quantity. Creation posts the move and requires
`inventory.transfer`.

`scrap_disposals` records `SALE`, `WRITE_OFF` or `RECYCLE`, the optional buyer or
reference and realised amount. Creation posts `SCRAP_DISPOSAL`: a negative
movement from SCRAP to outside ownership, with no on-hand effect. A write-off
realises zero. `inventory.scrap_dispose` is required. Existing scrap remains
disposable after the item's `can_scrap` flag is switched off; every new movement
*into* SCRAP requires that flag, including QC and job-work losses. QC rejection
uses DAMAGED when material must remain identifiable.

`inventory_cost_layers.pool` separates `ON_HAND` and `SCRAP` FIFO queues at the
same grain. Scrapping preserves the original receipt costs in the scrap queue;
disposal consumes that queue, independently of later on-hand receipts.
`stock_value` and average cost describe the on-hand queue. The legacy backfill
uses recorded scrap costs, with standard cost only for uncovered history, and
verifies that the queue covers the current scrap quantity. Ordinary valuation
and layer reconciliation exclude the scrap queue.

`internal_issues` names a department and/or cost centre and posts
`MATERIAL_ISSUE` with `reference_type="internal_issue"`. Its machine is DRAFT to
POSTED below the approval threshold, otherwise DRAFT to PENDING_APPROVAL to
POSTED. Draft or pending issues may be cancelled; rejection ends CANCELLED
with its reason. `INTERNAL_ISSUE_APPROVAL_VALUE` is 25,000 in company currency;
the actual FIFO posting rechecks it atomically. A separate approver is required,
with the existing superuser exception. Rights are `internal_issue.create` and
`internal_issue.approve`. CONSUMABLES is a warehouse classification, not an
inventory permission or a posting restriction.

`SCRAP_REGISTER` reads bounded ledger movements into and out of scrap, at their
recorded costs. `CONSUMPTION_BY_DEPARTMENT` groups only POSTED internal issues by
department, cost centre and item over a bounded period. Both respect tenant and
warehouse scope and participate in the report snapshot CHECK vocabulary.

Incoming QC currently addresses the unbinned warehouse position. A GRN line
held for inspection therefore refuses a bin; put accepted stock away afterwards
with a bin move. Serial identities are added in E8b; these new documents refuse
serial-tracked stock until they can identify the units they move.

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
                  INVOICED -> DISPATCHED (invoice cancellation)
                  (CANCELLED reachable from DRAFT/CONFIRMED/ALLOCATED only)

Sales invoice     ISSUED <-> PARTIALLY_PAID <-> PAID; ISSUED <-> PAID
                  ISSUED -> CANCELLED (irreversible; unpaid, within 24 hours)

Purchase          DRAFT -> PENDING_APPROVAL -> APPROVED -> CONVERTED -> CLOSED
requisition       (REJECTED from PENDING_APPROVAL; CANCELLED from DRAFT, PENDING_APPROVAL,
                  APPROVED; CLOSED from APPROVED or CONVERTED via POST .../close)

Purchase order    DRAFT -> PENDING_APPROVAL -> APPROVED -> SENT -> ACKNOWLEDGED
                        -> PARTIALLY_RECEIVED -> RECEIVED -> QC_PENDING -> QC_COMPLETED
                        -> GRN_COMPLETED -> INVOICED -> PAID -> CLOSED
                  (SENT -> VENDOR_DECLINED -> SENT or CANCELLED;
                   REJECTED from PENDING_APPROVAL; CANCELLED before receipt)

Production order  DRAFT -> APPROVED -> RELEASED -> MATERIAL_ISSUED -> IN_PROCESS
                        -> COMPLETED -> CLOSED   (CANCELLED before IN_PROCESS)

Vendor invoice    DRAFT -> PENDING_APPROVAL -> APPROVED <-> PARTIALLY_PAID <-> PAID
                        (DISPUTED from PENDING_APPROVAL/APPROVED/PARTIALLY_PAID;
                        CANCELLED from DRAFT, PENDING_APPROVAL, APPROVED, DISPUTED -
                        cancelling a posted bill reverses its journals)

QC inspection     PENDING -> IN_PROGRESS -> (ACCEPTED | REWORK | REJECTED | HOLD | SCRAP) -> CLOSED
                  (REWORK -> IN_PROGRESS re-opens a lot whose rework cycle finished,
                  with the verdict back at PENDING; earlier verdicts stay in
                  `qc_inspection_decisions`)

QC rework record  OPEN -> IN_PROGRESS -> DONE (a second round of work is a second
                  record; completing re-opens the inspection for re-decision)

Job work order    DRAFT -> APPROVED -> ISSUED -> PARTIALLY_RECEIVED -> RECEIVED
                        -> QC_PENDING -> COMPLETED -> CLOSED

Assembly order    DRAFT -> APPROVED -> RELEASED -> COMPONENTS_ISSUED -> IN_PROGRESS
                        -> QC_PENDING -> COMPLETED -> CLOSED
                  (IN_PROGRESS -> COMPLETED direct when no inspection is needed;
                  CANCELLED from DRAFT, APPROVED, RELEASED only - past issue the
                  parts are on the bench and the order completes or closes)

Dispatch          NOT_DISPATCHED -> READY_FOR_DISPATCH -> DISPATCHED -> IN_TRANSIT
                        -> DELIVERED -> QC_PENDING -> RECEIVED -> QC_COMPLETED -> GRN_COMPLETED
```

Assembly and job-work QC_PENDING have exactly one way out: the inspection
verdict. `POST /assembly/{id}/qc-result` and `POST /job-work/{id}/qc-result`
serve only holds no inspection covers; when the hold names an inspection, the
inspection is decided through quality instead, and the owner completes itself
from that verdict in the same transaction (§8). A PO in QC_PENDING advances to
QC_COMPLETED -> GRN_COMPLETED through
`procurement.record_incoming_inspection_outcome` once no received line is still
PENDING; assembly reservations taken at release carry the order's priority and
its planned start as their required date.

Master records with no status column - branch, plant, department, cost centre,
financial year, brand and manufacturer - project `is_active` / `deleted_at` onto
one derived lifecycle of their own (`organization.service.master_lifecycle`),
through the same kind of machine. There is no status column to disagree with
them:

```
Branch, plant, department, cost centre, financial year, brand, manufacturer
                  ACTIVE <-> INACTIVE;  ACTIVE | INACTIVE -> ARCHIVED (terminal; retire = soft delete)
```

Shift             ACTIVE <-> INACTIVE; ACTIVE | INACTIVE -> ARCHIVED
Shift assignment  PLANNED -> CANCELLED (terminal)

Every transition writes an audit row via `record_status_change`.

---

## 8. Cross-module integration points

| Event | Must also do |
|---|---|
| GRN posted | `inventory.post_movements` (`PURCHASE_RECEIPT`), advance PO status, create incoming QC inspection when the item requires it |
| Material issue | `allocation.consume` the reservation, then `post_movements` (`MATERIAL_ISSUE`) |
| Production order released | Snapshot the approved BOM roll-up unit standard and its material, operation and direct-outsourced operation parts on `production_orders`; snapshot `routing_operations.is_outsourced` on each `work_orders` row |
| Production completion | `post_movements` (`PRODUCTION_RECEIPT`) for output - into QC hold with an in-process inspection when the order holds output for QC - `ASSEMBLY_CONSUME`/`MATERIAL_ISSUE` for components; refused while any booked output is still held |
| Production order closed | Freeze FIFO issue cost, internal work-order cost, accrued job-work receipt charges, good-unit actual cost and standard variances on the order in the same transaction as the CLOSED transition |
| Job-work issue | `post_movements` (`JOB_WORK_ISSUE`) moving stock to `stock_state=JOB_WORK` — still company-owned |
| QC rejection | `change_stock_state` to `DAMAGED`/`SCRAP`, raise return/rework |
| QC verdict decided | `quality.owners.notify_owner` in the same transaction, after the stock moves: the owning document completes itself from the verdict and moves nothing again |
| Packing started | A FINAL inspection per line whose item requires final QC (stock reserved, not held); completion and hand-over refused until each is ACCEPTED; a REJECTED line is repacked and re-inspected |
| Dispatch | `allocation.consume`, then `post_movements` (`DISPATCH`) |
| Sales order confirmed | `allocation.reserve` per line against available stock |
| Stock arrives for a short sales order | `maintenance.allocate_short_orders` finds live shortfalls, then calls `sales.allocate_order` per order in its own transaction; default cadence 900 seconds, at most 200 orders per company |
| Invoice issued | `finance.post_journal` debits AR and credits sales revenue/output GST, posts round-off, increases customer outstanding and SO-line invoiced quantities; full dispatched orders become INVOICED; queue permanent PDF after commit |
| Invoice cancelled | Reverse its entire journal, reduce customer outstanding and SO-line invoiced quantities, restore INVOICED to DISPATCHED; dispatch cancellation stays blocked until the live invoice is cancelled |
| Customer receipt allocated or released | Lock payment then invoice headers in id order; update invoice paid quantities and status through SALES_INVOICE_LIFECYCLE; cancelling a receipt releases its allocations before reversing its journal |
| Requisition converted | One DRAFT purchase order per (vendor, procurement category, warehouse) through `procurement.service._create_purchase_order` (duplicate check included); `_link_source_documents` adds to each line's `ordered_quantity` and moves the requisition APPROVED → CONVERTED; one CREATE audit row per order and one "Split into …" row per requisition - all in one transaction, or nothing |
| Vendor invoice approved | `finance.post_journal`: one `PURCHASE_EXPENSE` debit per effective line cost centre (line -> header -> none), input tax, payable. A `JOB_WORK` invoice debits `JOB_WORK_EXPENSE` instead (line -> job-work order -> header -> none), moves each receipt line's `invoiced_amount`, and posts the excess-loss recovery (`job_work_recovery`: Dr AP / Cr job-work expense) for the still-unposted vendor debit |
| Vendor invoice cancelled | reverse every journal the invoice posted (the bill and, on a job-work invoice, the recovery beside it) and roll the job-work tracking back, in the same transaction |
| PO sent (also re-sent after a decline) | `notify` the assignee, else every user of that vendor holding `vendor_portal.respond` who can see the order (§C1-11) |
| Vendor accepts, declines, moves its committed date, marks ready | `notify` the order's creator, `purchase_order.approve` holders and the ADMIN and PURCHASE_MANAGER roles (§C1-7); the committed date never moves `expected_delivery_date` |
| Consignment recorded, corrected or delivered; LR copy or dispatch photo filed | The same buyer recipients, one collapsing row per consignment (`po-shipment:{id}`); the order header mirrors the latest consignment |
| Goods receipt posted / incoming inspection rejects goods | Also `notify` the vendor's users holding `vendor_portal.access` / `vendor_portal.quality` who can see the order |
| Vendor bill approved or disputed; payment to a vendor approved or allocated | Also `notify` the vendor's users holding `vendor_portal.invoice` |
| Dealer checkout | One DRAFT `sales_orders` row with `source = DEALER_PORTAL`, server prices and scheme `offer_id`s per line, cart cleared, one `dealer_checkout` audit row, and `notify` the assigned salesperson else `sales_order.approve` holders - all in one transaction, or nothing (§C3-6) |
| Any write naming a cost centre | `organization.service.require_cost_centre` / `require_cost_centres` (tenant, live, active) before the row is written |
| Any status change | `audit.record_status_change` in the same transaction |

Production obtains subcontract actuals only through
`job_work.service.production_order_charges(session, tenant, production_order_id)`.
It returns receipt-accrued `job_work_orders.total_charges` for the tenant's
orders whose source is that production order; estimates and vendor-debit
recoveries are not manufacturing charges. The query is aggregated in
`job_work.repository`, not performed by production against another module's
tables.

The release-time standard fields on `production_orders` are nullable because
existing orders have no reliable historical standard snapshot. Cost and
variance columns remain nullable until closure, and variance values may be
negative. `actual_subcontract_cost` is based on good output actually received
from job-work suppliers; `actual_unit_cost` divides the full actual cost by
good production output, and is NULL when no good output exists.

The `PRODUCTION_COST` report reads only CLOSED orders in the requested period.
It filters `production_orders.updated_at` (set by the close transition), item
and cost centre; work-centre filtering uses an `EXISTS` over tenant-owned
`work_orders` so one order is never duplicated. Its compound
`(company_id, status, updated_at)` index bounds the closed-period scan. The
report is restricted to principals with `report.view` and `item.cost_view`,
including catalogue visibility and background-job access.
The generic document list, metadata, download, and delete routes also hide the
generated artefact unless the caller has `item.cost_view`; a document ID cannot
bypass the report permission.

The WIP-by-order endpoint returns one live order per row. It joins two
tenant-scoped grouped aggregates: posted material issue cost less posted return
cost, and operation cost to date. Running clocked operations accrue elapsed time
through query time; outsourced operations are excluded. Because this exposes
cost, `/production/wip` requires both `production_order.view` and
`production_cost.view`. Its `late_only` filter is applied consistently by both
the order and work-centre WIP endpoints.

### QC verdict to owner (`quality.owners`)

Quality sits downstream of procurement, assembly, job work and production, so it may call
them - never the reverse. `quality.owners` is a registry from
`reference_type` to an owner handler, resolved once with lazy imports.
`decide_inspection` (and `close_inspection`, which settles nothing new) locks
the owner's row before touching a balance, posts its own movements, then calls
the handler **in the same transaction** with the inspection's cumulative
disposition: accepted, rejected, rework, hold, scrap, still-held. Handlers
**never move stock** - quality already did. An owner that refuses rolls the
whole decision back.

| Reference type | Handler | Effect, without moving stock |
|---|---|---|
| `goods_receipt` | `procurement.record_incoming_inspection_outcome` | Receipt line PENDING → PASSED/FAILED once nothing is held (docs/assumptions.md §E4-2); PO QC_PENDING → QC_COMPLETED → GRN_COMPLETED when no line is PENDING |
| `assembly_order` | `assembly.record_quality_verdict` | Tallies applied, QC_PENDING → COMPLETED |
| `job_work_receipt` / `job_work_order` | `job_work.record_quality_verdict` | Receipt released; order completed when nothing remains held (per-receipt release while PARTIALLY_RECEIVED allowed) |
| `production_entry` / `work_order` | `production.record_quality_verdict` | Booking's hold flag cleared once the verdict is settled; completion and gated operations unblock (docs/assumptions.md §E5-1) |
| `sales_return` | no handler | The return document pulls via `inspections_for_reference` |

A job-work lot that returns several output items raises **one inspection per
output item**, each recorded on its receipt line (`job_work_receipt_lines.qc_inspection_id`).
Rows stranded before the callback existed are repaired by the idempotent
`maintenance.reconcile_quality_owners` sweep (beat start, then daily), not by
a migration (docs/assumptions.md §E4-1).

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

### Vendor portal and inbound consignments (C1a)

**Purchase-order columns.** Nullable `vendor_committed_date` (the vendor's
promise, apart from the buyer's `expected_delivery_date` that MRP nets on - a
buyer adopts it explicitly, §C1-1), `vendor_decline_reason`,
`ready_for_dispatch_at`, `ready_packages` and `ready_note` (the ready flag is a
flag, not a status).

**VENDOR_DECLINED.** SENT -> VENDOR_DECLINED records the vendor's reason; the
buyer then cancels (the existing release path gives the quantity back to the
requisition) or re-sends (the vendor is re-checked and the old reason cleared).
A declined order still holds its requisition quantity and blocks retiring the
vendor, but it is not expected supply: it is outside `OPEN_PO_STATUSES`, so MRP
netting and the duplicate check ignore it and no partial-index predicate
changes. It stays vendor-visible (§C1-10).

**`purchase_order_shipments`** (TenantEntity) - one consignment, numbered 1..n
per order (`uq_po_shipments_number`; the order's row lock serialises
numbering): transporter (FK, tenant- and active-checked in the service) or
courier name (a CHECK requires one), tracking and LR numbers, vehicle, driver,
`shipped_at`, `delivered_at` (CHECK: not before dispatch), expected date,
packages, `weight_kg` (Quantity), `freight_amount` (Money, CHECK >= 0), notes.
Recorded and corrected only on ACKNOWLEDGED/PARTIALLY_RECEIVED orders, by the
vendor or a buyer through one service function (§C1-2). The order header's
`courier_name`/`tracking_reference`/`shipped_at` mirror the latest consignment
(§C1-9).

**`purchase_order_documents`** (TenantEntity) - links a `documents` row (unique)
to an order, optionally a consignment and, for an INVOICE file, the vendor bill
it was attached to; `kind` is one of INVOICE, DISPATCH_PHOTO, LR_COPY,
DELIVERY_CHALLAN, OTHER (literal CHECK). The generic `documents` columns are
unchanged. Files linked here are shared with the order's vendor; a file filed
against the order from the generic Documents screen is not (§C1-3).

**Tracking.** The derived `InboundTrackingStatus` (NOT_DISPATCHED,
READY_FOR_DISPATCH, DISPATCHED, IN_TRANSIT, DELIVERED, RECEIVED, QC_PENDING,
GRN_COMPLETED) is never stored. One ordered rule in
`procurement.inbound_repository.inbound_tracking_status` is rendered as a SQL
CASE over one projection row per order; the buyer's board, the vendor's tabs
and the vendor dashboard all filter and count by that CASE (§C1-5). Every
per-order figure in the projection (lines, receipts, open inspections, files,
consignments) is a subquery correlated to the order, so a page costs what the
caller can see, not the company's purchasing history. Open inspections are
counted by quality's `open_inspection_count`, reached through a guarded import.

**PO PDF.** `ReportKey.PURCHASE_ORDER` is document-only; the job row lives in
`report_snapshots` and the PDF is a REPORT document filed against the order
(`entity_type = purchase_order`) that never expires.
A vendor's shared-file query verifies the completed PURCHASE_ORDER job's
`document_id`; labelling an internal upload REPORT does not share it.

### Dealer portal core (C3a)

**Portal binding.** `users.customer_id` (nullable FK `customers` CASCADE,
`ix_users_customer`) binds an account to the dealer portal; the CHECK
`portal_binding_exclusive` refuses a vendor and a dealer binding together.
`roles.is_dealer_role` marks the three system dealer roles. A binding must
name a live DEALER/DISTRIBUTOR customer (§C3-1).

**`dealer_offers`** (TenantMasterEntity) - one percentage scheme: `code`
(unique per company), `scope_kind` (ALL, CATEGORY, BRAND, ITEM, with the
matching id column required and the others NULL - CHECKed),
`audience_price_type` (NULL means every channel), `customer_id` (NULL means
the audience; set means that dealer's negotiated scheme),
`min_quantity`, `discount_percentage` (0 < x <= 100), validity window,
`is_active`, `sort_order`. Best single offer wins; no stacking (§C3-3).

**Cart and wishlist.** `dealer_carts` (TenantEntity + optimistic locking) - one
per dealer user (`uq` on company and user) carrying that user's dealer.
`dealer_cart_lines` - item, variant, quantity > 0, unique per cart grain (NULL
variant folded to the nil-UUID sentinel), no stored price. Same grain for
`dealer_wishlist_items` per user (§C3-4).

**Sales-order columns.** `sales_orders.source` (`ERP | DEALER_PORTAL`, literal
CHECK, server default ERP, set by the server only) and
`sales_order_lines.offer_id` (bare UUID, no FK - the sales table does not
depend on the portal schema, §C3-3). A line with no explicit discount takes
the best scheme as its default discount in `price_line`, on ERP orders as well
as portal ones.

### Notification devices and recovery (C1b)
PUSH is a channel, after IN_APP and EMAIL. Preferences have constant true
`push_enabled`; registering a device provides opt-in. Null/omitted updates preserve
an existing push preference using COALESCE. CRITICAL only overrides an in-app mute.
`notifications.push_delivered_at` is nullable. Both email and push have created_at
partial recovery indexes for the channel with its delivery marker NULL.
`push_subscriptions` is a hard-deleted TenantEntity with user CASCADE FK, globally
unique endpoint hash, encrypted URL, P-256 public/auth keys, user agent/label,
expiry/seen/success/failure timestamps and nonnegative failure count.

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

`sales_invoices` and `sales_invoice_lines` belong to finance. The header snapshots
seller/buyer identity and addresses, GST registration, place of supply, currency,
bank details, terms and transport; lines snapshot HSN, UoM, agreed price and tax.
A partial unique index allows one non-cancelled invoice per dispatch. Issue uses
one transaction and the SI/financial-year/five-digit series. Flat discounts are
shared pro rata; the last invoice takes the rounding remainder. The total rounds
to the nearest rupee HALF_UP. Due date is invoice date plus customer credit days.

An invoice can precede removal on a READY_FOR_DISPATCH consignment. Its staged
quantity must still be reserved; invoiced quantity is capped by dispatched goods
plus that consignment's staged quantity. Departure moves the SO to INVOICED when
all ordered goods have both shipped and been invoiced. A free-stock dispatch
needs an agreed priced sales order before it can issue this invoice.

`payment_allocations` names exactly one nullable `vendor_invoice_id` or
`sales_invoice_id`, with separate partial uniqueness per payment/invoice. A
receipt only settles the same customer's issued invoices in the same currency.
An empty replacement set releases allocations. Vendor-payment semantics stay
unchanged. The income account `SALES_REVENUE` prefers code 4500, skipping any
client-owned code to the next free number.

Receivable ageing retains FIFO per customer/currency, but invoice debits use the
snapshotted due date; legacy journal debits use their entry date. GST output is
read from issued non-cancelled invoice lines by invoice date and stored HSN;
dispatched not yet invoiced goods form a separate operational figure. Invoice
PDF jobs and REPORT documents never expire.
