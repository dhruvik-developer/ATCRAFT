# WP B4: Smart alerts (timed) and internal event notifications

Lane B (operations), round 4. Placeholder prefix: `§B4-n`.

**What the proposal promised.**
- Add-on §08 "Smart Alerts": PO not accepted within 24 hours; vendor delivery date missed; dispatch not updated; tracking ID missing; material received but QC pending; QC rejected; GRN not created; vendor invoice pending; payment overdue.
- §01 "Admin panel notifications": new purchase request; new vendor order; PO approved; PO rejected; material received; QC pending; QC rejected; job work material sent; job work material received; sales order created; production shortage; stock reallocation request; dispatch created; courier tracking updated; payment pending; vendor delay.

**What the audit found.** "Timed alerts such as 'PO not accepted within 24 hours', 'tracking ID missing', and 'GRN not created'" are absent.

**Background.** Read `reports/notifications-alerts.md` in full, and follow "Proposed design (a)".

**Already on main:**
- C1a (vendor resolver `user_ids_for_vendor`, PO shipments table, `vendor_committed_date`, vendor-facing event notifications; do NOT duplicate those);
- C1b (the push channel);
- A4 (collections owns **customer** overdue reminders; do NOT alert on customer receivables here);
- A2 and A3 (invoices).

## Decisions (binding)

### 1. Engine, per design (a)

**Registry.** `notifications/alerts.py` holds the registry. It defines:
- `AlertRuleKey`;
- `AlertRule(key, notification_type, severity, default_threshold, default_remind_every, granularity ENTITY|DIGEST, internal_permission | internal_roles, vendor_permission, notify_creator)`;
- `AlertCandidate`;
- the reconcile logic.

`alerts.py` imports nothing from other modules. Scanners live in the owning modules and are wired up by the worker adapter `src/workers/tasks/alerts.py`, task `notifications.scan_smart_alerts`, beat every `settings.alerts_scan_interval_seconds` (900), `expires` 600, plus `settings.alerts_enabled`. The settings go in `.env.example` and compose.

**Config table `notification_alert_rules`.** Sparse, per company: `rule_key`, `is_enabled`, `threshold_minutes`, `remind_every_minutes`. It has `uq(company_id, rule_key)` and the CHECKs from the design.

Routes (audited CONFIG_CHANGE):
- `GET /notifications/alert-rules` (`settings.view`) returns the full effective matrix with defaults filled in.
- `PUT /notifications/alert-rules` (`settings.manage`) takes partial upserts.

**State table `notification_alert_states`.**
- Columns: `rule_key`, `entity_type`, `entity_id`, `first_detected_at`, `last_detected_at`, `last_notified_at`, `notified_count`, `resolved_at`.
- Partial unique index on `(company_id, rule_key, entity_id) WHERE resolved_at IS NULL`. The **same predicate** must be passed as `index_where`.

**Reconcile per (company, rule).**
1. Load the open episodes.
2. Compute new, still and gone.
3. Resolve gone in **one** UPDATE. An empty set must be handled with an explicit branch.
4. Insert new with `ON CONFLICT DO NOTHING`.
5. Due episodes = new ∪ reminder-due.
6. A quiet tick writes **nothing**: no notify, no audit.
7. Notify:
   - ENTITY granularity: one notification per entity, `group_key = f"alert:{rule}:{entity_id}"`.
   - DIGEST granularity: one per company, "N purchase orders not acknowledged".
8. Update `last_notified_at` and `notified_count` in one UPDATE.
9. Write one audit row per company per rule, and only when something was notified.

The per-company `try_advisory_xact_lock("smart-alerts")` and per-rule try/except isolate failures.

**Scanners.** Each scanner is tenant-scoped, takes `(session, tenant, *, as_of, threshold, limit=500)`, is bounded **per company**, and reuses the owning module's own predicates. One predicate shared with the screens means the screen and the alert cannot disagree.

