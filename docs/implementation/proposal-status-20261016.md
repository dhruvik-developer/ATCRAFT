# Proposal comparison: October 2026 review and dated amendments

> This is a historical proposal audit with dated follow-up notes. Its original
> status rows and source line references describe the reviewed revisions. Use
> the [current documentation](../README.md) and
> [later verification record](development-standards-20261022.md) alongside the
> code before treating an older gap as current.

> **Production readiness refresh — 17 October 2026:** the full proposal is not
> ready for production sign-off. See the [current release review](production-readiness-20261017.md)
> for actual CI results, reproduced financial defects and remaining UI/API gaps.
> That review supersedes this document's release-status assertions.

Baseline reviewed on 17 October 2026 against the `develop` branch, including
the committed returns/credit-note work and the return-QC integration.

Source: [Atcraft Manufacturing ERP Development, August 2026, version 1.0](<C:/Users/evenmore/Downloads/Atcraft Manufacturing ERP Development.pdf>),
24 pages. Both the original and compressed PDFs are scanned documents. The
requirements pages were reviewed as rendered images and cross-referenced with
the repository's [proposal transcription](specs/PDF-CHECKLIST.md). Requirement
IDs below come from that transcription; E/A/B/C/D codes identify existing
implementation work packages, not additional proposal phases.

This is a source, route, UI and existing-test review. No new application tests,
migrations or business actions were run for this audit. A feature with only an
enum, database column, API stub or disabled button is not treated as delivered.
Earlier gap counts and completion percentages are not reused: they predate
substantial recent work. Active uncommitted work is marked **in progress**.
Detailed implementation choices are distinguished from literal PDF requirements;
the agreed work packages expand some of the proposal's broader descriptions.

**Progress update — 19 October 2026:** the E2 reallocation approval now carries
the recipient sales-order line through the API, updates/extends its single live
reservation, reduces the donor line's allocation, advances affected order
headers through the sales state machine, and provides a permission-gated
approval inbox on Reservations. The allocation integration tests and the
Reservations screen tests cover the new behavior.

**E2 remainder — 19 October 2026:** decision notifications now go to the
requester (`APPROVAL_GRANTED` / `APPROVAL_REJECTED`, with the decision note), a
reallocation request can be raised directly from a short sales-order line and
from a directly demanded MRP shortage row (the donor is still chosen by the
server), and the restricted-role matrix is covered at the HTTP boundary
(`tests/api/test_allocation_reallocation_http.py`). See
[`e2-reallocation-uat-20261019.md`](e2-reallocation-uat-20261019.md).

**Additional progress — 19 October 2026:** serial customer returns now enter QC
hold and require an explicit accepted/damaged/scrapped disposition per serial;
only accepted units are credited. Paid-return settlement, mixed QC/non-QC
return credits, credit cancellation, GST net of issued credit notes, and
purchase-invoice matching net of returned quantities have PostgreSQL regression
coverage. The updated Python dependency lock audits clean, and production images
build. CI-equivalent Trivy scans found zero fixable high/critical
vulnerabilities in either image; the backend still contains unfixed
high-severity Debian findings that the configured `ignore-unfixed` policy
excludes.

**Additional progress — 19 October 2026:** non-zero purchase returns now post
the vendor debit and input-tax reversal in the same transaction as inventory
movement and receipt/PO quantity write-back. PostgreSQL tests verify the
balanced AP/expense/tax journal and rollback of the stock return if finance
posting fails. The purchase-return screen explains the financial effect before
posting. Finance can now apply a posted debit to same-vendor, same-currency
approved invoices; PostgreSQL coverage verifies invoice settlement, both
allocation caps, replacement/removal and the invoice-cancellation guard. The
purchase-return detail UI supports editing that allocation set. Posted returns
also queue an audited debit-note PDF through the report-job pipeline, which
return readers can monitor and download without general report access. The
worker integration test now exercises API queueing, PDF storage and audit,
completion, and download with only purchase-return view permission.
Remaining: add the missing procurement cost-centre carrier and complete the
QC-originated return/replacement workflow.

