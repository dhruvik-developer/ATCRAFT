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

> **Post-audit update - 21 October 2026 (E1b).** S01.1, S03.13, K02, K08 and
> K10 are closed. Confirming a short order now raises pegged DRAFT supply in the
> same transaction (one production order per made line; one requisition per
> order for bought lines), and the planner is idempotent, reconciles drafts
> after order changes, reports job-work needs, and treats capability mismatches
> as warnings. The order detail carries a full fulfilment trail across MRP,
> production, assembly, job work, procurement, receipts, QC, transfers, packing,
> dispatch, invoices, credit notes and returns; production/assembly/job-work
> detail link back, and packing/dispatch accept the `sales_order_id` filter.
> `available_elsewhere` plus **Raise transfer** covers the multi-warehouse
> suggestion, with no soft reservation at the donor. The entries below are the
> pre-E1b audit text and are left as they were. See
> `docs/implementation/specs/E1b-sales-automation-trace.md` and
> `docs/assumptions.md` §E1b-1 to §E1b-5.

### S01.1 [PARTIAL] (G1) One integrated system in which a sales order can automatically: check available stock, reserve inventory, calculate shortages, generate production plans, raise
- gap: Production plans and purchase requirements are not raised automatically from the sales order. Nothing in sales/service.py references MRP, and celery_app.py beat_schedule has no MRP entry, so a planner must start a run and then convert its suggestions. JOB_WORK suggestions cannot be converted at all. There is no lifecycle view centred on the sales order: production orders created by MRP store source_type=MRP_SUGGESTION and source_id=<suggestion id> (production/models.py:124-134), not the sales order, and the order detail page lists no linked production, job-work, packing or dispatch documents. In the base and dev compose files the worker has no -Q (docker-compose.yml:250, docker-compose.dev.yml:66), so asynchronous MRP runs (over 50 lines) never execute outside the prod overlay.
- planned: None covers the automation or the order-level trace. A2 only fixes the dev worker queue; B4 only adds a 'Production shortage' notification after a run.
- rec: Medium. (1) Add a company setting auto_plan_on_confirm. When it is on and a confirmed order is short, enqueue one MRP run per company after commit, coalescing bursts and reusing start_run, the planning lock and RateLimitPolicy.MRP_RUN. (2) Add a Celery beat task mrp.scheduled_run (nightly, per active company, switched by a setting). (3) Add an optional, audited auto-convert policy: PURCHASE to a DRAFT requisition, MANUFACTURE to a DRAFT production order, never a purchase order. (4) Add GET /sales/{id}/fulfilment (sales_order.view), batched through ports, returning reservations, MRP suggestions whose source_demands reference the order's lines, production orders, requisitions and POs, job-work orders, packing orders and dispatches. Show it as a Fulfilment panel on the order detail page, and persist sales_order_id/sales_order_line_id on MRP-created production orders and requisition lines.

### S01.2 [PARTIAL] (G1) Covers raw material planning, parts management, BOM, production, assembly, job work, inventory allocation and reallocation, QC, packing, dispatch and reporting
- gap: A reallocation cannot be completed in the UI. reservations-page.tsx can only raise a request (ReallocationDialog :677, POST /allocation/reallocation-requests :694). Every request is created PENDING and needs a separate approver (allocation/service.py create_reallocation_request :1949, _assert_separate_approver). No screen calls GET /allocation/reallocation-requests, /{id}/approve or /{id}/reject (router.py:212-297), or the generic /audit/approvals/{id}/approve. The dashboard approvals panel and entityLink('reallocation_request') both open /inventory/reservations, which has no approve or reject control. How complete the other areas are is tracked under P03-P14.
- planned: None. B4 only fixes the reallocation notification link.
- rec: Small. Add a 'Reallocation requests' tab to /inventory/reservations:
- list GET /allocation/reallocation-requests?status=PENDING, oldest first;
- a detail view with donor, recipient, quantity and alternatives;
- Approve and Reject dialogs with a reason, gated on P.ALLOCATION_APPROVE, showing the separate-approver refusal verbatim;
- invalidate the reservation and availability query keys;
- point entityLink('reallocation_request') and the B4 notification at the tab;
- renderScreen tests, plus sweep entries if it becomes a new route.

### S02.3 [PLANNED_PARTIAL] (G1) Product catalog management
- gap: On main, items have no product image at all: no column or field in the products models or schemas. Brand, manufacturer and specifications are missing (see S02.4).
Unplanned:
- No screen creates or edits a variant (item-detail-page.tsx:430-433 says so). POST /products/items/{id}/variants (router.py:495) and PATCH /products/variants/{id} (:509) are API-only.
- The item form cannot set sale_price or dealer_price, although ItemCreate/ItemUpdate accept them (schemas.py:233-234, :275-276) and the detail page displays them (item-detail-page.tsx:188-189).
- Item-vendor links are list-only: no UI for POST /products/items/{id}/vendors or PATCH/DELETE /products/item-vendors/{id}.
- planned: C3b adds items.image_document_id and PUT /products/items/{id}/image; C4b adds the item Images panel; A1 adds brand, manufacturer and MPN to the item form; D5 adds the specifications and accessories panels. Not covered: variant authoring UI, price fields in the item form, item-vendor link editor.
- rec: Small, for each of three pieces:
1. A variant create/edit dialog on the Variants panel. POST takes code, name, finish/size/colour selects (useItemAttributeOptions), price_delta, cost_delta (only with item.cost_view) and barcode. PATCH omits code. Gate on P.ITEM_CREATE / P.ITEM_UPDATE, and invalidate the item detail and mastersKeys.variants.
2. Sale price and Dealer price decimal inputs in item-form-dialog.
3. A vendor-link dialog on VendorsPanel: vendor picker, vendor item code and name, preferred toggle, lead time, MOQ and order multiple, gated on P.ITEM_UPDATE.
Write renderScreen request-body tests for each.

### S02.5 [PLANNED_PARTIAL] (G1) Product variants: size, finish, accessories
- gap: No screen creates or edits a variant (item-detail-page.tsx:430-433). Accessories have no model, route or screen; 'ACCESSORIES' exists only as a seeded item category (core/seed.py:158).
- planned: D5 covers accessories (item_accessories OPTIONAL/INCLUDED, /products/items/{id}/accessories, an Accessories panel). C3a and C4a only read variants for the dealer catalogue. No work package adds variant authoring.
- rec: Small. Add the variant create/edit dialog described under S02.3 (POST/PATCH bodies, attribute selects, price_delta/cost_delta, permission gates, tests). This matters more once C3a and C4a show per-variant prices to dealers.

### S02.6 [PLANNED_PARTIAL] (G1) Dealer pricing & discount management
- gap: There is no discount management:
- no schemes or offers;
- no dealer, category or brand discount;
- no discount authority. Anyone who can create or update a sales order can give a 0-100% discount with no permission check, and it never sets requires_approval. Only a unit_price override below standard_cost does (service.py:1100, :1282), and that check uses the price before discount.
Also, items.sale_price and dealer_price cannot be edited in the item form.
- planned: C3a: dealer_offers scoped ALL/CATEGORY/BRAND/ITEM, with audience, minimum quantity, percentage and an optional customer_id for dealer-specific schemes. The best offer becomes the default discount on both ERP and portal orders. C3b: /promotions offers admin API. C4b: staff Promotions page. Not covere
- rec: Small to medium.
1. Add a company setting or constant SALES_DISCOUNT_APPROVAL_PERCENT and a right such as sales_order.discount_override. A line discount above the threshold, or a net (after-discount) unit price below standard_cost, sets requires_approval and goes through approve_order. Refuse above-threshold discounts without the right, and apply the same rule to discounts that come from offers.
2. Add Sale price and Dealer price inputs to item-form-dialog.
Tests: a unit test for the rule, an integration test for the approval gate, and a frontend request-body test.

### N10 [PLANNED_PARTIAL] (G1) Conclusion: 12 most important modules (Inventory Engine, Multi-role Item Master, Multi-level BOM, Common Parts Calculation, Allocation & Reallocation, MRP Plann
- gap: Three modules have gaps:
- Finance has no sales invoices, credit notes or receivable follow-up.
- Allocation and reallocation cannot approve a reallocation in the UI (see S01.2).
- MRP produces no TRANSFER or reallocation suggestions (the docstring at mrp/models.py:132-136 says TRANSFER is not produced).
- planned: Finance: A2, A3, A4. Job-work invoice: B3a. Shift planning: B2. Not covered: the reallocation approval screen and MRP transfer/reallocation suggestions.
- rec: Small: the reallocation approval UI (see S01.2). Medium: an MRP pass that, before falling back to MANUFACTURE or PURCHASE, proposes a TRANSFER from free stock in other warehouses and a REALLOCATION from lower-priority soft reservations, feeding the existing reallocation-request API (cross-reference P06.O4/O5 and K10).

### S03.6 [PARTIAL] (G2) No assembly line tracking
- gap: No screen creates an assembly order (PageHeader has no action, assembly-page.tsx:225-228) or approves/releases/starts/closes/cancels it, records its QC verdict (POST /assembly/{id}/qc-result) or hands it to packing (POST /assembly/{id}/pack); nothing else creates assembly orders (MRP conversion hooks are procurement/production/job_work only, API/modules/mrp/service.py:204-230), so an assembly cannot be run DRAFT->COMPLETED from the UI. With requires_qc the order strands at QC_PENDING: completion raises a quality inspection (assembly/service.py:605) whose decision on /quality moves the QC_HOLD stock, while the order only leaves QC_PENDING via /qc-result (no UI), which moves the same QC_HOLD stock again (assembly/service.py:1568-1650).
- planned:
- rec: Medium. Add a 'New assembly order' dialog (item, ASSEMBLY BOM picker, qty, component and output warehouses, assembly_line, planned dates, requires_qc) and lifecycle buttons gated on ASSEMBLY_ORDER_MANAGE (approve, release, start, close, cancel with reason) whose predicates mirror ASSEMBLY_ORDER_LIFECYCLE; a 'Hand to packing' action (PACKING_MANAGE). Unify QC: when the inspection raised at completion is decided, advance the assembly order in the same transaction (or make /qc-result consume that decision instead of moving stock a second time). Optionally convert MANUFACTURE suggestions for items whose default BOM is ASSEMBLY into assembly orders, and add a line board grouped by assembly_line/status. Tests: lifecycle via UI, QC_PENDING->COMPLETED through the quality page.

### S03.8 [PLANNED_PARTIAL] (G2) No reservation / reallocation engine
- gap: No screen lists reallocation requests or approves/rejects them: no frontend code calls GET /allocation/reallocation-requests or /approve|/reject, and P.ALLOCATION_APPROVE (WEB/lib/permissions.ts:87) gates nothing, so a raised request can only be executed through the raw API. The approval notification links to /allocation/reservations/{id} (allocation/service.py:1849), which is not an SPA route.
- planned: B4-smart-alerts-events (notifies allocation.approve holders on request creation and fixes the broken link; adds no approval screen)
- rec: Small-medium. Add a 'Reallocation requests' tab on /inventory/reservations: server-paginated list with status filter, detail showing ranked donors and requires_approval, Approve / Reject-with-note gated on P.ALLOCATION_APPROVE (surface the self-approval refusal), and point notifications at it; add to route sweeps and handbook; also raise an approval_requests row (see P02.U3).

### S03.10 [PARTIAL] (G2) No production QC workflow
- gap: Production is not wired to QC: API/modules/production/{models,service}.py contain no QC reference; PRODUCTION_RECEIPT posts output straight to StockState.AVAILABLE (production/service.py:3020-3036); ProductionOrderStatus has no QC state; routing_operations.qc_required ('Production may not report this step complete until an inspection has been recorded', routing/models.py:390-394) is never read by production; STAGE_BY_REFERENCE_TYPE maps production_order/production_entry->IN_PROCESS and dispatch->FINAL (quality/service.py:183-195) but only assembly (assembly/service.py:605) and job work (job_work/service.py:735) call create_in_process_inspection; no UI calls POST /quality/inspections, so an in-process/final inspection cannot be raised from the screens; WEB/features/quality/quality-page.tsx:249 wrongly says production entries raise inspections automatically.
- planned:
- rec: Medium. Add requires_qc to production orders (default true when any routing operation has qc_required, or from an item flag); when set, post PRODUCTION_RECEIPT into QC_HOLD and call quality.create_in_process_inspection(reference_type='production_entry'); enforce routing qc_required on work-order completion; optional FINAL inspection before packing/dispatch for flagged items; add a 'Raise inspection' dialog on /quality (stage, item, warehouse, qty, hold_stock) and an Inspections panel on /production/:orderId; fix the empty-state copy; integration tests asserting stock states.

### S03.11 [PARTIAL] (G2) No packing material planning
- gap: Packing material consumed at packing (outer cartons, labels) is never planned: MRP demand sources are only SALES_ORDER and MANUAL (mrp/models.py:117-126) and FG demand covered from stock is never exploded, so orders shipped from stock generate no packing-material requirement; there is no item-to-packing specification (box type, units per carton); packing order creation does no material shortage check and cannot raise a purchase requisition (no shortage/requisition logic in packing/service.py), so the K06 scenario is unsupported; no packing-material requirement report.
- planned:
- rec: Medium. Add item_packing_specs (item_id, packing_material_item_id, quantity_per_unit or units_per_carton, is_default) with a panel on the item detail page; add a packing-material demand pass to MRP converting open SO / packing-order quantities through the spec (netted against on-hand, reserved, incoming) into PURCHASE suggestions; on packing-order create/start check spec requirements against availability and offer 'Raise purchase requisition' (prefilled, packing category) or auto-create a DRAFT requisition; add a 'Packing material requirement' report.

### S03.13 [PLANNED_PARTIAL] (G2) No sales-order-to-dispatch traceability
- gap: No SO-centric trail: the SO detail page never lists its production/assembly/job-work orders, QC inspections, packing orders or dispatches with tracking; /dispatch and /packing pages do not expose the sales_order_id filter; production detail shows the source type but no link to the SO (production-detail-page.tsx:1102); MRP suggestions, requisitions and POs are not pegged to the SO.
- planned: A2-sales-invoice (Invoices panel + Close on SO detail); C4a-dealer-app-frontend (dealer-side order timeline with dispatches). No WP adds the staff trace view.
- rec: Medium. Add GET /sales/{id}/fulfilment-trail (one batched query per document family) returning reservations, MRP demand/suggestions, production/assembly/job-work orders sourced from the SO with status, their QC inspections, packing orders/cartons, dispatches with latest tracking, and invoices once A2 lands; render a 'Fulfilment trail' panel/timeline on /sales-orders/:orderId; add the sales_order_id URL filter to /dispatch and /packing; link the SO from production and assembly detail.

### P01.1 [PARTIAL] (G2) Review existing API & frontend code
- gap: No document records the review of the legacy API/frontend or the disposition of each legacy module (S02.1-S02.15) and legacy finding.
- planned:
- rec: Small, doc-only: add a 'Legacy system review' section to the P01.D5 report mapping each legacy module/API group to its replacement (module, route, screen), 'dropped', or the planned WP, and listing the legacy findings the proposal names.

