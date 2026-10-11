# WP E8a: Inventory operations completion (transfers, adjustments, bins, scrap, consumables)

Lane D, round 2. Placeholder prefix `§E8-n`.

**Checklist IDs:**
- P03.F3 (transfer UI: batch, bins, cancel/edit; serial transfer is E8b)
- P03.F4 (adjustment reject/cancel, batch/bin/unit cost; serial is E8b)
- P03.F5 (bin support everywhere, putaway)
- P03.T8 and P03.S9 (scrap visible and disposable)
- P03.T7 (consumables issue)
- P02.I9 (`can_scrap` enforced)

**Background:** read the IDs in `trace/trace.json` and `trace/notes.md` G3 #7–#9, plus `reports/returns-inventory-qc.md` §1 (inventory engine rules: pools, `state_change_type`, `post_movements`, BalanceGrain).

WP B1 is on main. It added `stock_adjustments.cost_centre_id` and the cost-centre seam.

**Hard rule:** every stock change goes through `inventory.service.post_movement(s)` / `change_stock_state` with locks; never write a balance directly.

## Decisions (binding)

### 1. Transfers

Transfer dialog lines gain:
- a **batch picker** for batch-tracked items (reuse `features/dispatch/stock-pickers.tsx` BatchPicker);
- **from/to bin** pickers (a warehouse-filtered LocationPicker hook in `hooks/`).

Add **Edit draft** (PATCH) and **Cancel** (with reason) actions, gated on the exact rights and shown only on states the transfer machine allows.

### 2. Adjustments

- Add **Reject** (approver, with reason) and **Cancel** actions.
- Lines gain batch, bin and **unit cost** (a write-up needs a cost basis). Unit cost is visible and editable only with `inventory.valuation_view` / `item.cost_view`. Otherwise the server's fallback applies; say so in the dialog.
- Round 1 left a gap here: a stock adjustment's cost centre is not shown anywhere after it is raised. Show it, behind `cost_centre.view`, using B1's `CostCentreName`, as a list column and on the adjustment's detail/approve view. Keep the cost-centre picker in the create and edit dialogs.

### 3. Bins

**Pickers.** Add the LocationPicker to:
- GRN receive lines (procurement dialog)
- material-issue lines
- physical-count create (bin-scoped counts)

**Balances screen.** Add `location_code` to `BalanceRead` and a **Bin** column and filter on `/inventory`.

**Bin move (putaway) document.** Add `POST /inventory/bin-moves`, a document with lines (item, batch, from bin, to bin, quantity) inside **one warehouse**:
- It posts paired movements through the engine in one transaction.
- Choose the transaction types and source state that the engine accepts for a same-warehouse relocation without going in transit. Read `transfer_stock` and `validate_movement`. If needed, add a dedicated pair `BIN_MOVE_OUT/IN` to the TransactionType vocabulary with `TRANSACTION_SIGNS`, `SERIAL_MOVEMENT_TARGETS` and every meta-test that enumerates types. Document the choice (`§E8-1`).
- Permission: `inventory.transfer`.
- UI: a "Bin move" tab or action on `/inventory/movements`.

**Item default bin.** Add `items.default_location_id`, optional and validated (tenant, same default warehouse). It is pre-filled in pickers.

### 4. Scrap

**Visibility.** Add `scrap_quantity` to `WarehouseStockRow` / `ItemStockSummary` and show it on the balances grid and the item Stock tab.

**Scrap disposal document** (`scrap_disposals` + lines):
- Takes stock **out of SCRAP**. The engine forbids moves *from* SCRAP via `change_stock_state`, so add a dedicated outbound type `SCRAP_DISPOSAL` with sign −1 from the SCRAP pool. Do it with the meta-test updates and a documented rule.
- Disposal kind: `SALE`, `WRITE_OFF` or `RECYCLE`.
- Records an optional buyer or reference and the realised amount.
- Valued from the cost layers.
- Permission: new `inventory.scrap_dispose`, with a grant migration.

**Scrap register report:** ReportKey `SCRAP_REGISTER`, covering scrap in by source and scrap out by disposal. Include the CHECK-swap migration.

### 5. Consumables

- Add `WarehouseType.CONSUMABLES` (classification only) and seed it.
- **Internal issue document** (`internal_issues` + lines): issue to a **department and/or cost centre** with a reason.
  - Posts `MATERIAL_ISSUE` with `reference_type="internal_issue"`.
  - Needs approval above `INTERNAL_ISSUE_APPROVAL_VALUE` (a named constant, the same pattern as the adjustment threshold), using the state machine DRAFT → PENDING_APPROVAL → POSTED / CANCELLED.
  - New permissions: `internal_issue.create` and `internal_issue.approve`, with a grant migration.
  - UI: a tab on `/inventory/movements`.
- **Report** `CONSUMPTION_BY_DEPARTMENT`: per department / cost centre and item, over a period.

### 6. `can_scrap`

Enforce it in inventory `validate_movement` for any movement whose target pool is SCRAP: SCRAP transactions, QC SCRAP decisions and job-work scrap-at-vendor. Refuse with a clear `BusinessRuleError`. QC offers DAMAGED instead. Add tests in each path.

### 7. Placement and conflicts

- Keep inventory changes inside the inventory module.
- The engine vocabulary changes (new types) are shared contract. Update `docs/database.md` §4.2 and every test that enumerates TransactionType.
- Do **not** touch allocation, sales or dispatch; lane A and WP E2 own those this round and next.

## Frontend

All dialogs follow the conventions, are responsive, and are tested by request body and gating. New screens (bin move, scrap disposal, internal issue) are tabs, so no new routes are needed. If you add routes, include the sweeps.

## Docs

- `database.md` §4: new types and pools, plus new tables.
- `assumptions.md` `§E8-n`.
- `api.md`.
- Handbook: the Inventory chapter.

## Tests

- **Unit:** sign tables, the serial targets meta-test, the bin-move validation rules.
- **Integration:**
  - transfer with batch and bins
  - adjustment with batch, bin and cost; reject and cancel
  - bin move conserves stock and cost
  - scrap visible, then disposed (SCRAP pool decreases; ledger valued)
  - internal issue approval threshold
  - `can_scrap` refusals
  - reports sum correctly
  - list lists
- **Frontend:** bodies and gates.