**Additional progress — 19 October 2026 (migration baseline):** the
58-revision Alembic chain was squashed to one baseline, `20261021_0100`,
generated from the verified head schema. A fresh old-chain database and a fresh
baseline database matched on 134 tables, 2,771 columns, 3,392 constraints,
1,054 indexes, 4 triggers and 284 functions. The live development database and
a clone matched the baseline and were stamped with
`alembic stamp --purge 20261021_0100`; data counts were unchanged. The old chain
is preserved at git tag `pre-migration-squash-20261019`. See
[migration-squash-20261021.md](migration-squash-20261021.md) for evidence,
fresh/existing database procedures and recovery. The squash preserves the
deployed schema, including its known migration/model drift; those differences
still need forward migrations.

**Additional progress — 19 October 2026 (E3a, MRP engine depth):** MRP now
discovers each item's low-level code in batched per-level reads and nets every
item exactly once, at its deepest level: gross requirement (direct plus
dependent) against free stock and in-time supply - purchase orders and live
production output - then only the net shortfall is exploded through single-level
BOM lines. MANUFACTURE suggestions use the quantity-dependent routing lead time
with an item-master fallback, purchases keep the vendor-link lead, and every
child is offset by its parent's own lead time at every level. Requirements,
suggestions and demand lines carry `expected_available_date` (coverage date,
rolled up through child slippage; a late suggestion shows
`max(as_of, suggested_order_date) + lead`), added by migration `20261022_0100`.
Run detail and the shortage report show the level and the date; sales-order
detail reads the latest completed plan through
`GET /mrp/sales-orders/{id}/expected-availability` and offers an audited
"Use as promised date" action. PostgreSQL tests cover a stocked sub-assembly, the
date ordering and the availability endpoint. The deliberate behaviour change is
that live production output is netted as dated supply *before* the explosion, so
a released job reduces both the parent's shortage and its component demand; see
[assumptions §22](../assumptions.md) and
[the E3a spec](specs/E3a-mrp-engine-depth.md). Remaining MRP outputs
(job-work/transfer/reallocation supply, scheduled and capacity-aware runs) are
tracked under E3b.

**Additional progress — 20 October 2026 (E1b, sales automation and fulfilment trail):** confirming a short sales order now plans the balance in the same transaction, and the order page shows one trail from demand to cash. A sparse per-company `sales_settings.auto_plan_on_confirm` (NULL reads as the code default, on; `GET/PUT /sales/settings`, `settings.view`/`settings.manage`) controls automatic planning. `mrp/shortfall.py` raises only DRAFT documents: one production order per made line pegged by `source_type=SALES_ORDER`, `source_id` and the new `source_line_id`, and one purchase requisition per order for bought lines carrying `source_demand_refs`. It nets committed supply, never its own drafts, is idempotent per line, resizes or withdraws drafts when the order changes or is cancelled, reports job-work lines as `needs_job_work`, and degrades a capability mismatch to a warning rather than refusing confirmation. `POST /sales/{id}/plan-shortfall` and the allocation panel's **Plan shortfall** rerun it. The allocation read adds `available_elsewhere` (one grouped donor read); the panel names the best other warehouse and offers **Raise transfer**, pre-filling a normal transfer with the source warehouse, `min(shortfall, free)` and the order-line peg (`stock_transfers.source_type/source_id`, `stock_transfer_lines.source_line_id`); receipt does not reserve - the E1a top-up does. `GET /sales/{id}/fulfilment` returns one batched family per owning module (MRP, production, assembly, job work, requisitions/POs, receipts, QC, transfers, packing, dispatch, invoices, credit notes, returns) and the order detail renders the Fulfilment trail; production, assembly and job-work detail link back to the order, and packing/dispatch read the `sales_order_id` filter. Migration `20261023_0100`. See [assumptions §E1b-1](../assumptions.md) and [the E1b spec](specs/E1b-sales-automation-trace.md).