### 2. Rules, defaults and recipients

Each default carries an ASSUMPTION comment and a `§B4-n` entry.

| Rule | Detect | Default threshold / remind | Recipients | Link |
|---|---|---|---|---|
| PO_NOT_ACKNOWLEDGED | status SENT and `acknowledged_at` IS NULL and `sent_at <= now − threshold` | 24 h / 24 h | PO creator + `purchase_order.approve` holders; vendor: assignee (or vendor users with `vendor_portal.respond`) | PO detail / `/vendor/orders/{id}` |
| PO_DELIVERY_MISSED | the shared "PO late" predicate from C1a's inbound board (receivable status, open lines, expected date passed) | 0 days / 24 h | creator + purchase roles; vendor assignee | inbound board `?overdue=1` |
| PO_DISPATCH_NOT_UPDATED | ACKNOWLEDGED (or ready) with **no shipment** and (committed or expected) date − threshold ≤ today | 1 day before / 24 h | vendor users with `vendor_portal.ship` + PO creator | PO |
| PO_TRACKING_MISSING | a shipment with neither `tracking_reference` nor `lr_number`, older than the threshold | 4 h / 24 h | vendor users with ship right + PO creator | PO |
| GRN_NOT_CREATED | a shipment whose `expected_delivery_date + threshold` has passed while the PO still has open lines and no GRN posted after `shipped_at` | 2 days / 24 h | users holding `grn.create` (store) + PO creator | inbound board |
| QC_PENDING | open inspections (PENDING/IN_PROGRESS) older than the threshold (index `ix_qc_inspections_open_queue`) | 24 h / 24 h | `qc.inspect` holders | `/quality?tab=pending` (check the real route) |
| VENDOR_INVOICE_PENDING | PO fully received (RECEIVED or later receipt statuses) for longer than the threshold with no non-cancelled vendor invoice linked | 7 days / 7 days | `vendor_invoice.create` holders; vendor users with `vendor_portal.invoice` | PO / vendor invoices |
| VENDOR_PAYMENT_OVERDUE | vendor invoices past `due_date`, unpaid balance, payable status | 0 days / 3 days | `payment.approve` holders (internal only; never tell a vendor "we are late") | `/finance/vendor-invoices?overdue_only=...` (check) |
| JOB_WORK_OVERDUE | `job_work.service.pending_at_vendor(overdue_only=True)` | 0 days / 24 h | `job_work.issue` holders + order creator | job-work page |
| DISPATCH_DELAYED | **move the existing sweep onto the engine** | as today / 24 h | as today | `/dispatch?tab=tracking&delayed_only=true` |

When DISPATCH_DELAYED moves onto the engine:
- Fix its re-fire-every-hour defect (episodes now dedupe).
- Fix its non-existent link.
- Fix the wrong beat comment.
- Remove the old beat entry, or keep a thin wrapper, so nothing double-alerts.
- Keep `dispatches.delay_notified_at` written, or drop the dead column with a migration. Document which.

"QC rejected" stays an event (it exists; C1a added the vendor side). Customer "payment overdue" belongs to A4, so **do not** add it here.

### 3. Recipient resolution

- Add `user_ids_with_permission(session, tenant, permission, *, limit=MAX_ROLE_RECIPIENTS)`. It returns active internal users (`vendor_id IS NULL`, and after C3 also `customer_id IS NULL` if that column exists) whose effective permissions include the right: role union + extra − denied, folded in SQL or batched in Python, never one query per user.
- Use it instead of hard-coded role codes for new events. Existing `notify_role` callers may stay.
- Use C1a's `user_ids_for_vendor`.

### 4. Internal event notifications

These are raised in the same transaction as the event, with **links to real pages**. Skip any C1a already raises.

