> **Post-audit update - 22 October 2026 (E8b).** The traceability findings below
> are the pre-E8b audit text and are left as written. `P03.F3` (stock transfer),
> `P03.F4` (stock adjustment), `P03.F6` (batch/serial where required), `P08.M11`
> (production completion) and `P09.M6` (assembly completion) are now PRESENT.
> `/inventory/tracking` registers lots and units, runs the lot lifecycle and
> traces a serial; receipts, production bookings/completion, assembly completion,
> transfers and adjustments capture batch and/or unit identity, with transfer and
> adjustment labels resolved server-side. FEFO selects one covering lot for an
> unnamed issue, and the expiry sweep emits one `STOCK_EXPIRY` digest per company
> per day. E8a/E8b therefore close the serial-entry, dispatch and bin bullets in
> G3 #7, and the cancel/edit, reject/cancel and batch/serial-create endpoint gaps
> in G3 #8 now have screens. G6 #3 is closed for receipts, production, assembly,
> transfers and adjustments; job work carries a batch but still refuses
> serial-tracked items, and bin moves/internal issues stay quantity-only and
> refuse serial stock. There is no barcode/PDF label print or pattern
> generation. The DL-900 demo note has been corrected: the six units
> still enter through OPENING_BALANCE because the seeded MAKE item has no
> structure and no purchase order, not because the application lacks a serial
> inbound path. See `docs/implementation/specs/E8b-batch-serial-tracking.md`.
>
> **Post-audit update - 19 October 2026 (E3a).** P05.P2-P4 and P06.F2/F6/F7/F8
> are closed: MRP now discovers low-level codes in batched per-level reads,
> nets every item exactly once at its deepest level (free stock, then dated
> in-time supply), explodes only the net shortfall through single-level BOM
> lines, uses the quantity-dependent routing lead time with per-level date
> offsets, and stamps expected availability on requirements, suggestions and
> demand lines - surfaced on run detail, the shortage report, the sales-order
> detail screen and `GET /mrp/sales-orders/{id}/expected-availability`. The
> entries below are the pre-E3a audit text and are left as they were.
> See `docs/implementation/specs/E3a-mrp-engine-depth.md`.

================= G1
Scope and baseline. This is a read-only audit of the main tree C:\Project\Other\ATCRAFT at HEAD cdac4e0, which was unchanged throughout. No round-1 lane (A1, B1, C0) is merged, so every status describes main. Paths in the evidence are relative to the repository root.

Planned work packages. SPEC-INDEX gained D1–D5 during the audit. D5-specifications-accessories (R3/B) closes the 'specification masters' and 'accessories' gaps (S02.4, S02.5). C3a was revised at 17:08 to consume D5's batch helpers.

1. Dev and base worker queue defect (affects S01.1 and S02.9). The celery-worker in docker-compose.yml:250 and docker-compose.dev.yml:66, and the Makefile/make.ps1 dev-worker targets, run without -Q. celery_app.py routes mrp.* to 'mrp' and reports.* to 'reports', so in the dev stack these never execute:
   - MRP runs above 50 demand lines;
   - every PDF/XLSX job (packing list, large reports, valuation export).
   Only docker-compose.prod.yml:83 subscribes to those queues. A2 fixes this. Until then, a final e2e run on the dev stack will hang on PDFs and on large MRP runs.

2. Gaps in my sections that no work package covers:
   - (a) S01.1: SO confirmation never triggers MRP; there is no scheduled MRP run and no sales-order fulfilment/trace view.
   - (b) S01.2/N10: reallocation requests can be raised but never approved or rejected in the UI. The API exists at allocation/router.py:212-297. The generic /audit/approvals decide endpoints are also unused by the SPA. This also blocks P05.S6 and K05.
   - (c) S02.3/S02.5: no variant create/edit screen.
   - (d) S02.3/S02.6: the item form cannot edit sale_price/dealer_price (also relevant to P04.M9/M10).
   - (e) S02.6: manual SO line discounts are ungated and never trigger approval. The below-cost check uses the price before discount.
   - (f) S02.3: no editor for item-vendor links (also relevant to P07 and preferred-vendor-driven PO creation).
   - (g) S02.14: results of 5,000 rows or fewer cannot be exported, and no report has a PDF export.
   - (h) S02.14: PRODUCT_PROFITABILITY exposes standard cost to anyone with report.view.
   - (i) S02.2: address and contact writes on dealer accounts bypass the dealer.manage rule.
   - (j) N10: MRP never produces TRANSFER or REALLOCATION suggestions (P06.O4/O5, K10).

3. Notifications. Only 4 of the 20 NotificationType members are emitted on main: DISPATCH_DELAYED, ALLOCATION_CHANGED, QC_FAILED and REPORT_READY. B4, C1a and A5 cover the rest. Auditors for A01/A08 should verify against this list.

4. Sales lifecycle. SalesOrderStatus INVOICED and CLOSED are unreachable on main (no caller). A2 closes this.

5. Mobile API (S02.15). It is planned as a PWA inside the same SPA (C3a/C4a). The refresh token is only in an HttpOnly cookie with X-CSRF-Token (assumptions §14), so a native app would need a cookie jar. Confirm with the client whether any native app must keep working.