**Priority 2 progress - E3b job-work conversion:** MRP job-work suggestions can now create draft job-work orders when the planner supplies an approved vendor, process, expected return date and issuing warehouse. The hook delegates to the normal job-work creation service, preserving its tenant-scoped validation, document numbering and audit transaction. MRP run detail provides a dedicated one-suggestion form, restricts job-work conversion to users with the required screen permissions, and requires an explicit warehouse for runs spanning multiple or all warehouses. Open job-work output is now a dated MRP supply feed, so the next run no longer re-suggests work already at the vendor; its late tranches are reported separately from purchase and production supply. Transfer/reallocation/assembly conversions, scheduled/reorder runs and packing-material demand remain open under E3b.

## What is already implemented

| Area                          | Current implementation; avoid reopening old completed work                                                                                                                                                                                                              |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Authentication and foundation | Login, password hashing, JWT/refresh sessions, MFA, RBAC, audit, organisation/cost centres, partner and warehouse masters. Final security handover evidence remains below.                                                                                              |
| Inventory operations          | Stock balances/ledger, FIFO, reservations, transfers/adjustments, counts, bins/bin moves, consumables issues and scrap disposal. Tracked-stock coverage and complete allocation workflows remain below.                                                                 |
| Item engineering              | Item/variant editing, sale/dealer prices and discount controls, item-vendor planning fields, multi-level BOM/common-part consolidation, BOM draft editing/operations, routing and machine picker. Specification/accessory masters are a separate gap.                   |
| Production                    | Production/work orders, material issue/return, operator/shift roster, planning capacity board/machine overlap checks, daily bookings, WIP values, completion/closure capture, actual/standard variances and production-cost report. Tracked output remains limited.     |
| Assembly and QC               | Assembly lifecycle/board, QC callbacks, assembly-to-packing handover; production/final QC, manual inspection, photos, decision history and rework records/report. Customer-return QC hold/link, serial disposition and accepted-only credit settlement are implemented. |
| Staff job work                | Order detail/timeline, challan register/PDF, receipt dispositions/loss handling, reconciliation, vendor ledger/export, job-work invoices and capitalised receipt costs/report. Damaged-stock reissue and vendor-app job work remain below.                              |
| Commercial foundation         | Sales orders, partial dispatch/top-up, issued sales invoices/PDFs, receipts/allocations, party ledgers, ageing/outstanding, basic GST summary; requisitions/POs/GRNs, quotations/comparison, vendor invoices/payments. Remaining integrations/screens are listed below. |
| Portal/delivery backends      | Scoped vendor roles/categories, PO acceptance/decline/date/ready, shipments/documents/QC/invoices/payments; dealer catalogue/cart/wishlist/checkout/orders/statement APIs; in-app/email and Web Push backend. These APIs are not complete vendor/dealer apps.           |

Production, assembly, item editing, staff job work and core QC should therefore
not be reported as wholly absent, as the older gap checklist sometimes does.

## Pending development

Priority 1 protects stock/money correctness or closes a central proposal journey.
Priority 2 completes the operating screens and supporting reports. These are
feature groups, not a count of individual PDF requirements.

