# WP E5: Quality completion: production and final QC, manual inspections, inspection detail and photos, rework tracking

Lane B, round 4, step 1. Placeholder prefix: `§E5-n`.

**Checklist IDs to close:** S03.10, P11.T2, P11.T5, P11.F3, P11.F5, and the QC-related part of P01.2 (`routing_operations.qc_required` is read nowhere). Read each in `trace/trace.json`.

**Already on main:** E4's owner-callback registry in quality, with a `production_entry` placeholder.

## Decisions (binding)

### 1. Production QC

- **New columns.** `production_orders.requires_qc` (default true when any routing operation has `qc_required`, or when the item has the new flag `items.requires_production_qc`) and `items.requires_final_qc`. Both are editable in the item form (E10a is on main; add the fields) and on production order create.
- **Holding output.** When `requires_qc` is set, the production entry posts good output into **QC_HOLD** and raises `quality.create_in_process_inspection(reference_type="production_entry", ...)`.
- **Releasing.** The E4 owner handler `production.record_quality_verdict` marks the entry inspected. The order cannot COMPLETE while any of its output is held. Complete is refused with a message naming the open inspections. Quality posts the movements; the handler never moves stock.
- **Routing gate.** When a routing operation has `qc_required`, its work order cannot be completed until an inspection on that operation (`reference_type="work_order"`) is ACCEPTED.
- **Copy fix.** Correct the quality empty-state text on `quality-page.tsx:249`.

### 2. Final QC (P11.T5)

- **Raising the inspection.** For items with `requires_final_qc`, starting a packing order raises a FINAL-stage inspection per line (`reference_type="packing_order"`; add the stage mapping if missing). Stock is not held: the goods are reserved for the SO.
- **Blocking.** Packing complete and hand-over are refused until those inspections are ACCEPTED.
- **Rejection.** REJECTED moves the stock to DAMAGED through quality, and the packing line must be replaced.

Document both as `§E5-n`.

### 3. Manual inspection

Add a "Raise inspection" dialog on `/quality` (`P.QC_INSPECT`) that calls the existing `POST /quality/inspections`. Fields:
- stage: incoming / in-process / final / return
- item, warehouse, batch, quantity, `hold_stock`
- optional template
- reference
- reason

### 4. Inspection detail, photos and attachments (P11.F3)

- **Read-only detail.** Make any inspection openable from a row click, at any status. Show readings per parameter, the rejection reason, decided quantities, history, and **photo thumbnails**. Load images as blobs through `/documents/{id}/download`; never as public URLs.
- **Inspection-level attachments.** Allow attachments on the inspection itself, including inspections without a template: `qc_inspection_documents` link rows, or documents with `entity_type="qc_inspection"` and a kind. Document which (`§E5-n`).

### 5. Rework tracking (P11.F5)

- **Rework tab.** Add a "Rework" tab on `/quality`, using `rework_only` plus status filters.
- **`qc_rework_records` table.** Columns: `inspection_id`, `quantity`, `assigned_to` (user) or `work_center_id`, `started_at`, `completed_at`, `outcome` (`REWORKED_OK | SCRAPPED | RETURNED`), `cost` (Money, optional), `notes`. It records each rework cycle, with a state machine OPEN → IN_PROGRESS → DONE.
- **Re-inspection.** Completing rework re-opens the inspection for re-decision (REWORK → IN_PROGRESS if the machine allows it; otherwise add the edge, with tests). Earlier decisions stay in a `qc_inspection_decisions` history table (ImmutableLedgerEntity), so cycles are not lost by overwriting the five quantities.
- **Report.** `REWORK_RATE` ReportKey: rework quantity and rate per item and vendor over a period. Include the CHECK-swap migration.

### 6. Permissions

Reuse `qc.inspect`, `qc.decide` and `qc.view`. No new rights unless one is truly required.

## Tests

- **Integration:**
  - production output held, then decided, after which the order can complete;
  - complete is refused while output is held;
  - the routing `qc_required` gate;
  - final QC blocks hand-over until accepted;
  - manual raise;
  - attachments;
  - a rework cycle keeps history and re-decides;
  - the report sums correctly.
- **Frontend:** the raise dialog body; the detail view's photos (blob fetch mocked); the rework tab.

## Docs

- `database.md`: §7 machines and §8 events.
- `assumptions.md`.
- Handbook: the Quality chapter (remove the "cannot be raised by hand" text), Production and Packing.
