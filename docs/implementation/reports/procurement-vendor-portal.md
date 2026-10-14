# Procurement and vendor portal (audit gaps 2 and 8): code map

Path roots used below:
- `API` = `C:\Project\Other\ATCRAFT\apps\api\src`
- `TESTS` = `C:\Project\Other\ATCRAFT\apps\api\tests`
- `WEB` = `C:\Project\Other\ATCRAFT\apps\web\src`
- `DOCS` = `C:\Project\Other\ATCRAFT\docs`
- `MIG` = `C:\Project\Other\ATCRAFT\apps\api\migrations\versions`. The current head is `2f5e08c7ba61` (`20260906_0902_grant_item_attribute_permissions.py`).

## Audit verdict for this area

- **Gap 8 (split one purchase requirement into one PO per category): confirmed absent, and the gap is wider than the audit says.** Nothing converts a requisition into a purchase order, whether by endpoint or in the UI.
  - The only link is a caller building `POST /procurement/purchase-orders` by hand with `lines[].requisition_line_id`. Only the demo loader does this.
  - A PO has no category column. No procurement file reads `items.category_id`: `grep category API\modules\procurement\*.py` finds nothing.
- **Gap 2 (vendor mobile app and push notifications): confirmed absent for UI and push. Partly present in the backend.** What exists:
  - vendor-bound users (`users.vendor_id`, `allowed_category_ids`);
  - five `VENDOR_*` role presets;
  - vendor scoping in four procurement repositories;
  - three PO endpoints a vendor can use (`/acknowledge`, `/shipment`, `/tracking`).

  What is missing: there is no vendor screen. A vendor login lands in the internal app shell. Worse, several permissions given to vendor roles open modules that apply **no vendor scoping**, so a vendor can read other vendors' and customers' data (traps T1–T3). This must be fixed before any portal ships.

---

## Exists today

### E1. Purchase requisition

`API\modules\procurement\models.py`

`PurchaseRequisition(TenantEntity)` at :315, table `purchase_requisitions`:
- Columns: `code`, `requisition_date`, `required_date` (NOT NULL), `warehouse_id` (NOT NULL), `department_id`, `branch_id`, `requested_by_id`, `status`, `priority` (1–9, default 5), `approved_by_id`/`approved_at`, `rejection_reason`, `cancellation_reason`, `source_type` (default `MANUAL`), `source_id` (bare UUID, no FK), `purpose`, `notes`.
- CHECKs at :389–411.
- Indexes: `ix_purchase_requisitions_status_required`, `_warehouse`, `_source`.

`PurchaseRequisitionLine(TenantEntity)` at :415:
- Columns: `requisition_id`, `line_number`, `item_id`, `description`, `quantity`, `uom_id`, `required_date` (nullable), `estimated_unit_price` (nullable), `suggested_vendor_id` (nullable FK to vendors), `ordered_quantity` (running total; may exceed `quantity`), `status`, `notes`.
- Line status values: PENDING, PARTIALLY_ORDERED, ORDERED, CANCELLED.
- **No category column, and no SO/BOM/MRP reference on the line.** Only the header carries `source_type`/`source_id`.

Supporting types:
- `PurchaseSourceType` (:245): MANUAL, PURCHASE_REQUISITION, VENDOR_QUOTATION, MRP_SUGGESTION, MRP_RUN, SALES_ORDER, BOM_DEMAND, REORDER.
- `OPEN_REQUISITION_STATUSES` (:297): APPROVED, CONVERTED.

**State machine:** `REQUISITION_LIFECYCLE`, `API\modules\procurement\service_rules.py:47-109`.
- DRAFT→PENDING_APPROVAL (create)
- DRAFT→CANCELLED
- PENDING_APPROVAL→APPROVED or REJECTED (approve)
- PENDING_APPROVAL→CANCELLED ("Withdraw")
- APPROVED→CONVERTED (declared permission `purchase_order.create`)
- APPROVED→CLOSED or CANCELLED
- CONVERTED→CLOSED

`Transition.permission` is advisory. `StateMachine.assert_transition` (`API\core\state_machine.py:64-74`) only checks that the edge exists; the route's `require_permission` is what is actually enforced.

**Routes** (`API\modules\procurement\router.py`, prefix `/procurement`, mounted under `/api/v1`):

| Route | Line | Permission | Rule |
|---|---|---|---|
| GET `/purchase-requisitions` | 291 | purchase_requisition.view | filters: search, status, warehouse_id, department_id, item_id, open_only, required_from/to |
| POST `/purchase-requisitions` | 308 | ….create | `RequisitionCreate` (schemas :130); line `RequisitionLineIn` (:96) |
| GET `/purchase-requisitions/{id}` | 324 | ….view | `RequisitionDetail` |
| PATCH `/{id}` | 338 | ….create | DRAFT only, lines replaced |
| DELETE `/{id}?reason=` | 356 | ….create | DRAFT only |
| POST `/{id}/submit` | 370 | ….create | |
| POST `/{id}/approve` | 397 | ….approve | separate approver (service :280-292) |
| POST `/{id}/reject` | 425 | ….approve | |
| POST `/{id}/cancel` | 452 | ….create | |

There is **no `/close` route**. The CONVERTED→CLOSED and APPROVED→CLOSED edges cannot be reached over HTTP, so a converted requisition stays CONVERTED, and stays in `open_only`, for ever.

**Services** (`API\modules\procurement\service.py`):
- `create_requisition` :556, which calls `_create_requisition` :623. Numbering is `DocTypes.PURCHASE_REQUISITION` ("PR"); writes audit CREATE.
- `create_requisition_from_mrp(session, tenant, request) -> (id, code)` :572. This is the MRP hook:
  - it creates one DRAFT requisition per suggestion, with one line;
  - it sets `source_type=MRP_SUGGESTION, source_id=suggestion_id`;
  - it **drops** `request.source_demands`, which carries the SO/BOM references.
- Also: `transition_requisition` :774, `update_requisition` :698, `delete_draft_requisition` :823, `list_requisitions` :857, `get_requisition_detail` :870.

**MRP side:**
- `API\modules\mrp\service.py:208-221`: `CONVERSION_HOOKS[PURCHASE_REQUISITION]` points at `create_requisition_from_mrp`.
- `convert_suggestion` :1110; route `POST /mrp/suggestions/{id}/convert` (`mrp\router.py:350`, permission `mrp.convert_suggestion`).
- There is no bulk convert (only `/suggestions/bulk-ignore` at :319) and no MRP→PO path.

### E2. How a requisition becomes a PO today

1. **Via RFQ.** `POST /procurement/rfqs` with `requisition_id` (router :497; `create_rfq` service :936) copies the open lines through `_rfq_lines_from_requisition` :905.
   - It requires the requisition to be APPROVED or CONVERTED.
   - It does **not** move the requisition to CONVERTED, despite the enum comment at models :125.
   - Selecting a quotation (`POST /quotations/{id}/select`, :674; `decide_quotation` :1450) creates nothing.
2. **Via `POST /procurement/purchase-orders`** with `lines[].requisition_line_id` / `quotation_line_id` and header `source_type`/`source_id`. Then `_link_source_documents` (service :2206-2267):
   - locks the requisition lines (`RequisitionLineRepository.lock_many`, repository :426) and adds to `ordered_quantity`;
   - recomputes each line's status (`requisition_line_status_for`, service_rules :624);
   - moves each touched APPROVED requisition to CONVERTED through `_transition` (audited);
   - sets `quotation.converted_purchase_order_id` when `source_type=VENDOR_QUOTATION`.

   `_release_source_lines` (:2270) gives the quantity back on a line rewrite (:2362) and on CANCELLED or REJECTED (:2492-2501).
3. **None of these links is validated.** Nothing checks that the requisition is APPROVED, that the item or warehouse matches, or that the id belongs to this tenant (a foreign id is silently skipped at :2231-2233). Nothing checks that the quotation is SELECTED or from the same vendor.
4. **The only caller of (2)** is the demo loader, `API\core\demo_data\procurement.py:1478-1534` (`_seed_sourced_order`).
5. **No grouping by vendor or category exists.** `PurchasableItem` (repository :148-171) and `PurchasableItemRepository.load` (:215-258) do not select `Item.category_id`.

### E3. Purchase order model

`PurchaseOrder(TenantEntity, OptimisticLockMixin)`, models :792. Columns:
- Identity and parties: `code`, `vendor_id`, `warehouse_id` (NOT NULL), `branch_id`.
- Dates: `po_date`, `expected_delivery_date` (nullable; drives the delay flag and MRP netting).
- `status`, `priority`.
- Money: `currency`, `subtotal`, `discount`, `tax_amount`, `freight_amount`, `total_amount`, `payment_terms_days`.
- `delivery_address` (free text), `terms`.
- Lifecycle evidence: `approved_by_id`/`approved_at`, `sent_at`, `acknowledged_at`, `rejection_reason`, `cancellation_reason`.
- `duplicate_acknowledged` and `duplicate_acknowledgement_reason` (CHECK :970).
- `source_type`/`source_id`.
- `assigned_vendor_user_id` (:895; FK users, SET NULL). It has an index, `ix_purchase_orders_assigned_vendor_user` (:940, commented "the vendor portal's own list"), but **no query uses it**.
- `courier_name`, `tracking_reference`, `shipped_at` (:906-908; ASSUMPTION §19).
- `vendor_reference`, `notes`, `version`.