| Priority / state                 | Work to finish                                                         | Proposal references / package                | Current evidence and exact remainder                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------------------------- | ---------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1 - Core implemented              | Customer returns and credit notes                                      | S02.8, P11.T6, P13.M4; A3                    | Dispatch-line selection/creation, serial capture, per-serial QC disposition, non-serial RETURN inspections, accepted-only automatic crediting, paid-return customer-credit settlement, issue/cancel routes, list/detail UI, accounting journals, PDF generation and generated contracts are implemented. PostgreSQL integration tests cover serial stock states, mixed QC/non-QC crediting, paid returns and cancellation; restricted-role and live-candidate UAT remain release evidence. |
| 1 - **Partial**                  | Urgent reallocation that the recipient can actually ship               | P03.F8, P05.S5-S9, K05; E2                   | Ranking and request/decision APIs exist. Sales-order requests now require a demand-line id; approval locks sales orders and lines before stock, releases the donor and extends or creates the recipient's single reservation, synchronizes both lines' allocated quantity/status, advances newly short or fully covered order headers through the state machine, and audits both sides. The [Reservations UI](../../apps/web/src/features/inventory/reservations-page.tsx) has an approval inbox gated by allocation view and approve rights. Requests can now be raised from a short sales-order line and from a directly demanded MRP shortage row through the shared [reallocation dialog](../../apps/web/src/features/inventory/reallocation-request-dialog.tsx), the requester is notified of the decision, and the restricted-role matrix is covered at the HTTP boundary. See [E2 UAT](e2-reallocation-uat-20261019.md). Full package remains partial: policy settings, priorities, cross-warehouse execution, automatic proposals and spare requests are still open (below). |
| 1 - Core implemented             | MRP quantity and date correctness                                      | P05.P2-P4, P06.F2/F6-F8; E3a                 | [MRP engine](../../apps/api/src/modules/mrp/engine.py) discovers low-level codes in batched per-level reads and nets every item once, at its deepest level: gross (direct + dependent) against free stock and in-time supply (purchase orders, production and job-work output), then only the net shortfall is exploded through single-level BOM lines with per-level lead-time offsets (routing lead for manufacture, vendor-link lead for purchases, item master as fallback). `mrp_requirements`, `mrp_suggestions` and `mrp_demands` carry `low_level_code` / `expected_available_date` (migration `20261022_0100`); run detail and the shortage report show both columns, and sales-order detail reads the latest completed plan through `GET /mrp/sales-orders/{id}/expected-availability` with an audited "Use as promised date" action. PostgreSQL tests cover a stocked sub-assembly and the availability endpoint. Remaining MRP outputs (transfer/reallocation supply, scheduled runs) are tracked under E3b. |
| 1 - Core implemented             | Automatic sales shortfall planning and full fulfilment trail           | S01.1, S03.13, K02/K08/K10; E1b              | Confirming a short order reserves what is available, then - with the sparse per-company `sales_settings.auto_plan_on_confirm` on (NULL reads as the code default, on) - calls [mrp/shortfall.py](../../apps/api/src/modules/mrp/shortfall.py) in the same transaction. MAKE and MAKE_OR_BUY-with-a-default-BOM lines get one DRAFT production order each, pegged by `source_type=SALES_ORDER`, `source_id` and `source_line_id`; BUY lines get one DRAFT requisition per order with `source_demand_refs`; JOB_WORK is returned as `needs_job_work`. Planning nets committed supply, never its own drafts, resizes/withdraws drafts on line or date changes and cancellation, and treats a capability mismatch as a warning rather than a refused confirmation. `POST /sales/{id}/plan-shortfall` and the allocation panel's **Plan shortfall** rerun it. The allocation read adds `available_elsewhere`; the panel names the best donor and offers **Raise transfer**, pre-filled with the order-line peg, and the transfer follows normal approval/receipt. `GET /sales/{id}/fulfilment` returns the order-centred trail across MRP, production, assembly, job work, requisitions/POs, receipts, QC, transfers, packing, dispatch, invoices, credit notes and returns; the detail page renders it, and production/assembly/job-work detail link back. Settings live at `/admin/settings`; packing and dispatch read the `sales_order_id` filter. Migration `20261023_0100`. Deliberate boundary: MRP TRANSFER suggestions remain E3b. |
| 1 - Core implemented             | Batch/serial goods through every stock flow                            | P03.F3/F4/F6, tracked P08.M11/P09.M6; E8b          | [Batch and serial tracking](../../apps/web/src/features/inventory/tracking-page.tsx) now has lot and unit registers, registration, quarantine/activate/expire actions, serial trace and a batch list in FEFO order. The lot lifecycle, FEFO selection for unnamed issues, automatic `CONSUMED` when the lot's company-owned stock reaches zero and the daily expiry sweep with a 30-day warning are enforced in [inventory service](../../apps/api/src/modules/inventory/service.py). Goods receipts, production bookings/completion, assembly completion, transfers and adjustments capture batch and/or unit identity; transfer and adjustment lines accept typed/scanned labels and resolve them server-side to registered units. Serial dispatch remains supported. Job work captures one batch per same-item line and carries it through the challan, receipt and ledger (migration `20261025_0100`). Deliberate boundaries: bin moves and internal issues stay quantity-only and refuse serial stock; serial-tracked job work and tracked SKU transformations are refused rather than silently losing lineage; label runs are typed or scanned rather than pattern-generated or printed. PostgreSQL tests pass; restricted-role and live UAT remain release evidence. |
| 1 - Partial | Purchase returns, vendor debits and rejected-QC replacement | P07.M8, P13.M2/M3/M9, K09; E6b | Stock returns and QC write-back exist. Non-zero posted returns write a same-transaction vendor debit journal (Dr AP, Cr purchase expense and input tax); finance allocations now apply that credit to approved invoices of the same vendor/currency, bounded by return credit and invoice payable, with a detail-screen workflow. PostgreSQL coverage verifies the balanced journal, allocation settlement/caps/replacement, and cancellation guard. Invoice matching subtracts returned quantities from accepted GRN or PO totals. Posted returns queue a retained, audited debit-note PDF; an API-to-worker integration test verifies the filed private PDF and return-view-only download. Remaining: carry purchase cost centres (the procurement line model has no cost-centre field), create QC-originated return drafts, and resolve credit/replacement outcomes. |
| 1 - Backend present, UI missing  | Vendor mobile app, push controls and buyer inbound board               | A02, A05, A07, A10.M2-M10; C2a/C2b           | [Vendor routes](../../apps/api/src/modules/vendor_portal/router.py) and [Web Push backend](../../apps/api/src/modules/notifications/push_router.py) exist. [Web routes](../../apps/web/src/app/route-loaders.ts) lack `/vendor/*` and `/procurement/inbound-tracking`. Build vendor screens/actions/uploads/QC/payment views, installable app/service worker and push opt-in/device controls, plus buyer shipment/vendor-date/decline/PDF panels.                                                                                                                                                                                                                                          |
| 1 - Partial / missing            | Smart alerts and unified approval workflow                             | P02.U3, A01, A08; B4                         | Delivery channels, module-specific approvals and one dispatch-delay sweep exist. Timed PO/delivery/tracking/QC/GRN/invoice/payment alerts, settings and deduplicated recovery are missing. The [audit approval API](../../apps/api/src/modules/audit/query_service.py) records decisions but does not perform the business action; production wiring and a usable approval inbox/settings UI are missing.                                                                                                                                                                                                                                                                                  |
| 1 - Partial                      | Consistent permissions for financial/value reports                     | P01.10, S02.14; E10b                         | Production/job-work cost reports are protected. [Profitability and catalogue inventory valuation routes](../../apps/api/src/modules/reports/router.py) still use `report.view`; their [definitions](../../apps/api/src/modules/reports/service.py) have not adopted all planned cost/value restrictions. Complete the permission matrix across catalogue, direct route, jobs, generated files and cost-bearing stock columns.                                                                                                                                                                                                                                                              |
| 2 - Partial                      | Remaining MRP outputs and automatic replenishment                      | P06.O3-O5/F5/F6, K06/K10; E3b                | Purchase/production conversions work; job-work suggestions create draft orders from planner-supplied vendor/process/return-date/warehouse inputs, and issued job-work output now nets as dated MRP supply with per-feed late reporting. TRANSFER exists as vocabulary but lacks execution; reallocation/assembly conversion, scheduled/reorder runs and packing-material demand remain.                                                                                                                                                                                                                                                                                    |
| 2 - Partial                      | Allocation policies, committed stock and spare/service demand          | P03.S4, P05.R5/R6/D1-D7/P6; E2               | HARD reservation machinery exists. Finish allocated/committed quantity writes, usable SOFT policy, coherent customer/order priorities and actual work/issue-state inputs, cross-warehouse execution, automatic proposals and the spare-request lifecycle/UI. `DemandType.SPARE` alone is not that workflow.                                                                                                                                                                                                                                                                                                                                                                                |
| 2 - Partial                      | Procurement lifecycle screens and status synchronization               | P07.M1/M3/M5/M6/M9, A04; E6a                 | Requisition/GRN detail APIs, PO edit/close and invoice dispute APIs exist. Detail routes/screens, PO edit/short-close controls, quotation-to-PO action, dispute/resolve/cancel UI, update-time duplicate checks and automatic PO INVOICED/PAID synchronization remain. See [PO detail](../../apps/web/src/features/procurement/purchase-order-detail-page.tsx) and [quotation page](../../apps/web/src/features/procurement/quotations-page.tsx).                                                                                                                                                                                                                                          |
| 2 - Partial                      | Duplicate PO resolution and amendment history                          | A03, A10.S1/S2; D1                           | Detection/category splitting/override exist. [Duplicate dialog](../../apps/web/src/features/procurement/purchase-order-duplicate-dialog.tsx) explicitly lacks executable merge/increase/cancel paths. Finish these commands, BOM/category-demand evidence, separately approved duplicate creation, revision history and vendor re-acceptance.                                                                                                                                                                                                                                                                                                                                              |
| 2 - Partial                      | Purchase cost centres, transport/service vendors, lead times and email | P02.O5, P07.M2/M13/V5/V6, A04.5; B3b/E6a/E6b | Cost-centre master and invoice postings exist; carry centres through requests/POs/conversion. Transporter masters/types exist but need vendor/payables linkage. Finish non-stock service acceptance/matching, measured lead-time/adopt controls, rating refresh and PO/RFQ email with attached PDFs/outbox/delivery feedback. Current PO PDF and manual vendor ratings should not be listed absent.                                                                                                                                                                                                                                                                                        |
| 2 - Partial                      | Packing specifications and automatic box/material requisitions         | S03.11, P12.M1/M2, K06; E9/E3b               | Packing/sealing issues materials and assembly-to-packing works. [Packing page](../../apps/web/src/features/packing/packing-page.tsx) offers standalone and sales-order-origin packing creation. Packing creation and line replacement now serialize on the sales-order header and reject quantities exceeding ordered demand after dispatched, active packing, and open dispatch quantities are deducted. Item packing specifications, shortage detection and a linked/deduplicated purchase request remain.                                                                                                                                                                     |
| 2 - Partial                      | Printable barcode labels, carton scans and dispatch planning           | P12.M3/M5/M6/M8/M9, A06.13; E9               | Barcode strings, carton contents/dispatch matching, packing PDF and current consignment tracking exist. Carton labels render as browser-printable CODE128. Successful scans write `SCAN` audit events, and audit readers see recent scan history. Dispatch release captures gate date, revised expected delivery, LR, vehicle, tracking id and location; the server-recorded gate timestamp is visible on the tracking timeline. Dispatches in `NOT_DISPATCHED` or `READY_FOR_DISPATCH` can now amend their transport details with `dispatch.update`; loaded lines, warehouse and destination are deliberately unchanged. Forward dispatch planning, loaded-line amendments, and outbound photo/LR/POD attachments remain. Do not relist carton-to-dispatch linkage as absent.                                                                                                                                                                                                                  |
| 2 - **In progress / UI missing** | Dealer commerce, storefront and promotion management                   | S02.3/.8/.9/.12/.15; C3b/C4a/C4b/A5          | Core dealer APIs exist. Banner model/migrations and permissions are active WIP. Finish dealer invoice/credit/return/PDF and safe-image APIs, promotion CRUD and frontend, dealer account binding and the entire `/dealer/*` storefront. Staff `/dealers` is a directory, not this app. Add confirmed/dispatched/delivered/invoice/return/reminder events to dealer recipients; enum names alone are not emitters.                                                                                                                                                                                                                                                                          |
| 2 - Missing workflow             | Payment follow-up and dealer performance                               | P13.M11, P14.9; A4                           | Payments, outstanding and ageing exist. Add follow-up worklist, notes, promises, reminders/escalation, overdue recovery, dealer-performance report and receivable dashboard links. There is no collections module/report key in the reviewed code.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 2 - Partial                      | Finance reports, actual profitability and management KPIs              | P13.M9, P14.9/.11/.19, A09; E10b/B5          | Ledgers, basic GST summary, production/job-work costs, stock/operational reports exist. Finish missing dealer/purchase/inbound reports and richer management KPIs. [Profitability](../../apps/api/src/modules/reports/repository.py) still uses order revenue and standard cost, not posted invoice/credit revenue and FIFO actuals. GL drill-down, trial balance, registers, GSTR preparation exports and export-any-size are further agreed work-package deliverables, not separately named PDF line items.                                                                                                                                                                              |
| 2 - Missing                      | Product specifications and accessories                                 | S02.4/S02.5; D5                              | Brand/manufacturer and size/finish/colour variants exist. Governed specification masters, inherited category templates, typed values, accessory relations, completeness/filter controls and UI panels are absent from [product models](../../apps/api/src/modules/products/models.py) and item screens.                                                                                                                                                                                                                                                                                                                                                                                    |
| 2 - Partial                      | Job-work rejected-stock reissue and vendor process updates             | P10.M4/M8, A01.M8; remaining E7b/C2          | Staff quantities/costs/challans/ledger work. [Create rework draft](../../apps/web/src/features/job-work/job-work-order-form-dialog.tsx) explicitly only issues AVAILABLE stock; a safe DAMAGED-stock reservation/issue path is still needed. Vendor job-work list/detail/status events, screens and reminders remain outside the completed staff slice.                                                                                                                                                                                                                                                                                                                                    |
| Release evidence pending         | Security handover, cleanup and proposal acceptance                     | P01.1/.2/.D5/.D6; E12/K                      | Existing [security documentation](../security.md) and [deployment checklist](../deployment.md) are real. Finish the dedicated issue-to-fix/evidence/legacy-mapping report, documented dead-code/dependency review, final settings/role/runbook updates and UAT across all ten PDF scenarios. Existing [browser journey](../../tests/e2e/specs/order-to-receipt.spec.ts) explicitly stops at goods receipt, not the whole production-to-payment flow.                                                                                                                                                                                                                                       |