### P01.2 [PARTIAL] (G2) Remove unused / dead code
- gap: Unwired code still ships: the approval inbox (approval_requests table, GET /audit/approvals and approve/reject, dashboard 'Needs your attention' panel) has no writer anywhere in API, so it can never show a row; OrderPartyRepository.customer_price_type (API/modules/sales/repository.py:340) is never called; routing_operations.qc_required (routing/models.py:390-394) is persisted but never read.
- planned:
- rec: Small: wire each item (see P02.U3, S03.10) or delete it; optionally add a dead-code report (vulture / knip) as a non-blocking CI step, justified in the PR.

### P01.D5 [PARTIAL] (G2) Deliverable: security issue resolution report (document)
- gap: No issue-to-resolution report: no document maps the Phase-01 findings (hardcoded bypass token, MD5, plain-text passwords, unauthenticated APIs, no JWT expiry, secrets in code, missing RBAC/audit) to fix, evidence, status and residual risk; section 4 currently overstates vendor isolation (S03.14).
- planned:
- rec: Small: add docs/security-issue-resolution-report.md (exportable to PDF with the handbook tooling): one row per P01.3-P01.13 issue with finding, legacy behaviour, resolution (file/function), verification (test names; route scan 494/480/5), status, residual risk; open items: vendor isolation (C1a), malware scanning (security.md section 6 known gap), pip-audit/npm audit results; include the P01.1 legacy-review section; date and sign-off.

### P02.U3 [PLANNED_PARTIAL] (G2) Approval workflow
- gap: (1) The central queue is dead: approval_requests (API/modules/audit/models.py:113-141), GET /audit/approvals and approve/reject (audit/router.py:196-262) and the dashboard 'Needs your attention' panel (WEB/features/dashboard/dashboard-page.tsx:169-175,353-420) have no writer anywhere in API, so the inbox is permanently empty. (2) Reallocation requests cannot be approved in the UI (S03.8). (3) Rules are hard-coded (ADJUSTMENT_APPROVAL_VALUE_THRESHOLD = 25000, API/modules/inventory/service.py:199) or always-on states; no configurable thresholds, levels or approver routing by document/amount.
- planned: B4-smart-alerts-events (notifies approvers on requisition/PO submit, reallocation request, payment pending); D1-duplicate-po-actions (create-with-approval confirmed at PO approval). Neither populates the queue nor adds configurable rules.
- rec: Medium. Write an ApprovalRequest row in the same transaction whenever a document enters a pending-approval state (requisition/PO submit, BOM submit, production order awaiting approval, SO requires_approval, adjustment requires_approval, reallocation request, vendor invoice/payment pending) and close it on approve/reject/cancel (or replace the panel with a server-side union of pending documents); add an /approvals page with deep links; add company approval settings (PO amount limits per role/level, adjustment threshold, auto-approve below limit) read by the services.

### P02.I6 [PARTIAL] (G2) Item role: Use as spare
- gap: The flag is read by nothing outside products (grep); a SPARE reservation does not check it; no route consumes/issues a reservation (allocation/router.py has no consume/issue endpoint), so there is no spare-issue flow.
- planned:
- rec: Small: enforce can_be_spare in allocation reserve for DemandType.SPARE; add POST /allocation/reservations/{id}/issue (or a 'Spare issue' document) posting MATERIAL_ISSUE through allocation consume, with an Issue action on the reservations page; tests.

### P02.I8 [PLANNED_PARTIAL] (G2) Item role: Can return
- gap: Read by nothing outside products: create_purchase_return does not check it and no customer-return document exists on main.
- planned: A3-returns-credit-notes (sales return document enforcing can_return, its first enforcement); C3a shows it in the dealer catalogue
- rec: Small: decide and implement the purchase-return rule (check can_return in create_purchase_return, or document why returning rejected stock to a vendor is exempt) with a test.

### P02.I9 [PARTIAL] (G2) Item role: Can scrap
- gap: No scrap path checks it: inventory SCRAP transition (inventory/service.py:397), QC SCRAP verdict (quality decide), job-work scrap (job_work/service.py:1913) and stock-adjustment write-offs all ignore the flag.
- planned:
- rec: Small: enforce at the document entry points (stock-adjustment scrap lines, QC SCRAP decision offering DAMAGED instead, job-work SCRAP_AT_VENDOR) or once in inventory post_movement for SCRAP/target SCRAP, with a clear BusinessRuleError and tests.

### P03.T4 [PARTIAL] (G3) Assembly items
- gap: ItemGroup (products/models.py:73-89; web item-model.ts:46-56) has no ASSEMBLY / SUB_ASSEMBLY member, so an assembled item cannot be classified, filtered or reported as an assembly item. The assembly screen (features/assembly/assembly-page.tsx, assembly-detail-dialog.tsx) only calls /issue-components and /complete; POST /assembly, /{id}/approve, /{id}/release (assembly/router.py:148-258) have no UI, so assembly stock cannot be produced from a screen.
- planned: none
- rec: Small: add ItemGroup.ASSEMBLY (and SUB_ASSEMBLY if wanted) with defaults in products/service.py default_capabilities (L250) and the web ITEM_GROUPS list; items.item_group is String(24) with no CHECK, so no migration. Medium (overlaps P09): assembly order create/approve/release/start/close/cancel actions on the assembly page.

### P03.T7 [PARTIAL] (G3) Consumables
- gap: No consumables store type (WarehouseType warehouses/models.py:47-66 and the seed L138-147 have none) and no way to issue a consumable to a department/cost centre/maintenance: AdjustmentReason (inventory/models.py:294-307) has no CONSUMPTION/INTERNAL_ISSUE reason, material issues exist only against production orders (production/service.py:2503 _post_issue_lines), and inventory deliberately has no free movement endpoint (inventory/router.py:9-12). Consumables can only leave stock through a BOM issue or a mis-reasoned write-down.
- planned: none (adjacent only: B1-cost-centres adds stock_adjustments.cost_centre_id for write-offs, but no consumption reason or issue document)
- rec: Medium: an internal issue document ('Issue to department / cost centre': lines, department or cost centre, reason, approval above a value threshold) posting MATERIAL_ISSUE with reference_type internal_issue, as a tab on /inventory/movements with its own permission; optional WarehouseType CONSUMABLES and a consumption-by-department report.

### P03.T8 [PARTIAL] (G3) Scrap
- gap: The scrap pool is shown on no screen (balances-page.tsx columns L86-201 and item-detail-page.tsx L262-335 omit it; WarehouseStockRow/ItemStockSummary in inventory/schemas.py:138-188 have no scrap field). Nothing can take stock out of SCRAP: state_change_type refuses moves from SCRAP (inventory/service.py:529-536) and adjustments act only on AVAILABLE, so there is no scrap disposal or scrap sale. Production-entry scrap is only a counter on the order (production/service.py:1823) and posts no stock.
- planned: none
- rec: Medium: show scrap on the balances grid and item summary; add a scrap disposal document (sale or write-off out of the SCRAP pool with reason, valued from cost layers, optional sale through a SCRAP-group item); optionally post production-entry scrap as stock of a scrap item; scrap register report.

### P03.S4 [PARTIAL] (G3) Allocated
- gap: Nothing ever writes it: no module updates inventory_balances.allocated_quantity (allocation/service.py touches only reserved_quantity; packing/service.py:60-63 'Packing reserves nothing'), so the column always reads 0. sales_order_lines.allocated_quantity (sales/models.py:359) is only the reserved quantity. There is no pick/commit step between reservation and consumption.
- planned: none
- rec: Medium: an allocation 'commit/pick' primitive that moves part of a reservation into allocated_quantity (balance and reservation row) when stock is picked/packed or kitted (carton seal, production kitting, assembly issue), drawn down by consume and excluded from reallocation donors; or drop the column and the UI column if the business does not want the distinction.

### P03.S9 [PARTIAL] (G3) Scrap
- gap: Same as P03.T8: maintained but displayed on no screen (balances grid, item Stock tab and item summary schema all omit it) and never disposable (state_change_type refuses moves out of SCRAP, inventory/service.py:529-536).
- planned: none
- rec: Medium: see P03.T8 (scrap column + scrap disposal document + register).

### P03.F3 [PARTIAL] (G3) Stock transfer
- gap: UI lines carry only item + quantity (movement-dialogs.tsx:94-96): no batch picker (admitted in the comment at L65-73) although the API requires a batch on batch-tracked lines, so batch-tracked stock cannot be transferred from the screen; no from/to bin; no Cancel or Edit action although PATCH and /cancel exist; serial-tracked items are refused outright (docs/assumptions.md section 25); moves between bins of one warehouse are impossible (CHECK from<>to, inventory/models.py:1111-1113).
- planned: none
- rec: Small: reuse the dispatch BatchPicker (features/dispatch/stock-pickers.tsx) and a LocationPicker for from/to bins on transfer lines; add Cancel (with reason) and Edit-draft actions. Medium: serial_ids[] on stock_transfer_lines to lift section 25.

### P03.F4 [PARTIAL] (G3) Stock adjustment
- gap: No Reject or Cancel action in the UI although both endpoints exist. Dialog lines carry only item + signed quantity (movement-dialogs.tsx:94-96): no batch, bin or unit cost, so batch-tracked items cannot be adjusted (a write-up needs a batch; a write-down without one draws the empty batch-less position). stock_adjustment_lines has no serial_id (inventory/models.py:1280-1330) while ADJUSTMENT_INCREASE/DECREASE are serial-gated (inventory/service.py:1709, 1718), so serial-tracked items cannot be adjusted even via the API.
- planned: none
- rec: Small: Reject (approver) and Cancel actions plus batch, bin and unit-cost fields in the adjustment dialog. Medium: serial_ids on adjustment lines with one movement per unit.

### P03.F5 [PARTIAL] (G3) Bin / location support
- gap: No screen can put stock into, move it between, or show it by bin: GRN receive, transfer, adjustment, count-create and reservation dialogs have no location field (no location_id usage in features/inventory dialogs or grn-receive-dialog.tsx); balances-page.tsx has no bin column or filter (BalanceRead has location_id but no code); bin-to-bin moves inside one warehouse are impossible (CHECK source_differs_from_destination inventory/models.py:1111-1113; transfer_stock refuses the same warehouse inventory/service.py:3409-3410).
- planned: none
- rec: Medium: warehouse-filtered LocationPicker on GRN lines, transfer lines (from/to), adjustment lines, material-issue lines and count create; location_code on BalanceRead with a bin column/filter on /inventory; a same-warehouse bin move (putaway) document posting TRANSFER_OUT/IN between locations; optional default bin per item.

### P03.F6 [PARTIAL] (G3) Batch / serial where required
- gap: No batch/serial screen: POST /inventory/batches and /inventory/serials have no frontend caller, so units cannot be registered from the UI. Serial-tracked stock has no user inbound path: GRN refuses it (section 20), production refuses tracked finished goods (production/service.py:190-197), assembly refuses serial output, transfers refuse it (section 25), adjustment lines carry no serial; only demo data posts it (core/demo_data/operations.py:1396-1416). A reserved serial-tracked SO line is refused at dispatch (dispatch/service.py:153-158, 1499-1508). BatchStatus QUARANTINED/EXPIRED/CONSUMED is never set (no expiry/quarantine handling, no FEFO). Transfer/adjustment dialogs have no batch picker.
- planned: none
- rec: Large: serial capture on GRN lines (auto-register units), production/assembly output and adjustment/transfer lines; a Batches & Serials screen (list/search/trace, register units, quarantine/expire lots via a state machine, expiry alert); batch pickers on transfer/adjustment/issue dialogs; let dispatch consume a reservation per picked serial.

### P03.F7 [PARTIAL] (G3) Stock reservation & release
- gap: Releasing from the screen (or by expiry or reallocation) never informs the owning document: a sales line keeps reservation_id/allocated_quantity and the order stays ALLOCATED, so dispatch then fails consuming a RELEASED reservation (dispatch/service.py:1496-1524), and cancelling or editing the order fails as well because the sales/job-work release paths call allocation.release on a closed reservation, which raises (allocation/service.py:376-399; sales/service.py:1471-1479, 1784-1792; job_work/service.py:1430-1434). Found by reading code, not executed; no test covers it. Manual Reserve needs a pasted demand UUID (reservations-page.tsx:138-144); UI release is all-or-nothing.
- planned: none
- rec: Medium: make every document release path idempotent for RELEASED/EXPIRED/CONSUMED reservations; add an allocation-to-demand callback port (or derive sales allocated_quantity and line status from open reservations) so manual release, expiry and reallocation update SO lines/order status and job-work/assembly pointers; demand pickers instead of UUID paste; partial-release quantity in the release dialog.

### P03.F8 [PLANNED_PARTIAL] (G3) Stock reallocation
- gap: No screen lists, approves or rejects requests (P.ALLOCATION_APPROVE is unused in apps/web; docs/handbook/content.json:3183 states it), so no reallocation can be executed from the UI. The recipient reservation is created with demand_line_id=None (allocation/service.py:1440) and the request has no line id (allocation/schemas.py:194-214), so dispatch and production issue never consume it and the demand documents are not updated (P05.S7/S8). Donors are limited to the same item and warehouse (allocation/repository.py:275-287); a demand holding no reservation cannot start a request in the UI.
- planned: B4-smart-alerts-events (only: notify allocation.approve holders when a request is raised, and fix the broken /allocation/reservations/{id} link). Still missing: approval queue UI, demand-line linkage and document sync, entry point for an unallocated demand, cross-warehouse donors.
- rec: Medium-large: reallocation approval tab, to_demand_line_id on request and recipient reservation, demand sync port, 'Request reallocation' from SO/production shortfalls (details under P05.S5-S8).

### P05.R3 [PARTIAL] (G3) Assembly
- gap: The assembly UI has no create/approve/release action (assembly-page.tsx and assembly-detail-dialog.tsx call only /issue-components and /complete), so assembly reservations are never created from a screen; they carry default priority 100 and no required date; issuing components does not touch the reservation, so parts already on the bench stay reallocatable (P05.S4).
- planned: none
- rec: Medium: assembly order create/approve/release/start/close/cancel in the UI; pass order priority and planned start into the reservation; mark the reservation issued on /issue-components.

### P05.R5 [PARTIAL] (G3) Service / spare
- gap: No service/spare demand document (no module uses DemandType.SPARE; items.can_be_spare only filters items), and nothing can consume a SPARE reservation - allocation.consume is called only by dispatch (dispatch/service.py:1514) and production (production/service.py:2569) - so a spare reservation can only be released.
- planned: none
- rec: Medium: a spare/service request document (customer or service ticket, can_be_spare lines, priority, required date) that reserves as SPARE and issues or dispatches through allocation.consume, with screen, permissions and audit; or let SO lines flagged as spare reserve as SPARE.

