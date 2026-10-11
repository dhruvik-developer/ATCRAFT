# WP C1a — Vendor portal backend (with the vendor-isolation security fix)
Lane C (portals), round 2, step 1 of 2 (C1b = Web Push backend follows in the same lane). Placeholder `§C1-n`.

Proposal: Add-on §02–§07 (Vendor Purchase Mobile App: roles, category mapping, PO separation, status flow,
vendor dashboard, PO detail, vendor actions, dispatch tracking, admin dispatch tracking). Audit: "A separate vendor
mobile app, and phone push notifications" absent. The *app* is delivered as a mobile-first installable PWA inside
the same SPA (frontend in round 3, WP C2) — no native store app, no new stack (CLAUDE.md "Things not to do").
Background: `reports/procurement-vendor-portal.md` (all; esp. E7–E12, Traps T1–T20, "Conclusion (b)"),
`reports/dealers-reports-docs.md` §1.3 (vendor pattern and its leaks), `reports/anatomy.md` A2/A13.
Round 1 (WP C0, on main) added: PO `item_category_id`, assignee rule, vendor visibility in `_scoped`
(SENT+ and category subtree), and a named constant for the vendor "respond" right — switch it now.

## Decisions (binding)
### P0 — structural vendor isolation (do this first, with tests)
1. **Allow-list gate.** In `src/core/deps.py`, next to the temporary-password gate in `get_current_principal`:
   a vendor principal (`vendor_id is not None`) may call ONLY paths under `{api_prefix}/vendor-portal/`,
   `{api_prefix}/notifications` (and below), and the auth self-service paths (`/auth/me`, `/auth/logout`,
   `/auth/logout-all`, `/auth/change-password`, `/auth/sessions…`, `/auth/mfa…`). Everything else → 403
   `AuthorizationError(code=SCOPE_DENIED)`. Implement as a pure `vendor_route_allowed(path)` (unit-tested) like
   `password_change_route_allowed`. Note `api_client` in tests overrides `get_current_principal` — test the predicate
   as a unit test **and** the gate through the real dependency chain (pattern: `tests/api/test_route_writes_persist.py`).
