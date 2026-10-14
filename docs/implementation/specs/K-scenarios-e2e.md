# WP K: Key business scenarios K01–K10, end to end

Lane D, round 7. Placeholder `§K-n`.

The proposal's "Key Business Scenarios Covered" (p.22) are its acceptance tests. Every scenario must work end to end through the API and, where the proposal implies a person acts, through the UI. Read K01–K10 in `PDF-CHECKLIST.md` and `trace/trace.json`. The audit found most of them broken on the original main; WPs E1a, E1b, E2, E3a, E3b, E4, E6b and E9 were built to fix them.

## Deliverables

### 1. Backend scenario test suite

Create `apps/api/tests/integration/test_key_scenarios.py`: one test per scenario, against real Postgres, seeded through `tests/seed_support`. Each test asserts the proposal's sentence literally:

| Scenario | What the test asserts |
|---|---|
| K01 | Dealer orders 100 handles. Finished-goods stock is checked and reserved (HARD), then dispatched through the dispatch service with a UI-shaped payload. The reservation is consumed, `dispatched_quantity` = 100, and the order is DISPATCHED. |
| K02 | 500 ordered with 120 available. 120 reserved; a DRAFT production order of 380 is created automatically and pegged to the line (auto-plan on); after production completes, top-up reserves 380 and the order becomes ALLOCATED. |
| K03 | One item sold directly and used in a BOM: one stock, both roles work. |
| K04 | The proposal's lock example (100 kits, 50 rose handles, 25 lock bodies). Spring and screw totals are merged once; with E3a, stocked sub-assemblies reduce component demand. |
| K05 | Production holds the stock and an urgent dealer order arrives. A reallocation is suggested, then approved by a separate approver; the urgent order's line is linked and dispatchable; the production order's reservation is reduced and its owner notified. |
| K06 | Finished goods ready but no box. Packing-order start creates a DRAFT requisition for the box, deduplicated. |
| K07 | Material sent for coating. Sent, received, rejected and pending quantities are visible on the job-work order detail and reconciliation. |
| K08 | 500 ordered with 200 available. 200 dispatched (partial); the balance of 300 is planned (production order or requisition) and reserved on arrival. |
| K09 | Received material fails QC. It moves to DAMAGED, a DRAFT purchase return is created, and the REPLACEMENT resolution re-opens the PO line. |
| K10 | Warehouse A short, B has stock. A transfer is suggested (SO panel and MRP TRANSFER), raised, and received; the order becomes ALLOCATED. |

### 2. Playwright journeys

Create `tests/e2e/specs/key-scenarios.spec.ts`. Follow the conventions: API-driven where a person would only press one button, UI-driven for the key screens:
- K01 dispatch from the SO detail
- K05 approval tab
- K08 partial dispatch
- K10 Raise transfer

These run in the final round against the live stack. Write them so they pass there, and run them yourself only if a stack is already up. Never start or stop Docker.

### 3. Handbook

Add a "Business scenarios" chapter that explains each K scenario as the system now handles it, with the screens involved.

### 4. Fix what breaks

If a scenario test fails, fix the product code. Do not weaken the test. Keep the fix small and within the owning module's rules, and record it in the commit body.

### 5. Report

Return a scenario-by-scenario table: pass or fail, plus what you fixed.
