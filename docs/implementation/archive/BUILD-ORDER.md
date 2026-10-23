# Build Order — closing the proposal gaps (local-only)

> Historical snapshot: 13 October 2026. The statuses below describe that baseline.
> See the [current standards verification](../development-standards-20261022.md)
> and [local seed workflow](../../local-seed-data.md) for the latest verified work.

Derived from the [proposal gap checklist](PROPOSAL-GAP-CHECKLIST.md),
[specification index](../specs/SPEC-INDEX.md) and
[master plan](../specs/PLAN.md) R2–R7 schedule.
Rule: foundation before engine before execution before portal before alerts/reports. No remote, no worktree changes by this doc.

## Current lane state (13 Oct, from PLAN.md log)
- Committed: B2 (`c768493`), C1a/C1b review (`e1c0e7b`), R1 (A1/B1/C0).
- Uncommitted, resume first: lane A E1a+A2, lane D E8a, lane E E4.
- Deferred by you: C2a/C2b vendor app. C1a/C1b stay. Next on lane C is C3a.

## Order
### Step 0 — finish what is open (do not start new work)
1. Lane A: E1a dispatch-linkage + A2 sales-invoice (fixes K01/K08, dev worker `-Q`, unlocks finance/packing/invoice).
2. Lane D: E8a inventory-ops (transfer/adjust UI, bins, scrap, consumables, `can_scrap`).
3. Lane E: E4 assembly + QC-unification (lifecycle UI, one verdict path, packing hand-over seam).
4. Review + integrate R2. Why first: every later step assumes dispatch lines carry `sales_order_line_id`, invoices exist, bins/scrap work, assembly can complete.

### Step 1 — masters + execution prerequisites
5. E10a item-master (variants UI, prices, planning fields, vendor-link editor, discount authority) — unlocks MRP MOQ/multiple/lead-time + SO variant select.
6. B2-shifts review/merge (already committed) + E7a-production-completion part 1: complete/close capture, WIP tab, daily bookings, routing-machine select, BOM draft editor. Small, unblocks planning.
7. E5 quality-completion: production + final QC wiring, Raise-inspection dialog, inspection detail/photos, rework tab. Depends on E4 verdict seam.

### Step 2 — allocation + MRP core (proposal §05/06 heart)
8. E2 allocation-reallocation: approval tab, `to_demand_line_id` + owner sync, `allocated_quantity` commit step, customer priority, SOFT policy, cross-warehouse donors, spare flow, auto-proposal hooks.
9. E3a MRP depth: low-level-code netting, routing lead times, per-level offsets, expected dates. Depends on E10a + E2.
10. E1b sales-automation + trace: auto-plan on confirm, fulfilment trail, other-warehouse availability + transfer action (K02/K10/S01.1/S03.13).

### Step 3 — procurement + job-work close-out
11. E6a procurement-flow: PO INVOICED/PAID sync, req detail, quotation→PO, PO edit/close + duplicate re-check, GRN detail/cancel, invoice dispute UI, PO email. Depends on A2 + E4 QC write-back.
12. E7b job-work-completion: detail/timeline, receive options (`is_final`/excess), rework orders, challan register+PDF, vendor ledger tab. Depends on E4/E5.
13. E6b procurement-finance-vendors: return debit notes + return-aware match, QC-reject→return/replacement (K09), lead-time report, transport `vendor_id`, service-acceptance flow, RFQ PDF/email, rating recompute.

### Step 4 — outputs + portals
14. E3b MRP outputs: TRANSFER + REALLOCATION + JOB_WORK + ASSEMBLY targets, scheduled run, reorder + packing-material demand (fixes K06 packing shortage).
15. C1a/C1b hardening (already committed, needs review): vendor isolation allow-list, shipments/documents/PDF/receipts/invoices/payments, push channel.
16. C3a/C3b + C4a/C4b dealer portal + promotions + images (needs A2/A3 first). Keep C2a/C2b deferred per your call.
17. A3 returns/credit-notes, B3a job-invoice + B3b cost-centres-on-procurement, A4 collections/dealer-performance. These complete finance postings.

### Step 5 — packing/dispatch + traceability hardening
18. E9 packing-dispatch: packing-order create, packing specs + shortage requisition, carton labels/barcodes + scan, carton→dispatch link, planning board + edit, outbound LR/photo/POD. Depends on E1a/E3b/E8a.
19. E8b batch/serial: register screen, quarantine/expiry/FEFO, capture on GRN/production/assembly/transfer/adjust, reserved-serial dispatch. Depends on E8a/E9.

### Step 6 — cross-cutting commercial
20. B4 smart-alerts + events (needs C1a events, E1a dispatch, B3a/JW states). Fix QC link, dispatch fallbacks, approval-pending helper in same pass.
21. B5 procurement reports + ops e2e; D1 duplicate-PO actions + amendment flow (needs C0 + C1a PDF); D3 courier auto-track; D2 WhatsApp/SMS; D4 PO signature (needs C1a PDF + D1 revision). Ask Atcraft on D4: Ed25519 vs DSC/PAdES before building.
22. E10b finance-reports-dashboard: GL statement, trial balance, registers, GSTR-1/3B, permissions + export, actual profitability, management tiles. Depends on A2/A3/A4/B3a.
23. E12 docs: security-resolution report + legacy review, dead-code removal, deployment checklist sign-off.

### Step 7 — prove it
24. K scenarios e2e (K01–K10 journeys + Playwright) + handbook chapter. Fix what breaks; this is the proposal acceptance test.
25. Final integration (PLAN.md R8): full suites, migrate/seed/demo, stack up, e2e, handbook rebuild, hooks, commit.

## What to build this week (if capacity is 2 lanes)
- Finish Step 0 as-is (A, D, E lanes).
- Then: E10a + E5 (unblocks MRP + QC), then E2 → E3a.
- Defer: C2a/C2b (your call), D2/D3/D4 (needs C1a stable), E10b/E12/K (tail).

## Acceptance per step
Each step: service + router + permission + audit + idempotent release paths + UI action gated on the right + `POST` body vitest + integration test for the state transition + `make contract` regen. Reallocation/MRP/transfer steps each need one e2e (500/200 partial-ship; 100-kit common-part; urgent-order donor flow).
