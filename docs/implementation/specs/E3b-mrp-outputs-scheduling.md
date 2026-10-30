# WP E3b: MRP outputs and scheduling (TRANSFER, REALLOCATION, JOB_WORK and ASSEMBLY targets; scheduled runs; reorder and packing-material replenishment)

Lane D, round 5, step 1. Placeholder `§E3b-n`.

**Checklist IDs to close:**
- MRP outputs: P06.O3, P06.O4, P06.O5, P05.P9.
- The scheduling parts of P06.F5 and P06.F6.
- The "automatic" parts of S03.11 and K06 (packing material). E9 owns the packing-time check.
- The MRP assembly target from P08.M2/P09.M1.

Read each of these in `trace/trace.json`. E3a, E2, E4 and E1b are already on main.

## Decisions (binding)

### 1. TRANSFER suggestions (P06.O4, K10)

- Add a per-warehouse netting pass that runs **before** PURCHASE/MANUFACTURE. For each short (item, demand warehouse), look for free stock in other in-scope warehouses: available, minus safety stock, excluding QC and reserved stock. Emit TRANSFER suggestions that reduce the shortage.
- New nullable columns: `from_warehouse_id` and `to_warehouse_id`.
- Add `ConversionTarget.STOCK_TRANSFER`, plus `inventory.service.create_transfer_from_mrp`, which creates a DRAFT transfer carrying the suggestion as its source.
- Enable Convert for TRANSFER in the UI.

### 2. REALLOCATION suggestions (P06.O5)

- Add `MrpSuggestionType.REALLOCATION`, persist the donor reservation and both demand routes, and account for the planned quantity separately from free stock and dated supply.
- For eligible short sales-order lines, batch-read candidate donors through allocation's ranking (E2 policy) and emit route-level REALLOCATION suggestions. They reduce the shortage before PURCHASE/MANUFACTURE. Aggregate BOM, mixed-source, manual and unassigned demand stays unallocated because it has no safe single sales-line recipient.
- Conversion calls `allocation.create_reallocation_from_mrp`, which creates the normal PENDING request. SOFT donors are not auto-executed; the existing allocation service does not provide a separate policy execution path, so requests follow the same auditable decision flow.

### 3. JOB_WORK conversion (P06.O3, P05.P9)

- Extend `SuggestionConvertRequest` with `vendor_id`, `process` and `expected_return_date`. Defaults come from `routing_operations.job_work_vendor_id` or the preferred item-vendor link.
- `job_work.create_order_from_mrp` then creates a DRAFT job-work order (`source=MRP_SUGGESTION`) instead of refusing.
- UI: a "Convert to job work order" dialog asking for vendor, process and return date.
- **Supply netting:** add a third dated supply feed, `job_work.service.open_job_work_output(item_ids, warehouse_ids, upto)`, so the next run stops re-suggesting work already at the vendor.

### 4. ASSEMBLY target

- MANUFACTURE suggestions for items whose default approved BOM is of type ASSEMBLY convert to **assembly orders** (`ConversionTarget.ASSEMBLY_ORDER`, through the E4 creation seam).
- MRP nets open assembly orders as supply.
- Persist the effective BOM ID on the suggestion and use that exact approved BOM when converting, so a later revision does not change the planned work.
- Net assembly output separately from production output. Only dated `APPROVED`, `RELEASED`, `COMPONENTS_ISSUED` and `IN_PROGRESS` orders in the run's output-warehouse scope count; undated and `QC_PENDING` orders do not.

### 5. Scheduled runs (P06.F5/F6, S01.1)

- New beat task `mrp.scheduled_run`, nightly. The time comes from the setting `MRP_SCHEDULE_CRON`; the default is daily at 02:00 IST, expressed in UTC.
- It runs per company, controlled by the company setting `mrp_scheduled_enabled` (default **true**) with its horizon and scope.
- Each run is an ASYNC run on the `mrp` queue, using the existing planning lock and rate policy.
- Completion notifies planners. B4 already has "MRP run completed with shortages".
- Optional auto-convert policy per suggestion type, **off** by default (`§E3b-n`): PURCHASE becomes a DRAFT requisition, MANUFACTURE a DRAFT production order. TRANSFER and REALLOCATION are never auto-converted.

### 6. Reorder and packing-material replenishment (K06, S03.11)

- **Reorder sweep.** Add a demand source `REORDER` for items with `reorder_level > 0` whose available quantity is at or below `reorder_level`. Such an item gets PURCHASE (or MANUFACTURE) suggestions up to `reorder_quantity` (or up to the level + safety stock). `PurchaseSourceType.REORDER` finally gets a writer.
- **Packing material.** Add a packing-material demand pass. Open SO quantities, and packing orders not yet packed, are converted into packing-material demand through E9's `item_packing_specs`. If E9 is not merged yet, read the spec table through a tolerant seam, or ship this part with E9. Coordinate through the integrator notes.
- The result is PURCHASE suggestions for boxes even when the finished goods come from stock: K06 at planning time.

### 7. Consistency

- One suggestion-type vocabulary, everywhere: badges, filters, `mrp-types.ts` `isConvertible` (which must match the backend exactly), and the convert-dialog per type.
- Bulk convert skips types that need input (JOB_WORK) and reports them.

## Tests

- **Unit:** the transfer pass (surplus in A, short in B), reallocation suggestion ranking, reorder detection, packing-material demand from the spec.
- **Integration:**
  - each conversion creates the right DRAFT document;
  - the job-work output feed stops re-suggestion;
  - the scheduled run task honours the setting;
  - an assembly target is created from an ASSEMBLY BOM;
  - the CHECK migration allows REALLOCATION.
- **Frontend:** the convert dialogs per type, and `isConvertible` parity with the backend (a test that imports the backend list from the contract types).

## Docs

- `database.md` §6 (binding): the new types and columns.
- `assumptions.md`: auto-convert policy, reorder rule, packing demand.
- Handbook: the MRP chapter, plus the scenarios K06 and K10.
