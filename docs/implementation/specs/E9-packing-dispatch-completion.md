# WP E9: Packing and dispatch completion

Lane A, round 6. Placeholder prefix: `§E9-n`.

**Scope:** packing order creation, labels and barcodes, carton tracking, dispatch planning and edit, LR and attachments, and packing material specs with a shortage check.

**Checklist IDs:**
- P12.M1, P12.M3, P12.M5, P12.M6 (planning board and edit; the SO-line linkage was done in E1a), P12.M8
- A06.13 (outbound attachments)
- S03.11, K06 (packing-time check and requisition; the MRP demand pass is E3b)

Read each in `trace/trace.json`.

**Already on main:** E1a (dispatch lines linked to SO lines), E4 (assembly → packing hand-over panel, packing creation seam), E5 (final QC on packing).

## Decisions (binding)

### 1. Packing order creation (P12.M1)

- **"New packing order" dialog on `/packing`.** Fields: customer, warehouse, planned date, and an optional SO, which pre-fills the open allocated lines with `sales_order_line_id`.
- **"Create packing order" action on the SO detail**, shown for ALLOCATED / PARTIALLY_DISPATCHED orders.
- **Optional auto-create on full allocation.** Behind the company setting `AUTO_PACKING_ON_ALLOCATION`, default **off** (`§E9-n`).
- Fix every copy claiming automatic creation.

### 2. Packing material specs and shortage (S03.11, K06)

**`item_packing_specs` table.** Columns: `item_id`, `packing_material_item_id` (must be `can_purchase`; typically item group PACKING_MATERIAL), `quantity_per_unit` **or** `units_per_carton` (CHECK exactly one), `is_default`. Partial unique: one default per item and packing material.

**Packing panel on item detail:** add, edit and remove specs.

**Check on packing-order create/start:**
- compute the packing-material requirement from the specs;
- compare it with free stock in the packing warehouse.

**When short, in the same transaction:**
- create a **DRAFT purchase requisition** (new `source_type=PACKING_ORDER`, extend the CHECK with a migration) with `source_demand_refs` pointing at the packing order;
- deduplicate against open requisitions and POs for that item (one query);
- notify the purchase roles ("Packing material short for PK-…").

**Packing screen** shows "Requisition PR/… raised". Packing may still proceed on other lines.

ASSUMPTION `§E9-n`: requisition, never a PO; a packing material on hold does not block packing of lines that have their boxes.

### 3. Labels and barcodes (P12.M3)

- **Carton labels:** 100×150 mm PDF using reportlab Code128, containing:
  - company
  - carton barcode / number
  - customer and ship-to
  - SO / packing order
  - contents summary
  - gross weight
  - "n of N"

  Use a document-only report key `CARTON_LABELS` with a CHECK-swap migration, and a task.
- **Buttons:** "Print label" and "Print all labels" in the carton builder.
- **Packing list PDF:** draw real barcodes.
- **Scanning lookup:** `GET /packing/cartons/by-barcode/{code}`.
- **Optional:** ZPL output via a `format=zpl` text/plain endpoint for thermal printers (`§E9-n`).

### 4. Carton tracking (P12.M5)

On hand-over / dispatch create:
- set `dispatches.packing_order_id`;
- fill `dispatch_lines.carton_ids` from the sealed cartons;
- copy carton count and gross weight into `total_boxes` / `total_weight_kg`.

Also:
- **Carton status:** `PACKED → LOADED → DISPATCHED → DELIVERED`, via a state machine updated by dispatch events, plus `DAMAGED`.
- **"Find carton" screen** (barcode or serial): shows where it is and its history. Add it to the sweeps.
- **Optional loading scan step:** scan cartons onto a consignment and refuse foreign cartons.

### 5. Dispatch planning and edit (P12.M6, P12.M8)

- **Dispatch planning board on `/dispatch`:**
  - allocated or packed but undispatched SO lines, grouped by customer and required date;
  - "Create consignment" pre-filled with the SO lines, `packing_order_id` and cartons.
- **Edit dispatch dialog** (PATCH) for NOT_DISPATCHED / READY: transporter, vehicle, driver name and mobile, LR number and `lr_date` (new column), tracking ID, expected delivery, boxes, weight, freight, e-way bill.
- **Dispatch action:** the gate-time LR, vehicle and tracking fields on the Dispatch action are now used.
- **Actual delivery date:** pickable at confirm-delivery, no longer always today.

### 6. Outbound attachments (A06.13)

Typed dispatch documents: `LR_COPY`, `DISPATCH_PHOTO`, `POD` (proof of delivery), `INVOICE`.
- Reuse C1a's `document_kind` with `entity_type="dispatch"`.
- An upload panel on the dispatch timeline dialog, with camera capture on mobile.
- Uploading an LR copy or POD raises a grouped notification (`group_key` per dispatch) to the order's sales user and dispatch roles.

## Tests

**Integration:**
- packing order from an SO
- the packing spec drives a requisition when short, deduplicated
- carton labels render with a barcode
- the by-barcode lookup
- hand-over fills `carton_ids` and totals
- carton status flow
- planning board query
- dispatch edit
- attachments and notifications

**Frontend:** dialog request bodies, board, find-carton, print buttons (job flow).

## Docs

- Handbook: Packing and Dispatch chapters, scenario K06.
- `database.md`, `assumptions.md`.