### P05.R6 [PARTIAL] (G3) Soft
- gap: No module creates SOFT reservations (sales, production, assembly, job work all reserve HARD). A soft reservation blocks availability exactly like a hard one and can only be taken through the same PENDING request plus approval (create_reallocation_request always PENDING, allocation/service.py:1949-2043), contradicting docs/assumptions.md section 10 ('SOFT reservations may be taken by a higher-priority demand without approval').
- planned: none
- rec: Small-medium: implement section 10 - auto-execute a request whose donor is SOFT (or let reserve() draw SOFT stock for a more urgent demand) - and create SOFT reservations from planning (DRAFT SO or MRP planned orders); align UI copy and handbook.

### P05.P2 [PARTIAL] (G3) Assembly stock
- gap: Only directly demanded items are exploded, through one consolidated multi-level explosion of the whole BOM (mrp/engine.py:1071-1086; bom/engine.py _walk L449-554), so stock of an intermediate assembly does not reduce demand for its own children: with 50 Rose Handles in stock, 100 kits still raise handle bodies, springs and screws for 100. The 'assembly stock before components/raw materials' order is therefore not honoured; nothing reserves assembly stock for the sales order (reservation is FG-only); assembly requirements convert to production orders, not assembly orders (mrp/service.py:208-221). No test covers intermediate-stock netting.
- planned: none
- rec: Medium: explode level by level in MRP (bom_engine.explode with max_depth=1 per manufactured item, or propagate netted intermediate quantities down the consolidated tree) so each intermediate's free stock and in-time supply are netted before its children are exploded; tests for 'sub-assembly stock reduces component demand'; optional ASSEMBLY_ORDER conversion target.

### P05.P3 [PARTIAL] (G3) Semi-finished goods
- gap: Same defect as P05.P2: semi-finished stock does not reduce the demand for the components/raw materials beneath it because only directly demanded items are exploded and the explosion is consolidated across all levels (mrp/engine.py:1071-1086).
- planned: none
- rec: Medium: same level-by-level netting fix as P05.P2.

### P05.P4 [PARTIAL] (G3) Components
- gap: For components that are themselves manufactured from raw materials, component stock does not reduce raw-material demand (same consolidated-explosion defect as P05.P2), so the components-before-raw-materials order holds only for bought components.
- planned: none
- rec: Medium: same level-by-level netting fix as P05.P2.

### P05.P6 [PARTIAL] (G3) Cross-warehouse stock
- gap: SO confirmation reserves only at order.warehouse_id (sales/service.py:1667-1691); reallocation donors are limited to the same warehouse (allocation/repository.py:275-287). In MRP, other warehouses' stock silently covers a shortage (and is checked before BOM explosion, not after raw materials), while MrpSuggestionType.TRANSFER is never produced (mrp/models.py:129-143) and is not convertible (web features/mrp/mrp-types.ts:73-79), so nothing proposes or reserves the transfer; the SO stays short.
- planned: none
- rec: Medium-large: cross-warehouse step in allocation - show other warehouses' free stock on the SO allocation panel and offer 'reserve at source + raise transfer' (DemandType.TRANSFER reservation and a StockTransfer to the SO warehouse); MRP pass emitting TRANSFER suggestions per demand warehouse, convertible to stock transfers; allow reallocation donors in other warehouses.

### P05.P9 [PARTIAL] (G3) Job work requirement
- gap: Conversion is refused: job_work/service.py:2502-2520 create_order_from_mrp raises MRP_CONVERSION_REFUSED (mrp/service.py:234-239), so the planner must raise the job-work order by hand on /job-work and then ignore the suggestion.
- planned: none
- rec: Small: a 'Convert to job work order' dialog on JOB_WORK suggestions that asks vendor, process and return date, creates the order with source = suggestion and marks it CONVERTED.

### P05.S4 [PARTIAL] (G3) Check material issue status
- gap: Assembly 'issue components' moves no stock and does not touch the reservation (assembly/service.py:17-23, 1192-1205), so parts already on the bench look untouched and are offered with the bonus; stock already packed into cartons for an SO is still an untouched reservation (packing/service.py:60-63); allocated_quantity is never maintained (P03.S4).
- planned: none
- rec: Small-medium: record issue on the reservation (consume at issue or an issued/allocated quantity on stock_reservations) for assembly and mark packed stock allocated; candidate_eligibility excludes or penalises them.

### P05.S5 [PARTIAL] (G3) Suggest reallocation
- gap: The UI starts only from an existing reservation row of the requesting demand (reservations-page.tsx:1127-1134, body built from the row L694-706): an urgent order with zero allocation - the proposal's scenario, when production holds all the stock - has no row and cannot request; no action on the SO allocation panel or production shortages; priority/required date come from the stored row, not editable; donors same warehouse only.
- planned: none
- rec: Small: 'Request reallocation' on SO allocation-panel shortfall lines and production component shortages, prefilled with item, warehouse, shortfall, priority, required date and demand line id.

### P05.S6 [PLANNED_PARTIAL] (G3) Take approval
- gap: No screen lists, approves or rejects requests (ALLOCATION_APPROVE unused in apps/web); approvers are not notified when a request is raised (create_reallocation_request notifies nobody).
- planned: B4-smart-alerts-events - adds the 'Stock reallocation request' notification to allocation.approve holders; still missing the approval queue UI.
- rec: Small: 'Reallocation requests' tab on /inventory/reservations (GET /allocation/reallocation-requests?status=PENDING with impact_summary donor/requester, Approve/Reject with note, gated P.ALLOCATION_APPROVE, hidden from the requester).

### P05.S7 [PARTIAL] (G3) Release old reservation
- gap: Reachable only through approval, which has no UI. The donor's demand document is not updated: a sales donor keeps allocated_quantity and ALLOCATED status and then fails at dispatch (consume more than remaining), or, if fully taken, cannot be cancelled or edited (release of a RELEASED reservation raises) - see P03.F7.
- planned: none
- rec: Medium: demand sync port plus idempotent release paths (as P03.F7).

### P05.S8 [PARTIAL] (G3) Allocate to urgent order
- gap: The recipient reservation has demand_line_id=None (L1440) and the request carries no line id (allocation/schemas.py:194-214), so dispatch (consumes only the SO line's reservation_id, dispatch/service.py:1496-1524) and production issue (only reservations with demand_line_id, production/service.py:2546-2550) never consume it; the urgent SO's line allocated_quantity/status and order status are not updated (sales/service.py:841-892 reads the line), so the order stays CONFIRMED and dispatch is refused (dispatch/service.py:443-464). Found by reading; no test covers reallocation followed by dispatch.
- planned: none
- rec: Medium: to_demand_line_id on ReallocationRequestCreate/model and recipient reservation; owning module told through a port (sales sets line reservation_id/allocated_quantity and advances CONFIRMED->ALLOCATED, or derives them from reservations); production issue consumes order-level reservations; integration test reallocate -> dispatch.

### P05.S9 [PLANNED_PARTIAL] (G3) Notify affected department
- gap: No department/role recipients (production planning for a production donor, assigned sales user/sales roles for an SO donor); the requester is not told of approve/reject; action_url /allocation/reservations/{id} (L1849) has no frontend route (app/router.tsx has only /inventory/reservations); nobody is notified when a request is raised.
- planned: B4-smart-alerts-events - fixes the broken link and notifies allocation.approve holders on request creation; still missing department/role notification of the donor's owners and decision notification to the requester.
- rec: Small: notify_role for the donor demand's department (map DemandType to roles, plus the customer's assigned sales user for SO donors) and APPROVAL_GRANTED/REJECTED to the requester.

### P05.D1 [ABSENT] (G3) Customer priority
- gap: No customer priority exists anywhere and nothing feeds it into allocation or reallocation.
- planned: none
- rec: Small-medium: customers.priority (1-9 or A/B/C tier) defaulting the SO priority, plus a customer_priority_weight term in ReallocationPolicy/score_candidate.

### P05.D2 [PARTIAL] (G3) Delivery date
- gap: Used only when a planner manually raises a request: there is no automatic reallocation anywhere (no beat task besides release_stale_reservations, workers/celery_app.py:81; no hook on SO confirm/update); SO date edits after confirmation are not pushed to reservations (sales/service.py:1341-1352).
- planned: none
- rec: Medium (shared by D2-D7): auto-reallocation proposals - on SO confirm/update with a shortfall (or urgent flag / priority raise) and on a periodic task, run suggest_reallocation and raise PENDING requests automatically; per-company policy settings (weights, protection window, auto mode) with an admin screen.

### P05.D3 [PARTIAL] (G3) Sales order priority
- gap: No automatic trigger; is_urgent ignored; priority edits after confirmation not propagated (sales/service.py:1341-1352); 1-9 scale versus default 100 elsewhere (see P05.S3).
- planned: none
- rec: Medium: see P05.D2 auto-reallocation plus the scale/propagation fixes in P05.S3.

### P05.D4 [PARTIAL] (G3) Production priority
- gap: No automatic trigger; default 100 means any SO (priority <= 9) outranks it unless planners renumber production orders.
- planned: none
- rec: Medium: see P05.D2 and P05.S3.

### P05.D5 [ABSENT] (G3) Work order status
- gap: Work order status is not a reallocation input at all.
- planned: none
- rec: Small-medium: production pushes a started/in-progress flag onto its reservations (or allocation reads it through a production port) and candidate_eligibility refuses or penalises donors whose work order is IN_PROGRESS.

### P05.D6 [PARTIAL] (G3) Material issue status
- gap: Assembly-issued and packed stock are not reflected (P05.S4); no automatic trigger (P05.D2).
- planned: none
- rec: Small-medium: see P05.S4 and P05.D2.

### P05.D7 [PARTIAL] (G3) Manager approval rules
- gap: No approval UI; rules are fixed in code - no thresholds by quantity/value/customer/demand type, no approver routing or escalation, no SOFT auto-approval despite docs/assumptions.md section 10.
- planned: none
- rec: Medium: approval-rule settings (auto-approve SOFT donors, threshold above which a plant manager must approve, approver role per donor demand type) applied in create_reallocation_request/reallocate, plus the approval queue UI (P05.S6).

### P04.M7 [PARTIAL] (G4) Product master: Variant
- gap: No screen creates or edits a variant - stated in the code itself (item-detail-page.tsx:430-433: POST /products/items/{id}/variants and PATCH /products/variants/{id} 'have no screen anywhere in the application yet'). The sales-order line editor never offers a variant (item_variant_id is only carried through, C:/Project/Other/ATCRAFT/apps/web/src/features/sales/sales-types.ts:126). Inventory balances have no variant grain, so a variant cannot be stocked, reserved or dispatched on its own.
- planned: none
- rec: Small-medium: VariantFormDialog on the Variants tab (code, name, finish/size/colour selects via useItemAttributeOptions, price_delta, cost_delta gated on item.cost_view, barcode, active) using the existing POST/PATCH routes, plus an Edit row action; a variant select on the SO line editor when the item has variants (pricing already honours variant_id); record in docs/assumptions.md that variants share the parent's stock and BOM, or agree with the client to model separately stocked finishes as items.

### P04.M9 [PARTIAL] (G4) Product master: Sale price
- gap: The item master's own Sale price cannot be entered or edited on any screen: ItemFormDialog's schema and payload omit sale_price (C:/Project/Other/ATCRAFT/apps/web/src/features/items/item-form-dialog.tsx:63-100,167-199), yet /items/:itemId shows a read-only 'Sale price' row (item-detail-page.tsx:188) that stays 0 for every UI-created item; only the API can set it.
- planned: none
- rec: Small: add a Sale price input (decimal string) to ItemFormDialog and send sale_price on POST/PATCH; optionally require price.manage for changing item-master prices, as sales-line price overrides already do.

### P04.M10 [PARTIAL] (G4) Product master: Dealer price
- gap: Same as P04.M9: no Dealer price field in ItemFormDialog (C:/Project/Other/ATCRAFT/apps/web/src/features/items/item-form-dialog.tsx:63-100,167-199); /items/:itemId shows a read-only 'Dealer price' (item-detail-page.tsx:189) that screen users cannot set.
- planned: none
- rec: Small: add a Dealer price input to ItemFormDialog in the same change as P04.M9.

### P04.B4 [PARTIAL] (G4) BOM management: BOM versioning
- gap: No screen edits a DRAFT: PATCH /api/v1/bom/{bom_id} (router.py:173-188; BomUpdate replaces lines and operations, bom/schemas.py:230-268) is never called by the frontend, and the detail page offers no Edit action (bom-detail-page.tsx:985-1030). A copied version is an identical structure that cannot be changed on screen; a structurally revised version must be re-keyed from scratch through New BOM; a BOM rejected back to DRAFT cannot be corrected.
- planned: none
- rec: Medium: 'Edit draft' action on /bom/:bomId for DRAFT with bom.update, reusing the BomFormDialog line grid prefilled from GET /bom/{id} (component, qty per, scrap, optional, substitute_for_line_number, operation_sequence, notes) plus an operations grid; send PATCH /bom/{id} and surface the row_version 409; optional compare view between two versions of an item.

### P04.B6 [PARTIAL] (G4) BOM management: BOM costing
- gap: The operation (labour/process) half cannot be populated from the UI: BomFormDialog posts no operations (C:/Project/Other/ATCRAFT/apps/web/src/features/bom/bom-dialogs.tsx:187-210), there is no draft editor, and nothing snapshots the item's routing (setup/run minutes, work-centre cost_per_hour, process_cost) into bom_operations - so every UI-built BOM is costed on material only (operation cost 0), and the routing's own cost (GET /api/v1/routing/{routing_id}/cost) is never combined with it. Operations appear only as a count on the BOM page (bom-detail-page.tsx:1065).
- planned: none
- rec: Medium: when a BOM is created or approved, snapshot the item's effective default approved routing into bom_operations (sequence, name, routing_operation_id, work_center_id, setup/run minutes, work-centre cost_per_hour, process_cost as fixed_cost, is_outsourced) - the purpose BomOperation's docstring states - and/or add an Operations grid to the BOM create/edit dialog; list the operations on /bom/:bomId.

### P04.R3 [PARTIAL] (G4) Routing: Machine
- gap: The routing screen cannot assign a machine to an operation: the form carries machine_id without any control ('Carried, not edited', C:/Project/Other/ATCRAFT/apps/web/src/features/routing/routing-dialogs.tsx:508-513; new rows default to null, :546) and RoutingDetailDialog has no Machine column (routing-dialogs.tsx:158-270). A machine can only be picked later on each work order (production WorkOrderAssignDialog).
- planned: none
- rec: Small: add a Machine select per operation row filtered by that row's work centre (GET /routing/machines?work_center_id=..., excluding RETIRED and inactive), cleared when the work centre changes and disabled when Outsourced is ticked; show the machine in RoutingDetailDialog.