6. Handover (N09). The repository has no git remote; plan the source handover mechanism.
================= G2
Paths: API = apps/api/src, WEB = apps/web/src, repo root C:/Project/Other/ATCRAFT (main at cdac4e0). The audit was read-only. Route evidence comes from an AST scan of all 494 route decorators in API/modules/*/router.py: 480 carry require_permission/require_any_permission/require_superuser, 9 auth self-service routes need CurrentUser, and 5 are public (login, refresh, mfa/verify, password-reset request/confirm). The scan scripts are in the scratchpad at audit_s03_p01_p02/. The specs folder gained D1-D5 while I was auditing; the statuses take them into account.

Totals for my 65 items: 50 PRESENT, 8 PARTIAL, 4 PLANNED_PARTIAL, 2 PLANNED_FULL, 0 ABSENT. The 2 PLANNED_FULL are P02.O5 (B1) and S03.14 (C1a). The 4 PLANNED_PARTIAL are S03.8, S03.13, P02.U3 and P02.I8. The 8 PARTIAL are S03.6, S03.10, S03.11, P01.1, P01.2, P01.D5, P02.I6 and P02.I9.

Cross-cutting findings, most important first:
1) Security hole on main: vendor accounts are not isolated from other data. Every vendor role holds dispatch.view and document.view. Owner/account roles also hold vendor_invoice, payment, ledger, job_work and qc view rights. None of the dispatch, finance, job_work, quality or documents repositories filters by tenant.vendor_id, and there is no global gate for vendor users. docs/security.md section 4 claims the opposite. C1a's first step fixes this; land it before any vendor account goes live.
2) The approval inbox never shows anything. Nothing writes to the approval_requests table, so GET /audit/approvals and the dashboard 'Needs your attention' panel are always empty. No WP fixes this.
3) Backend endpoints with no screen:
   - the assembly lifecycle: create, approve, release, start, qc-result, close, cancel and pack;
   - reallocation request list and approve/reject (P.ALLOCATION_APPROVE is never used in the UI);
   - POST /quality/inspections, so in-process and final QC cannot be raised by hand;
   - GET /products/items/{id}/resolved-price;
   - creating and editing item-vendor links.
   Also, the reallocation notification links to /allocation/reservations/{id}, which has no SPA route; B4 fixes only the link.
4) Assembly QC moves the same stock twice, and orders get stuck at QC_PENDING. Completing an assembly raises a quality inspection, and deciding it on /quality moves the QC_HOLD stock. The order itself only leaves QC_PENDING through POST /assembly/{id}/qc-result, which has no UI and would move that stock again. This is the same class of defect as the job-work trap in CLAUDE.md.
5) Production is not connected to QC:
   - production output is posted straight to AVAILABLE;
   - the routing qc_required flag is never read;
   - the production and dispatch QC stage mappings are never used;
   - the empty state on WEB/features/quality/quality-page.tsx:249 wrongly says production entries raise inspections automatically.
6) The can_be_spare, can_return and can_scrap item flags can be stored and edited but are never checked outside the products module.
7) MRP gaps:
   - it runs only on demand;
   - its only demand sources are sales orders and manual entries (no reorder-level or safety-stock replenishment, no packing-material demand);
   - it never produces TRANSFER suggestions;
   - MANUFACTURE suggestions for kit items become production orders because there is no assembly conversion hook.
8) Minor items:
   - the job-work vendor picker does not filter to JOB_WORK vendors;
   - /admin/organisation is gated only on COMPANY_VIEW (WEB/app/router.tsx:367), so custom roles with only branch, plant or department view rights cannot reach those tabs;
   - finished goods are received at standard cost and never revalued to actual;
   - job-work charges and overheads are not included in production cost.
9) Work no current WP covers, which needs new WPs:
   - assembly UI completion plus QC unification (S03.6), medium;
   - production and final QC wiring plus a manual-inspection dialog (S03.10), medium;
   - packing-material planning plus the K06 auto-requisition (S03.11), medium;
   - a staff sales-order fulfilment trail (S03.13, beyond A2's invoices panel), medium;
   - a reallocation approval screen (S03.8), small to medium;
   - a populated approval inbox plus configurable approval rules (P02.U3), medium;
   - a spare-issue flow plus can_be_spare/can_scrap enforcement (P02.I6, P02.I9), small;
   - a decision on can_return for purchase returns (P02.I8), small;
   - the security issue resolution report with a legacy-review section (P01.D5, P01.1), small and documentation only;
   - cleaning up the unused code listed under P01.2, small.
================= G3
Read-only audit of main at cdac4e0 (apps/api/src, apps/web/src, docs). Nothing was executed; defects marked 'found by reading' are unverified at runtime and no existing test covers them.

Totals: P03 = 20 PRESENT, 9 PARTIAL, 1 PLANNED_FULL (T9 via A3). P05 = 12 PRESENT, 17 PARTIAL, 2 PLANNED_PARTIAL (S6, S9 via B4), 2 ABSENT (D1, D5); P03.F8 is PLANNED_PARTIAL via B4. Only A3 (return stock) and B4 (approver notification + link fix) touch these sections; everything else here is unplanned.

Cross-cutting findings:
1. Allocation has no back-channel to the demand documents. Sales lines, job-work lines and assembly components keep reservation_id/allocated_quantity. A manual Release on /inventory/reservations, an expiry, or a reallocation leaves them stale. Their own release paths then call allocation.release on a closed reservation, which raises (allocation/service.py:376-399). Result: a sales order can no longer be dispatched, cancelled or edited.
2. Reallocation does not work end to end:
   - There is no approval screen, so ALLOCATION_APPROVE is unused in apps/web (the handbook says so too).
   - The recipient reservation has demand_line_id=None, so dispatch and production issue never consume it.
   - The urgent sales order stays CONFIRMED and dispatch refuses it.
   - A demand with zero reservation cannot start a request from the UI. That is exactly the proposal's 'urgent order vs production' scenario.
3. MRP explodes only directly demanded items, with one consolidated multi-level BOM explosion (mrp/engine.py:1071-1086). So stock of an assembly, semi-finished item or manufactured component does not reduce the demand beneath it. The P05 priority order FG -> assembly -> SFG -> components -> RM holds only at the FG level. This also matters for P06/P04.CON auditors. The TRANSFER suggestion type is never produced, and JOB_WORK suggestions cannot be converted.
4. inventory_balances.allocated_quantity is never written, so the 'Allocated' column on /inventory always shows 0.
5. Priority inputs are inconsistent:
   - Sales order priority is 1-9, but production, assembly, job-work and manual reservations default to 100.
   - is_urgent is unused, and there is no customer priority.
   - Priority/date edits after confirmation are not pushed to reservations.
   - Assembly and job-work reservations carry no priority or date.
6. docs/assumptions.md section 10 (SOFT may be taken without approval) and the handbook do not match the code: every request needs approval, and no module creates SOFT reservations.
7. Traceability and bins:
   - Serial-tracked stock has no user-facing way in; only demo data posts it.
   - Serials and reservations cannot be combined at dispatch.
   - No screen can address a bin, and same-warehouse bin moves are impossible.
8. Endpoints with no screen:
   - transfer cancel/edit
   - adjustment reject/cancel
   - batch/serial create
   - reallocation list/approve/reject
   - assembly create/approve/release
   - physical-count scoping
9. The scrap pool and damaged stock have no disposal path, and scrap is not displayed anywhere.

Report sources: scratchpad/reports/returns-inventory-qc.md and anatomy.md were used for navigation; every claim was re-checked in code.
================= G4
Scope: P04 (27 items) and P06 (14 items), audited read-only against the main tree at C:/Project/Other/ATCRAFT. Tally: P04 has 21 PRESENT and 6 PARTIAL (M7, M9, M10, B4, B6, R3). P06 has 6 PRESENT and 8 PARTIAL (O3, O4, O5, F2, F5, F6, F7, F8). Nothing is ABSENT outright. Screen routes are registered in C:/Project/Other/ATCRAFT/apps/web/src/app/router.tsx: /items 250-251, /inventory/reservations 263, /bom 271-272, /routing 273, /mrp 283-284, /masters 348-352.

Planned coverage: none of the work packages in SPEC-INDEX close any P04 or P06 gap. A1, D5, B2, B4, C0 and D1 were checked. B4 only adds an 'MRP run completed with shortages' notification, and B2 adds rostered capacity without touching MRP. planned_wp is 'none' throughout.

Cross-cutting findings, which suggest three work packages:
(1) Item master form gap (small). ItemFormDialog (C:/Project/Other/ATCRAFT/apps/web/src/features/items/item-form-dialog.tsx:63-100) omits fields that the API accepts and that planning depends on: sale_price, dealer_price, default_warehouse_id, min_order_quantity, safety_stock, reorder_quantity, shelf_life_days and the batch/serial flags. Item-vendor links can be listed but not created or edited, and variants cannot be created or edited at all. Fixing these closes P04.M7, M9 and M10, most of P06.F5, and the conversion trap below.
(2) BOM draft editor (medium). PATCH /api/v1/bom/{bom_id} has no screen. This blocks versioning with changes (B4), fixing a draft after rejection (B5), entering BOM operations so costing includes operations (B6), and setting substitute lines or line operation_sequence.
(3) MRP depth (medium-large):
- level-by-level netting, so stocked sub-assemblies stop over-ordering their components (F2);
- routing-based manufacture lead time and offsets per level (F6, F7);
- expected availability dates (F8);
- TRANSFER, REALLOCATION and usable JOB_WORK outputs, with job-work output netted as supply (O3, O4, O5);
- an optional scheduled run.
Adding REALLOCATION, or new TRANSFER columns, changes the binding contract in docs/database.md section 6 and needs a CHECK-constraint migration.

Defects found while auditing:
(a) create_item does not run _require_attributes for finish_id, size_id or colour_id (C:/Project/Other/ATCRAFT/apps/api/src/modules/products/service.py:1133-1200 versus 1119-1130). The POST route therefore accepts cross-tenant, wrong-kind or retired attribute ids. This is the 'a foreign key to a UUID is not a tenant check' trap from CLAUDE.md, and the fix is small.
(b) Unscoped MRP runs ('All warehouses', the default on /mrp) cannot convert suggestions for items without a default warehouse, and no screen can set one. The guards are at production/service.py:1236-1244 and procurement/service.py:589-597.
(c) Production orders raised from MRP without a bom_id need a default approved BOM at release. BomFormDialog leaves 'default' unchecked by default, and its hint 'the API rejects a second' is inaccurate: approval demotes the previous default instead (bom/service.py:797-824).
(d) The UI offers Convert for JOB_WORK suggestions (mrp-types.ts:79), but the API always refuses them (job_work/service.py:2502-2520).

Operational note: async MRP runs (more than 50 demand lines; mrp_sync_threshold_demand_lines at C:/Project/Other/ATCRAFT/apps/api/src/core/config.py:261) are routed to the 'mrp' Celery queue. The base and dev compose workers consume only 'default' (C:/Project/Other/ATCRAFT/docker-compose.yml:250, C:/Project/Other/ATCRAFT/docker-compose.dev.yml:66). The prod overlay consumes it (C:/Project/Other/ATCRAFT/docker-compose.prod.yml:83). Until A2's dev fix lands, large runs on the dev stack stay QUEUED.

Minor: /mrp does not offer the manual demand lines that POST /api/v1/mrp/runs accepts (mrp/schemas.py:143).
================= G5
Paths are relative to C:\Project\Other\ATCRAFT. The audit is of main at cdac4e0: round 1 is not merged, so C0 is not on main yet.

1. **Trap T7 strands POs after receipt.** The PO state machine is effectively dead after receipt: nothing drives QC_PENDING->QC_COMPLETED, GRN_COMPLETED->INVOICED or INVOICED->PAID, and goods_receipt_lines.qc_status never leaves PENDING. As a result:
   - every QC'd PO is stuck at QC_PENDING;
   - the PO tracking panel shows 'QC pending' permanently;
   - the GRN 'Waiting on QC' filter never clears;
   - QC_PENDING has no cancel or close exit;
   - there is no Close button in the UI for GRN_COMPLETED POs;
   - vendors with such POs can never be retired (vendors/repository.py:55-67 OPEN_PURCHASE_ORDER_STATUSES; vendors/service.py:1047).

   No planned WP fixes this. C1a only derives a display status from inspections and explicitly avoids these PO statuses. This is the largest unplanned gap in A04, and it should become a small dedicated WP:
   - procurement hooks called from quality (QC verdict) and finance (invoice approve/cancel, payment allocate/reverse);
   - the reverse edges those hooks need;
   - a backfill migration;
   - lifecycle meta-tests.

2. **Gaps no WP covers** (candidates for new WPs):
   - P07.M1: read-only requisition detail for approvers. Approval today is blind.
   - P07.M2: RFQ document and sending it to vendors.
   - P07.M3 / A04.2: selected quotation -> PO.
   - P07.M5: draft PO edit UI, Close/Short-close UI, and the duplicate re-check on PATCH.
   - P07.M6: GRN detail and cancel-draft UI.
   - P07.M7 / A04.11-A04.14: status write-back.
   - P07.M8: vendor debit note on purchase return, and a match net of returns.
   - P07.M9: dispute / resolve / cancel UI for vendor invoices. The routes exist with no screen.
   - P07.M13: actual lead-time measurement.
   - P07.V5: transporter <-> vendor link and freight payables.
   - P07.V6: non-stock service procurement.

3. **Duplicate control (A03) on main.** Detection is real but runs only in create_purchase_order (service.py:2101-2133).
   - Merge, increase quantity and cancel are advisory text (procurement-types.ts:474-511, actionable:false).
   - 'Create new with approval' works only for holders of purchase_order.approve. PURCHASE_EXECUTIVE cannot use it.
   - The backend's CANCEL_DUPLICATE means 'cancel the existing order'. That is the opposite of the proposal's 'cancel duplicate request'.
   - D1 (round 5) plus C0 cover all rules and options. Two caveats remain:
     - D1 keeps vendor and warehouse as hard filters, so a same-demand order raised with a different vendor is never flagged. Confirm with the client.
     - Neither WP re-runs the check on PATCH /purchase-orders/{id}.

4. **Security issues found in the audited flows** (all planned):
   - T2: /check-duplicates lets vendor users read other vendors' open lines (C0 §9).
   - T3: assigned_vendor_user_id and the requisition/quotation line ids are unvalidated FKs (C0 §7, §10).
   - Vendor users see DRAFT and PENDING_APPROVAL POs with prices, and land in the staff shell (C0 §11, C2a).
   - Vendor role presets open unscoped dispatch, finance, quality and documents (C1a P0 allow-list).
   - The internal 'Vendor acknowledged' button is gated on purchase_order.update, which vendor roles also hold.

5. **VendorType is classification only.** vendor_type_predicate (vendors/repository.py:608) has no caller, and no picker filters by type, despite the VendorType docstring. All six types exist in the enum, the DB CHECK and the UI. The demo data has no TRANSPORT vendor.

6. **Related observations outside my sections:**
   - GET /procurement/pending-approvals has no UI.
   - GOODS_RECEIPT_POSTED is never raised.
   - Every planned vendor-notification channel (C1a, C1b, D2) targets vendor user accounts only. A vendor without a portal login gets nothing, and no WP e-mails the PO to the vendor master's e-mail address.
================= G6
Scope and method: read-only audit of branch main (HEAD cdac4e0). All paths are relative to C:\Project\Other\ATCRAFT. Every claim was checked in code (models, routers, services, frontend features, route table app/router.tsx:308-359, navigation.ts Manufacturing group). Planned coverage was judged against specs B1-cost-centres, B2-shifts, B3a-job-work-invoice and B4-smart-alerts-events. Tally: 16 PRESENT (P08.M1,M3,M4,M5, P08.S1-S8, P09.M2, P10.M1,M2,M5), 15 PARTIAL, 3 PLANNED_FULL (P08.M7, P08.M8, P10.M9), 4 PLANNED_PARTIAL (P08.M6, P08.M9, P10.M4, P10.M10).

Cross-cutting findings:
1. Assembly cannot be operated from the UI. The /assembly screens call only list/detail/components, issue-components and complete. Create, edit, delete, approve, release, start, close, cancel, qc-result and pack exist as permissioned APIs with no screen. Complete needs IN_PROGRESS, which only Start reaches, so a live tenant cannot take one assembly order through. Only demo orders seeded via the service progress. No WP covers this; it is the biggest gap in P09.
2. QC double-path defect for assembly and job work. quality.decide_inspection moves QC_HOLD stock but never calls back the source module. Assembly and job-work have their own qc-result endpoints, which move the same stock again. Assembly orders get stranded in QC_PENDING with no way to close or pack them, and job-work orders stay QC_PENDING unless the separate job-work verdict is used. This is the same class as the CLAUDE.md trap about a status the state machine cannot leave. It also affects P11.T3/T4, so flag it to the P11 auditor. No WP covers it.
3. Tracked items are refused end to end. Production output refuses batch and serial FG (service.py:3013). Assembly refuses batch components and outputs, and serial outputs. Job work refuses both sides. The material-issue UI has no batch picker. The demo's serialised MAKE item DL-900 can only enter stock through an opening movement (demo_data/operations.py:1335-1344).
4. APIs with no screen: GET /production/wip; GET /production/material-issues and /{id}; PATCH and DELETE /production/{id}; CompleteRequest and CloseRequest fields (the UI posts {}); GET /job-work/{id}/challans, /receipts and /reconciliation; GET /job-work/charges; ReceiveRequest is_final, approve_excess_loss, rejection_action and vendor_challan_date; IssueRequest.transporter_id; GET /assembly/ready-for-packing; the assembly lifecycle endpoints in point 1.
5. UI/API mismatches and wrong UI text:
   - Production booking shift allows 32 characters; the API allows 16.
   - Job-work vehicle number allows 32 characters; the API allows 24.
   - RecordProductionDialog's alert and the EntriesPanel empty state say the first booking moves the order to IN_PROCESS. Only Start does that.
   - JobWorkReceiveDialog's alert says the API decides scrap or debit from the order. In fact rejection_action defaults to RETURN_TO_STOCK.
   - Assembly 'Issue components' is offered on COMPONENTS_ISSUED and IN_PROGRESS, but the state machine has no edge for it, so it always fails.
   - The job-work receive prefill is miscomputed.
6. Machine status is never consulted. WORKABLE_MACHINE_STATUSES (routing/repository.py:59) is dead. Production supervisors lack user.view, so every operator picker is empty for them; B2 fixes this.
7. Linkage gaps:
   - Outsourced routing operations (is_outsourced, job_work_vendor_id) do not create or link job-work orders at release.
   - Production cost excludes job-work charges.
   - MRP has no assembly-order target and does not net open assembly orders.
   - MRP refuses to convert JOB_WORK suggestions.
   - Packing never consumes the assembly ready-for-packing feed.
8. Suggested new work packages, none planned today:
   - (a) Assembly completion: lifecycle UI, QC sync, packing handover, Start and partial completion (P09.M1, M3-M7, EX).
   - (b) Job-work operations: order detail and timeline, challan PDF and register, receive options (is_final, excess-loss approval, rejection action), QC sync, rework order, vendor material ledger (P10.M1, M3-M8, M10).
   - (c) Production completion, costing and WIP: complete/close dialogs, tracked-FG output, costing breakdown and variance report, WIP-by-order tab and valuation (P08.M10-M12).
   - (d) Production planning board and capacity load (P08.M2), after B2 lands.
   - Also add to B2 if possible: work-order-level machine overlap warnings (P08.M6) and a company-wide GET /production/entries (P08.M9).
================= G7
I audited main at cdac4e0 (read-only), checked every claim in code, and cross-checked against the handbook notes in C:/Project/Other/ATCRAFT/docs/handbook/content.json, which independently confirm the UI gaps. All paths are under C:/Project/Other/ATCRAFT.

Defects on main that no work package plans to fix, ordered by severity:

1. **Dispatch lines from the UI never carry `sales_order_line_id`** (C:/Project/Other/ATCRAFT/apps/web/src/features/dispatch/dispatch-dialogs.tsx L169-176).
   - Dispatching against a sales order skips `allocation.consume` and the order-line arithmetic (C:/Project/Other/ATCRAFT/apps/api/src/modules/dispatch/service.py L1446-1449, L1550-1553).
   - The reservation stays in place and goods leave from free stock; the dispatch is refused if the only stock is the reserved stock.
   - The order line's `dispatched_quantity` never moves, yet the sales order is still walked to PARTIALLY_DISPATCHED.
   - Demo data names the order lines, which is why seeded data looks right.
   - This blocks A2: A2 builds invoice lines and limits from sales-order lines and `dispatched_quantity`. Fix it before or inside A2 (small).

2. **The QC decision never notifies the document that raised the inspection** (`quality.decide_inspection` has no owner callback).
   - Job work has two release paths: the QC-result action on the job-work screen and the inspection in Quality. Using one leaves the other open. Deciding the leftover later double-posts QC_HOLD stock, either refused or silently releasing another lot at the same item/warehouse/batch position.
   - Assembly orders stay stuck in QC_PENDING: no screen calls `/assembly/{id}/qc-result`.
   - The GRN line's QC status and the purchase order's QC_PENDING never advance, so GRN_COMPLETED is unreachable after QC.
   - Fix with one owner-callback port in quality, keyed on the reference type (medium).

3. **QC coverage is partial.**
   - No screen raises an inspection by hand: the POST `/quality/inspections` endpoint has no UI.
   - Production and packing never raise inspections, so In-process and Final QC are API-only stage labels.
   - Decided inspections cannot be opened, so readings and photos are not viewable afterwards.
   - No UI can close an inspection.

4. **Packing.**
   - No screen or automatic path creates a packing order, and the empty-state copy wrongly says allocated sales orders create one.
   - Labels and barcodes exist only as data: the label endpoint is unused and nothing prints.
   - Cartons are never linked to dispatches: the UI sends `carton_ids: []`, and hand-over does not set `dispatches.packing_order_id`.
   - Assembly-to-packing hand-over has no UI.

5. **Dispatch UX.**
   - No edit after creation (PATCH is unused).
   - The gate-time LR, vehicle and tracking fields on the Dispatch action are unused.
   - The actual delivery date is always today.
   - Outbound QC_COMPLETED and GRN_COMPLETED cannot be reached from the UI.
   - The outbound tracking board's QC and GRN "pending" flags come from manually typed statuses, not real inspections or GRNs.
   - The DISPATCH_DELAYED notification links to `/dispatch/delayed`, which is not a page (B4 fixes this). The beat comment says it covers purchase orders, but it scans only outbound dispatches.

6. **Infrastructure and latent issues.**
   - In the base and dev compose files the Celery worker ignores the `reports` queue, so packing-list PDFs never render outside the prod overlay (A2 fixes this).
   - API-only latent defect: a GRN line posted with `location_id` holds stock at a bin, while the QC decision releases at the bin-less position, so the decision is refused (A3 only investigates).
   - Until C1a's allow-list gate lands, vendor roles (VENDOR_OWNER, VENDOR_DISPATCH_MANAGER) hold `dispatch.create` and `dispatch.update` (C:/Project/Other/ATCRAFT/apps/api/src/core/permissions.py L612-655), so a vendor user can see and raise company outbound dispatches.

**A06/A07 are about vendor shipments (inbound).**
- Outbound customer dispatches already capture all 12 transport fields, but only at creation, with no attachments and no immediate notifications.
- Inbound on main has only courier name, tracking reference and shipped date on the purchase order, writable through the API alone. There is no UI form, no notification, no board, and no inbound delay alert.
- C1a, C2a, C2b, B4 and D3 cover nearly all of it. Gaps left in the plan:
  - Staff cannot enter a full shipment on a vendor's behalf (staff get GET only).
  - Vendor-uploaded documents are not shown to staff on the PO detail or the inbound board.
  - Shipment notifications do not include ADMIN users and cannot say "LR copy uploaded".
  - Outbound attachments (LR copy, dispatch photo, POD) are not planned.
  - The purchase order's own status still sticks at QC_PENDING.

**No work package covers:** In-process, Final, Job-work and Assembly QC gaps; rework tracking; photo viewing; packing-order creation; label printing; carton tracking; dispatch planning and editing; the LR-copy attachment.
================= G8
Scope and basis. This is a read-only audit of main at HEAD cdac4e0; no lane branch is merged, so every PLANNED status depends on WPs in rounds 1-5 landing. Paths are relative to C:\Project\Other\ATCRAFT. Every report under /reports is served by the generic runner apps/web/src/features/reports/reports-page.tsx (route /reports, permission report.view, populated from GET /reports/catalogue). That is the screen evidence for each PRESENT report.

1. The GL is minimal. The default chart (finance/service.py:477-492) has no revenue, sales-returns, inventory, WIP, COGS, job-work-expense or freight account. Only vendor invoices, payments and customer adjustments post journals. Dispatch, production, job-work receipts and purchase returns post nothing, and OUTPUT_TAX is never posted by application code, so the GST report and the ledger disagree until A2. No route shows one account's ledger or a trial balance, even though ix_ledger_entries_account_date exists. A GL account statement plus a trial balance (small to medium, not in any WP) would close the 'Sales ledger' and 'Purchase ledger' gaps once A2 lands.

2. Defects found that affect the ledgers:
   - Vendor-invoice cancel after a dispute (APPROVED->DISPUTED->CANCELLED, finance/service.py:2357-2389) does not reverse the posted journal. B3a fixes it; make sure it lands before the vendor and purchase ledgers are trusted.
   - Purchase returns post no debit note and no GST input reversal. No WP covers this, and it affects P13.M2, M3 and M9.

3. Actual cost never reaches inventory or finance:
   - Production receipts are valued at standard cost by explicit ASSUMPTION (production/service.py:3004-3009), and nothing ever revalues them.
   - Job-work outputs carry no unit_cost (job_work/service.py:1807-1920).
   - Product profitability uses the current manual standard_cost against booked orders.
   - No WP addresses P13.M6, P14.11, or the capitalisation part of P13.M5.

4. Report authorisation. Every /reports route needs only report.view, and /reports/catalogue is not filtered per user, despite the comment at reports-page.tsx:4. SALES_EXECUTIVE holds report.view but not item.cost_view or inventory.valuation_view, yet sees product cost and margin plus stock values (inventory-valuation, and valuation_amount on stock-availability and dead-stock). Recommend per-report permissions with catalogue filtering (small).

5. Dashboard defect. The 'POs awaiting approval' tile (dashboard-page.tsx:225-236) shows POs awaiting delivery (APPROVED/SENT/ACKNOWLEDGED/PARTIALLY_RECEIVED). It is not planned to be fixed.

6. Machine-utilisation defect. Machines with no finished work order in the period are omitted, because the query starts from work_orders (reports/repository.py:1066-1078). B2 does not fix it.

7. Report UX gaps:
   - There is no XLSX/PDF export for results under 5000 rows; export only exists past the inline limit.
   - The UI offers no category filter (reports-page.tsx:120-122).
   - Period reports filter on one date column with a 366-day cap, so long-overdue items outside the chosen period are missed (purchase-pending by po_date, job-work-pending by expected_return_date).

8. A09 dispatch reports. In the proposal these concern vendor (inbound) shipments. Today's courier-tracking, dispatch and vendor-dispatch-history reports are outbound only; vendor-dispatch-history lists our dispatches to vendors. Inbound coverage depends on the chain C0 (PO category) -> C1a (purchase_order_shipments, inbound board API) -> C2b (board page) -> B5 (reports, round 5). Small outbound gaps that no WP covers: an expected-today filter, a courier filter on courier-tracking, and freight totals per transporter.

9. Security observed in passing. Vendor-role users can currently read /finance/ledger/customer/{any} and /finance/ledger/vendor/{any}, because finance has no vendor scoping and the vendor presets hold ledger.view. C1a's allow-list gate closes this.

10. The sales lifecycle never reaches INVOICED or CLOSED (no caller). A2 adds invoicing and POST /sales/{id}/close.
================= G9
I audited the main tree only: C:\Project\Other\ATCRAFT, branch main, HEAD cdac4e0. None of the lane work packages are merged yet, so every PLANNED_* status depends on rounds 1-5 landing. Paths in the evidence are relative to that root.

Cross-cutting findings, each checked in code:

1. **Notifications barely fire.** Only 4 of the 20 NotificationType members are ever raised: REPORT_READY, DISPATCH_DELAYED, QC_FAILED and ALLOCATION_CHANGED. Three of the four have unusable links:
   - '/dispatch/delayed' is not a SPA route; the working page is /dispatch?tab=tracking&delayed_only=true.
   - '/allocation/reservations/{id}' is not a route either.
   - REPORT_READY links to the API path /api/v1/reports/jobs/{id}.
   - QC_FAILED has no link at all.

   The dispatch delay alert also re-fires every hour once it is read, because delay_notified_at is written but never read. Nothing anywhere targets vendor users. notify_role with a vendor role code would reach every vendor in the company.

2. **Vendor security holes.**
   - The vendor role presets grant dispatch.*, vendor_invoice.*, payment.view, ledger.view, job_work.view, qc.view and document.* on modules that apply no vendor scope. A vendor login can read every customer's dispatches and every vendor's invoices and payments, and can create invoices for any vendor_id.
   - POST /procurement/purchase-orders/check-duplicates leaks other vendors' open POs.
   - Vendors see their own DRAFT and PENDING_APPROVAL POs.

   No demo vendor users exist, but /admin/users can create them today. C1a P0 and C0 §9/§11 must ship before any vendor account is issued.

3. **Trap T7 is fixed by no work package.**
   - quality decide_inspection never updates GRN line qc_status (it is set only at procurement/service.py:2863-2866, 3162 and 3247).
   - So a PO whose lines needed QC stays in QC_PENDING for ever, and the TrackingPanel shows qc_pending=true for ever.
   - INVOICED and PAID are never set on a PO.

   C1a and B4 deliberately route around this, but the PO status the staff screen shows, and the vendor app will show, stays wrong. It needs a small fix: a procurement port called from decide_inspection.

4. **The generic approvals queue is empty.** approval_requests (audit/models.py:113) is never written by anything in src. The dashboard panel 'Approvals waiting for me' (GET /audit/approvals) is therefore always empty, and GET /procurement/pending-approvals has no screen. This compounds the A01.M10 gap.

5. **Job-work vendors get nothing.** C1a's allow-list gate removes vendors' current (insecure) job-work access and gives job-work vendors no portal replacement. No work package covers 'Job work pending' for the vendor (A01.M8).

6. **Delivery-channel assumptions to confirm with Atcraft.**
   - Push and the vendor/dealer 'mobile app' are planned as an installable PWA with Web Push: Android Chrome, and iOS 16.4+ only after Add to Home Screen. No store app is planned.
   - WhatsApp/SMS (D2) and the courier API (D3) stay inert until Atcraft provides accounts: a Meta WhatsApp Business account with approved templates, a DLT-registered SMS sender and templates, and Shiprocket credentials.
   - D4's signature is app-level Ed25519, not a CA-issued DSC.

7. **Types still unraised after every work package:** PRODUCTION_ORDER_DELAYED, SALES_ORDER_AT_RISK, STOCK_BELOW_REORDER, BACKGROUND_JOB_COMPLETED/FAILED and SYSTEM_ANNOUNCEMENT. They are not in the proposal's lists, but the Preferences screen offers them. Either raise them (the reorder and production delay sweeps are small) or hide them.

8. **Small issues in what exists.**
   - The frontend NOTIFICATION_TYPES list (notification-types.ts:27-48) is a hand-maintained copy of the enum; B4 adds a guard test for it.
   - The beat comment at celery_app.py:85-86 wrongly claims purchase-order delay alerts.
   - E-mail has no recovery sweep for lost publishes; C1b adds one.

9. **Recipient gaps that remain after the specs** (the PLANNED_PARTIAL rows):
   - B4 sends 'Dispatch created' and outbound 'Courier tracking updated' only to the customer's optional assigned sales user, with no fallback.
   - No spec notifies the buyer about inbound shipment updates after creation, or about delivery.
   - B4's vendor-invoice-pending rule ignores partially received POs.

   All are small fixes inside B4.
================= G10
Cross-cutting findings (all verified in code on main; nothing modified):

1. DEFECT, sales dispatch from the UI. Dispatch lines created on the dispatch screen never carry sales_order_line_id: LineEditor in apps/web/src/features/dispatch/dispatch-dialogs.tsx:111-187 sends only item, quantity, batch and serials, and no file in apps/web sets the field. The backend does not auto-match lines either (dispatch/service.py _validate_lines and _persist_lines). Consequences:
   - A UI dispatch against a sales order never consumes the order's HARD reservation.
   - When the warehouse holds only the reserved stock, the release fails the inventory 'reserved' rule (inventory/service.py:1158).
   - When it holds more, free stock ships, the order's lines show 0 dispatched and the reservation stays standing, yet the header advances to PARTIALLY_DISPATCHED.
   - The dialog text at dispatch-dialogs.tsx:266 and :373 claims the opposite.
   - Integration tests and demo data pass sales_order_line_id through the API, which is why nothing caught this. A2 (invoice per dispatch, whose lines carry dispatch_line_id and sales_order_line_id) and A5's commercial e2e depend on the fix. This affects K01 and K08.

2. DESIGN GAP, short orders can never ship. A sales order confirmed with any shortfall stays CONFIRMED permanently. The only CONFIRMED->ALLOCATED transition is inside confirm_order (sales/service.py:1709-1719). Quantity edits re-reserve without transitioning, there is no allocate/top-up endpoint, and reallocation does not touch the order. Dispatch accepts only ALLOCATED or PARTIALLY_DISPATCHED (dispatch/service.py:440-464, pinned by tests/unit/test_dispatch_lifecycle.py). So K02, K05 and K08 cannot complete, even after production or purchase delivers the balance. The inverse inconsistency also exists: an ALLOCATED order whose quantity is raised past free stock stays ALLOCATED and remains dispatchable.

3. Reallocation is not wired to its recipient or its approver.
   - Executed reallocations create the recipient reservation with demand_line_id=None (allocation/service.py:1440) and never update the sales order line.
   - No UI lists, approves or rejects reallocation requests.
   - No approval_requests row is written, so the dashboard approval queue never shows them.
   - The donor notification links to /allocation/reservations/{id}, which is not a SPA route (B4 fixes the link only).
   - is_urgent on sales orders is never read by allocation; urgency counts only through the numeric priority.

4. Every bolded automation in K02, K06, K08, K09 and K10 is manual today. The code deliberately chooses 'report, don't raise' in each place:
   - confirm reports the shortfall and links to /mrp;
   - packing just fails on a missing box;
   - quality sets return_required and notifies (ASSUMPTION at quality/service.py:77-81);
   - MRP never emits TRANSFER suggestions (mrp/models.py:129-137);
   - PurchaseSourceType.REORDER and ProductionSourceType.SALES_ORDER are vocabulary nothing uses.

   No work package in SPEC-INDEX closes any of these. B4 adds only notifications ('Production shortage', 'Stock reallocation request', QC rejected), and C1a/C2a add only vendor-side QC visibility. A new work package is recommended, 'Scenario automation and allocation lifecycle', covering:
   - linked dispatch lines (K01);
   - partial dispatch of the reserved part plus an allocate/top-up endpoint, automatic on stock arrival (K08, K02);
   - auto-plan on confirm (K02);
   - a reallocation approval UI and linkage to the sales order line (K05);
   - a packing-material requisition (K06);
   - QC rejection creating a draft purchase return or a replacement (K09);
   - multi-warehouse transfer suggestions (K10);
   - a job-work order detail view (K07, small).

   Each should come with tests, docs/assumptions.md entries and the four e2e sweeps. Rough size: two lanes for one round.

5. No end-to-end test exercises any K scenario through the UI. tests/e2e covers only SO confirm -> MRP -> PO -> GRN by API, plus form write paths. Per the project's own trap list, defects 1 and 2 are the kind green suites hide. A K01-K10 Playwright journey should accompany the fix work package.

6. K03 and K04 are fully present, with integration tests. K07 is complete in the backend; the gap is display only.
