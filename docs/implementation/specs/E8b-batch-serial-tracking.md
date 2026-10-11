# WP E8b: Batch and serial tracking, end to end

Lane C, round 6. Placeholder `§E8b-n`.

**Checklist IDs to close:**
- P03.F6
- The tracked-output parts of P08.M11, P09.M6 and S03.12
- Serial transfer and adjustment, deferred from E8a: P03.F3 and P03.F4

Read the IDs in `trace/trace.json`, `trace/notes.md` G3 #7 and G6 #3, and `docs/assumptions.md` §20, §23 and §25. Read the CLAUDE.md traps about batch versus serial gates **carefully**: `test_the_batch_and_serial_rules_disagree_on_purpose` must keep passing.

## Decisions (binding)

### 1. Batches & Serials screen

New page `/inventory/tracking`, gated on `P.INVENTORY_VIEW`. Add it to the sweeps.

**Batches tab**
- List and search by item, batch number, status and expiry.
- Register a batch through the existing `POST /inventory/batches`.
- Batch state machine (entity "Stock batch"): ACTIVE → QUARANTINED → ACTIVE, ACTIVE → EXPIRED, and → CONSUMED, which is set automatically when the batch's on-hand reaches 0.
  - Quarantine and expire actions take a reason.
  - Quarantining a batch moves its AVAILABLE stock to QC_HOLD through `change_stock_state`.
  - Expired stock is not issuable.
- New right `inventory.batch_manage`, with a grant migration.

**Serials tab**
- Search by serial.
- Register units through `POST /inventory/serials`; bulk registration from a range or a pasted list is bounded.
- Each unit shows its status, location and full movement trace (ledger by `serial_id`).

### 2. Expiry

- **Expiry sweep:** a beat task raises a STOCK digest notification for batches expiring within `BATCH_EXPIRY_WARN_DAYS` (30), plus the EXPIRED transition on expiry.
- **FEFO:** batch pickers order by expiry, first-expiry-first-out. Issue and dispatch default to FEFO when the user does not pick a batch (`§E8b-n`), but only where the engine already requires a batch choice.

### 3. Serial capture where stock enters

**GRN lines**
- Serial-tracked items capture serial numbers: typed, scanned, or auto-generated from a pattern.
- Units are minted on posting: one movement per unit at the line's state, QC_HOLD when inspected.
- This lifts the §20 refusal for serialised receipts. Record the new rule, `§E8b-n`, and update §20.

**Production output and assembly output**
- Capture serials or batch per booking or completion. Production and assembly use child tables or arrays and mint units.
- This lifts the tracked-FG refusals (`production/service.py:3013`, `assembly _assert_trackable`).
- Batch-tracked components in assembly get a batch selection at issue and completion.
- The serialised MAKE demo item DL-900 can now be produced. Update the demo note.

**Adjustments**
- `stock_adjustment_lines.serial_ids` (ARRAY, CHECK cardinality equals quantity).
- One movement per unit.

**Transfers**
- `stock_transfer_lines.serial_ids`.
- Units travel through IN_TRANSIT with relocation.
- Lift §25 only if the engine's in-transit serial rules can be satisfied. Otherwise keep the refusal and document why (`§E8b-n`). Prefer lifting.

### 4. Dispatch with reservations and serials

Lift the refusal at `dispatch/service.py:153-158, 1499-1508`: a reserved serial-tracked SO line consumes the reservation **per picked serial**.

### 5. Tests

**Unit**
- the batch machine;
- FEFO ordering;
- serial count checks.

**Integration**
- GRN mints serials;
- production mints serials and batch;
- assembly with batch components and a serial output;
- adjustment per serial;
- transfer of serials (or the documented refusal);
- reserved serial dispatch;
- quarantine moves stock to QC_HOLD;
- the expiry sweep;
- the trace view.

**Frontend**
- tracking page;
- serial capture inputs in the GRN, production, assembly and adjustment dialogs, checking the exact request bodies.

## Docs

- `database.md` §4 (serial and batch rules).
- `assumptions.md`: rewrite §20 and §25 to the new truth.
- Handbook: Inventory tracking chapter.
- CLAUDE.md: update the batch/serial trap text if the rules changed. Keep the "disagree on purpose" invariant if it still holds; if it does not, explain why it changed.