### P06.O3 [PARTIAL] (G4) MRP output: Job work order suggestion
- gap: They cannot become job-work orders: job_work.service.create_order_from_mrp always raises 'A job work order needs a vendor and a process...' (C:/Project/Other/ATCRAFT/apps/api/src/modules/job_work/service.py:2496-2520), so the planner raises the order by hand and ignores the suggestion. The UI still offers Convert for JOB_WORK (isConvertible excludes only TRANSFER, C:/Project/Other/ATCRAFT/apps/web/src/features/mrp/mrp-types.ts:62-79), so every bulk convert containing one reports failures. Open job-work orders' expected output (job_work_order_lines.expected_output_item_id with expected_return_date, C:/Project/Other/ATCRAFT/apps/api/src/modules/job_work/models.py:235,414) is not netted as supply (DatabaseMrpSource reads only POs and production, C:/Project/Other/ATCRAFT/apps/api/src/modules/mrp/repository.py:461-485), so the next run re-suggests work already at the vendor.
- planned: none
- rec: Medium: 'Raise job-work order' from the suggestion - dialog prefilled with item, quantity and required date where the planner picks vendor and process (default from routing_operations.job_work_vendor_id or the preferred item-vendor link); extend SuggestionConvertRequest with vendor_id, process and expected_return_date and make the hook create a DRAFT JW order (JobWorkSourceType.MRP_SUGGESTION exists, job_work/models.py:147-159); add job_work.service.open_job_work_output(item_ids, warehouse_ids, upto) as a third dated supply feed; until then exclude JOB_WORK from isConvertible.

### P06.O4 [PARTIAL] (G4) MRP output: Stock transfer suggestion
- gap: Never generated: a run pools all scoped warehouses into one position per item (StockPositionRepository.positions, C:/Project/Other/ATCRAFT/apps/api/src/modules/mrp/repository.py:317-359), so it cannot see 'surplus in A, short in B'; ConversionTarget has no stock-transfer target (C:/Project/Other/ATCRAFT/apps/api/src/modules/mrp/schemas.py:75-96); no hook into inventory transfers.
- planned: none
- rec: Medium-large: per-warehouse netting pass before PURCHASE/MANUFACTURE - for each short (item, demand warehouse) look for free stock (excluding reserved, QC and safety stock) in other warehouses and emit TRANSFER suggestions with new from_warehouse_id/to_warehouse_id columns that reduce the shortage; add ConversionTarget.STOCK_TRANSFER and inventory.service.create_transfer_from_mrp creating a DRAFT transfer; enable Convert for TRANSFER; update database.md section 6.

### P06.O5 [PARTIAL] (G4) MRP output: Reallocation suggestion
- gap: Not an MRP output: MrpSuggestionType has no REALLOCATION value (C:/Project/Other/ATCRAFT/apps/api/src/modules/mrp/models.py:139-142; database.md section 6 lists none); the engine simply excludes reserved stock (C:/Project/Other/ATCRAFT/apps/api/src/modules/mrp/engine.py:258-279) and never proposes taking it from a lower-priority demand for a short urgent order, so the MRP run cannot show it; the allocation suggestion is reachable only one reservation at a time by hand.
- planned: none
- rec: Medium-large: add REALLOCATION to MrpSuggestionType (CHECK-constraint migration, badge, database.md section 6); during a run, for short demand lines with priority and date, batch-read candidate reservations via allocation's candidate_pool/rank_candidates and emit REALLOCATION suggestions (donor reservation, from-demand, to-demand, quantity) that reduce the shortage before PURCHASE/MANUFACTURE; conversion hook -> allocation create_reallocation_request (PENDING; approval and donor notification already exist).

### P06.F2 [PARTIAL] (G4) MRP feature: Multi-level demand calculation
- gap: No level-by-level netting: only directly demanded items are netted before exploding (_settle_demand candidates, engine.py:1081-1086), and the root's explosion carries gross quantities through every sub-assembly (C:/Project/Other/ATCRAFT/apps/api/src/modules/bom/engine.py:589-652). Free stock or in-time supply of an intermediate sub-assembly (e.g. Rose handles on the shelf) reduces only that sub-assembly's own requirement, never the demand for its children (springs, bushes), so components are over-ordered by up to the sub-assembly's free stock - contrary to the proposal's assembly / semi-finished before components priority. Every level is also dated from the root's lead time only (_dependent_required_date, engine.py:932-944). The engine docstring states the limitation (engine.py:121-149).
- planned: none
- rec: Medium-large: low-level-code MRP - compute each item's lowest BOM level, process items level by level, net gross (direct + dependent) against free stock and in-time supply, explode only the net shortfall one level down (single-level lines from the BOM engine) and offset each child's date by its parent's own lead time; keep merge, time-phasing and provenance; add a stocked-sub-assembly case to tests/unit/test_mrp_engine_merge.py and update docs/assumptions.md.

### P06.F5 [PARTIAL] (G4) MRP feature: Auto purchase planning
- gap: (1) The ordering-policy inputs cannot be maintained on any screen: preferred item-vendor links (lead_time_days, min_order_quantity, order_multiple) are list-only on /items/:itemId (C:/Project/Other/ATCRAFT/apps/web/src/features/items/item-detail-page.tsx:777-898, although POST /api/v1/products/items/{item_id}/vendors and PATCH /api/v1/products/item-vendors/{link_id} exist, products/router.py:645-702), and items.min_order_quantity / safety_stock / default_warehouse_id are absent from ItemFormDialog (C:/Project/Other/ATCRAFT/apps/web/src/features/items/item-form-dialog.tsx:63-100) - so UI-maintained items plan with no MOQ or multiple and the item lead time only. (2) Converting from an unscoped run (the /mrp default) fails for items without a default warehouse (procurement/service.py:589-597). (3) Nothing runs MRP automatically: no Celery beat entry (C:/Project/Other/ATCRAFT/apps/api/src/workers/celery_app.py:71-110) and no trigger on sales-order confirmation; every run is a manual POST /api/v1/mrp/runs.
- planned: none
- rec: Small-medium: item-vendor link dialog (add, edit, set preferred) on the item Vendors tab using the existing routes; add Default warehouse, Min order qty, Safety stock and Reorder qty to ItemFormDialog; optional per-company scheduled MRP (beat task creating an ASYNC run with configured horizon and scope) with a completion notification (B4 adds the 'Production shortage' event, not the schedule).

### P06.F6 [PARTIAL] (G4) MRP feature: Auto production planning
- gap: (1) Manufacture lead time ignores the routing: routing.service.manufacture_lead_time_days, documented as 'The contract MRP calls' (C:/Project/Other/ATCRAFT/apps/api/src/modules/routing/service.py:1587-1602), has no caller - MRP uses static items.lead_time_days whatever the quantity (engine.py:1505-1512). (2) Sub-assembly orders are not offset by their own lead times (all levels dated root date - root lead time, engine.py:932-944). (3) Default-warehouse refusal on unscoped runs and the default-BOM requirement at release (see P06.O1). (4) No scheduled run and no capacity check - work orders are planned back to back at infinite capacity (production/service.py:613-618 ASSUMPTION).
- planned: none
- rec: Medium: batched routing lead-time read in DatabaseMrpSource (fallback items.lead_time_days) used for MANUFACTURE order-by dates and child offsets; per-level offsets with P06.F2; the P06.O1 / P06.F5 master-data fixes; optional scheduled run; optional work-centre load check (routing minutes vs working_hours_per_day, later B2 rostered capacity) flagging overloaded days on converted orders.

### P06.F7 [PARTIAL] (G4) MRP feature: Lead time calculation
- gap: MRP never uses the calculated routing lead time (manufacture_lead_time_days has no caller anywhere in src), offsets only once from the root, and the vendor lead time it prefers cannot be entered on screen (item-vendor links are list-only).
- planned: none
- rec: Small-medium: add manufacture_lead_times(item_quantities, as_of) to MrpDataSource calling the routing service in one batch, and use it for MANUFACTURE suggestions and child date offsets; per-level offsets with P06.F2; vendor-link editor with P06.F5.

### P06.F8 [PARTIAL] (G4) MRP feature: Expected availability date
- gap: No expected availability date is computed: not for a suggestion (when the material arrives if ordered now - for a late suggestion the required date is unattainable and no realistic date is given), not for a requirement (when the whole gross is covered), and not per sales-order line (when the order can be fulfilled). GET /api/v1/sales/{id}/allocation-status returns quantities only (C:/Project/Other/ATCRAFT/apps/api/src/modules/sales/schemas.py:305-329), allocation ATP returns a quantity, and SO promised_date is typed by hand.
- planned: none
- rec: Medium: add expected_available_date to mrp_suggestions (= max(as_of, suggested_order_date) + lead_time_days) and to mrp_requirements (date the gross is covered by stock, in-time supply, late tranches and the new order); roll it up per mrp_demands line through its BOM provenance (max over components plus parent lead time) and expose it on GET /mrp/runs/{id}/demand; show it on the run, the shortage report and the SO detail, optionally offering it as promised_date.

### P07.M1 [PLANNED_PARTIAL] (G5) Purchase requisition
- gap: 1. An approved requisition cannot become a PO through any endpoint or screen. The only path is raising an RFQ, and the PO form never sends requisition_line_id.
2. There is no /close route, so the APPROVED->CLOSED and CONVERTED->CLOSED edges are unreachable.
3. There is no read-only requisition view. The approve/reject ConfirmDialog (requisitions-page.tsx:624-675) shows only the code and a note field, and RequisitionSummary has no lines, so approvers decide without seeing the items. GET /purchase-requisitions/{id} is used only by the draft edit dialog.
4. create_requisition_from_mrp drops request.source_demands (the sales-order and BOM references).
- planned: C0-requisition-split covers:
- convert-to-orders (single and batch) and the conversion dialog;
- POST /purchase-requisitions/{id}/close with a row action.
D1-duplicate-po-actions persists source_demand_refs.
Not covered: a read-only requisition detail for approvers.
- rec: Small. Add a requisition detail drawer or page (/procurement/requisitions/:id) that reads GET /procurement/purchase-requisitions/{id}. Show lines, quantity, ordered_quantity, suggested vendor, required date and source. Embed the line table in the approve/reject dialog. Add the page to route-loaders and the sweeps.

### P07.M2 [PARTIAL] (G5) RFQ
- gap: Send only stamps sent_at and changes the status (transition_rfq, service.py:1090-1131). Nothing reaches the named vendors: no RFQ document or PDF, no e-mail, no vendor-facing view. The buyer has to send the request outside the ERP.
- planned: none
- rec: Medium.
- Add a document-only ReportKey RFQ and a pure render_rfq_pdf, reusing the packing-list and C1a PO-PDF helpers.
- Add POST /procurement/rfqs/{id}/pdf, answering 202 with a job.
- On send, queue one e-mail per named vendor (vendors.email or the primary VendorContact) with the PDF attached. This needs attachment support added to core/mail.py OutboundEmail.
- Audit each recipient and show 'Sent to N vendors' on the RFQ detail.

### P07.M3 [PARTIAL] (G5) Vendor quotation
- gap: A SELECTED quotation leads nowhere:
- decide_quotation creates nothing.
- No endpoint or UI raises a PO from it (see the quotations-page.tsx:303 comment).
- converted_purchase_order_id is set only when a PO is POSTed by API with source_type=VENDOR_QUOTATION (service.py:2263). Only the demo loader does that (core/demo_data/procurement.py:1524).
- The PO form never sends quotation_line_id.
- planned: none. C0 resolves vendor and price from requisition-line suggestions, item and category preferences, and estimated or last price, never from a selected quotation.
- rec: Medium. Add POST /procurement/quotations/{id}/convert-to-order (purchase_order.create + forbid_vendor_users), building a DRAFT PO through the shared create body:
- vendor and currency from the quotation; warehouse from the RFQ;
- lines carrying quotation_line_id, price, discount and required_date, plus requisition_line_id followed from rfq_line to requisition_line (so _link_source_documents updates ordered_quantity);
- source_type VENDOR_QUOTATION;
- it runs the duplicate check and sets converted_purchase_order_id.
In the UI, add a 'Raise purchase order' action on SELECTED rows and in the decision dialog.

### P07.M5 [PLANNED_PARTIAL] (G5) Purchase order
- gap: 1. No edit screen. PATCH exists, but the form dialog is create-only, so a DRAFT or PENDING_APPROVAL PO cannot be corrected in the UI.
2. No Close or Short-close action, although POST /{id}/close exists. The Command type at detail-page:391 has no close.
3. No PO PDF or print.
4. No link to requisitions or quotations: buildPayload (form-dialog:147-180) sends no requisition_line_id, quotation_line_id or source.
5. update_purchase_order (service.py:2302) never re-runs the duplicate check.
6. Send has no assignee picker, and POs have no category.
7. Status never advances past QC_PENDING or GRN_COMPLETED (see A04.11-A04.14).
- planned: C0: category, requisition conversion, send with assignee.
C1a + C2b: PO PDF, shipments, vendor date, declined handling.
D1: amend for APPROVED, SENT and ACKNOWLEDGED.
Not covered:
- draft edit UI;
- close/short-close UI;
- quotation->PO;
- duplicate re-check on PATCH;
- status write-back after QC, i
- rec: Small:
- Reuse purchase-order-form-dialog in edit mode for DRAFT and PENDING_APPROVAL (PATCH, P.PURCHASE_ORDER_UPDATE).
- Add a 'Short close' / 'Close' command (P.PURCHASE_ORDER_CANCEL) on PARTIALLY_RECEIVED, GRN_COMPLETED and PAID.
- In update_purchase_order, take the same po-duplicate advisory lock and call find_duplicates_for_lines(exclude_order_id=order.id), with the acknowledge rule.
Medium: quotation->PO (P07.M3) and the status write-backs (A04.11-A04.14).

### P07.M6 [PARTIAL] (G5) GRN
- gap: 1. No GRN detail screen. GET /goods-receipts/{id} is read only inside purchase-return-dialogs.tsx:392 and vendor-invoice-form-dialog.tsx:816.
2. No cancel-draft action, although POST /goods-receipts/{id}/cancel exists.
3. goods_receipt_lines.qc_status never leaves PENDING: it is written only at service.py:2863, :3162 and :3247. The 'Waiting on QC' filter (qc_pending_only, repository.py:1211) therefore keeps every inspected receipt for ever.
- planned: none for the staff GRN screens. C1a adds only a vendor-side receipts view; B4 adds a GRN_NOT_CREATED alert.
- rec: Small. Add a GRN detail drawer or page (/procurement/goods-receipts/:id) showing lines, batch, accepted and rejected quantities, QC status and an inspection link. Add a 'Cancel draft' row action gated on P.GRN_CREATE. The qc_status write-back belongs with P07.M7.