Absent from the PO: category, vendor decline reason, ready-for-dispatch, transporter_id, LR number, vehicle number, inbound freight, a vendor-committed date distinct from the buyer's date, delivered_at.

`PurchaseOrderLine` (:977) columns:
- `item_id`, `description`, `quantity`, `uom_id`.
- Pricing: `unit_price`, `discount_percentage`, `discount`, `tax_rate` (always the item's `gst_rate`, never the payload; service :2033), `tax_amount`, `line_total`.
- `required_date`.
- Receipt progress: `received_`, `accepted_`, `rejected_`, `returned_quantity`.
- `requires_qc`, `status` (derived by `po_line_status_for`, service_rules :600).
- Links: `requisition_line_id` (FK), `quotation_line_id` (FK).
- CHECKs: over-receipt tolerance of 5% (:1104); accepted + rejected = received.

Status groups (change these together with their partial indexes):

| Group | Location | Members |
|---|---|---|
| `OPEN_PO_STATUSES` | models :271 | DRAFT → PARTIALLY_RECEIVED; predicate of `ix_purchase_orders_open_vendor` (:928) |
| `EDITABLE_PO_STATUSES` | models :283 | DRAFT, PENDING_APPROVAL |
| `RECEIVABLE_PO_STATUSES` | models :290 | SENT, ACKNOWLEDGED, PARTIALLY_RECEIVED |
| `NETTABLE_PO_STATUSES` (MRP) | repository :124 | APPROVED, SENT, ACKNOWLEDGED, PARTIALLY_RECEIVED |

### E4. PO state machine as implemented

`PURCHASE_ORDER_LIFECYCLE`, service_rules :219-383:

| From | To | Permission |
|---|---|---|
| DRAFT | PENDING_APPROVAL | update |
| DRAFT, PENDING_APPROVAL, APPROVED, SENT, ACKNOWLEDGED | CANCELLED | cancel |
| PENDING_APPROVAL | APPROVED (irreversible) | approve |
| PENDING_APPROVAL | REJECTED | reject |
| APPROVED | SENT | send |
| SENT | ACKNOWLEDGED | update |
| SENT or ACKNOWLEDGED | PARTIALLY_RECEIVED or RECEIVED | grn.post |
| PARTIALLY_RECEIVED | RECEIVED | grn.post |
| PARTIALLY_RECEIVED | CLOSED (short close) | cancel |
| RECEIVED | QC_PENDING or GRN_COMPLETED | grn.post |
| QC_PENDING | QC_COMPLETED | qc.decide |
| QC_COMPLETED | GRN_COMPLETED | grn.post |
| GRN_COMPLETED | INVOICED | vendor_invoice.create |
| GRN_COMPLETED | CLOSED | update |
| INVOICED | PAID | payment.record |
| PAID | CLOSED | update |

Terminal: CLOSED, CANCELLED, REJECTED.

What drives the edges:
- `transition_purchase_order` (:2419) drives submit, approve, reject, send, acknowledge, cancel and close.
- `_advance_order_after_receipt` (:3252-3311) drives the receipt edges.
- **Nothing drives QC_PENDING→QC_COMPLETED, GRN_COMPLETED→INVOICED or INVOICED→PAID.** Only :3296 writes those later statuses. GRN line `qc_status` is set to PENDING (:3247) and never updated. `quality.service.decide_inspection` (`API\modules\quality\service.py:1735`) never touches the GRN line or the PO. See T7.

There is no vendor-decline edge, no ready-for-dispatch state and no in-transit state on the PO.

### E5. Purchase-order routes

All paths are under `/api/v1/procurement`.

| Route | Line | Dependencies | Body → response | Rules |
|---|---|---|---|---|
| GET `/purchase-orders` | 727 | po.view | query: search, status, vendor_id, warehouse_id, item_id, open_only, overdue_only, source_type, source_id, expected_from/to, page, sort → `PaginatedResponse[PurchaseOrderSummary]` | `_filtered` repository :927; sortable fields :888-896 |
| POST `/purchase-orders/check-duplicates` | 740 | po.view only (**no `forbid_vendor_users`**) | `DuplicateCheckRequest` → `DuplicateCheckResult` | read-only |
| POST `/purchase-orders` | 764 | po.create | `PurchaseOrderCreate` (schemas :634) → `PurchaseOrderDetail` 201 | see below |
| GET `/purchase-orders/{id}` | 788 | po.view | → Detail | `_get_order_for_tenant` :2621 (404 for another vendor's order) |
| GET `/purchase-orders/{id}/tracking` | 802 | po.view | → `PurchaseOrderTracking` (schemas :882) | `order_tracking` :2636 |
| PATCH `/purchase-orders/{id}` | 821 | po.update + `forbid_vendor_users` (:830) | `PurchaseOrderUpdate` (:675) | DRAFT or PENDING_APPROVAL only |
| POST `/{id}/submit` | 848 | po.update + forbid (:855) | `ApproveRequest{reason?}` | needs lines; vendor must be orderable |
| POST `/{id}/approve` | 879 | po.approve | `ApproveRequest` | separate approver; audit `PURCHASE_APPROVAL` |
| POST `/{id}/reject` | 906 | po.reject | `ReasonRequest{reason 3-255}` | internal approver's reject from PENDING_APPROVAL only |
| POST `/{id}/send` | 927 | po.send | `SendRequest{assigned_vendor_user_id?, reason?}` (:775) | stamps `sent_at`; assigns the user (**unvalidated**, :2456-2457); sends no notification, PDF or email |
| POST `/{id}/acknowledge` | 954 | po.update | `AcknowledgeRequest{expected_delivery_date?, vendor_reference?, reason?}` (:783) | SENT→ACKNOWLEDGED; may overwrite `expected_delivery_date` (≥ po_date) (:2458-2467) |
| POST `/{id}/shipment` | 984 | po.update | `ShipmentUpdate{courier_name?≤120, tracking_reference?≤120, shipped_at?, expected_delivery_date?}` (:792; at least one field) | `record_shipment` :2524. Allowed on APPROVED, SENT, ACKNOWLEDGED or PARTIALLY_RECEIVED (:2554-2557). No status change. Audit UPDATE of 4 fields |
| POST `/{id}/cancel` | 1011 | po.cancel | `ReasonRequest` | gives quantity back to the requisition |
| POST `/{id}/close` | 1032 | po.cancel | `ReasonRequest` | short close |

`create_purchase_order` (:2071) checks, in order:
- vendor is orderable (`can_receive_purchase_orders`, `API\modules\vendors\service.py:467`);
- warehouse exists; items are purchasable and active; `expected_delivery_date ≥ po_date`;
- takes `advisory_xact_lock(company, "po-duplicate", vendor, warehouse)` (:2107);
- runs the duplicate check. A duplicate is refused with 409 `DUPLICATE_DOCUMENT` unless the request sets `acknowledge_duplicates` with a reason **and** the caller holds po.approve (:2123-2133);
- numbers the order "PO", creates it as DRAFT, and writes audit CREATE.

There is **no POST `/tracking`**. "Tracking" is the GET view; courier details go through POST `/shipment`.

### E6. Duplicate detection (`/check-duplicates` and create)

**Contracts** (schemas):
- `DuplicateCheckRequest` (:826): `vendor_id`, `warehouse_id`, `expected_delivery_date?`, `source_type` (default MANUAL), `source_id?`, `lines[1..200]{item_id, required_date?}`.
- `DuplicateCandidateRead` (:839):
  - order and line: `purchase_order_id`, `purchase_order_code`, `purchase_order_line_id`, `status`, `vendor_id`, `item_id`, `warehouse_id`;
  - quantities and date: `ordered_quantity`, `received_quantity`, `open_quantity`, `required_date`;
  - source: `source_type`, `source_id`;
  - verdict: `confidence` (EXACT | STRONG | POSSIBLE), `match_reasons[]`, `recommended_action`, `available_actions[]`.
- `DuplicateCheckResult` (:863): `candidates`, `requires_acknowledgement`.

**SQL narrowing** (repository :975-1075): same company, same vendor, same warehouse, open PO status, open line status, item in the set, and one of:
- line date (falling back to header date) inside the envelope;
- no date;
- same `(source_type, source_id)`.

The query has `LIMIT 200`.

**Decision**, `classify_candidate` (service_rules :877):
- Requires the same vendor, item and warehouse, **and** either dates within ±7 days (`DUPLICATE_REQUIRED_DATE_WINDOW_DAYS`, models :98; an unknown date counts as overlapping) or the same source (`same_source_demand` :774; MANUAL never matches).
- Confidence: EXACT means same source; STRONG means the existing order is DRAFT, PENDING_APPROVAL or APPROVED; otherwise POSSIBLE.
- Available actions (`available_actions_for` :796), with the recommendation ladder in `recommend_action` :818:
  - MERGE when the existing order is DRAFT or PENDING_APPROVAL;
  - INCREASE and CANCEL when nothing has been received and the status is ACKNOWLEDGED or earlier;
  - CREATE_NEW_WITH_APPROVAL always.

| Proposal rule or option | Implemented |
|---|---|
| same vendor | Yes, as a **hard filter**, so a duplicate across two vendors is never found |
| same item/category | Item only. Category is never considered |
| same requirement date | ±7 days; a null date counts as a match |
| same SO/MRP reference | Only when the PO header `(source_type, source_id)` is equal. A PO raised from a requisition carries `PURCHASE_REQUISITION`, and MRP-born requisitions drop the SO references |
| same pending PR | Header pair only. The line `requisition_line_id` is not matched |
| same BOM demand | `BOM_DEMAND` exists, but nothing ever writes it |
| same warehouse | Yes, hard filter |
| merge / increase qty / cancel | Advisory only. There is no merge or amend endpoint; PATCH works only in DRAFT and PENDING_APPROVAL. The UI shows these as "go and do it" (`WEB\features\procurement\procurement-types.ts:483-511`, `actionable:false`). INCREASE is recommended for SENT/ACKNOWLEDGED orders that no endpoint can amend |
| new PO with approval | Yes, `acknowledge_duplicates` + reason + po.approve |

Background docs: `DOCS\assumptions.md` §16 (:312-331), the 7-day window.

### E7. Vendor-portal plumbing (backend)

**User binding:**
- `User.vendor_id` (`API\modules\users\models.py:151-153`).
- `allowed_category_ids ARRAY(GUID)` (:156; empty means all).
- `is_vendor_user` (:187); index `ix_users_vendor`.

**Validation:**
- `assert_vendor_role_consistency` (`API\modules\users\service.py:466-495`): a vendor account may hold vendor roles only, and vendor roles may be held only by vendor accounts.
- `_assert_scope_is_meaningful` (:1335-1349): a category list is allowed only on a vendor account.
- **Not validated:** that category ids exist, belong to this tenant, or are a subset of the vendor's `vendor_categories` rows. `API\modules\vendors\models.py:347-350` claims that subset rule; nothing enforces it.
- **The admin UI cannot set `allowed_category_ids` at all.** `WEB\features\admin\user-dialogs.tsx:449-472` has a vendor picker only.

**Principal and tenant** (`API\core\deps.py`):
- `Principal` (:66-140) carries `vendor_id`, `allowed_category_ids`, `is_vendor_user` (:107), and `can_access_category()` (:111-119; empty means unrestricted; **no caller uses it**).
- `forbid_vendor_users` (:319-329) answers 403 `SCOPE_DENIED`.
- `TenantContext` (:332-341) carries `vendor_id` and `allowed_category_ids`, filled by `get_tenant_context` (:344-357).
- `MeResponse` (`API\modules\auth\schemas.py:155-178`) returns both fields to the browser.

**Login:**
- Same `POST /api/v1/auth/login` as internal staff (`auth\router.py:107`).
- Refresh is cookie + CSRF only (`/auth/refresh` :131-153, "A refresh cookie is required."). That works for a same-origin PWA and not for a native app.

**Where vendor scope is applied** (grep of `tenant.vendor_id` / `forbid_vendor_users` across `API`):

| Location | Scope applied |
|---|---|
| `PurchaseOrderRepository._scoped` (procurement\repository.py:898-910) | vendor_id only, on every get, lock and list. Not category, not status (vendors see DRAFT and unapproved prices), not assignee |
| `_get_order_for_tenant` service :2621-2630; `record_shipment` :2552 | second vendor check |
| `QuotationRepository._filtered` :671-676; `get_quotation_detail` :1513-1527; `compare_quotations` :1557 | vendor (dormant: no `quotation.view` on vendor roles) |
| `GoodsReceiptRepository._filtered` :1216 | list only. **`get_goods_receipt_detail` :3360 is not vendor-checked** (dormant: no `grn.view`) |
| `PurchaseReturnRepository._filtered` :1320 | list only (dormant) |
| `open_lines_for_duplicate_check` :975 | **not scoped** |
| products item filter `API\modules\products\repository.py:531-535` | category: `Item.category_id IN allowed`, exact match, not subtree (dormant: no `item.view`) |
| `VendorRepository._scoped` vendors\repository.py:122-132; contacts :363-367; `VendorCategoryRepository._scoped` :443-454 | vendor, plus category for categories (dormant: no `vendor.view`) |
| `UserRepository._scoped` users\repository.py:102-121 | vendor |
| dispatch, documents, job_work, quality, finance, reports | **no vendor scoping at all** |

**Role presets** (`API\core\permissions.py`; all `is_system` and `is_vendor_role`; refreshed by `_seed_roles`, `API\core\seed.py:193`):

| Role (line) | Permissions |
|---|---|
| `_VENDOR_BASE` :451 | purchase_order.view, dispatch.view, document.view, notification.view |
| VENDOR_OWNER :612 | base + purchase_order.update, dispatch.create, dispatch.update, vendor_invoice.view/create, payment.view, job_work.view, qc.view, document.upload, user.view |
| VENDOR_MANAGER :631 | base + purchase_order.update, job_work.view, document.upload. Described as "limited to assigned item categories", which is **not enforced on POs** |
| VENDOR_DISPATCH_MANAGER :644 | base + dispatch.create, dispatch.update, document.upload. **No purchase_order.update, so it cannot call `/shipment`**, even though models :905 and assumptions §19 say the dispatch manager records shipments |
| VENDOR_QC_CONTACT :656 | base + qc.view, document.upload |
| VENDOR_ACCOUNT_MANAGER :663 | base + vendor_invoice.view/create, payment.view, ledger.view, document.upload |

Related vendor-master tables:
- `VendorContact` (vendors\models.py:280) uses roles OWNER, MANAGER, DISPATCH, QC, ACCOUNTS (`VendorContactRole` :78) but has **no link to `users`**.
- `VendorCategory` (:339) holds the vendor ↔ category qualification, with at most one preferred vendor per category (:387-393).
- `VendorType` (:61) is classification only.
- No demo vendor-portal users are seeded.

**Existing tests:**
- `TESTS\api\test_vendor_portal_scope_http.py`:
  - another vendor's order answers 404 on GET, acknowledge and shipment;
  - PATCH and submit answer 403 `SCOPE_DENIED`;
  - **a vendor's own DRAFT order answers GET 200** (:171-190).
- `TESTS\unit\test_procurement_vendor_scope.py`: `forbid_vendor_users` is present on PATCH and submit and absent on acknowledge and shipment (:141-163).

### E8. What a vendor user sees in the web app today

- **No vendor-specific code exists.**
  - `isVendorUser` is computed in `WEB\features\auth\auth-context.tsx:164,351`, but only the admin dialogs read anything vendor-related.
  - The user lands in the internal `AppLayout`. The index route `/` is ungated (`WEB\app\router.tsx:214-221`) and renders `DashboardPage`.
  - Without `dashboard.view` the dashboard shows only the "Good day, &lt;name&gt;" header and an empty body (`WEB\features\dashboard\dashboard-page.tsx:148-157,293`).
  - The sidebar is filtered by permission (`WEB\app\navigation.ts`), and the notification bell works (`WEB\app\app-layout.tsx:83-93`).
- **VENDOR_OWNER sidebar:** Purchase Orders, Job Work, Quality Control, QC Templates, Dispatch, Vendor Invoices, Payments, Documents, Users & Roles. Of these:
  - only Purchase Orders and Users are vendor-scoped;
  - Job Work, Quality, Dispatch, Vendor Invoices, Payments and Documents show **company-wide data**;
  - Dispatch also offers Create, so the vendor can raise company consignments.
- **VENDOR_ACCOUNT_MANAGER** also gets Chart of Accounts (`ledger.view`).
- **Every vendor role** gets Dispatch, which lists all outbound consignments, including customer names.
- **PO list** (`WEB\features\procurement\purchase-orders-page.tsx`):
  - shows only the vendor's own orders, including DRAFT and PENDING_APPROVAL with prices;
  - the vendor column shows "-", because `/vendors/options` needs `vendor.view` (:103,132-150);
  - there is no "New" button.
- **PO detail** (`purchase-order-detail-page.tsx`):
  - "Submit for approval" appears on a DRAFT order, because the UI gates on `PURCHASE_ORDER_UPDATE` (:770). The API refuses it with `SCOPE_DENIED`.
  - "Vendor acknowledged" appears on SENT (:798). It posts only `{reason}` (:503-507), so no committed date or reference is captured.
  - There is no shipment form, no decline, no documents and no PDF.
  - "Deliver to" warehouse and UoM codes show "-" (no `warehouse.view` / `uom.view`).
  - `TrackingPanel` (:170, used at :944) works.

### E9. Proposal vendor actions: API and UI today

| Action | API today | Permission | UI today |
|---|---|---|---|
| Accept PO | POST `/purchase-orders/{id}/acknowledge` | po.update (OWNER, MANAGER) | internal "Vendor acknowledged" button; no date or reference |
| Reject with reason | **none for vendors.** `/reject` is the internal approver's PENDING_APPROVAL→REJECTED | none | none |
| Update delivery date | Indirect: `/acknowledge` (SENT only) or `/shipment.expected_delivery_date`. Both overwrite the date MRP nets on | po.update | none |
| Mark ready for dispatch | none (no field or status) | none | none |
| Add courier/transport | `/shipment`: courier_name, tracking_reference, shipped_at only (no transporter_id, LR, vehicle, freight) | po.update (not DISPATCH_MANAGER) | none (`ShipmentUpdatePayload` is declared, unused, at procurement-types.ts:395) |
| Upload invoice | generic `POST /documents` (free-text `entity_type`) and `POST /finance/vendor-invoices` (payload `vendor_id`, **any vendor**). Neither is vendor-scoped | document.upload / vendor_invoice.create | internal Documents and Vendor Invoices pages |
| Upload dispatch photo / LR | generic `POST /documents`; no document "kind" field (`DocumentCategory` = DOCUMENT/REPORT/ATTACHMENT/EXPORT/TEMP) | document.upload | internal Documents page |
| View QC result | `/purchase-orders/{id}/tracking`: `qc_pending`, receipt totals. Quality routes (qc.view) are unscoped | po.view / qc.view | tracking panel |
| View payment status | `/finance/vendor-invoices` and `/finance/payments`, **unscoped (all vendors)** | vendor_invoice.view / payment.view | internal pages |
| Dashboard counts (pending, accepted, ready, dispatched, delivered, payment pending, QC rejected) | none | none | none |
| PO detail (number, items, category, qty/rate, delivery date, address, terms) | GET `/purchase-orders/{id}`: no category, no item code/name (description only), free-text address | po.view | internal detail page |
| Download PO PDF | none | none | none |

### E10. Admin inbound dispatch-tracking view

- **Per order only:** `GET /purchase-orders/{id}/tracking`. `PurchaseOrderTracking` (schemas :882-914) returns:
  - vendor and PO: `vendor_name`, PO `code`, `status`, `expected_delivery_date`;
  - courier: `courier_name`, `tracking_reference`, `shipped_at`, `sent_at`, `acknowledged_at`;
  - quantities: ordered, received, accepted, rejected, pending;
  - flags: `is_delayed` and `days_late`, `grn_pending`, `qc_pending` and `qc_pending_line_count`;
  - `receipts[]`.
- How the flags are computed (service :2673-2707): `is_delayed` is days_late > 0 **and** `grn_pending`; `grn_pending` means open lines exist and the status is receivable.
- **No list or board endpoint.** The PO list has `overdue_only` and a Delay column, but `PurchaseOrderSummary` (schemas :699-724) lacks courier, tracking and shipped_at. There is no "expected today" filter (only `expected_from`/`to`) and no in-transit concept.
- **No inbound alerts.** `NotificationType.PURCHASE_ORDER_DELAYED` exists (`API\modules\notifications\models.py:110`) and is never raised. The beat entry "dispatch-delay-alerts" (`API\workers\celery_app.py:85-91`) is commented as covering purchase orders, but it calls `dispatch_service.scan_delays`, which covers outbound consignments only.

### E11. Reports

All reports are gated on `report.view`; no vendor role holds it. Routes are in `API\modules\reports\router.py`; `ReportKey` is at `API\modules\reports\models.py:62-95`.

| Proposal report | Exists |
|---|---|
| Pending PO | `GET /reports/purchase-pending` (:466): repository :519; statuses `PENDING_PURCHASE_STATUSES` (:88) = APPROVED, SENT, ACKNOWLEDGED, PARTIALLY_RECEIVED; needs a period on `po_date`; filters vendor, warehouse, status, search; derives days_overdue and ageing bucket (service :429). `category_id` is accepted by `report_filters` and **ignored** |
| Vendor-wise PO | partial: purchase-pending with `vendor_id`; nothing aggregated |
| Category-wise PO | none |
| Delayed PO | partial: purchase-pending `days_overdue` and the PO-list `overdue_only`. `/reports/order-delay` is about **sales** |
| Duplicate PO warning | none; `duplicate_acknowledged` cannot be filtered |
| Vendor acceptance | none, although `sent_at` and `acknowledged_at` exist |
| Delivery performance | `GET /reports/vendor-performance` (:609; repository :716): on time = GRN `receipt_date` ≤ PO `expected_delivery_date`; also rejection % |
| Inbound in-transit, expected today, late, courier-wise, transport cost, vendor dispatch history | **none for inbound.** `/reports/courier-tracking` (:661, repository :1146) and `/reports/vendor-dispatch-history` (:682, repository :1212) read the outbound `dispatches` table |

### E12. PO PDF

None.
- `reportlab` is declared for "packing lists, POs" (`C:\Project\Other\ATCRAFT\apps\api\pyproject.toml:53`).
- Only `render_packing_list_pdf` exists (`API\workers\tasks\reports.py:248`), with task `reports.generate_packing_list` (:328).
- The route `GET /packing/{order_id}/packing-list` (`API\modules\packing\router.py:474-494`) returns 202 with a job id, and the client polls `/reports/jobs/{id}`, which needs `report.view`.

### E13. Frontend procurement screens (`WEB\features\procurement\`)

| File | What it has | What it lacks |
|---|---|---|
| `requisitions-page.tsx` | list; row actions at :414-497: Edit draft, Submit, Approve, Reject, Cancel, Delete | **no "convert to PO", no "raise RFQ", no requisition detail page** |
| `requisition-form-dialog.tsx` | lines with item, qty, estimated price, required date, `suggested_vendor_id` | |
| `rfq-dialogs.tsx` | can raise an RFQ from a requisition (`requisition_id` :271) | |
| `quotation-dialogs.tsx` | shows "Purchase order raised" when `converted_purchase_order_id` is set (:443-449) | no action to raise a PO from the selected quotation |
| `purchase-order-form-dialog.tsx` | `buildPayload` (:147-180) sends header fields and lines `{line_number, item_id, quantity, unit_price}`; duplicate resubmit sends `acknowledge_duplicates` | never sends requisition/quotation links, source, `requires_qc`, discount or assignee; **never calls `/check-duplicates`** (it relies on the 409) |
| `purchase-order-duplicate-dialog.tsx` | opens on 409: candidates, confidence, reasons; only "raise anyway" is actionable | |
| `purchase-orders-page.tsx` | filters status, vendor, expected window, open_only, overdue_only; Delay column | |
| `purchase-order-detail-page.tsx` | commands submit, approve, reject, send, acknowledge, cancel (`Command` :391); tracking panel | no close/short-close, no shipment, no assignee picker on Send, no PDF |
| `procurement-types.ts` | predicates at :285-292; `PO_LIFECYCLE` :264 | |

Other wiring:
- Query keys: `WEB\lib\query-client.ts:178-184` (`procurement.{all, purchaseOrders, purchaseOrder, grns, tracking}`).
- Lazy routes: `WEB\app\route-loaders.ts:43-51`.

---

## Missing vs proposal

### Gap 8: automatic PO split

1. No endpoint or service converts an approved requisition into one or more POs. Nothing groups lines by vendor or by category.
2. There is no vendor-resolution rule for a requisition line. Sources exist but nothing combines them:
   - `suggested_vendor_id` (requisition line);
   - `ItemVendorLink.is_preferred` (`API\modules\products\models.py:671`, one preferred per item);
   - `VendorCategory.is_preferred` (one per category).
3. There is no price-resolution rule. Possible sources: `estimated_unit_price`, `ItemVendorLink.last_purchase_price`. `PriceType` has no purchase price (`products\models.py:101` lists SALE, DEALER, DISTRIBUTOR).
4. POs have no category column, and `PurchasableItem` has no `category_id`.
5. No assignment rule maps (vendor, category) to a vendor user. `assigned_vendor_user_id` is set only by hand: in the API payload or `SendRequest`. The UI never sets it, nothing validates it, and buyers cannot list vendor users (`user.view` is not in `_PURCHASE`, permissions.py:291).
6. Batch conversion of the many single-line requisitions MRP creates does not exist.
7. The duplicate rule does not match on `requisition_line_id` ("same pending PR"), on category, or across vendors.
8. No UI exists for any of this.

### Gap 2: vendor portal, mobile and push

1. There is no vendor UI at all: no portal shell, no mobile layout, no PWA manifest, no service worker (only `favicon.svg` in `apps\web\public`).
2. Push is absent. `NotificationChannel` (`notifications\models.py:82-90`) is IN_APP and EMAIL only. There is no device or subscription table, and procurement raises no notifications at all (no `notify` call in procurement).
3. These vendor actions have no API: decline/reject with reason, ready for dispatch, a vendor-committed delivery date kept separate from the buyer's date, structured shipments (transporter, LR, vehicle, freight, several consignments per PO), typed document uploads tied to a PO, vendor invoice submission scoped to the vendor and its PO, a vendor view of QC/GRN results, a vendor payment view, dashboard counts, and a PO PDF.
4. These existing vendor endpoints need work: `/shipment` is unreachable for VENDOR_DISPATCH_MANAGER, and neither `/acknowledge` nor `/shipment` enforces category or assignee.
5. Vendor visibility is wrong. Vendors see DRAFT, PENDING_APPROVAL and REJECTED orders. Category restriction is not applied to POs. `/check-duplicates` exposes other vendors' open orders.
6. Admin side: no inbound tracking board, no inbound reports, no alerts for "PO not acknowledged" or "tracking missing", and no way to set a vendor user's categories in the UI.

---

## Patterns to copy

| New piece | Closest precedent | Conventions to follow |
|---|---|---|
| Convert requisition → POs (service) | `create_requisition_from_mrp` → `_create_requisition` (service :572-695): builds a typed `RequisitionCreate` and calls the shared body. Refactor `create_purchase_order` (:2071-2203) into `_create_purchase_order(..., vendor, items)` the same way | one `async with transaction(session)`; `next_document_number(…, DocTypes.PURCHASE_ORDER, on=po_date)`; `record_audit` with `snapshot(order, ORDER_AUDITED_FIELDS)`; `_link_source_documents` for requisition linkage; errors from `src.core.errors` (`BusinessRuleError` with `details`, `NotFoundError`, `PermissionDeniedError`) |
| Read-only preview | `check_duplicates` (service :1981; route :740) | POST with a body, no writes |
| Pure grouping, resolution and assignee rules | `service_rules.py` (pure functions plus dataclasses `CandidateInput` and `DuplicateCandidate`); tests in `TESTS\unit\test_procurement_duplicates.py` | no session or I/O in rules |
| Batched master lookups | `PurchasableItemRepository.load` (repository :215); preferred vendor per item in `API\modules\mrp\repository.py:280-308`; `VendorLookupRepository.load` (:266) | one statement per entity type; `dict.fromkeys` dedupe; projections, not entities |
| Locks | `RequisitionRepository.lock_for_update` (:465); `RequisitionLineRepository.lock_many` (ordered by id, :426); `advisory_xact_lock` (`API\core\database.py:206`) | lock first, re-read after, deterministic order |
| New PO status (VENDOR_DECLINED / READY_FOR_DISPATCH) | `PURCHASE_ORDER_LIFECYCLE`; CHECK-widening migration `MIG\20260905_0100_report_snapshots_packing_list_key.py` (drop, then add NOT VALID, then VALIDATE) | update `ck_purchase_orders_status_known`, `PO_LIFECYCLE` (procurement-types.ts:264), `STATUS_VARIANTS` in `WEB\components\shared.tsx`, `TESTS\unit\test_procurement_lifecycle.py` |
| Router shape | `router.py` :927-1008 | thin: bind, call one service, `ApiResponse[...].ok(…, message)`; literal paths before `/{id}` (router docstring :7-11); `require_permission(...)` on every route; `Depends(forbid_vendor_users)` for buyer-only routes |
| Vendor-only dependency | `forbid_vendor_users` (deps.py:319) | mirror it as `require_vendor_user` |
| Vendor scoping | `PurchaseOrderRepository._scoped` (:898); `VendorCategoryRepository._scoped` (:443-454) for category | scope in the repository root, answer "not found" (never "forbidden") |
| Granting permissions to existing roles | `MIG\20260906_0902_grant_item_attribute_permissions.py`; `_seed_roles` refresh (seed.py:193-245) | literal permission strings inside the migration; review docstring covering locking, data, rollback, size, indexes |
| PO PDF | `queue_packing_list` (`API\modules\packing\service.py:2029-2083`); `generate_packing_list` (`API\workers\tasks\reports.py:328-446`); `ReportKey.PACKING_LIST` is document-only (`DOCUMENT_ONLY_REPORT_KEYS`) | 202 with `AcceptedJobResponse`; job row in `report_snapshots`; `Document` row with checksum and audit `DATA_EXPORT` |
| Inbound reports | `REPORT_CATALOGUE` (`API\modules\reports\service.py:538+`), `purchase_pending` repository :519, derive helpers | new `ReportKey` members need a widened `ck_report_snapshots_report_key_is_known` (reports\models.py:205) |
| Delay alerts | `notifications.scan_dispatch_delays` (`API\workers\tasks\notifications.py:60`), `dispatch.service.scan_delays`, `notify(..., group_key=...)` (`API\modules\notifications\service.py:364`) | idempotent sweep, per-tenant nested transaction |
| Frontend list page | `purchase-orders-page.tsx` (`useListQuery` + `DataTable`, URL params) | keys from `queryKeys`; server-side filter, sort and page |
| Frontend command dialog | `purchase-order-detail-page.tsx` `COMMAND_COPY` + `ConfirmDialog` with a reason field (:393-456, :947-989) | `REASON_MIN_LENGTH`/`REASON_MAX_LENGTH` from `lib/constants.ts`; success text from `lib/messages.ts` |
| Frontend form dialog | `purchase-order-form-dialog.tsx` (zod + react-hook-form, `buildPayload`, 409 duplicate dialog) | |
| Frontend tests | `purchase-orders-page.test.tsx`, `requisitions-page.test.tsx` using `renderScreen`, `ok`, `page`, `fail` from `WEB\test\harness.tsx` | |
| Lazy routes and sidebar | `WEB\app\route-loaders.ts`, `WEB\app\navigation.ts`, `WEB\app\router.tsx` `routeGroups` (:249) | `route-loaders.test.ts` requires every sidebar destination to have a loader |

---

## Integration points

- **Requisition:** `_link_source_documents` and `_release_source_lines` keep `ordered_quantity` and the CONVERTED transition correct. Conversion must go through them, not around them.
- **Duplicates:** `find_duplicates_for_lines` (:1909) inside the advisory lock; extend `open_lines_for_duplicate_check` and `CandidateInput` to match `requisition_line_id`.
- **MRP:** `incoming_quantities` (service :3791) nets only `NETTABLE_PO_STATUSES`. Converted DRAFT POs are not supply until APPROVED, which is correct. A vendor changing `expected_delivery_date` moves MRP netting (repository :767-882).
- **Products:** `Item.category_id` (products\models.py:340); hierarchical `ItemCategory.path`/`depth` (:159); `ItemVendorLink` (`is_preferred`, `last_purchase_price`); `Item.gst_rate`.
- **Vendors:** `can_receive_purchase_orders`; `VendorCategory` (qualification and preferred vendor); `Transporter` master (vendors\models.py:397) for structured courier data.
- **Users and auth:** `users.vendor_id` and `allowed_category_ids`, roles via `user_roles`. Effective permissions are computed in `deps._resolve_permissions` (:148-168): role union, plus extra, minus denied. `MeResponse` already carries what a portal shell needs.
- **Quality:**
  - `create_incoming_inspection` (quality\service.py:1466; called from GRN post through `resolve_quality_port` :324);
  - `inspections_for_reference(session, tenant, "goods_receipt", receipt_id)` (:2072) and `vendor_quality_summary` (:2141) for the vendor QC view;
  - closing T7 needs a callback from `decide_inspection` (:1735) to GRN `qc_status` and the PO.
- **Finance:**
  - `VendorInvoice.purchase_order_id`/`goods_receipt_id`/`paid_amount`/status (finance\models.py:487-626; `OPEN_INVOICE_STATUSES` :201) for "payment pending";
  - `create_invoice` takes `vendor_id` from the payload, so a portal wrapper must force `tenant.vendor_id`;
  - nothing moves the PO to INVOICED or PAID.
- **Documents:** `DocumentService.upload` with `entity_type="purchase_order"`, `entity_id`. `entity_type` is free text, so ownership must be checked by the caller.
- **Notifications:** `notify()` with a closed `NotificationType` vocabulary (no DB CHECK); add the members you need, for example PURCHASE_ORDER_SENT, _DECLINED and _SHIPMENT_RECORDED.
- **Reports:** catalogue plus the `report_snapshots` CHECK. **Celery beat** is the place for "not acknowledged in 24h" and "tracking missing".
- **Audit:** `record_audit` and `record_status_change` (via `_transition`) in the same transaction.
- **Numbering:** `DocTypes` in `API\common\codes.py:65-91`.

---

## Traps and constraints

- **T1 — vendor presets open unscoped modules.** dispatch (view, create, update), documents, job_work, quality, finance (vendor invoices, payments, ledger) never read `tenant.vendor_id`. A vendor user can list all customers' consignments and **create** company dispatches (OWNER, DISPATCH_MANAGER), read all vendors' invoices and payments, and create an invoice for any `vendor_id`. `DOCS\security.md:177-190` overstates the isolation.
- **T2 — `POST /procurement/purchase-orders/check-duplicates` leaks other vendors' orders.** It is gated on po.view, which every vendor holds, has no `forbid_vendor_users`, and its query takes `vendor_id` from the payload without vendor scoping. A vendor can read a competitor's open PO codes, quantities, dates and sources.
- **T3 — unvalidated foreign keys** (CLAUDE.md: "A foreign key to a UUID is not a tenant check"):
  - `assigned_vendor_user_id` on create (:2159), update (:2345) and send (:2456);
  - `requisition_line_id` / `quotation_line_id` / `source_id` on PO lines and header;
  - `users.allowed_category_ids`.
- **T4 — vendors see unsent orders.** Visibility of DRAFT/PENDING_APPROVAL orders is asserted by `TESTS\api\test_vendor_portal_scope_http.py:171-190`. Change that test deliberately when you narrow visibility.
- **T5 — UI gate mismatch.** A vendor sees "Submit for approval" on DRAFT (gated on `PURCHASE_ORDER_UPDATE`; the endpoint also has `forbid_vendor_users`). `test_frontend_permission_gates.py` cannot catch this, because it checks only permission names.
- **T6 — VENDOR_DISPATCH_MANAGER cannot record shipments** (no `purchase_order.update`). If you grant `purchase_order.update` instead of a new permission, that role can also acknowledge.
- **T7 — QC_PENDING strands the order.** Nothing moves GRN `qc_status` off PENDING or the PO off QC_PENDING, and QC_PENDING has no other exit. The tracking flag `qc_pending` stays true for ever. INVOICED and PAID are never set either. Do not build portal counts on those statuses; derive "QC rejected" from GRN/QC rows and "payment pending" from vendor invoices. This is the same class as the job-work QC_PENDING trap in CLAUDE.md.
- **T8 — vendors can rewrite the planning date.** `/acknowledge` and `/shipment` let a vendor overwrite `expected_delivery_date`, which MRP nets on. Keep a separate vendor-committed date, or record this as an assumption.
- **T9 — creating several POs in one transaction:**
  - each `create_purchase_order` takes `advisory_xact_lock("po-duplicate", vendor, warehouse)`; sort groups by `(vendor_id, category_id)` so concurrent conversions acquire locks in the same order;
  - lock the requisition header first;
  - no `await` on the database inside a loop for lookups: batch-load items, vendors, links and users before the loop, and bound the number of groups with a named constant (precedents for bounded per-document writes: `_link_source_documents` :2249-2261, `_raise_inspections` :3233);
  - `created_at` ties inside one transaction, so order by `(created_at, id)`.
- **T10 — empty-list filters.** `if xs:` with no `else` inverts a filter. For `allowed_category_ids`, "empty means unrestricted" is the documented contract (deps.py:111-119), so that is fine. For a new restriction such as category-scoped POs, decide explicitly what an empty set means and test it.
- **T11 — enum CHECKs need migrations.** A new PO status changes `ck_purchase_orders_status_known` (naming `ck_%(table_name)s_%(constraint_name)s`, `API\core\models.py:38`), and possibly `OPEN_PO_STATUSES` together with the `ix_purchase_orders_open_vendor` predicate. A new `ReportKey` changes `ck_report_snapshots_report_key_is_known`. Autogenerate does not see CHECK changes. Hand-written CHECK names must be ≤ 63 bytes (`TESTS\unit\test_schema_identifier_length.py`).
- **T12 — state machine meta-tests.** `TESTS\unit\test_procurement_lifecycle.py` runs:
  - `validate()` (no unreachable state, no non-terminal dead end);
  - `HAPPY_PATH`;
  - `test_every_purchase_order_transition_declares_a_permission`;
  - the irreversible-marking test.
- **T13 — meta-tests for new code.** Each of these files is under `TESTS\unit\`:
  - `test_status_badge_catalogue.py`: every `*Status`/`*State` enum member needs a badge colour;
  - `test_frontend_permission_catalogue.py`: `WEB\lib\permissions.ts` must mirror `Permission`;
  - `test_frontend_permission_gates.py`: every POST/PATCH/DELETE call in a feature folder must name the enforced `P.X`, found by parsing `API\modules\*\router.py` `require_permission`;
  - `test_role_presets_reach_roles.py`: every permission belongs to a `PERMISSION_GROUPS` resource (permissions.py:209-241); a new `vendor_portal.*` resource must be added there;
  - `test_frontend_contract_types.py`: register hand-written mirrors, or use `SchemaOf<'…'>`;
  - `test_state_machine_entity_names.py`, `test_entity_names.py`: human-readable entity names;
  - `test_postman_and_contract_targets.py`: `make contract` regenerates `C:\Project\Other\ATCRAFT\openapi.json`, `WEB\types\openapi.ts`, `C:\Project\Other\ATCRAFT\postman_collection.json`; `DOCS\api.md:28` "494 requests" must be updated;
  - `test_e2e_sweep_routes.py`: new screens must be added to `tests\e2e\scripts\*-sweep.mjs`.
- **T14 — reading lazy relationships.** `model_validate(orm_row)` reads `lazy="raise"` relationships. Set committed values before validating, and test a list that contains a real row.
- **T15 — sortable columns.** A column marked sortable in the UI must be in the repository's `sortable_fields`, or sorting answers 422.
- **T16 — never call `session.rollback()` in a service.** And after an expected failure inside `transaction()`, do not read attributes of objects loaded earlier; hold ids in plain variables.
- **T17 — partial unique indexes are checked per statement.** If you add "one primary assignee per (vendor, category)", demote the old row before promoting the new one.
- **T18 — native app auth.** The cookie-only refresh means a native app needs a new token transport. A same-origin PWA does not. Per CLAUDE.md, a new dependency (for example a PWA plugin) must be justified in the PR, and there must be no new services or microservices.
- **T19 — category matching is exact.** Vendor category scoping for items is exact (`Item.category_id IN allowed`, not subtree), while `item_categories` is a hierarchy. Pick one granularity for grouping and assignment and record it as an assumption.
- **T20 — record new rules.** Every new business rule (vendor resolution order, price source, ambiguity handling, vendor visibility) needs an `ASSUMPTION:` comment and a `DOCS\assumptions.md` entry. Today's entries are §16 (duplicate window), §19 (inbound shipment on the PO), §33 (vendor portal scope, :746-768) and open question 5 (:906).

---

## Files a change here will touch

**Backend**
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\models.py`: `PurchaseOrder.item_category_id`, shipment fields or a new `purchase_order_shipments` table, new statuses or flags, `VENDOR_VISIBLE_PO_STATUSES`.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\schemas.py`: conversion request/preview/result; category on the summary and detail; extended `ShipmentUpdate`; vendor decline request.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\service_rules.py`: pure vendor, price, grouping and assignee rules; new transitions; matching on `requisition_line_id`.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\repository.py`: `PurchasableItem.category_id`; preferred-vendor, category-preferred and vendor-assignee projections; `_scoped` category and status; duplicate query.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\service.py`: `_create_purchase_order` refactor, `preview_requisition_conversion`, `convert_requisition_to_orders`, `_assert_vendor_assignee`, send-time default assignee and notify.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\router.py`: new routes; `forbid_vendor_users` on `/check-duplicates`.
- `C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`: new permissions, revised `_VENDOR_BASE` and vendor presets, `PERMISSION_GROUPS`.
- `C:\Project\Other\ATCRAFT\apps\api\src\core\deps.py`: `require_vendor_user`; a pure permission resolver reusable for assignee eligibility.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\users\service.py`: validate `allowed_category_ids` (exists, tenant, subset of the vendor's categories).
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\notifications\models.py`: new `NotificationType` members (and later a PUSH channel).
- New module `C:\Project\Other\ATCRAFT\apps\api\src\modules\vendor_portal\` (`router.py`, `service.py`, `schemas.py`, `repository.py`), registered wherever routers are mounted (`API\main.py` / `API\core\registry.py`).
- PO PDF: `C:\Project\Other\ATCRAFT\apps\api\src\workers\tasks\reports.py` (render function and task); `C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\models.py` (`ReportKey`, document-only set).
- Inbound reports: `C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\{models,repository,service,router,schemas}.py`.
- Alerts: `C:\Project\Other\ATCRAFT\apps\api\src\workers\celery_app.py`, `C:\Project\Other\ATCRAFT\apps\api\src\workers\tasks\notifications.py`.
- Possibly `C:\Project\Other\ATCRAFT\apps\api\src\modules\quality\service.py` (QC decision updating the GRN line and PO; fixes T7) and `C:\Project\Other\ATCRAFT\apps\api\src\modules\finance\service.py` (invoice and payment moving the PO; a vendor-forced create wrapper).
- `C:\Project\Other\ATCRAFT\apps\api\src\core\demo_data\procurement.py` and `masters.py`: demo vendor users and a converted requisition.

**Migrations** (new files in `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\`; `down_revision = "2f5e08c7ba61"`):
- add `purchase_orders.item_category_id` (+ FK, index) and backfill single-category orders;
- shipment columns or table;
- widen `ck_purchase_orders_status_known`;
- widen `ck_report_snapshots_report_key_is_known`;
- grant and revoke vendor-role permissions on system roles.

**Tests**
- `TESTS\unit\test_procurement_requisition_split.py` (new, pure rules)
- `TESTS\unit\test_procurement_duplicates.py`
- `TESTS\unit\test_procurement_lifecycle.py`
- `TESTS\unit\test_procurement_vendor_scope.py`
- `TESTS\api\test_vendor_portal_scope_http.py` (update the DRAFT-visible case; add category and `check-duplicates` cases)
- `TESTS\integration\test_procurement_service.py` (conversion, assignee, concurrency)
- `TESTS\integration\test_users_service.py`
- new `TESTS\api\test_vendor_portal_http.py`
- `TESTS\unit\test_reports_catalogue.py`

**Frontend**
- `WEB\features\procurement\requisitions-page.tsx` (+ `.test.tsx`)
- new `WEB\features\procurement\requisition-conversion-dialog.tsx` (+ test)
- `purchase-order-detail-page.tsx` (send assignee, shipment display, close, PDF)
- `procurement-types.ts`
- new `WEB\features\vendor-portal\*`
- `WEB\app\router.tsx`, `navigation.ts`, `route-loaders.ts`
- `WEB\lib\permissions.ts`, `query-client.ts`, `messages.ts`, `constants.ts`
- `WEB\components\shared.tsx` (badges)
- `WEB\features\admin\user-dialogs.tsx` (vendor category scope)
- generated `WEB\types\openapi.ts`

**Docs**
- `DOCS\assumptions.md` (new sections; revise §19 and §33)
- `DOCS\security.md` §4
- `DOCS\database.md` §2, §7, §8
- `DOCS\api.md` (request count)
- Handbook if screens change

---

## Conclusion (a): design for "convert an approved requisition into one PO per (vendor, item category)"

**Endpoints.** Add both to `procurement\router.py`, after the requisition routes:
- `POST /procurement/purchase-requisitions/{requisition_id}/conversion-preview`: read-only, returns the proposed split.
- `POST /procurement/purchase-requisitions/{requisition_id}/convert-to-orders`: creates the POs.
- Both use `dependencies=[require_permission(Permission.PURCHASE_ORDER_CREATE), Depends(forbid_vendor_users)]`.
- Optional batch form for MRP's single-line requisitions: `POST /procurement/purchase-requisitions/convert-to-orders` with `{requisition_ids: [...]}`. Declare it before any `/{id}` route. Its group key adds `warehouse_id`.

**Schemas** (`extra="forbid"`):

```python
class ConversionLineOverride(BaseModel):
    requisition_line_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    unit_price: NonNegativeUnitPrice | None = None
    quantity: PositiveQuantity | None = None   # default: quantity - ordered_quantity
    exclude: bool = False

class RequisitionConversionRequest(BaseModel):
    lines: list[ConversionLineOverride] = Field(default_factory=list, max_length=MAX_LINES_PER_DOCUMENT)
    po_date: date | None = None
    expected_delivery_date: date | None = None   # default: line.required_date / requisition.required_date
    priority: Priority | None = None             # default: requisition.priority
    delivery_address: str | None = None; terms: str | None = None; notes: str | None = None
    acknowledge_duplicates: bool = False
    duplicate_acknowledgement_reason: str | None = None   # same validator as PurchaseOrderCreate

class ConversionGroupRead(BaseModel):
    vendor_id; vendor_code; vendor_name; item_category_id: uuid.UUID | None; warehouse_id
    assigned_vendor_user_id: uuid.UUID | None; assignee_reason: str
    lines: list[ConversionLineRead]      # requisition_line_id, item_id, quantity, uom_id, unit_price, price_source, vendor_source
    duplicate_candidates: list[DuplicateCandidateRead]
    purchase_order_id: uuid.UUID | None; purchase_order_code: str | None   # filled after convert

class RequisitionConversionResult(BaseModel):
    requisition: RequisitionSummary
    groups: list[ConversionGroupRead]
    unresolved_lines: list[UnresolvedLineRead]   # requisition_line_id + reason
```

**Pure rules** (`service_rules.py`, unit-tested without a database):
- `resolve_line_vendor(override, suggested_vendor_id, preferred_item_vendor, preferred_category_vendor) -> (vendor_id | None, source)`. Order: request override, then `suggested_vendor_id`, then the item's preferred `ItemVendorLink`, then the category's preferred `VendorCategory`.
- `resolve_line_price(override, estimated_unit_price, last_purchase_price_for_vendor) -> (price | None, source)`. Never `standard_cost`; that is a cost, not a vendor price.
- `group_lines(lines) -> list[((vendor_id, category_id), lines)]`, sorted by `(str(vendor_id), str(category_id or ""))`.
- `choose_vendor_assignee(candidates, category_id) -> (user_id | None, reason)`. Eligible candidates: active, not deleted, `vendor_id` matches, and effective permissions include the vendor PO-respond right. Then:
  1. if exactly one candidate has `category_id ∈ allowed_category_ids`, assign them;
  2. if several do, leave the group unassigned with reason "ambiguous" (ASSUMPTION; the buyer picks at Send);
  3. if none do, try unrestricted users (empty `allowed_category_ids`); assign if there is exactly one;
  4. otherwise leave it unassigned.

  A NULL category matches unrestricted users only.

**Repository additions** (`procurement\repository.py`; one statement each):
- Add `Item.category_id` to `PurchasableItemRepository.load` and the `PurchasableItem` dataclass.
- `ItemVendorLinkRepository.purchasing_terms(item_ids, vendor_ids)`: the preferred link per item, and `last_purchase_price` per (item, vendor). Copy the shape of `mrp\repository.py:280-308`.
- `VendorCategoryLookupRepository.preferred_for(category_ids)` and `qualified_pairs(vendor_ids, category_ids)`.
- `VendorAssigneeRepository.candidates(vendor_ids)`: users joined to `user_roles` and `roles` (permissions, active flags), plus `extra_permissions`/`denied_permissions`. Fold to effective permissions in Python with the resolver extracted from `deps._resolve_permissions`.

**Service** (`convert_requisition_to_orders(session, tenant, principal, requisition_id, payload)`), all inside one `async with transaction(session)`:
1. `RequisitionRepository.lock_for_update`. Refuse unless the status is in `OPEN_REQUISITION_STATUSES`.
2. Lock the lines with `RequisitionLineRepository.lock_many` (id order). Compute `outstanding = quantity - ordered_quantity`. Drop lines that are CANCELLED, fully ordered or excluded.
3. Batch-load: items (assert purchasable), vendor facts, purchasing terms, preferred category vendors, qualifications, assignee candidates.
4. Resolve vendor and price for every line. If any line is unresolved, refuse the whole conversion with a `BusinessRuleError` listing the lines (conservative; ASSUMPTION). Refuse vendors that are not orderable. Optionally refuse a vendor with no `VendorCategory` row for the line's category, behind a named constant `REQUIRE_CATEGORY_QUALIFICATION` (ASSUMPTION).
5. Group and sort (T9). Bound the groups with `MAX_ORDERS_PER_CONVERSION` (named constant).
6. For each group, build `PurchaseOrderCreate(vendor_id, warehouse_id=requisition.warehouse_id, source_type=PURCHASE_REQUISITION, source_id=requisition.id, assigned_vendor_user_id=resolved, priority, dates, lines=[PurchaseOrderLineIn(line_number=i, item_id, quantity, uom_id=req_line.uom_id, unit_price, required_date=req_line.required_date, requisition_line_id=req_line.id)])`. Call the refactored `_create_purchase_order(..., vendor=..., items=...)`, which:
   - takes the advisory lock;
   - runs the duplicate check (409 with candidates unless acknowledged with a reason and po.approve);
   - numbers the order, inserts it as DRAFT, and sets `item_category_id`;
   - calls `_link_source_documents`. The first group moves the requisition to CONVERTED; later groups see CONVERTED and skip it;
   - writes audit CREATE per PO.
7. Write one audit row on the requisition: "Split into PO-…, PO-…", with each group's `(vendor, category, assignee)` in `context`.
8. Return the result.

The POs stay DRAFT, so they still go through submit → approve → send. MRP keeps counting the shortage until approval, which is correct.

**Assignment at Send.** `transition_purchase_order(SENT)`:
- validates any `assigned_vendor_user_id` through `_assert_vendor_assignee`: the user exists in this tenant, has the same `vendor_id`, is active, and has a category match or is unrestricted;
- when none is given, defaults it with `choose_vendor_assignee` over the PO's `item_category_id`;
- calls `notify()` for the assignee (new `NotificationType.PURCHASE_ORDER_SENT`; email channel by preference).

Apply the same validation in `create_purchase_order` and `update_purchase_order` (closes T3).

**PO category column.** `purchase_orders.item_category_id` (nullable FK to `item_categories`, RESTRICT), with `Index("ix_purchase_orders_vendor_category", "company_id", "vendor_id", "item_category_id")`. It is always derived from the lines (one distinct category gives that category, otherwise NULL) on create, update and convert, and never accepted from the client. Backfill it in the same migration (one UPDATE over `purchase_orders`; document locking and rollback).

**Vendor-side visibility** (`PurchaseOrderRepository._scoped`):
- add `status IN VENDOR_VISIBLE_PO_STATUSES` (SENT and later, plus CANCELLED with `sent_at` set);
- when `allowed_category_ids` is non-empty, add `item_category_id IN allowed OR assigned_vendor_user_id = tenant.user_id`;
- update `TESTS\api\test_vendor_portal_scope_http.py:171-190`.

**Duplicates.** Add a `requisition_line_ids` argument to `open_lines_for_duplicate_check` as an extra OR branch. Add `requisition_line_id` to `CandidateInput`; a match on it is EXACT ("same pending PR").

**UI:**
- `requisitions-page.tsx` row action "Raise purchase orders", shown when the requisition is APPROVED or CONVERTED with open lines and `can(P.PURCHASE_ORDER_CREATE)`.
- New `requisition-conversion-dialog.tsx`:
  - calls the preview;
  - shows one card per (vendor, category) with lines, prices, sources, assignee and duplicate warnings;
  - allows per-line vendor and price override (`VendorPicker`; buyers hold `vendor.view`);
  - confirms with the convert call;
  - on 409 reuses the `PurchaseOrderDuplicateDialog` presentation;
  - on success, toasts the PO codes and invalidates `queryKeys.procurement.all`.
- PO detail: Send opens a dialog with an assignee picker. It needs a buyer-readable list of vendor users, for example `GET /procurement/purchase-orders/{id}/vendor-assignees` under po.send, because buyers lack `user.view`.
- Admin user dialog: an "Item categories" multi-select for vendor accounts, limited to that vendor's `VendorCategory` rows, plus server validation.

**Record in `DOCS\assumptions.md`:**
- vendor resolution order;
- price source and refusal when there is no price;
- grouping by the item's own category, exact rather than ancestor;
- an unresolved line refuses the whole conversion;
- ambiguous assignee means unassigned;
- qualification switch;
- vendors see orders only from SENT.

---

## Conclusion (b): what a mobile-first vendor portal still needs

### P0 prerequisites (security; do these before exposing anything)

1. Remove from the vendor presets, or scope those modules first: `dispatch.view/create/update`, `document.view`, `job_work.view`, `qc.view`, `vendor_invoice.*`, `payment.view`, `ledger.view`. Revoke them from the system vendor roles in a migration.
2. Close `/check-duplicates` to vendor users (T2).
3. Validate `assigned_vendor_user_id`, the requisition/quotation links, and `allowed_category_ids` (T3).
4. Apply the status and category visibility rules in `_scoped`.

### Backend endpoints

All in a new module `API\modules\vendor_portal` under `/api/v1/vendor-portal`. Every route carries `require_permission(...)` plus a new `require_vendor_user` dependency, and `vendor_id` is always taken from `TenantContext`.

| # | Route | Proposed permission | Notes |
|---|---|---|---|
| 1 | GET `/me` | vendor_portal.view | vendor name and code, my categories, my roles |
| 2 | GET `/dashboard` | vendor_portal.view | one grouped statement: pending acceptance (SENT), accepted, ready for dispatch, dispatched/in transit (shipment recorded, not received), delivered (received statuses), payment pending (vendor invoices in `OPEN_INVOICE_STATUSES`), QC rejected (posted GRN lines with rejected_quantity > 0, or failed inspections). Do not rely on QC_COMPLETED/INVOICED/PAID (T7) |
| 3 | GET `/purchase-orders` | view | paginated; status tab, search, `assigned_to_me` filter |
| 4 | GET `/purchase-orders/{id}` | view | header; lines with item code, name, category, qty, uom, rate, tax, total; dates; delivery address (fallback to warehouse address fields, warehouses\models.py:107-115); terms; timeline; documents; receipts and QC; invoice and payment summary |
| 5 | POST `/purchase-orders/{id}/accept` `{committed_delivery_date?, vendor_reference?, note?}` | vendor_portal.respond | SENT→ACKNOWLEDGED; store the vendor-committed date separately (T8) |
| 6 | POST `/purchase-orders/{id}/decline` `{reason}` | respond | new edge SENT→VENDOR_DECLINED, then the buyer cancels or re-sources; releases the requisition quantity on cancel; migration for the status CHECK |
| 7 | POST `/purchase-orders/{id}/delivery-date` `{date, reason}` | respond | SENT, ACKNOWLEDGED or PARTIALLY_RECEIVED; audited; notifies the buyer |
| 8 | POST `/purchase-orders/{id}/ready-for-dispatch` `{ready_on?, packages?, note?}` | vendor_portal.ship | new `ready_for_dispatch_at` (flag, not a status) |
| 9 | POST/GET `/purchase-orders/{id}/shipments` | ship | `{transporter_id? or courier_name, tracking_reference, lr_number, vehicle_number, shipped_at, expected_delivery_date, freight_amount?}`; supports several consignments per PO (assumption §19 says what changes if this is wrong) |
| 10 | GET `/transporters/options` | ship | picker; vendor roles lack `transporter.view` |
| 11 | POST/GET `/purchase-orders/{id}/documents`, GET `/documents/{id}/download` | ship or invoice | multipart with `kind` ∈ {INVOICE, DISPATCH_PHOTO, LR_COPY, DELIVERY_CHALLAN, OTHER}; entity is `purchase_order`; ownership checked |
| 12 | GET `/purchase-orders/{id}/pdf` | view | serve the document generated at SEND, or queue as in the packing list plus a portal job-status route (vendors lack `report.view`) |
| 13 | GET `/purchase-orders/{id}/receipts` | vendor_portal.quality or view | GRN lines (received, accepted, rejected, reason) plus `quality.service.inspections_for_reference` |
| 14 | POST/GET `/invoices`, GET `/invoices/{id}` | vendor_portal.invoice | wraps `finance.create_invoice` with `vendor_id` forced and `purchase_order_id` required; status, `paid_amount`, due date |
| 15 | GET `/payments` | invoice | allocations against own invoices |
| 16 | (later) POST/DELETE `/push-subscriptions` | view | Web Push; new `NotificationChannel.PUSH` and a subscription table |

Internal-side companions:
- `GET /procurement/inbound-tracking`: a paginated board over open POs and shipments with vendor, PO, courier/transporter, tracking id, status, expected date, delay, received status, QC pending, GRN pending, and filters `overdue`, `expected_today`, `in_transit`, `category`.
- `GET /procurement/purchase-orders/{id}/vendor-assignees`.
- `POST /procurement/purchase-orders/{id}/pdf`.
- Report keys: PURCHASE_ORDER_REGISTER (vendor- and category-wise), DELAYED_PURCHASE_ORDERS, VENDOR_ACCEPTANCE, DUPLICATE_PO_OVERRIDES, INBOUND_IN_TRANSIT, INBOUND_EXPECTED_TODAY, INBOUND_LATE, INBOUND_COURIER_WISE, INBOUND_TRANSPORT_COST, VENDOR_INBOUND_HISTORY.
- Beat sweeps for "SENT and not acknowledged within N hours", "ACKNOWLEDGED/ready with no tracking id", and "expected date passed with no GRN", using `notify(group_key=...)`. Raise the existing `PURCHASE_ORDER_DELAYED`.

Permissions: add `vendor_portal.view/respond/ship/invoice/quality` and put the `vendor_portal` resource in `PERMISSION_GROUPS`. Map them as:

| Role | Portal permissions |
|---|---|
| OWNER | all |
| MANAGER | view, respond, ship |
| DISPATCH_MANAGER | view, ship |
| QC_CONTACT | view, quality |
| ACCOUNT_MANAGER | view, invoice |

### Frontend screens (mobile-first, `WEB\features\vendor-portal\`)

- **Shell.** In `router.tsx`, a principal with `user.vendor_id` mounts `VendorPortalLayout` at `/portal/*` (bottom tab bar: Home, Orders, Shipments, Invoices, More), and every internal route redirects there. Internal users never see `/portal`. Add the routes to `route-loaders.ts`, add `queryKeys.vendorPortal.*`, and add strings to `lib/messages.ts`.
- **Pages:**
  - `/portal` dashboard: seven count tiles linking to filtered lists.
  - `/portal/orders`: status chips (To accept, Accepted, Ready, Dispatched, Delivered, All); card list using `useListQuery`.
  - `/portal/orders/:poId`: detail with a sticky action bar (Accept, Decline, Change date, Ready for dispatch, Add shipment, Upload, Download PDF).
  - `/portal/shipments`
  - `/portal/receipts`: GRN and QC results.
  - `/portal/invoices`: list with a payment-status chip, plus a "New invoice against PO" sheet with file upload.
  - `/portal/payments`
  - `/portal/profile`: reuse the password and MFA parts of `features/auth/profile-page.tsx`.
  - `/notifications`: reuse.
- **Sheets and dialogs:** AcceptSheet, DeclineSheet (reason required), DeliveryDateSheet, ReadyForDispatchSheet, ShipmentSheet (transporter picker or courier, tracking, LR, vehicle, date, freight), UploadSheet (`kind` select; `<input type="file" accept="image/*,application/pdf" capture="environment">`).
- **PWA (optional; justify the dependency):** manifest, service worker and Web Push registration.
- **Admin additions:** requisition conversion dialog, Send-with-assignee dialog, shipment details on the PO detail page, `/procurement/inbound-tracking` board page, vendor category scope in the user dialog.
- **Tests:** a `renderScreen` test for every page and dialog. Extend `tests\e2e\scripts\permission-sweep.mjs` with a vendor account, and add new statuses to the badge map.