## PDF future enhancements and optional implementation extras

The PDF itself labels WhatsApp notification, courier API/auto tracking, vendor
rating/payment dashboard and PO digital signature as **Future Enhancement** on
page 21. The repository's [14 October full-build checkpoint](README.md) records
these being restored to the expanded project scope. They are therefore pending
for that full scope, while remaining distinct from the PDF's Must Have tier.

| Item                                                | Present versus pending                                                                                                                                                                 |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| WhatsApp/SMS - D2                                   | In-app/email/Web Push exist; WhatsApp/SMS providers, consent, templates, delivery history and workers do not.                                                                          |
| Courier integration/auto tracking - D3              | Manual tracking fields/updates exist; provider accounts/client/webhook/polling/refresh and admin UI do not.                                                                            |
| Vendor rating/payment dashboard                     | Rating and payment summary/list APIs exist. Vendor-facing dashboard UI and the scheduled/refined rating policy remain.                                                                 |
| PO application signature - D4                       | PO PDFs exist; signing/digest records, approval integration, QR verification and history do not. The client selected certificate-backed DSC/PAdES rather than an app-only signature; the [compliance/procurement memo](d4-dsc-pades-compliance-20261019.md) is ready, and legal/compliance confirmation plus certificate procurement precede implementation. |
| Public catalogue, ITC-04, ZPL/loading-scan variants | Optional in the implementation specs; keep separate from missing mandatory proposal workflows.                                                                                         |