2. **Vendor roles** (`VENDOR_OWNER`, `VENDOR_MANAGER`, `VENDOR_DISPATCH_MANAGER`, `VENDOR_QC_CONTACT`,
   `VENDOR_ACCOUNT_MANAGER`): replace their permission sets with the new `vendor_portal.*` rights +
   `notification.view` only. New permissions (group "Vendor Portal", resource `vendor_portal`; names avoid the
   `.view` suffix so AUDITOR's VIEW_ONLY does not absorb them):
   `vendor_portal.access`, `vendor_portal.respond`, `vendor_portal.ship`, `vendor_portal.invoice`, `vendor_portal.quality`.
   Mapping: OWNER all; MANAGER access+respond+ship; DISPATCH_MANAGER access+ship; QC_CONTACT access+quality;
   ACCOUNT_MANAGER access+invoice. Migration: set these five system roles' `permissions` arrays to exactly the new
   sets (literal strings; downgrade restores the old literal arrays) and grant the new rights to SUPER_ADMIN/ADMIN.
   Update `test_role_presets_reach_roles.py` expectations deliberately; switch C0's "respond right" constant to
   `vendor_portal.respond`.
3. Rewrite `tests/api/test_vendor_portal_scope_http.py` deliberately: vendors now get 403 on `/procurement/**`,
   `/dispatch/**`, `/finance/**`, `/documents/**`, `/job-work/**`, `/quality/**`; their PO access is via `/vendor-portal`.
   Keep the internal PO acknowledge/shipment routes for **staff** (a buyer may record on a vendor's behalf).
4. `notifications`: add a vendor-scoped resolver `user_ids_for_vendor(vendor_id, *, permission=None)` (active vendor
   users of that vendor, optionally holding a right); never use `notify_role` with vendor role codes again.
5. `docs/security.md` §4 rewritten: vendor isolation is a route allow-list + portal-only endpoints scoped by
   `tenant.vendor_id` (no opt-in per module). Fix the overstated claim.

### Vendor portal module `src/modules/vendor_portal` (prefix `/vendor-portal`, tag "Vendor Portal")
Downstream module (imports procurement, finance, quality, documents, vendors services — check
`docs/architecture.md` dependency direction and update it). Every route: `require_permission(Permission.VENDOR_PORTAL_X)`
literally + a `require_vendor_user` dependency (403 SCOPE_DENIED for staff — ADMIN holds every permission).
`vendor_id` always from `TenantContext`; ownership checks answer 404 for foreign rows.
Endpoints (paginate every list):
| Route | Right | Notes |
|---|---|---|
| GET `/me` | access | vendor code/name, my roles, my categories (codes+names), contact, unread count hint |
| GET `/dashboard` | access | ONE grouped statement: to-accept (SENT), accepted (ACKNOWLEDGED, not ready), ready-for-dispatch, in-transit (shipment recorded, not fully received), delivered/received (receipt statuses), payment pending (own vendor invoices in open statuses with balance>0), QC rejected (own GRN lines with rejected_quantity>0 in last N days, `VENDOR_QC_LOOKBACK_DAYS`). Never rely on QC_COMPLETED/INVOICED/PAID statuses (T7). |
| GET `/purchase-orders` | access | status tab filter (to_accept, accepted, ready, dispatched, delivered, all), search, `assigned_to_me`; row: code, date, category, item count, total, expected/committed date, status, flags |
| GET `/purchase-orders/{id}` | access | header (delivery address: PO text else warehouse address), lines (item code, name, category, qty, uom, rate, tax, total, received/accepted/rejected), dates, terms, timeline (status audit), shipments, documents, receipts+QC summary, invoice/payment summary |
| POST `/purchase-orders/{id}/accept` `{committed_delivery_date?, vendor_reference?, note?}` | respond | SENT→ACKNOWLEDGED via the PO machine; store `vendor_committed_date` (NEW column, separate from buyer's `expected_delivery_date` — T8, `§C1-1`) + `vendor_reference`; audit |
| POST `/purchase-orders/{id}/decline` `{reason}` | respond | NEW status `VENDOR_DECLINED` (SENT→VENDOR_DECLINED; buyer then `VENDOR_DECLINED→CANCELLED` (releases requisition qty via existing path) or `VENDOR_DECLINED→SENT` re-send/re-assign). CHECK-swap migration for `ck_purchase_orders_status_known` with literal list; update `OPEN_PO_STATUSES`/partial-index predicates only if needed (document); lifecycle tests; badge colour |
| POST `/purchase-orders/{id}/delivery-date` `{date, reason}` | respond | ACKNOWLEDGED/PARTIALLY_RECEIVED; updates `vendor_committed_date` only; audit; notify buyer |
| POST `/purchase-orders/{id}/ready-for-dispatch` `{ready_on?, packages?, note?}` | ship | sets `ready_for_dispatch_at`, `ready_packages`, note (flag, not a status) |
| GET/POST `/purchase-orders/{id}/shipments` | ship (POST) / access (GET) | NEW table `purchase_order_shipments` (TenantEntity): po_id, `shipment_number` (1..n per PO, unique), `transporter_id` (FK transporters, tenant-validated) or `courier_name`, `tracking_reference`, `lr_number`, `vehicle_number`, `driver_name`, `driver_mobile`, `shipped_at`, `expected_delivery_date`, `packages`, `weight_kg` (Quantity), `freight_amount` (Money), `notes`. Allowed on ACKNOWLEDGED/PARTIALLY_RECEIVED (and APPROVED/SENT? — no: vendor must accept first, `§C1-2`). Keep the PO's legacy `courier_name/tracking_reference/shipped_at` updated from the latest shipment for back-compat. |
| GET `/transporters/options` | ship | active transporters (id, code, name) |
| POST `/purchase-orders/{id}/documents` (multipart: file, `kind` ∈ INVOICE, DISPATCH_PHOTO, LR_COPY, DELIVERY_CHALLAN, OTHER, optional shipment_id) | ship (or invoice for INVOICE kind) | via `DocumentService.upload` with `entity_type="purchase_order"` + entity_id; persist `kind` (new nullable `documents.document_kind` String(32) with CHECK of literals, or a portal link table — choose the least invasive; document it) |
| GET `/purchase-orders/{id}/documents`, GET `/documents/{document_id}/download` | access | only documents attached to the vendor's own POs (and the PO PDF) |
| POST `/purchase-orders/{id}/pdf` → 202 ; GET `/jobs/{job_id}` | access | PO PDF (below); portal job-status route because vendors lack `report.view` (only own jobs) |
| GET `/receipts` and `/purchase-orders/{id}/receipts` | quality or access | own posted GRN lines (received/accepted/rejected, reasons) + QC results via `quality.service.inspections_for_reference` (pull model) |
| GET/POST `/invoices`, GET `/invoices/{id}` | invoice | wraps `finance.create_invoice` with `vendor_id` forced from tenant, `purchase_order_id` REQUIRED and owned; optional invoice file (document kind INVOICE); list own invoices (status, total, paid, due, balance) — add vendor scoping in the finance call path (not by trusting payload). The portal payload has **no `cost_centre_id`** on header or lines (B1 added those to the staff schema; a vendor cannot list centres and must not name one) — vendor-raised bills are stored with null centres and staff tag them while reviewing; test that an extra `cost_centre_id` key is rejected (extra="forbid") |
| GET `/payments` | invoice | own approved payments + allocations |
| GET `/payments/summary` | invoice | vendor payment dashboard (proposal "Vendor payment dashboard"): totals in one statement — outstanding (open invoice balance), overdue (past due date), due in next 7 days, paid this month, last payment date/amount; per currency |
| GET `/rating` | access | the vendor's own rating as computed by the existing vendor-rating service (on-time %, rejection %, score, sample size, period) — proposal "Vendor rating" visible to the vendor |

### Tracking status vocabulary (proposal Add-on §07, binding — exact values)
The proposal's admin tracking statuses are: **Not dispatched, Ready for dispatch, Dispatched, In transit, Delivered,
Received at warehouse, QC pending, GRN completed**. Implement them as a derived, read-only `InboundTrackingStatus`
StrEnum (`NOT_DISPATCHED, READY_FOR_DISPATCH, DISPATCHED, IN_TRANSIT, DELIVERED, RECEIVED, QC_PENDING,
GRN_COMPLETED`) computed in ONE pure function from PO + latest shipment + receipts + inspections, used by the inbound
board, the vendor portal PO list/detail and the dashboard buckets (so the three never disagree). Definitions
(`§C1-n`): NOT_DISPATCHED = accepted, not ready; READY_FOR_DISPATCH = `ready_for_dispatch_at` set, no shipment;
DISPATCHED = shipment recorded today/without transit evidence; IN_TRANSIT = shipment recorded and not delivered;
DELIVERED = shipment `delivered_at` set (new column; set by the vendor via
`POST /vendor-portal/purchase-orders/{id}/shipments/{shipment_id}/delivered` (ship right) or by staff via
`POST /procurement/purchase-orders/{id}/shipments/{shipment_id}/delivered` (`grn.create`)), no GRN yet;
RECEIVED = a GRN exists (draft or posted) for goods after the shipment; QC_PENDING = posted GRN lines with open
inspections; GRN_COMPLETED = all received lines decided and the PO fully received (or closed). Add every value to
`STATUS_VARIANTS`. Also add a **"Delivery expected date"** column everywhere tracking is shown, and make
shipment creation notify the buyer immediately (proposal: "admin is notified immediately") — with courier,
tracking ID, dispatch date, expected date, boxes and weight in the notification body.

Internal (staff) companions in procurement router (no vendor access — the gate blocks it anyway):
- `GET /procurement/inbound-tracking` (`purchase_order.view`): paginated board over open POs + shipments: vendor,
  PO, category, assignee, status, expected date, vendor committed date, ready flag, latest shipment (courier/transporter,
  tracking, LR, shipped_at), days late (one shared "PO late" predicate reused by list filter and alerts — pick
  `order_tracking.is_delayed` semantics: receivable status with open lines and expected date passed), received %,
  GRN pending, QC pending; filters `overdue`, `expected_today`, `in_transit`, `not_acknowledged`, `vendor_id`,
  `category_id`, search. Frontend comes in round 3.
- `GET /procurement/purchase-orders/{id}/shipments`, `POST /procurement/purchase-orders/{id}/accept-vendor-date`
  (`purchase_order.update`; copies `vendor_committed_date` → `expected_delivery_date`, audited).
- `POST /procurement/purchase-orders/{id}/pdf` → 202 (`purchase_order.view`).
- PO state-machine edges for VENDOR_DECLINED handled by `transition_purchase_order` for staff (cancel / re-send).

### Additions from the PDF traceability audit (binding)

Close these: A06.13, A06.EX and A01.A14 (inbound side). Read each ID in `trace/trace.json`.

1. **Staff can record a full shipment on a vendor's behalf.** Many vendors will phone the details in.
   - Add `POST /procurement/purchase-orders/{id}/shipments` and `PATCH .../shipments/{shipment_id}` (`purchase_order.update` + `forbid_vendor_users`).
   - They take the same fields and validation as the portal route, and share one service function with it.
   - Staff can also upload typed documents (LR copy, dispatch photo, delivery challan, invoice) against a PO or shipment: `POST /procurement/purchase-orders/{id}/documents` (`document.upload`).
2. **Staff see vendor-uploaded documents.** Add `GET /procurement/purchase-orders/{id}/documents` (`purchase_order.view`), listing documents by kind with download through the normal documents route. Also add a `documents_count` / `has_lr_copy` flag on the inbound board rows.
3. **Notification recipients.**
   - Shipment recorded, shipment edited, delivered marked, and LR copy or dispatch photo uploaded all notify: the PO creator, plus `user_ids_with_permission(purchase_order.approve)`, plus ADMIN-role users.
   - Add a small permission-based resolver if none exists. Exclude vendor and dealer users.
   - Collapse with `group_key=f"po-shipment:{shipment_id}"`, so one consignment gives one updating notification, for example "Shipment 2 for PO-…: Blue Dart · 123456789 · 5 boxes · 70 kg · LR copy uploaded".
   - Use the same group for later tracking updates (edits, delivered, and D3 courier-API events), so the admin sees each change immediately, as the proposal's example says.

### PO PDF
`ReportKey.PURCHASE_ORDER` (document-only; CHECK-swap migration listing ALL keys literally), task
`reports.generate_purchase_order`, pure `render_purchase_order_pdf(data)` reusing the packing-list helpers: company
block, vendor block (GSTIN), PO number/date/status, delivery address, lines (item code, vendor item code if a link
exists, description, HSN, qty, UoM, rate, discount, tax, total), totals in INR with Indian grouping (reuse/promote the
helpers WP A2 adds in the commercial lane if already on main; otherwise write small local ones and note it for the
integrator), terms, "Authorised signatory". Document entity `purchase_order`, never expires.

### Vendor-facing and vendor-action notifications (events)
Add `NotificationType` members as needed (`PURCHASE_ORDER_SENT`, `PURCHASE_ORDER_ACKNOWLEDGED`,
`PURCHASE_ORDER_DECLINED`, `PURCHASE_ORDER_DATE_CHANGED`, `PURCHASE_ORDER_READY`, `PURCHASE_ORDER_SHIPPED`,
`GOODS_RECEIPT_POSTED` (exists), `QC_FAILED` (exists), `VENDOR_INVOICE_STATUS`, `VENDOR_PAYMENT_RECORDED`) and mirror
them in `features/notifications/notification-types.ts`. Raise, in the same transaction as the event:
- PO SENT → the assignee (or, if unassigned, eligible vendor users of that vendor via the new resolver); deep link
  `/portal/vendor/orders/{id}` (the vendor app routes come in C2 — agree on that path now: vendor app lives under
  `/vendor/*` — use `/vendor/orders/{id}`).
- Vendor accept / decline / date change / ready / shipment → the PO's `created_by_id` + PURCHASE_MANAGER role users
  (internal), link `/procurement/purchase-orders/{id}`.
- GRN posted on a vendor's PO → that vendor's users with `vendor_portal.access` ("Material received"); QC rejection on
  that vendor's goods → vendor users with `vendor_portal.quality` (in addition to the existing internal QC_FAILED).
- Vendor invoice approved/disputed and payment approved/allocated against their invoice → vendor users with
  `vendor_portal.invoice`.
Every `action_url` must be a real SPA route (internal) or a `/vendor/...` route (vendor app, built in C2).
Leave internal-only events (new requisition, PO approved/rejected internally, job work, sales, dispatch…) to WP B4.

### Demo data
Use C0's demo vendor users; add one SENT PO assigned to each demo manager, one ACKNOWLEDGED with a shipment, one
declined, one delivered with a GRN, and a vendor invoice against one — so the vendor app demo has every tab populated.
Idempotent by code.

### Docs
database.md (§2 vendor_portal + shipments table, §7 PO machine incl. VENDOR_DECLINED, §8 events), assumptions
(`§C1-n`: vendor committed date separate from planning date, ship only after accept, document kinds, dashboard
definitions, vendors see SENT+), security.md §4, architecture.md (new module), api.md, handbook (staff side: inbound
tracking described; vendor app chapter comes with C2).

## Tests (minimum)
Unit: `vendor_route_allowed`; PO lifecycle with VENDOR_DECLINED (validate, edges); dashboard bucket rules (pure where
possible); render PO PDF. Integration: accept stores committed date and does NOT change expected date; decline →
staff cancel releases requisition qty; delivery-date change; ready flag; shipments (numbering, transporter tenant
check, foreign PO 404); documents (own only; kind stored; download of foreign doc 404); receipts/QC view; invoice
submit forces vendor + requires own PO; payments list own only; dashboard counts on seeded data; inbound tracking board
filters. API (real dependency chain): vendor gets 403 on staff routes (procurement, dispatch, finance, documents,
job-work, quality, reports, users) and 200 on portal routes; staff get 403 SCOPE_DENIED on portal routes; vendor A
cannot see vendor B's PO/docs/invoices (404). Notifications: PO sent notifies the assignee only (not other vendors).
