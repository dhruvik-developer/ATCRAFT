# WP E3a: MRP engine depth (level-by-level netting, routing lead times, per-level offsets, expected availability dates)

Lane D, round 4, step 1. Placeholder `§E3-n`.

**Checklist IDs:** P05.P2, P05.P3, P05.P4, P06.F2, P06.F6, P06.F7, P06.F8. Read each one in `trace/trace.json`, and read `trace/notes.md` G3 #3 and G4.

**Read first:**
- `apps/api/src/modules/mrp/engine.py` in full, including its docstring on the current limitation (engine.py:121-149);
- the `_settle_demand` fixed point;
- `bom/engine.py` `_walk`;
- the existing MRP tests (`tests/unit/test_mrp_engine*.py`, `tests/integration/test_mrp*.py`);
- CLAUDE.md's MRP trap: "Do not read a value while the loop that computes it is still mutating it".

## Decisions (binding)

### 1. Low-level-code MRP (P06.F2, P05.P2–P4)

- Compute each item's **low-level code**, the deepest BOM level at which it appears across the BOMs in scope. Load BOMs in batched level queries, as the BOM engine already does.
- Process items **level by level**. For each item:
  1. Gross requirement = direct demand + dependent demand from the parents already processed.
  2. Net it against free stock, then in-time supply (POs, production, and **job-work output once E3b adds it**), keeping time-phasing.
  3. Explode only the **net shortfall** one level down, through single-level BOM lines (quantity per, scrap %).
- Keep common-part consolidation, which now falls out naturally because each item is netted once at its LLC. Keep `source_paths`/provenance, so the merge stays auditable.
- Keep cycle detection.
- **Required test (the proposal's example):** with 50 Rose Handles in stock, 100 Premium Lock Kits raise handle bodies, springs and bushes for **50** handles, not 100. Spring and screw demand is still merged across handle, lock body and kit.

### 2. Lead times (P06.F6, P06.F7)

- MANUFACTURE suggestions use `routing.service.manufacture_lead_time_days(item, quantity)`. That is the contract MRP was meant to call, and nothing calls it today. Read it in one batch per run through a new `MrpDataSource.manufacture_lead_times(...)` method.
- Fall back to `items.lead_time_days` when an item has no routing.
- PURCHASE suggestions keep the preferred vendor-link lead time, falling back to the item's.
- **Per-level offsets.** A child's required date = the parent order's start = the parent's required date − the parent's lead time. Do not offset only once from the root.

### 3. Expected availability date (P06.F8)

- **Suggestions.** Add `expected_available_date` to `mrp_suggestions`: `max(as_of, suggested_order_date) + lead_time`. A late suggestion therefore shows a realistic date.
- **Requirements.** Add it to `mrp_requirements`: the date the gross requirement is fully covered.
- **Demand lines.** Roll it up per `mrp_demands` line through provenance: the max over its components plus the parent's own lead time. Expose it on `GET /mrp/runs/{id}/demand`.
- **Sales order detail.** Show an "Expected availability" per line from the latest completed run that covered the order, with an "Use as promised date" action. That action updates `promised_date` through the existing PATCH and is audited.
- Migration: nullable columns.
- Update `docs/database.md` §6 (binding contract) and §7 where relevant.

### 4. Compatibility

- Suggestion types are unchanged in this package; new types come in E3b.
- Existing runs and suggestions stay readable.
- Golden expectations in existing tests may change, since that was the defect. Update each one **deliberately**, with a comment explaining the new correct number.
- Performance: no per-node queries. Assert query counts in a test with a 3-level BOM and 50 items.

## Frontend

- Run detail: a "Low-level" column and an "Expected availability" column.
- Shortage report: an expected availability column.
- SO detail: the expected-availability badge and the "Use as promised date" action.

## Tests

**Unit (pure engine):**
- LLC computation
- sub-assembly stock reduces component demand
- semi-finished stock reduces raw material demand
- common parts still merged
- per-level date offsets
- routing lead time used
- availability date maths
- the CLAUDE.md fixed-point trap is not reintroduced: an item demanded directly and also used as a component is phased consistently

**Integration:** a run against seeded data with a stocked sub-assembly.

**Frontend:** run detail columns; SO promised-date action body.

## Docs

- `database.md` §6
- `assumptions.md`: rewrite the MRP limitation notes; add `§E3-n` for the LLC method and the lead-time sources
- handbook: the MRP chapter