### P07.M7 [PLANNED_PARTIAL] (G5) Incoming QC
- gap: The verdict never flows back to procurement. decide_inspection updates neither goods_receipt_lines.qc_status, which stays PENDING, nor the PO. As a result:
- A fully received PO with QC lines stays QC_PENDING for ever. Its only exit edge, QC_PENDING->QC_COMPLETED, is never driven.
- The PO tracking panel shows 'QC pending' permanently (order_tracking, service.py:2636-2735).
- The GRN QC filter never clears.
- planned: C1a derives InboundTrackingStatus (QC_PENDING / GRN_COMPLETED) from inspections for the new inbound board and vendor portal, and sends QC rejections to vendor users. B4 adds a QC_PENDING alert from open inspections. No WP writes the verdict back to the GRN line or the PO status.
- rec: Small-medium. Add procurement.service.record_incoming_inspection_outcome(session, tenant, receipt_id, inspection_id). Quality sits downstream of procurement in docs/architecture.md, so it may call this. Invoke it from decide_inspection and close when the reference is the GRN:
- set the GRN line's qc_status to PASSED or FAILED once nothing remains on hold;
- when the PO has no PENDING lines left, take QC_PENDING->QC_COMPLETED->GRN_COMPLETED through _transition in the same transaction.
Add a data migration that backfills the stranded orders, and tests for partial and hold decisions.

### P07.M8 [PARTIAL] (G5) Purchase return
- gap: There is no financial leg.
- Posting a return raises no vendor debit note and no AP or input-tax reversal. Finance has no purchase-return reference and only customer adjustments (finance/models.py:150), so the vendor payable stays at the pre-return amount.
- The vendor-invoice three-way match compares against accepted_quantity without netting returned_quantity (finance/service.py:2043, _accepted_quantity :2118), so returned goods can be billed and still match.
- planned: none. B3b only asks to document that returns post nothing.
- rec: Medium. On post_purchase_return, call a finance seam (finance is downstream) that posts a vendor debit note:
- Dr ACCOUNTS_PAYABLE (party VENDOR);
- Cr PURCHASE_EXPENSE;
- Cr INPUT_TAX;
- for the returned taxable value plus GST, with reference_type purchase_return, reversed on cancel.
Optionally give it its own DN/ series and PDF. Change evaluate_match to use accepted minus returned quantity. Integration test: GRN -> return -> invoice for the full quantity is refused by the match.

### P07.M9 [PLANNED_PARTIAL] (G5) Vendor invoice
- gap: 1. Dispute, resolve-dispute and cancel have routes but no screen. No UI caller exists.
2. APPROVED->DISPUTED->CANCELLED cancels an invoice whose journal is already posted without reversing it: cancel_invoice (service.py:2357) never calls reverse_journal. This leaves a phantom payable on the vendor ledger.
3. The match does not net purchase returns (see P07.M8).
4. Approval never moves the PO to INVOICED (see A04.13).
- planned: B3a-job-work-invoice §7 fixes the cancel reversal, and §9 adds the Type column and filter. C1a / C2a add vendor-submitted invoices.
Not covered:
- dispute / resolve / cancel UI;
- a return-aware match;
- the PO INVOICED transition.
- rec: Small, beyond B3a:
- Add Dispute (reason), Resolve dispute and Cancel (reason) actions to the vendor-invoice row or match dialog, gated P.VENDOR_INVOICE_APPROVE and shown only on the statuses the machine allows.
- Make the match subtract returned_quantity.
- Call the A04.13 PO status hook on approve and cancel.

### P07.M13 [PARTIAL] (G5) Lead time tracking
- gap: Actual lead time is never measured:
- nothing computes order/sent date -> GRN receipt date per PO, vendor or item;
- no report compares quoted or master lead time with actuals;
- master lead times are never refreshed from history.
- planned: none. B5's VENDOR_ACCEPTANCE measures hours to acknowledge, not delivery lead time.
- rec: Medium.
- Add report VENDOR_LEAD_TIME (report.view; ReportKey, catalogue entry and CHECK-swap migration). Per vendor and item it shows sample size, master and quoted lead time, and actual average, median and p90 days (GRN receipt_date minus PO sent_at::date, else po_date), plus on-time %.
- Show the actual average on the vendor rating dialog and on item-vendor links.
- Optionally add an audited 'adopt measured lead time' action on item_vendor_links.

### P07.V5 [PARTIAL] (G5) Vendor type: transport vendor
- gap: The payable party (a TRANSPORT vendor) and the carrier (a Transporter) are not connected: transporters has no vendor_id. Consequences:
- freight on a dispatch, or on an inbound PO shipment, cannot be billed, paid or reconciled against the vendor that carried it;
- dispatch freight posts nothing to finance;
- no TRANSPORT vendor exists in the demo data.
- planned: none. C1a adds transporter_id and freight_amount on inbound shipments, and B5 an INBOUND_FREIGHT cost report, but neither links a transporter to a vendor or payable.
- rec: Medium.
- Add transporters.vendor_id: nullable FK to vendors (RESTRICT), tenant-validated, and the vendor must hold the TRANSPORT type. Add a vendor picker to the transporter dialog.
- Allow freight vendor-invoice lines to reference dispatch or shipment ids.
- Add a 'freight incurred vs billed' report per transporter.
- Seed one TRANSPORT vendor linked to the demo courier.

### P07.V6 [PARTIAL] (G5) Vendor type: service vendor
- gap: Service procurement is not modelled:
- A SERVICE item on a PO is received by post_goods_receipt into inventory. No non-stock exclusion exists (service.py:2954-3032).
- A service bill without a PO always fails the match with 'not linked to a purchase order' (finance/service.py:737-741), so it needs a variance override with a reason.
- There is no service acceptance step.
- planned: none. B3a types vendor invoices only as PURCHASE or JOB_WORK.
- rec: Medium-large.
- Add a non-stock flag on items, or a SERVICE kind on PO lines.
- Confirm service lines through a service acceptance (quantity or amount accepted; no inventory movement and no QC) instead of a GRN.
- Match the invoice against the acceptance; optionally add invoice_type SERVICE on top of B3a's typed invoice.
- Refuse stock posting for non-stock items in post_goods_receipt.

### A04.2 [PLANNED_PARTIAL] (G5) PO flow: vendor selection
- gap: No selection flows into a PO. A selected quotation creates nothing, and suggested or preferred vendors are read by no PO creation path.
- planned: C0 §2: vendor resolution in the order override, suggested vendor, item preferred, category preferred, with a per-line override in the conversion dialog. Not covered: a selected quotation becoming a PO.
- rec: Medium. See P07.M3: convert a selected quotation to a PO, or have C0's conversion prefer a SELECTED quotation's vendor and price for lines covered by an RFQ.

### A04.11 [PLANNED_PARTIAL] (G5) PO flow: incoming QC
- gap: 1. The QC_PENDING->QC_COMPLETED edge (service_rules.py ~337-343, qc.decide) is never driven, and the GRN line qc_status is never updated, so the PO stays QC_PENDING for ever.
2. QC_PENDING has no cancel or close exit.
3. The vendor keeps counting as having open POs (OPEN_PURCHASE_ORDER_STATUSES, vendors/repository.py:55-67), so it cannot be retired (vendors/service.py:1047).
- planned: C1a derives the QC state from inspections for the board and portal, and shows vendors their QC results. B4 adds a QC_PENDING alert. No WP writes the verdict back.
- rec: Small-medium: build the write-back described under P07.M7, with a backfill migration and updated tests/unit/test_procurement_lifecycle.py expectations.

### A04.12 [PLANNED_PARTIAL] (G5) PO flow: GRN
- gap: 1. For any order with QC lines, GRN_COMPLETED is unreachable, because it requires QC_COMPLETED first.
2. No GRN detail or cancel screens (P07.M6).
3. No UI action takes GRN_COMPLETED->CLOSED.
- planned: C1a's derived status shows GRN_COMPLETED on the board and portal once every received line is decided. B4 adds a GRN_NOT_CREATED alert. Not covered: the PO status itself, the GRN screens and close.
- rec: Use the same write-back as P07.M7, plus the GRN detail page (P07.M6) and the Close action (P07.M5).

### A04.13 [PLANNED_PARTIAL] (G5) PO flow: vendor invoice
- gap: 1. Nothing moves the PO to INVOICED: no code writes PurchaseOrderStatus.INVOICED.
2. Vendors cannot submit an invoice.
3. The vendor-invoice list is unscoped for vendor users (trap T1).
- planned: - C1a: POST /vendor-portal/invoices, with the vendor forced and the PO required to be its own.
- C2a: 'New invoice against PO' sheet.
- B4: VENDOR_INVOICE_PENDING alert.
- B3a: cancel reversal.
Not covered: the PO INVOICED transition.
- rec: Small-medium.
- Add procurement.service.sync_order_billing_status(order_id). Finance is downstream, so it may call this on invoice approve and cancel.
- Move GRN_COMPLETED->INVOICED once approved, non-cancelled invoices cover the accepted quantity.
- Add a reverse edge INVOICED->GRN_COMPLETED for cancellations.
- Show linked invoices on the PO detail.

### A04.14 [PLANNED_PARTIAL] (G5) PO flow: payment
- gap: 1. Nothing moves the PO to PAID.
2. No vendor-side payment view.
3. The PO detail does not trace to its payments.
- planned: - C1a: GET /vendor-portal/payments and /payments/summary.
- C2a: /vendor/payments.
- B4: VENDOR_PAYMENT_OVERDUE internal alert.
Not covered: the PO PAID transition.
- rec: Small.
- From allocate_payment and allocation reversal or payment cancel in finance, call the A04.13 sync: INVOICED->PAID when every non-cancelled invoice of the PO is PAID.
- Add a PAID->INVOICED edge for reversed allocations.
- Add an invoices and payments panel to the PO detail.

### P08.M2 [PARTIAL] (G6) Production planning
- gap: No planning board/calendar/Gantt of orders or operations; scheduling is infinite-capacity, back-to-back, 24/7 (ASSUMPTION service.py:613-618) and ignores work_centers.capacity_per_hour (stored, unused) and working_hours_per_day; no capacity/load-vs-capacity check per day or week; no master production schedule; MRP JOB_WORK suggestions cannot be converted (job_work/service.py:2502-2520 refuses) and MRP has no assembly-order target and does not net open assembly orders (no 'assembly' in modules/mrp).
- planned: none for planning itself (B2-shifts only adds rostered capacity columns to the machine-utilisation report and explicitly leaves scheduling and MRP lead times unchanged)
- rec: Large: new /production/planning board showing work orders per work centre/machine on a day/week timeline (planned_start_at/planned_end_at) with drag-to-reschedule via PATCH /production/work-orders/{id}; new GET /production/capacity?date_from&date_to&work_center_id bucketing remaining planned minutes per day against working_hours_per_day (and B2 roster minutes once built) with overload flags; shift/working-hours-aware plan_work_orders behind a named constant (after B2); ConversionTarget.ASSEMBLY_ORDER plus netting of open assembly orders in MRP.

### P08.M6 [PLANNED_PARTIAL] (G6) Machine allocation
- gap: _assert_machine checks only tenant + work centre: RETIRED/inactive/BREAKDOWN/MAINTENANCE machines can be assigned (routing/repository.py:59 WORKABLE_MACHINE_STATUSES is defined but never used); no overlap/double-booking check of one machine across work orders; no machine schedule view; queue has no machine column.
- planned: B2-shifts: roster with one-operator-per-machine EXCLUDE constraint, _assert_machine refuses RETIRED/inactive, rostered capacity on utilisation. Still missing after B2: work-order-level machine overlap/capacity check and a machine-wise schedule view.
- rec: Medium (after B2): on work-order assignment and at release compute overlapping planned windows for the same machine (one query on ix_work_orders_machine) and return a warning or BusinessRuleError naming every conflict; warn on MAINTENANCE/BREAKDOWN via WORKABLE_MACHINE_STATUSES; add a machine x day timeline tab on /production/work-orders.

### P08.M9 [PLANNED_PARTIAL] (G6) Daily production entry
- gap: Bookings only per order: no company-wide entries endpoint or daily board; UI never sends work_order_id (payload production-dialogs.tsx:1179-1191) so per-operation bookings are impossible; form refuses downtime-only bookings (L1110-1119) though API accepts; shift 32 vs 16 chars; operator list hidden without user.view (L1161); Alert L1374-1379 and empty state L595 wrongly say the first booking moves the order to IN_PROCESS (only Start does); batch/serial-tracked finished goods cannot be booked (service.py:3013-3018).
- planned: B2-shifts: shift select, operator directory, optional work-order select, downtime-only allowed, text fixes, SHIFT_PRODUCTION 'Daily production by shift' report. Still missing after B2: company-wide entries endpoint/screen for multi-order daily entry, tracked-FG output capture.
- rec: Small on top of B2: GET /production/entries (literal route before /{order_id}; ProductionEntryFilters/entry_filters router.py:167-188 already exist) and a 'Daily bookings' tab (date x shift across all open orders, inline Record per order). Tracked-FG capture under P08.M11.

### P08.M10 [PARTIAL] (G6) WIP tracking
- gap: GET /production/wip (WIP by order, lateness) has no screen - only types/openapi.ts references it; late_only is ignored by the work-centre board (documented wip-board-panel.tsx:27-29); no WIP valuation (material issued and operation cost not yet converted) anywhere.
- planned: none
- rec: Small-medium: 'WIP by order' tab on /production/work-orders (or /production) using GET /production/wip with plant/warehouse/item/late-only filters, days-late badge and open-operations count; add a WIP value column (net issued material cost + operation cost to date) from one batched aggregate; honour late_only in load_by_work_centre or drop the parameter.

### P08.M11 [PARTIAL] (G6) Production completion
- gap: UI posts {} for complete and close (production-detail-page.tsx:809), so final produced/rejected/scrap quantities, actual end date, completion note and closure note cannot be entered (scrap_quantity is never recorded from the UI); batch- or serial-tracked finished goods cannot be received at all (_post_output_receipt service.py:3004-3018) - demo_data/operations.py:1335-1344 states the serialised MAKE item DL-900 cannot be produced.
- planned: none
- rec: Small: Complete dialog capturing produced/rejected(+reason)/scrap, actual end date and note (CompleteRequest schemas.py:243-276) and a Close dialog with closure_note. Medium-large: tracked FG output - capture/create a batch or serial numbers per booking (child table or array on production_entries), pass them to post_movements, update the ASSUMPTION in docs/assumptions.md.

### P08.M12 [PARTIAL] (G6) Production costing
- gap: The per-component and per-operation breakdown returned by the API is not rendered; no standard-vs-actual variance (FG received at standard cost, service.py:3004-3009, never revalued); job-work/outsourced charges (routing_operations.is_outsourced routing/models.py:384-388, job_work_orders source PRODUCTION_ORDER) are not rolled into the order cost; no overhead absorption; no cross-order production cost report; nothing posted to finance.
- planned: none (B1-cost-centres only tags production_orders.cost_centre_id; B3a posts job-work expense to finance, not to the order cost)
- rec: Small: render materials[] and operations[] tables in the Costing tab. Medium: snapshot standard cost at release, add variance fields to ProductionCostRead and a PRODUCTION_COST report key (catalogue entry, CHECK-swap migration, /reports/production-cost, frontend union) listing closed orders actual vs standard per unit. Medium: add a subcontract-cost component from job-work orders sourced from the production order. Overhead absorption/FG revaluation only after a client costing policy (record as ASSUMPTION).