| Event | Recipients |
|---|---|
| requisition submitted ("New purchase request") | `purchase_requisition.approve` holders |
| requisition approved/rejected | requester |
| PO submitted ("approval request") | `purchase_order.approve` holders |
| PO approved/rejected | creator |
| GRN posted ("Material received") | PO creator + `qc.inspect` holders when QC is needed |
| QC inspection created ("QC pending") | `qc.inspect` holders, DIGEST via group_key per stage |
| job-work challan issued ("Job work material sent") | order creator + `job_work.receive` holders |
| job-work receipt ("material received") | order creator |
| sales order created | customer's `assigned_sales_user_id`, else `sales_order.approve` holders. Mark portal-sourced orders "New order request from <dealer>" once C3 lands; if `sales_orders.source` exists, use it |
| MRP run completed with shortages ("Production shortage") | the run's requester + `mrp.run` holders |
| production release blocked by shortage | the releaser |
| reallocation request created ("Stock reallocation request") | `allocation.approve` holders. The approval notification already exists; fix its broken link |
| dispatch created / dispatched ("Dispatch created") | order's assigned sales user |
| outbound dispatch tracking event ("Courier tracking updated") | order's assigned sales user; group per dispatch |
| payment created pending approval ("Payment pending") | `payment.approve` holders |

Add the `NotificationType` members needed, and mirror them in `features/notifications/notification-types.ts`.

**New guard test `tests/unit/test_frontend_notification_types.py`.** The TS list must equal the enum, in both directions.

### 5. Link fixes and guard

Fix the three broken `action_url`s the report found (dispatch delayed, allocation reservations, `REPORT_READY`, which points at an API path). For report ready, use a real page: open `/reports` with the job panel, or `/documents` filtered to the document. Make the page handle it.

**New guard test `tests/unit/test_notification_action_urls.py`.** It collects every `action_url` literal or f-string prefix used in `src/modules/**` notify calls and the alert registry, then asserts each resolves to a route declared in `apps/web/src/app/route-loaders.ts`. This includes `/vendor/...` and later `/dealer/...`.

### 5b. Additions from the PDF traceability audit (binding; formerly WP E11)

Close these IDs: A01.A7, A01.A13, A01.A14 (outbound), A01.M10, A08.6, A08.8, A10.M1, P02.U3. Read each in `trace/trace.json`.

**Approval inbox: make it real (P02.U3, A01.M10).**
- `approval_requests` exists, but nothing writes it, so the dashboard "Needs your attention" panel and `GET /audit/approvals` are always empty.
- Add one helper, `audit.service.open_approval(session, *, company_id, entity_type, entity_id, entity_code, approve_permission, requested_by, summary, amount=None)`, and `close_approval(..., outcome)`. Both are written in the **same transaction** as the document's transition.
- Call them from **every** submit-for-approval and decision path:
  - requisition, PO, BOM, production order, below-cost / approval-required sales order;
  - stock adjustment needing approval, internal issue (E8a), reallocation request, vendor invoice;
  - payment, job-work order (if it has an approval step), vendor master (if it has one).
- Grep for every `PENDING_APPROVAL`-style state and every `.approve` route, and list them in the test.
- `notify_approval_pending` sends `APPROVAL_PENDING` to `user_ids_with_permission(approve_permission)`, excluding the requester. The decision sends `APPROVAL_GRANTED` / `APPROVAL_REJECTED` to the requester.
- Add a new **`/approvals` page** (Administration or Overview nav; gate: any `.approve` right) listing open approvals the user may decide, with deep links to each document. Decisions happen on the document's own screen, which keeps one decision path. The dashboard panel reads the same data.
- Add a guard test: every route whose permission ends in `.approve` either opens/closes approvals or is listed in an explicit, justified exemption list.

**Configurable approval thresholds** (P02.U3 rules): `company_approval_settings`, a sparse per-company table with code defaults, for:
- PO value above which a second approver role is required (`po_dual_approval_amount`);
- the adjustment value threshold (replacing the hard-coded 25 000 constant via the setting, keeping the same default);
- the internal-issue threshold;
- auto-approve below a limit for requisitions.

