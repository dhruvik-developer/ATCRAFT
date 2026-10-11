# WP E7a: Production completion (complete/close capture, costing and variance, WIP, planning board and capacity, machine overlap, daily bookings, routing machine, BOM draft editor)

Lane B, round 4, step 2 (after E5). Placeholder `§E7-n`.

**Checklist IDs to close:** P08.M2, P08.M6, P08.M9, P08.M10, P08.M11, P08.M12, P13.M6, P04.B4, P04.B6, P04.R3, P14.16. Also the non-tracked part of S03.12; tracked FG output is E8b. Read each in `trace/trace.json`.

**Already on main:** B2 (shifts and roster with `shift_id`), E5 (production QC), E4.

## Decisions (binding)

### 1. Complete and close dialogs (P08.M11)

The UI today posts `{}` for both.

- **Complete** captures, per `CompleteRequest`:
  - produced quantity
  - rejected quantity and its reason
  - scrap quantity
  - actual end date
  - note
  - shift
- **Close** captures the closure note.
- Both dialogs show the order's planned against booked figures.

### 2. Costing and variance (P08.M12, P13.M6)

- **Costing tab.** Render the existing `materials[]` and `operations[]` breakdown.
- **Standard snapshot.** At release, store `standard_unit_cost` from the BOM roll-up.
- **Order totals.** At close, compute these totals and store them on the order:
  - actual material: net issued FIFO value
  - actual operations: the work-order snapshots
  - **subcontract**: job-work charges from job-work orders whose source is this production order (read through job_work's seam)
  - `actual_unit_cost`
  - variance, split into material, operation and subcontract parts
- **Revaluation (`§E7-1`).** At close, receipts of this order still on hand are **revalued** to actual cost through an inventory cost-adjustment mechanism, if the engine supports layer revaluation. If it does not, record the variance only, and say why in assumptions (the conservative choice, given FIFO layers already consumed downstream). Do not invent GL postings. There are no inventory accounts. Document that.
- **Report.** New report `PRODUCTION_COST`: closed orders over a period, showing standard against actual per unit and variance. Filters: item, work centre, cost centre. Include the CHECK-swap migration.

### 3. WIP (P08.M10)

- **WIP tab.** Add a "WIP by order" tab on `/production/work-orders` (or `/production`), using `GET /production/wip`.
  - filters: plant, warehouse, item, late only
  - days-late badge
  - open-operations count
  - **WIP value**: net issued material plus operation cost to date, as one batched aggregate
- **`late_only`.** Honour it in `load_by_work_centre`, or remove the parameter.

### 4. Planning board and capacity (P08.M2)

- **Capacity endpoint.** `GET /production/capacity?date_from&date_to&work_center_id` buckets the remaining planned minutes per work centre per day. It compares them against capacity: B2's rostered minutes when a roster exists, otherwise `working_hours_per_day × 60`. Over-capacity days are flagged.
- **Planning page.** New page `/production/planning`, gated on `production_order.view`; add it to the sweeps.
  - a day/week timeline of work orders per work centre and machine, from `planned_start_at` / `end_at`
  - drag (or a reschedule dialog on mobile) to change the planned window through `PATCH /production/work-orders/{id}`, which re-validates
  - a capacity heat strip
- **Work-order planning.** Behind a named constant `PLAN_WITHIN_SHIFTS` (default true when a roster exists), `plan_work_orders` places operations inside the rostered shift windows of their work centre instead of running 24/7 back to back. Keep the old behaviour available. `§E7-n`.

### 5. Machine allocation (P08.M6)

- **Overlap check.** On work-order assignment and at release, compute overlapping planned windows for the same machine in one query, using `ix_work_orders_machine`. Return a warning listing the conflicts. When the named constant `BLOCK_MACHINE_OVERLAP` is true (default false), raise a `BusinessRuleError` instead. `§E7-n`.
- **Machine status.** Warn on MAINTENANCE or BREAKDOWN using `WORKABLE_MACHINE_STATUSES`.
- **Machine timeline.** Add a machine × day timeline on the planning page.

### 6. Daily bookings (P08.M9)

- **Endpoint.** `GET /production/entries`, company-wide. Declare it before `/{order_id}`. It uses the existing `ProductionEntryFilters` and is paginated.
- **Tab.** A "Daily bookings" tab shows date × shift across all open orders, with inline "Record" per order (reuse B2's dialog).

### 7. Routing machine (P04.R3)

- Each routing operation row gets a machine select, filtered by that row's work centre and excluding RETIRED and inactive machines.
- The select is cleared when the work centre changes and disabled for outsourced operations.
- The routing detail shows the machine.

### 8. BOM draft editor and operations (P04.B4, P04.B6)

- **Edit draft.** Add an "Edit draft" action on `/bom/:bomId` for DRAFT BOMs (`bom.update`). It reuses the BOM form line grid prefilled from GET (component, quantity per, scrap %, optional, substitute-for line, operation sequence, notes) and adds an **Operations grid**. It sends PATCH and surfaces the row-version 409.
- **Routing snapshot.** On create and approve, when the BOM has no operations, snapshot the item's default approved routing into `bom_operations`: sequence, name, `routing_operation_id`, work centre, setup/run minutes, work-centre cost per hour, process cost, outsourced. This makes BOM costing include operations. List the operations on the BOM page.
- **Compare (optional).** A compare view between two versions.

### 9. Machine utilisation (P14.16)

Build the query from active, non-retired machines, LEFT JOINed to the busy-minutes and downtime subqueries, so idle machines show 0%. Keep B2's rostered columns.

## Tests

- **Integration:**
  - complete and close capture
  - variance computation, including subcontract
  - revaluation (or variance-only, per the decision)
  - WIP value
  - capacity buckets with and without a roster
  - shift-aware planning
  - machine overlap warning and block
  - entries endpoint
  - routing machine validation
  - BOM PATCH round trip with operations
  - routing snapshot
  - utilisation includes idle machines
  - reports
- **Frontend:** dialog request bodies, planning page reschedule body, BOM editor body, routing machine select.

## Docs

- `database.md`
- `assumptions.md`: costing policy, planning within shifts, overlap rule
- handbook: Production, Planning, BOM, Routing