### P09.M1 [PARTIAL] (G6) Assembly order
- gap: The frontend calls only GET list/detail/components, POST issue-components and POST complete (assembly-detail-dialog.tsx:166, 575, 765, 772). There is no 'New assembly order' button and no Edit/Delete draft, Approve, Release, Start, Close or Cancel action anywhere, so in a live tenant an assembly order cannot be created or advanced from the UI (only demo orders seeded through the service, core/demo_data/operations.py:1216-1332, reach later states). MRP neither suggests nor nets assembly orders.
- planned: none
- rec: Medium: 'New assembly order' dialog (item, approved ASSEMBLY BOM select, planned qty, component + output warehouse, assembly line, planned start/end, requires-QC) posting to POST /assembly; Edit/Delete draft, Approve, Release (show shortage details), Start, Close, Cancel (reason) buttons in AssemblyDetailDialog gated on assembly_order.manage; optionally ConversionTarget.ASSEMBLY_ORDER and netting of OPEN_SUPPLY_STATUSES assembly orders in MRP. Add a frontend test per action.

### P09.M3 [PARTIAL] (G6) Component issue
- gap: Only works from RELEASED, and nothing in the UI can release an order (P09.M1). The UI also offers it on COMPONENTS_ISSUED and IN_PROGRESS (assembly-model.ts:77-78), but the state machine has no COMPONENTS_ISSUED->COMPONENTS_ISSUED or IN_PROGRESS->COMPONENTS_ISSUED edge (service.py:205-211; assert L1213-1215), so every top-up issue is refused; no kitting/issue slip print.
- planned: none
- rec: Small: allow top-ups in issue_components (skip the transition when already COMPONENTS_ISSUED/IN_PROGRESS, still audit) or restrict acceptsComponentIssue to RELEASED; printable kitting list; depends on the Release action from P09.M1.

### P09.M4 [PARTIAL] (G6) Assembly tracking
- gap: No Start action in the UI (POST /assembly/{id}/start router.py:304 unused), so actual_start is never stamped from the screen and IN_PROGRESS is unreachable; assembly line is free text with no line master or line board; completion is one-shot (assembled_quantity written once, models.py:176-179) so progress during a run cannot be booked; no assembly WIP/progress board or lateness indicator; no operator/station output.
- planned: none
- rec: Medium: Start button; 'Assembly board' grouping open orders by assembly_line with planned vs actual and lateness (GET /assembly?open_only=true); incremental output bookings while IN_PROGRESS (assembly_entries table or repeatable partial completion); optional assembly-line master.

### P09.M5 [PARTIAL] (G6) Assembly QC
- gap: POST /assembly/{id}/qc-result has no screen. Deciding the inspection on /quality moves the held stock (quality/service.py:1735-1930) but never updates the assembly order, which stays QC_PENDING forever: it cannot be closed (only COMPLETED->CLOSED) or handed to packing (mark_packed requires COMPLETED, service.py:1760-1764); calling qc-result afterwards would move the QC_HOLD stock a second time. requires_qc cannot be set from the UI (no create dialog).
- planned: none
- rec: Medium: one authoritative verdict path - when quality decides an inspection with reference_type assembly_order, call an assembly seam that applies accepted/rejected and completes the order without re-posting movements (or have qc-result carry qc_inspection_id and let quality skip movements for module-held lots); add 'Record inspection verdict' in AssemblyDetailDialog (qc.decide) and a requires-QC switch on create; regression test that the order reaches COMPLETED and stock moves exactly once.

### P09.M6 [PARTIAL] (G6) Assembly completion
- gap: Complete is shown only on IN_PROGRESS (assembly-model.ts:94) and the UI has no Start action, so completion is unreachable from the screen for any new order; batch-tracked components/outputs and serial-tracked outputs are refused at creation (_assert_trackable service.py:708-727), so a serialised lock set cannot be assembled; one completion per order (no partial).
- planned: none
- rec: Small: Start action (P09.M4). Medium: serial-tracked output (capture/mint serials for assembled units, one ASSEMBLY_RECEIPT per serial) and batch-tracked items (component batch selection, output batch creation); optional partial completions.

### P09.M7 [PARTIAL] (G6) Packing handover
- gap: Neither endpoint is called by any screen; the packing module never references assembly (packing/models.py mentions it only in docstrings; packing orders are keyed to sales order/customer, L157-226) and never calls mark_packed, so completed assemblies are never offered to packers and the handover is never recorded.
- planned: none
- rec: Medium: 'Ready from assembly' panel on /packing (GET /assembly/ready-for-packing); create a packing order from an assembly (prefilled line, customer/sales order from the assembly source) and call assembly.mark_packed through a port in the same transaction; show the packing order on the assembly detail; e2e test.

### P09.EX [PARTIAL] (G6) Lock set assembly consumes Handle, Lock body, Cylinder, Keys, Screw kit and Packing box; system consumes components and creates finished assembly stock
- gap: Not usable end to end from the UI (no create/approve/release/start - P09.M1/M4); a serial-tracked finished lock set cannot be an assembly output (service.py:721-727) and batch-tracked components are refused.
- planned: none
- rec: Covered by P09.M1, M4, M6 recommendations; add an e2e journey: create lock-set assembly -> approve -> release -> issue -> start -> complete, asserting component ledger rows and FG balance.

### P10.M3 [PARTIAL] (G6) Job work challan
- gap: No screen lists or shows challans (GET /{id}/challans unused by the frontend); no challan print/PDF (only packing has a PDF generator); no challan register or ITC-04 export.
- planned: none
- rec: Medium: Challans section in the job-work order detail and a company-wide register (GET /job-work/challans, paginated, vendor/date filters); GST rule-55 delivery challan PDF (consignor/consignee GSTIN, HSN, qty, taxable value, vehicle, transporter, 'for job work') via the existing reports/documents PDF worker with a Download button; optional ITC-04 report key.

### P10.M4 [PLANNED_PARTIAL] (G6) Vendor process tracking
- gap: NotificationType.JOB_WORK_OVERDUE is declared (notifications/models.py:112) but never raised; no per-order tracking timeline (challans/receipts/reconciliation APIs have no screen); no vendor-side progress updates (vendor portal C1a is PO-only).
- planned: B4-smart-alerts-events (JOB_WORK_OVERDUE sweep, 'job work material sent/received' events). Still missing: per-order timeline/detail and vendor-reported process status.
- rec: Medium: job-work order detail (P10.M1) with a timeline (approved -> challans -> receipts -> QC -> closed) and the reconciliation table; later optionally extend the vendor portal to job-work orders (acknowledge, in process, dispatched back).

### P10.M6 [PARTIAL] (G6) Short / excess receipt
- gap: JobWorkReceiveDialog never sends is_final or approve_excess_loss (payload job-work-dialogs.tsx:434-455), so a short return cannot be closed from the screen: the order stays PARTIALLY_RECEIVED and can never be closed (close needs RECEIVED/COMPLETED, service.py:2453-2484); excess-loss approval and the vendor debit are API-only.
- planned: none (B3a only posts the already-accrued excess-loss debit as a recovery journal at invoice approval)
- rec: Small: per line 'Close line - write off shortfall' (is_final) showing computed loss vs allowance, and an 'Approve excess loss (debit vendor)' checkbox visible to job_work.close holders; show the 2% over-receipt tolerance; frontend test on the request body.

### P10.M7 [PARTIAL] (G6) Job work QC
- gap: Two disconnected verdict paths: deciding the inspection on /quality moves the held stock (quality/service.py:1735-1930) but leaves the job-work order QC_PENDING and the receipt unreleased; the job-work verdict neither decides nor closes the quality inspection; doing both moves the stock twice. Held output can be released only when the whole order is back (PARTIALLY_RECEIVED has no COMPLETED edge). A multi-output receipt raises one inspection naming only the first output item (service.py:2155-2174).
- planned: none
- rec: Medium: make the quality decision authoritative for job-work receipts (quality calls a job_work seam that marks receipts released and completes the order when nothing remains held, without a second movement) or have the job-work verdict pass and close qc_inspection_id; allow per-receipt release while PARTIALLY_RECEIVED; one inspection per output item; regression test 'decide on /quality, order completes, stock moved once'.

### P10.M8 [PARTIAL] (G6) Rejection / rework
- gap: Receive dialog captures a rejected quantity but never sends rejection_action (always RETURN_TO_STOCK), so rework-at-vendor and scrap-at-vendor cannot be recorded; its Alert wrongly says the API decides scrap/debit from the order (job-work-dialogs.tsx:620-627); no rejection reason; no 'raise rework order' (form hard-codes source_type MANUAL, job-work-order-form-dialog.tsx:295); no vendor debit for rejected output.
- planned: none
- rec: Medium: per-line rejection action select + reason in JobWorkReceiveDialog (fix the Alert); 'Send for rework' from a rejected receipt line or DAMAGED stock creating a JW order with source_type REWORK and source_id = receipt; if the client wants charge-backs for rejected output, a vendor debit (ASSUMPTION) posted through B3a's recovery mechanism.

### P10.M10 [PLANNED_PARTIAL] (G6) Job work vendor ledger
- gap: No job-work vendor ledger screen: no per-vendor, per-period statement of opening / issued (challans) / received (output, returned, scrap) / closing material, and no charges accrued vs billed vs debited view; job-work charges reach the AP ledger only via override-entered invoices.
- planned: B3a-job-work-invoice (JOB_WORK invoices and excess-loss recoveries post to AP with the vendor as party; invoiced_amount per receipt line). Still missing: the per-vendor material ledger and a charges/billing-status screen.
- rec: Medium: GET /job-work/vendor-ledger?vendor_id&date_from&date_to (rows from challans and receipts with running at-vendor balance per item; charges/billed/debit columns once B3a lands) and a 'Vendor ledger' tab on /job-work with vendor picker, period and CSV export; surface GET /job-work/charges there.

### P11.T2 [PARTIAL] (G7) In-process QC
- gap: Nothing raises an in-process inspection: there is no requires_qc on production or work orders and no QC hold on production output. No screen raises an inspection by hand either: quality-page.tsx has no 'Raise inspection' action and the handbook confirms 'Inspections cannot be raised by hand from this screen'.
- planned: none
- rec: Medium. Add requires_qc (optional template) to production orders or work-order operations. Have the production entry post good output to QC_HOLD and call quality.create_in_process_inspection(reference_type='production_entry'), with the order completing on the verdict. Add a 'Raise inspection' dialog on /quality (item, warehouse, batch, stage, qty, hold_stock, reason) gated P.QC_INSPECT that calls POST /quality/inspections.

### P11.T3 [PARTIAL] (G7) Job work QC
- gap: The two release paths are not reconciled. Recording the verdict on the job-work screen moves QC_HOLD->AVAILABLE/DAMAGED and completes the order, but leaves the Quality inspection PENDING in /quality indefinitely. If that inspection is later decided, it posts QC_HOLD->AVAILABLE a second time: the post is refused, or it silently releases another lot held at the same item/warehouse/batch grain. Deciding in Quality first never completes the job-work order, because quality has no owner callback, and the job-work verdict then fails for lack of held stock. The job-work verdict also skips the check sheet and offers no rework, hold or scrap.
- planned: none (B3a covers the job-work invoice only)
- rec: Medium. Make the QC inspection the single release path. Add an owner-callback port in quality.decide_inspection, keyed on reference_type, that marks the job-work receipt released and moves the order to COMPLETED. Make /job-work/{id}/qc-result either decide the linked inspection or refuse when one exists. Have the job-work QC action open the inspection dialog.

### P11.T4 [PARTIAL] (G7) Assembly QC
- gap: No screen calls /assembly/{id}/qc-result: assembly-detail-dialog.tsx L966-979 only links 'Inspection raised' to /quality, and the handbook notes that no verdict can be recorded there. Deciding in Quality releases the stock but never moves the assembly order out of QC_PENDING. The order is stranded (there is no QC_PENDING->CLOSED edge) and never appears in GET /assembly/ready-for-packing. Calling qc-result afterwards would try to release QC_HOLD a second time.
- planned: none
- rec: Small to medium. Use the same owner callback as T3: the quality decision completes the assembly order with its accepted and rejected tallies. Guard or remove the duplicate qc-result release, and show the verdict on the assembly detail.

