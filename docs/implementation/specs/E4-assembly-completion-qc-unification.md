# WP E4: Assembly line completion, plus one authoritative QC verdict path

Lane B, round 2, step 2 (after B2). Assumption placeholder prefix: `§E4-n`.

**Checklist IDs.** Assembly: S03.6, P09.M1, P09.M3, P09.M4, P09.M5, P09.M6 (the Start part only; tracked outputs belong to E8b), P09.M7, P09.EX. Also P03.T4, P05.R3 (priority and date part), and the QC hand-back items P07.M7, A04.11, A04.12, P10.M7, P11.T3 and P11.T4, plus the qc_status part of P07.M6.

**Background.** Read these IDs in `trace/trace.json`; `trace/notes.md` (G2 #4, G6 #1–#2, G5 #1, G7 #2); `reports/returns-inventory-qc.md` §3 (quality internals, ports); and CLAUDE.md's trap "A button offered on a status the state machine cannot leave".

## Decisions (binding)

### 1. One QC verdict path, with an owner callback in quality

Quality is downstream of procurement, assembly and job work: they reach quality through tolerant ports. So quality may call them.

- Add `quality.owners`: a small registry from `reference_type` to an owner handler. It uses lazy imports and is resolved once.
- `decide_inspection` (and close, if it settles anything) calls the handler **in the same transaction**, after posting its own stock movements. The callback passes inspection id, reference id, decided status and quantities: accepted, rejected, rework, hold, scrap, still_held.
- Handlers **never move stock**. Quality already moved it. That is the whole point.

The handlers:

| Reference type | Handler | What it does |
|---|---|---|
| `goods_receipt` | `procurement.record_incoming_inspection_outcome` | Sets the GRN line `qc_status` PASSED/FAILED once nothing is held. When no lines are PENDING, takes the PO QC_PENDING → QC_COMPLETED → GRN_COMPLETED through `_transition`. Add reverse/exit edges only if required. Clears the QC_PENDING strand. |
| `assembly_order` | `assembly.record_quality_verdict` | Applies the accepted and rejected tallies and moves QC_PENDING → COMPLETED without posting movements. |
| `job_work_receipt` / `job_work_order` | `job_work.record_quality_verdict` | Marks the receipt released. Completes the order when nothing remains held. Allow per-receipt release while PARTIALLY_RECEIVED. |
| `production_entry` | no-op placeholder | WP E5 wires it. |
| `sales_return` | no handler | A3 pulls. |

The existing `POST /assembly/{id}/qc-result` and `POST /job-work/{id}/qc-result` endpoints:
- When a linked open inspection exists, they **decide that inspection** through quality (one path), or refuse with a message pointing to it.
- They never post a second movement. Keep them for cases with no inspection, such as `requires_qc` toggled after the fact.

Add regression tests ("decide on /quality → the order completes → stock moved exactly once") for assembly, job work and GRN.

Job-work multi-output receipts: raise **one inspection per output item**, not one for the first output only.

### 2. Repair stranded rows on deploy

Write a migration or idempotent backfill:
- POs stuck in QC_PENDING whose inspections are all decided;
- GRN lines still PENDING with decided inspections;
- assembly and job-work orders stuck in QC_PENDING with decided inspections.

Prefer an idempotent maintenance task `maintenance.reconcile_quality_owners` that runs the same handlers as the system actor with audit, scheduled once via beat at startup plus daily (cheap when there is nothing to do). Pure SQL in a migration cannot write proper audit rows. Document this (`§E4-1`).

### 3. Assembly UI (`/assembly`)

- **New assembly order dialog.** Fields: item (`can_manufacture`), an approved ASSEMBLY or PRODUCTION BOM select, planned quantity, component and output warehouses, assembly line, planned start/end, `requires_qc`. Posts `POST /assembly`.
- **Edit / Delete draft.**
- **Lifecycle buttons**, each gated on its exact permission, shown only on states the machine allows, with a reason where required: Approve, Release (show shortage details from the API), Start, Close, Cancel.
- **"Issue components" top-ups** fail today. Fix one of two ways: allow top-ups in `issue_components` (skip the transition when already COMPONENTS_ISSUED/IN_PROGRESS, still audit), or restrict the UI to RELEASED. Prefer allowing top-ups.
- **Record QC verdict** opens the linked inspection's decide dialog from quality (reuse the component).
- **Assembly board tab:** open orders grouped by assembly line, planned against actual, with a lateness badge.
- **Reservations:** pass the order's priority and planned start date into its component reservations. Today they default to 100 and no date.

### 4. Hand-over to packing

- On `/packing`, add a "Ready from assembly" panel listing `GET /assembly/ready-for-packing`.
- Its **Create packing order** action creates a packing order prefilled from the assembly (item, quantity, warehouse; customer/SO from the assembly's source when present) and calls `assembly.mark_packed` through a port **in the same transaction**.
- Show the packing order on the assembly detail.
- Fix the packing empty-state copy that wrongly claims automatic creation.
- WP E9 (round 6) adds the general packing-order dialog. Keep the creation service reusable.

### 5. Item group

Add `ItemGroup.ASSEMBLY` and `SUB_ASSEMBLY` with defaults in `default_capabilities`, the web `ITEM_GROUPS` list and badges. `item_group` has no CHECK, so no migration is needed. The rule "nothing branches on item_group" still holds.

### 6. MRP assembly target

Not in this package. WP E3b adds the ASSEMBLY_ORDER conversion target. Keep `assembly.service.create_order_from_mrp`-style creation reusable (`source_type`, `source_id`).

## Frontend tests

- Every assembly action's request body and gating.
- Board render.
- The ready-from-assembly flow.
- QC verdict opens the inspection.

## Docs

- `database.md` §7: assembly, PO and job-work machines as now driven; §8: the "QC verdict → owner" event.
- `assumptions.md` (`§E4-n`).
- Handbook: Assembly, Quality, Packing. Remove the "cannot be operated from the UI" and "no verdict can be recorded" notes.
- CLAUDE.md: add a trap entry, "Two release paths for one hold": what happened and the rule now.