## Ten proposal scenarios

These are code/wiring findings, not a fresh signed UAT result.

| Scenario                                  | Current status and remaining acceptance work                                                                                                                  |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| K01 - Direct finished-goods sale          | Reservation, dispatch and invoicing paths exist; include the full business acceptance journey.                                                                |
| K02 - 120 available of 500                | Automatic reservation of 120 and a DRAFT production order for the remaining 380 exist (E1b); release/complete the draft and verify the top-up reserves the output. |
| K03 - Part sold and also used in BOM      | Multi-role item/stock and BOM behavior exist; retain regression/UAT proof.                                                                                    |
| K04 - Common parts                        | Consolidation exists. Correct nested-stock/live-job netting still needs E3a; do not equate common-part grouping with a fully correct multi-level supply plan. |
| K05 - Urgent reallocation                 | Pending recipient linkage/owner updates and a complete approval UI in E2.                                                                                     |
| K06 - Finished product ready, box missing | Material issue exists; automatic packing-material purchase request is pending E9/E3b.                                                                         |
| K07 - Vendor coating process              | Staff sent/received/rejected/pending tracking, costs and documents exist. Vendor app and damaged-stock reissue are remaining extensions.                      |
| K08 - Dispatch 200 of 500                 | Partial dispatch of the reserved 200 works, and confirmation raises draft supply for the balance 300 (E1b); receive/produce it and verify the top-up reserves it. |
| K09 - Incoming quality rejection          | QC disposition exists; linked vendor return/replacement and financial settlement remain E6b.                                                                  |
| K10 - Another warehouse can supply        | Allocation now names the best other warehouse and offers Raise transfer pre-filled against the order line (E1b); the transfer follows normal approval/receipt and receives no soft reservation. Automatic MRP balancing remains E3b. |