### P11.T5 [PARTIAL] (G7) Final QC
- gap: Nothing raises a FINAL inspection. Packing never calls quality (no quality reference in C:/Project/Other/ATCRAFT/apps/api/src/modules/packing/*.py). Dispatch only has a QC_PENDING status word. Production output goes straight to AVAILABLE. No gate requires final QC before packing or dispatch, and the manual raise has no UI.
- planned: none
- rec: Medium. Add a final-inspection step for finished goods flagged 'final QC required' at item or category level. Either hold assembly or production output and raise a FINAL inspection, or have packing start or complete raise a FINAL inspection per line (reference_type 'packing_order') and refuse complete/hand-over until it is decided. Add the manual 'Raise inspection' dialog (see T2).

### P11.F3 [PARTIAL] (G7) Photo upload
- gap: A photo can only be attached to a template parameter: an inspection without a template, or an inspection-level defect photo, cannot carry one. The dialog shows only the text 'Photo attached', with no thumbnail or download. Decided inspections cannot be opened at all, because Inspect is offered only for PENDING/IN_PROGRESS/REWORK/HOLD (quality-page.tsx L441-447). Photos are therefore reachable only through the generic Documents page.
- planned: none
- rec: Small. Add a read-only inspection detail for any status (row click) showing readings, rejection reason and photo thumbnails, with download via /documents/{id}/download. Allow inspection-level attachments, including on inspections without a template.

### P11.F5 [PARTIAL] (G7) Rework tracking
- gap: No rework work list in the UI: rework_only is not exposed and there is no status filter. Nothing records the rework itself (who, where, operation, cost, time). There is no rework-cycle history: each re-decision overwrites the five quantities and only the audit log keeps earlier values. Reworked stock stays in QC_HOLD with no way to issue it to a rework bench and receive it back, and there is no rework rate or yield report.
- planned: none
- rec: Medium. Add a 'Rework' tab on /quality using rework_only. Add a qc_rework_records table (inspection_id, qty, work centre or assignee, started/completed, outcome, cost, notes), or a production rework order type that issues QC_HOLD stock and returns it for re-inspection. Report rework rate per item and vendor.

### P12.M1 [PARTIAL] (G7) Packing order
- gap: No screen and no automatic path creates a packing order. There is no 'New' button, and sales and allocation never call packing; only demo data does (C:/Project/Other/ATCRAFT/apps/api/src/core/demo_data/operations.py L2413). The empty state says 'Packing orders are created from allocated sales orders' (packing-page.tsx L313-317), which is false. The assembly->packing hand-off (GET /assembly/ready-for-packing, POST /assembly/{id}/pack) has no UI.
- planned: none
- rec: Small to medium. Add a 'New packing order' dialog on /packing (customer, warehouse, planned date, optional SO that pre-fills open allocated lines with sales_order_line_id). Add a 'Create packing order' action on the SO detail for ALLOCATED/PARTIALLY_DISPATCHED orders, and optionally auto-create on full allocation behind a company setting. Wire ready-for-packing->pack and fix the empty-state copy.

### P12.M3 [PARTIAL] (G7) Box / label / barcode generation
- gap: No label or barcode artwork is produced anywhere. No screen calls the label endpoint (no '/label' in apps/web/src). The UI shows the barcode only as text (packing-order-dialog.tsx L754), and the packing-list PDF prints it as text (C:/Project/Other/ATCRAFT/apps/api/src/workers/tasks/reports.py L218). There is no PDF or ZPL label, no print button, no barcode scan or lookup, and no unit or product labels.
- planned: none
- rec: Medium. Render carton labels as 100x150 mm PDFs with reportlab Code128 (reportlab is already a dependency), with optional ZPL. Add 'Print label' and 'Print all labels' in the carton builder, draw real barcodes on the packing list, and add GET /packing/cartons/by-barcode/{code} for scanning. Unit labels from item barcodes are optional.

### P12.M5 [PARTIAL] (G7) Carton tracking
- gap: Tracking stops at the packing bench. The serial trace has no UI. The dispatch UI always sends carton_ids: [] (C:/Project/Other/ATCRAFT/apps/web/src/features/dispatch/dispatch-dialogs.tsx L175) and never sends packing_order_id, and hand-over sets only packing_orders.dispatch_id (packing/service.py hand_over_to_dispatch L1892). As a result no consignment records which cartons it carried. There is no carton barcode lookup or scan, no carton status (loaded, delivered, damaged), and dispatch box count and weight are typed by hand rather than taken from the cartons.
- planned: none
- rec: Medium. On hand-over or dispatch create, set dispatches.packing_order_id, fill dispatch_lines.carton_ids from the sealed cartons, and copy carton count and gross weight into total_boxes and total_weight_kg. Add a 'Find carton' (barcode or serial) screen and an optional loading-scan step.

### P12.M6 [PARTIAL] (G7) Dispatch planning
- gap: There is no planning view of what is due to ship (allocated or packed SO lines not yet on a consignment). The UI never sends sales_order_line_id (dispatch-dialogs.tsx L169-176), so dispatch_consignment skips allocation.consume and the SO line arithmetic (dispatch/service.py L1446-1449, L1550-1553). The reservation stays and stock leaves from free stock; the dispatch is refused if only reserved stock exists. SO lines' dispatched_quantity never moves while the SO is still walked to PARTIALLY_DISPATCHED. The create form has no packing-order picker. PATCH /dispatch/{id} (router.py L224) has no UI, so the plan cannot be amended. There is no vehicle or load planning.
- planned: none
- rec: Medium. First fix the SO-line linkage (small, critical): build the line editor from the SO's open lines with sales_order_line_id. Then add a dispatch-planning board (allocated or packed but undispatched lines by customer and date) with 'Create consignment' pre-filled with SO lines, packing_order_id and cartons. Add an Edit dispatch dialog (PATCH) for NOT_DISPATCHED/READY.

### P12.M8 [PARTIAL] (G7) LR details
- gap: The LR can only be typed at creation. The gate-time LR capture (DispatchRequest.lr_number, schemas.py L220-223) is unused by the UI and there is no edit. There is no LR date. There is no LR-copy upload: the only document link is invoice_document_id, and no UI sets it.
- planned: none for outbound (C1a covers LR and LR copy for inbound vendor shipments only)
- rec: Small. Add lr_date and typed dispatch attachments (LR_COPY, DISPATCH_PHOTO, POD), reusing C1a's document_kind with entity_type='dispatch'. Add LR, vehicle and tracking fields to the Dispatch confirmation and an edit dialog.

### A06.13 [PLANNED_PARTIAL] (G7) Attachment / photo
- gap: Neither direction has an integrated attachment on main.
- planned: C1a (POST /vendor-portal/purchase-orders/{id}/documents, kind INVOICE/DISPATCH_PHOTO/LR_COPY/DELIVERY_CHALLAN/OTHER, optional shipment_id) + C2a (upload with camera capture); A2 sets invoice_document_id on outbound. Still missing from the plan: outbound LR copy, dispatch photo and POD attachments, a
- rec: Small. Reuse C1a's document_kind for entity_type='dispatch' with an upload panel (LR_COPY, DISPATCH_PHOTO, POD) on the dispatch timeline dialog. Add a documents list (kind, download) to C2b's shipments panel and inbound board.

### A06.EX [PLANNED_PARTIAL] (G7) Admin notified immediately on dispatch (example message incl. LR copy uploaded)
- gap: Nothing notifies anyone immediately in either direction.
- planned: C1a: shipment creation notifies the PO creator and PURCHASE_MANAGER users immediately with courier, tracking ID, dispatch date, expected date, boxes and weight. B4: outbound 'Dispatch created' and 'Courier tracking updated' go to the order's assigned sales user. C1b/C2b add push. Still missing: ADMI
- rec: Small. Make C1a's shipment-notification recipients configurable by permission, including ADMIN. Raise a grouped notification when an LR_COPY or DISPATCH_PHOTO is uploaded against a shipment (same group_key), so the admin sees 'LR copy uploaded'.

### P13.M1 [PLANNED_PARTIAL] (G8) Sales ledger
- gap: There is no sales document, no revenue posting, no sales register, and no account-level ledger view for a Sales account.
- planned: A2-sales-invoice: GST invoice per dispatch posting Dr AR / Cr SALES_REVENUE / Cr OUTPUT_TAX, plus the /finance/sales-invoices register page with date, customer and status filters. Still missing after A2: an account statement to open the Sales (or any) GL account, and register totals by period, custo
- rec: Small. Add GET /finance/ledger/account/{account_id} (ledger.view), reusing statement_stmt with an account predicate on ix_ledger_entries_account_date and an opening balance, plus a 'Ledger' drill-down on accounts-page.tsx. Add period totals (taxable/tax/total) to the A2 invoice list, or a SALES_REGISTER report grouped by month x customer/HSN.

### P13.M2 [PLANNED_PARTIAL] (G8) Purchase ledger
- gap: Debit notes for purchase returns are not posted. Invoices cancelled after a dispute leave their journal in the ledger. There is no GL account statement for the Purchases account.
- planned: B3a-job-work-invoice fixes the cancel-without-reversal defect and routes job-work bills to JOB_WORK_EXPENSE. B1/B3b add a cost-centre split. Not covered by any WP: purchase-return debit-note posting, and an account-level ledger view.
- rec: Medium. On purchase-return post, issue a vendor debit note (own DN series) posting Dr AP (vendor) / Cr PURCHASE_EXPENSE (or a new PURCHASE_RETURNS system account) / Cr INPUT_TAX for the returned lines' GST, linked to the original vendor-invoice line and allocatable against open invoices. Small: the GL account statement shared with P13.M1.

### P13.M3 [PLANNED_PARTIAL] (G8) Vendor ledger
- gap: Debit notes and job-work recovery debits are absent from the vendor ledger, and the cancel defect leaves stale AP credits.
- planned: B3a-job-work-invoice posts job-work recovery journals (Dr AP vendor) and fixes the cancel reversal. C1a's vendor allow-list gate blocks vendor users from /finance. Not covered: purchase-return debit notes.
- rec: Medium. Implement purchase-return debit-note posting as in P13.M2. With B3a, the vendor ledger then carries every vendor-side document.

### P13.M5 [PLANNED_PARTIAL] (G8) Job work cost
- gap: There is no job-work invoice or expense account, no billed-vs-accrued tracking, and no screen for the job-work cost summary. Processing cost is never capitalised into the output stock value.
- planned: B3a-job-work-invoice: typed JOB_WORK vendor invoice matched to receipt lines, JOB_WORK_EXPENSE account, invoiced_amount and billed-vs-unbilled tracking, excess-loss recovery journal, billable-lines endpoint, and an Invoices tab. B1 adds job_work_orders.cost_centre_id. Not covered: capitalising job-w
- rec: Medium. On receipt, post JOB_WORK_RECEIPT with unit_cost = (FIFO value of the input consumed + receipt charge) / good output quantity. Alternatively, revalue on invoice approval. Add a JOB_WORK_COST report (vendor, process, order, input cost, charges, debits, cost per unit) and a Costs tab over /job-work/charges.

### P13.M6 [PARTIAL] (G8) Production cost
- gap: Actual cost never flows into inventory value or the GL. There is no standard-vs-actual variance and no production-cost report by item, period or work centre.
- planned: none (B1 only tags production_orders.cost_centre_id. Its COST_CENTRE_EXPENSE report reads ledger_entries, which production never writes.)
- rec: Medium to large. At close, revalue the job's receipts still on hand to actual cost, or post an actual-cost unit_cost / variance movement. Add a PRODUCTION_COST report (order, item, quantity, material, operation, total, per unit, standard vs actual variance; filters for period, work centre and cost centre). Add WIP/variance journals only if inventory accounts are introduced.

### P13.M9 [PLANNED_PARTIAL] (G8) GST report
- gap: The report is not invoice-based. HSN codes are not snapshotted. Credit notes and purchase-return debit notes are not netted. There are no GSTR-1/3B-style sections or export.
- planned: A2-sales-invoice: output from issued invoice lines with the HSN snapshot, a dispatched-not-invoiced figure, and OUTPUT_TAX posted. A3-returns-credit-notes: credit notes subtracted. Not covered: input-tax reversal for purchase returns/debit notes, and a GSTR-1 invoice-wise listing with export.
- rec: Medium, after A2 and A3. Deduct vendor debit notes from input tax. Add GSTR-1 sections (B2B invoice-wise with buyer GSTIN/POS, B2C summary, CDNR from credit notes, HSN summary) and a GSTR-3B summary, exported as XLSX through the reports job pipeline.

### P14.11 [PARTIAL] (G8) Product profitability
- gap: Revenue and cost are both estimates (booked orders, current standard cost), so the report is not actual profitability. The cost columns also bypass item.cost_view.
- planned: none
- rec: Medium. Take COGS from DISPATCH inventory_transactions (FIFO value), net of customer returns. Take revenue from dispatched lines now, and from A2 invoice lines (net of A3 credit notes) once they exist. Gate the report or its cost columns on item.cost_view.

### P14.16 [PLANNED_PARTIAL] (G8) Machine utilization
- gap: Idle machines are omitted, and capacity ignores shifts and holidays.
- planned: B2-shifts adds rostered_minutes and rostered_utilisation_percent. It does not make the query machine-driven, so idle machines still vanish.
- rec: Small. Build the query from active, non-retired machines LEFT JOINed to the busy-minutes and downtime subqueries, so idle machines show 0%. Keep B2's rostered capacity alongside.

### P14.19 [PLANNED_PARTIAL] (G8) Dashboards (management-level visibility)
- gap: The dashboard is operational only. It has no financial or trend view, and the PO tile is mislabelled.
- planned: A4-collections-dealer-performance adds receivables_outstanding, receivables_overdue and follow_ups_due tiles. Nothing else is planned, and the mislabel is not addressed.
- rec: Medium: add payables due/overdue, invoiced sales and collections month-to-date, MRP shortage count, job-work overdue, and inbound POs late (after C1a), plus a 30-day trend endpoint built from summary tables. Small: relabel the tile 'POs awaiting delivery', or count PENDING_APPROVAL instead.

### A01.A7 [PLANNED_PARTIAL] (G9) Admin notification: QC rejected
- gap: There is no link, and the vendor is not told. After the planned WPs: C1a adds the vendor-facing notification, and B4 keeps QC rejected as an event. B4 §5 fixes only the three broken links (dispatch, allocation, report-ready), and its action_url guard checks only URLs that exist. The internal QC_FAILED therefore stays without a link.
- planned: C1a (vendor users with vendor_portal.quality); B4 (keeps the event)
- rec: Small. Add an 'inspection' query param to quality-page.tsx that opens that inspection on the 'inspections' tab. Pass action_url=f'/quality?tab=inspections&inspection={id}' in both notify_role calls in _announce_failure. Make B4's test_notification_action_urls.py also fail when a notify call sets entity_id but no action_url.

### A01.A13 [PLANNED_PARTIAL] (G9) Admin notification: Dispatch created
- gap: Nothing is raised on main. B4 notifies only the order's assigned sales user. customers.assigned_sales_user_id is optional (customers/schemas.py:87), and JOB_WORK, PURCHASE_RETURN and TRANSFER dispatches (dispatch/models.py:88-102) have no sales order. In either case nobody is notified.
- planned: B4 §4 (-> order's assigned sales user); A5 (dealer-facing 'Order dispatched')
- rec: Small, in B4. When there is no sales user, fall back to dispatch roles (precedent DELAY_ALERT_ROLES, dispatch/service.py:163) or to user_ids_with_permission(DISPATCH_VIEW). For JOB_WORK dispatches, notify the job-work order creator; for PURCHASE_RETURN, the purchase roles. Link /dispatch?tab=tracking.

### A01.A14 [PLANNED_PARTIAL] (G9) Admin notification: Courier tracking updated
- gap: After the WPs: B4 sends outbound tracking events only to the order's assigned sales user, with no fallback. C1a notifies the buyer only when a vendor shipment is created. Later inbound updates (delivered marking, and D3 courier-API events on purchase_order_shipments) notify nobody under any spec.
- planned: B4 §4 (outbound tracking event -> assigned sales user, grouped per dispatch); C1a (shipment recorded -> PO creator + PURCHASE_MANAGER); D3 (courier-API updates raise B4's event)
- rec: Small. Use the same recipient fallback as A01.A13. Raise a 'tracking updated' notification, grouped per shipment, to the PO creator and purchase roles whenever an inbound shipment is edited or marked delivered, whether by staff, the vendor or D3.

### A01.M8 [PLANNED_PARTIAL] (G9) Mobile notification: Job work pending
- gap: After the WPs: B4 raises JOB_WORK_OVERDUE and 'challan issued' internally only. C1a's portal has no job-work endpoints, and its allow-list gate removes vendors' job-work access. A job-work vendor (polishing, coating, PVD and so on) therefore never sees pending job work and is never told about it.
- planned: B4 (internal JOB_WORK_OVERDUE and challan-issued event); C1b/C2b push
- rec: Medium. Add vendor_portal job-work endpoints: GET /vendor-portal/job-work and /job-work/{id}, returning own orders, challans, issued/received/pending quantity and expected_return_date. Notify the job-work vendor's users (user_ids_for_vendor) when a challan is issued and when it goes overdue (the B4 rule with vendor recipients). Add a 'Job work' list to the C2a vendor app.

### A01.M10 [PLANNED_PARTIAL] (G9) Mobile notification: Approval request
- gap: B4 notifies approvers only for requisitions, POs, reallocation requests and payments. BOM, vendor invoice, stock adjustment, production order, below-cost sales order, vendor master and job-work order approvals stay silent, and the dashboard approvals queue stays empty.
- planned: B4 (PR, PO, reallocation and payment approvals); C1b/C2b push
- rec: Medium. Add one helper, notify_approval_pending(entity, approve_permission), and call it from every submit-for-approval transition: APPROVAL_PENDING to user_ids_with_permission(right), then APPROVAL_GRANTED or APPROVAL_REJECTED to the submitter on decision. Either write an approval_requests row at submit and close it at decision, or repoint the dashboard panel at a union of module PENDING_APPROVAL rows.

### A08.6 [PLANNED_PARTIAL] (G9) Smart alert: QC rejected
- gap: C1a adds the vendor side. No WP adds a link to the internal QC_FAILED; B4 §5 fixes only three other links.
- planned: C1a (vendor QC-rejection notification); B4 (keeps the event)
- rec: Small. Same fix as A01.A7: an inspection deep-link param on /quality, plus action_url in _announce_failure.

### A08.8 [PLANNED_PARTIAL] (G9) Smart alert: Vendor invoice pending
- gap: B4's rule fires only when the PO is fully received with no non-cancelled invoice linked. A partially received PO (lot-wise deliveries) never alerts, and one invoice for the first lot silences the alert for every later lot.
- planned: B4 VENDOR_INVOICE_PENDING (internal vendor_invoice.create holders + vendor users with vendor_portal.invoice)
- rec: Small. Detect per posted GRN: goods_receipts.posted_at older than the threshold with no non-cancelled vendor_invoice whose goods_receipt_id is that GRN (or whose matched lines cover it), grouped per PO.

### A10.M1 [PLANNED_PARTIAL] (G9) Must have: Admin notification
- gap: B4 and C1a cover almost the whole admin list. Still open after them: the QC_FAILED link (A01.A7), recipient fallback for dispatch created and courier tracking (A01.A13/A14), inbound tracking-update notifications, and approval requests beyond PR, PO, reallocation and payment (A01.M10).
- planned: B4 (events, alerts, link fixes, guard tests); C1a (vendor-action events to buyers)
- rec: Close the small gaps listed under A01.A7, A01.A13, A01.A14 and A01.M10 inside B4. Small to medium.

### A10.F4 [PLANNED_PARTIAL] (G9) Future (now wanted): Vendor rating
- gap: A vendor cannot see its own rating; C1a's GET /vendor-portal/rating and C2a's card fix that. Still uncovered after the WPs: the stored vendors.rating, which drives the list stars and the min_rating sourcing filter, refreshes only when a user presses 'Record this rating'. No WP schedules a recompute, and none feeds vendor-app behaviour (acceptance within 24 h, committed-date adherence) into the score.
- planned: C1a (GET /vendor-portal/rating) + C2a (dashboard rating card)
- rec: Small. Add a nightly maintenance.recompute_vendor_ratings beat task: per company, batched over active vendors, calling recompute_rating as the system actor with audit. Optionally add acknowledgement-within-24h % and committed-date adherence as weighted inputs (ASSUMPTION weights).

### A10.F6 [PLANNED_PARTIAL] (G9) Future (now wanted): Digital signature on PO
- gap: D4 delivers an application-level Ed25519 signature: signed at approval, block and QR printed on the C1a PO PDF, a public /verify/po/:token page, and a new revision on D1 amendments. By its own spec it does not deliver a certificate-based DSC/PAdES signature, the CA-issued Class 3 DSC or Aadhaar eSign that Indian counterparties usually mean by 'digitally signed'; it lists that only as an upgrade path.
- planned: D4 (requires C1a PO PDF and D1 amendment seam)
- rec: Ask Atcraft which they mean. If a legally recognised DSC is required: PAdES signing of the PO PDF with the client's DSC through a signing agent or HSM, or an eSign provider API. That needs a PDF-signing dependency (for example pyHanko), justified in the PR. Large.

### K01 [PARTIAL] (G10) Direct finished-goods sale. Dealer orders 100 handles; FG stock is checked, reserved and dispatched.
- gap: The dispatch screen never links a line to a sales-order line. LineEditor (dispatch-dialogs.tsx:111-187) sends only item, quantity, batch and serials, and nothing in apps/web sets sales_order_line_id. The backend does not auto-match lines either (_validate_lines/_persist_lines, dispatch/service.py:696-875). dispatch_consignment therefore treats every UI line as unreserved (:1491-1538) and posts a plain DISPATCH from AVAILABLE. The inventory engine refuses that when the only stock is the stock reserved for this order (inventory/service.py:1158, 'reserved' shortfall), which is exactly the K01 case. When surplus free stock exists, it ships the free stock instead: the order's 100 stay reserved, sales_order_lines.dispatched_quantity stays 0, and the header is still walked to PARTIALLY_DISPATCHED (open_line_count, dispatch/repository.py:460). The dialog text at dispatch-dialogs.tsx:266 and :373 promises a reservation consumption that does not happen. The sales order detail page has no Dispatch action. Invoice and close after dispatch are covered by A2.
- planned: none (A2 builds invoice lines from dispatch lines carrying sales_order_line_id, so A2 depends on this fix; C3a/C4a add dealer self-ordering, not this)
- rec: Small/medium. (1) In CreateDispatchDialog, when a sales order is chosen, load GET /sales/{id}/allocation-status and pre-fill one line per open SO line with sales_order_line_id, item and quantity = min(reserved, outstanding), editable downward only. (2) Server-side, when a SALES dispatch names sales_order_id, require every line to carry sales_order_line_id (or auto-match by item when unambiguous) and refuse otherwise. (3) Add a 'Dispatch' action on sales-order-detail-page.tsx that opens the pre-filled dialog. (4) Add a vitest asserting the POST body carries sales_order_line_id, an API test showing a UI-shaped payload consumes the reservation, and an e2e journey SO -> confirm -> dispatch.

### K02 [PARTIAL] (G10) Finished goods not available. Dealer orders 500 handles with only 120 available; the system allocates 120 and generates a production plan for the remaining 380.
- gap: Nothing generates a production plan when the order is confirmed. There is no hook, event or task after confirm_order, and the Celery beat schedule has no MRP job. ProductionSourceType.SALES_ORDER exists (production/models.py:134) but nothing ever creates a production order with it. The plan therefore needs two manual planner steps plus approval, and the resulting production order is not tied to the sales order. Worse, the order can never ship even after the 380 are made. It stays CONFIRMED for good: CONFIRMED->ALLOCATED happens only inside confirm_order when every line is fully covered (sales/service.py:1709-1719), and _update_confirmed never transitions. Dispatch accepts only ALLOCATED or PARTIALLY_DISPATCHED (dispatch/service.py:440-464).
- planned: none (B4 only adds a 'Production shortage' notification after an MRP run completes)
- rec: Medium. (1) On confirm with a shortfall, behind a setting such as AUTO_PLAN_ON_CONFIRM, create in the same transaction a DRAFT production order (source_type SALES_ORDER, source id plus line id, quantity = shortfall net of open supply) for MAKE items or MAKE_OR_BUY items with a default BOM, and a DRAFT purchase requisition for BUY items. Alternatively, enqueue a scoped MRP run (Celery) that auto-converts. (2) Add a 'Plan shortfall' button to the allocation panel. (3) Add the allocation top-up described under K08, so production output is reserved for the order and the order moves CONFIRMED->ALLOCATED once covered.

### K05 [PARTIAL] (G10) Urgent order reallocation. Parts reserved for future production are needed by an urgent dealer order; the system suggests reallocation with approval.
- gap: (1) No screen lists, approves or rejects reallocation requests. GET /allocation/reallocation-requests, /approve and /reject are called nowhere in apps/web, and no approval_requests row is written, so the dashboard approval queue never shows them either. Approval is API-only. (2) A request can only be raised from an existing reservation row of the recipient. An urgent order that got nothing reserved (all stock held by production) has no row to start from, and the sales order screen has no such action. Nothing suggests reallocation automatically when a short or urgent order is confirmed: suggest_reallocation has no other caller, and is_urgent is never read by allocation. (3) Executing creates the recipient reservation with demand_line_id=None (allocation/service.py:1440) and never updates sales_order_lines.allocated_quantity, reservation_id or status, nor the order status. The urgent order therefore still shows the shortfall and stays CONFIRMED, so it cannot be dispatched. Dispatch also cannot consume the reallocated reservation, because it only consumes order_line.reservation_id (dispatch/service.py:1496).
- planned: none (B4 only adds a 'Stock reallocation request' notification to allocation.approve holders and fixes the donor notification link)
- rec: Medium. (1) Add a Reallocation requests page or tab: GET ...?status=PENDING with Approve and Reject gated on P.ALLOCATION_APPROVE, or write approval_requests rows so requests reach the dashboard queue. (2) Add 'Request reallocation' to the SO allocation panel for short lines, pre-filling to_demand SALES_ORDER plus the line, quantity = shortfall and priority from the order. (3) Add to_demand_line_id to reallocation_requests. On execute, link the new reservation to the SO line (merge into reservation_id, raise allocated_quantity, recompute the line status) and move CONFIRMED->ALLOCATED when the order is covered. (4) Optionally, auto-suggest donors when an urgent order confirms short, and map is_urgent to a priority.

### K06 [PARTIAL] (G10) Packing material shortage. The finished product is ready but the box is unavailable; the system creates a purchase request for packing material.
- gap: Nothing creates a purchase request for packing material. The packing module never calls procurement. There is no packing-material requirement per packing order or per item (no packing specification). There is no reorder-level sweep: PurchaseSourceType.REORDER is defined (procurement/models.py:260) but no code uses it. MRP plans a box only when it is a BOM component of something being manufactured, which does not apply here because the finished goods are already made.
- planned: none
- rec: Medium. (1) Add a packing specification per sellable item (box item, units per carton), or derive the box requirement from the packing order. (2) On packing-order create or start, compare the box requirement with free stock. When short, create a DRAFT purchase requisition in the same transaction (new source_type PACKING_ORDER, or REORDER), deduplicated against open requisitions and POs for the item, and notify purchase. (3) Add a daily Celery sweep that raises requisitions for PACKING_MATERIAL items below reorder_level. (4) Show 'requisition PR/... raised' on the packing screen.

### K07 [PARTIAL] (G10) Job work vendor process. Material goes to a vendor for coating; sent, received, rejected and pending quantities are tracked.
- gap: Sent, received and pending quantities are visible, but the rejected quantity is only captured, never shown afterwards. There is no job-work order detail view: the list shows only the receipt count (job-work-page.tsx:387-402). No screen calls GET /job-work/{id}/reconciliation. JobWorkPendingRow (reports/schemas.py:207-220) has no rejected column.
- planned: none (B3a covers the job-work invoice, not this view)
- rec: Small. Add a job-work order detail drawer or page on /job-work that renders the GET /job-work/{id} lines (issued, received output, rejected, scrap, still at vendor) and the reconciliation report. Add rejected_quantity and scrap_quantity to the JOB_WORK_PENDING report row.

### K08 [PARTIAL] (G10) Partial dispatch. A customer orders 500 with 200 available; the system dispatches 200 and plans the balance 300.
- gap: The scenario itself is refused. Confirming 500 with 200 free leaves the order CONFIRMED (sales/service.py:1708-1719). assert_order_dispatchable accepts only ALLOCATED or PARTIALLY_DISPATCHED (dispatch/service.py:440-464, pinned by tests/unit/test_dispatch_lifecycle.py:265-303), so the 200 cannot ship: the error says 'must be fully allocated before a consignment can leave'. Nothing ever moves CONFIRMED->ALLOCATED later. There is no top-up or re-allocate endpoint, _update_confirmed re-reserves but never transitions, and reallocation does not touch the sales order. Planning the balance is not automatic (see K02). Through the UI the dispatch would also hit the unlinked-line defect described under K01.
- planned: none
- rec: Medium. (1) Let a partially allocated order ship what is reserved: add a CONFIRMED->PARTIALLY_DISPATCHED edge (the dispatchable set derives from the state machine, so it follows automatically) and cap each line's dispatch at its live reservation. (2) Add POST /sales/{id}/allocate (sales_order.update) that reserves newly free stock for short lines by priority and moves CONFIRMED->ALLOCATED when covered. Also top up automatically when stock arrives: GRN post, production or assembly output, transfer receipt. (3) Plan the balance automatically as in K02. (4) Update the unit test that pins the gate, and add an e2e for the 500/200 case.

### K09 [PARTIAL] (G10) Quality rejection. Received material fails QC; the system moves it to rejected stock and starts a vendor return / replacement process.
- gap: Nothing starts the return or the replacement. Quality deliberately only flags the lot (ASSUMPTION at quality/service.py:77-81 and :1821-1825). No draft purchase return is created, and the inspection has no 'Raise return' action. return_required is never linked to a return, nor cleared when one is posted. There is no replacement path at all: PurchaseReturn has only DRAFT, POSTED and CANCELLED, with no CREDIT vs REPLACEMENT resolution and no replacement PO or expected re-receipt. Rejected stock stays in the DAMAGED pool of the same warehouse rather than moving to a rejection store, which is acceptable but should be stated.
- planned: none for the return itself (C1a/C2a add only vendor-portal visibility of QC results and a vendor-facing QC_FAILED notification)
- rec: Medium. (1) On a REJECTED incoming decision against a vendor, create a DRAFT purchase return in the same transaction (lines from the GRN line, stock_state DAMAGED, quantity = rejected, reason from the inspection), or at minimum add a 'Raise return' button that pre-fills it. (2) Add resolution_type (CREDIT_NOTE or REPLACEMENT) to purchase_returns. For REPLACEMENT, re-open the PO line's pending quantity or add a replacement PO line, so the replacement is expected and received against the PO. (3) Link the inspection to the return and clear return_required when the return is posted. (4) Show open-return status on the quality and GRN screens.

### K10 [PARTIAL] (G10) Multi-warehouse allocation. If one warehouse is short, the system checks another and suggests a stock transfer.
- gap: Confirmation and allocation look only at the order's own warehouse: sales/service.py:1667 calls available_by_item(item_ids, order.warehouse_id), and the 'Free stock' figure in allocation-status does the same (:936). Nothing checks other warehouses or suggests a transfer. The MRP engine never produces TRANSFER suggestions, as mrp/models.py:129-137 says outright, although mrp-run-detail-page.tsx has UI text for them. Transfers carry no link to the demand they serve.
- planned: none
- rec: Medium. (1) Add an availability-by-warehouse read (one grouped query) to confirm and allocation-status that returns transfer suggestions (source warehouse, quantity) for short lines. (2) On the SO allocation panel, show 'Available elsewhere' with a 'Raise transfer' action that pre-fills POST /inventory/transfers with a source reference to the order, optionally taking a SOFT reservation at the source warehouse. (3) Optionally, add an MRP warehouse-balancing pass that produces TRANSFER suggestions, with a conversion hook into inventory transfers.