Routes are `GET/PUT /audit/approval-settings` (`settings.view` / `settings.manage`). Add a tab on `/admin/alerts` or its own admin tab. The services read the settings through one cached-per-request helper; there is no cache across requests.

**Link and recipient fixes.**
- **QC failed** (A01.A7 / A08.6): add an `inspection` query parameter to `quality-page.tsx` that opens that inspection, and set `action_url=f"/quality?tab=inspections&inspection={id}"` in `_announce_failure`. The action-url guard test also fails when a notify call sets `entity_id` but no `action_url`.
- **Dispatch created / courier tracking updated** (A01.A13/A14, outbound):
  - when the customer has no assigned sales user, fall back to `user_ids_with_permission(dispatch.view)` restricted to internal users;
  - JOB_WORK dispatches notify the job-work order creator;
  - PURCHASE_RETURN dispatches notify `purchase_order.approve` holders;
  - link to `/dispatch?tab=tracking`.
- **Vendor invoice pending** (A08.8): detect per posted GRN, not per fully received PO. A GRN posted longer ago than the threshold with no non-cancelled vendor invoice covering it raises the alert, grouped per PO.

**Unraised notification types.** `PRODUCTION_ORDER_DELAYED`, `SALES_ORDER_AT_RISK` and `STOCK_BELOW_REORDER` are offered on the Preferences screen but never raised. Add rules for each:
- production order past its planned end and not completed;
- open SO whose required date is within N days and still short;
- `available_quantity <= reorder_level` per item and warehouse, as a digest.

Hide `BACKGROUND_JOB_*` and `SYSTEM_ANNOUNCEMENT` from Preferences unless something raises them. If nothing does, remove them from the enum after checking that no DB rows use them.

### 6. Retention

- `maintenance.purge_read_notifications`, daily: read notifications older than `NOTIFICATION_RETENTION_DAYS` (180, an ASSUMPTION), batched with `PURGE_BATCH` and `MAX_PURGE_BATCHES`, as `purge_expired_sessions` does.
- Resolved alert states older than 90 days are purged too.
- The model docstring already promises this.

### 7. Frontend

New page `/admin/alerts` ("Alerts"). It goes in the Administration nav and is gated `[P.SETTINGS_VIEW]`.
- It has a table of rules: name, description, enabled toggle, threshold (shown in hours or days with a unit select), and remind-every.
- Save sends a PUT with only the changed rows, gated `P.SETTINGS_MANAGE`.
- Add it to the sweeps and the handbook.
- The notifications page type filter gains the new types.

### 8. Demo data

Make some demo POs old enough (anchor-relative, identity-keyed) that the first sweep raises PO_NOT_ACKNOWLEDGED, TRACKING_MISSING and QC_PENDING. Document how to trigger the sweep manually: `python -m` invocation or a Celery call.

### 9. Docs

- `database.md`: tables, §8 alert events.
- `assumptions.md` (`§B4-n`): thresholds, reminder cadence, recipients, digest versus entity, the alerts-are-UTC-timed note (or company timezone if you implement it), retention.
- `architecture.md`: background work.
- `api.md`.
- Handbook: notifications chapter lists every event and alert; the Alerts admin screen.
- `ONE-BOX-RUNBOOK.md`: new settings.

## Tests

**Unit**
- Reconcile set logic (new, still, gone; empty sets; remind cadence; a quiet tick writes nothing).
- Rule registry completeness.
- Both new guard tests.
- `user_ids_with_permission` folding as a pure helper.
- Every beat task is registered.

**Integration**
- One test per scanner against seeded data.
- Episodes dedupe across two sweeps.
- An episode resolves when its condition clears and re-opens when it recurs.
- Config overrides apply.
- Vendor recipients are limited to that vendor.
- Event notifications fire with the right recipients and links: one test per event family.
- Retention purge.

**Frontend**
- Alerts page gating and the PUT body.