## Current release state

- **Returns/credits:** the core module, permissions, migrations, creation UI,
  credit-note routes, accounting, list/detail screens, return-QC hold/link and
  idempotent quality-owner settlement callback and automatic return-linked
  crediting are implemented on `develop`. Non-QC serial capture is implemented;
  credit-note PDF queueing, rendering, audited document storage and the frontend
  polling/download flow are implemented. Serial QC disposition and PostgreSQL
  end-to-end verification remain release gates.
- **Batch and serial tracking:** E8b is implemented on `develop`. The
  `/inventory/tracking` register covers lot lifecycle, expiry/FEFO and serial
  trace; receipts, production, assembly, transfers and adjustments capture lot
  and unit identity, and job work carries one batch per same-item line. Serial
  job work, tracked SKU transformations and quantity-only bin moves/internal
  issues refuse rather than bypass unit identity. Live UAT and a barcode-label
  print service remain outside this release.
- **Sales automation and trail:** E1b is implemented on `develop`: automatic
  idempotent draft planning on confirmation and on demand, reconciliation after
  order changes, other-warehouse transfer suggestions and the full order
  fulfilment trail. The automatic plan deliberately stops at DRAFT documents;
  planner release remains the commitment decision. MRP TRANSFER suggestions,
  reallocation supply and scheduled runs remain E3b.
- **Dealer banners/promotions:** model, migration, permissions and service
  groundwork exists; the public API, screens, security tests and acceptance
  evidence are still required.
- **Migration safety:** the historical Alembic chain is preserved at tag
  `pre-migration-squash-20261019`, and the squashed baseline is verified; see
  [`migration-squash-20261021.md`](migration-squash-20261021.md). Existing
  databases are stamped with `alembic stamp --purge 20261021_0100`, never
  upgraded through the baseline.
- This document is an implementation status report, not a claim that the full
  proposal is production-complete. The remaining rows require staged releases,
  database-backed integration tests and restricted-role UAT.

## Suggested execution order

1. Finish and verify the active returns/credit and dealer-content work; preserve
   it rather than starting a competing implementation.
2. E2 shortage-origin requests, E3a multi-level netting/dates and E1b shortfall
   automation/trail are done; next is E3b outputs (transfer/reallocation
   supply and scheduled runs).
3. Build C2a/C2b vendor app/inbound/push in parallel, since much of its backend
   already exists. B4 alerts/approval integration can follow shared workflow
   contracts rather than being invented separately in every screen.
4. Tracked goods E8b is implemented; continue procurement/returns/matching
   E6a/E6b/D1/B3b and packing/dispatch E9 to close the remaining stock/money
   and K06/K09 paths.
5. Finish dealer storefront/collections, specifications/accessories, missing
   financial/purchase reports and the agreed future integration tier.
6. Run all ten scenario journeys with restricted roles and realistic quantities,
   including negative cases, before final UAT, deployment and handover evidence.

GSTR formats, expiry/FEFO policies, exact priority scales, thermal-label formats
and other detailed work-package choices should remain labelled implementation
decisions or enhancements where the literal PDF is less specific.
