# Notifications, push and timed alerts: area map (gaps 2-push and 9)

## Verdict on the audit claims (verified in code)

- **Push notifications are absent, as the audit says.** There is no `PUSH` channel, no subscription table, no VAPID setting, no service worker, no web manifest, and nothing in the frontend calls `Notification` or `PushManager`. WhatsApp and SMS are also absent. The only trace is `users.phone`, a `String(32)` with no validation.
- **Timed alerts: one exists, the rest are missing.** One scheduled alert exists: `notifications.scan_dispatch_delays`. It runs hourly and covers our own outbound consignments that are past their promised date. Of the nine smart alerts in the proposal:
  - "QC rejected" is raised when the decision is made, not on a timer, and only to internal roles.
  - The other eight are absent.
  - There is no purchase-order overdue sweep. The comment above the beat entry in `celery_app.py:85-86` says there is one, but the task only looks at dispatches.
- **Most notification types are never raised.** Of the 20 `NotificationType` members, only 4 are ever raised: `DISPATCH_DELAYED`, `QC_FAILED`, `ALLOCATION_CHANGED` and `REPORT_READY`. Nothing is sent to vendor users, although vendor roles do hold `notification.view`.
- **Four defects in the existing implementation:**
  - Three of the four types that are raised either point at a link the app has no page for, or carry no link at all.
  - Dispatch delay alerts come back every hour once read.
  - There is no recovery sweep for notification e-mail.
  - There is no retention sweep for notifications.

---

## Exists today

### 1. Tables (`apps/api/src/modules/notifications/models.py`)

**`notifications`** (`Notification(TenantEntity)`, lines 135-262)

Base columns come from `TenantEntity`: `id` (UUIDv7), `company_id`, `created_at`, `updated_at`, `created_by_id`, `updated_by_id`. Own columns:

| column | type / default | notes |
|---|---|---|
| `user_id` | GUID, FK `users.id` ON DELETE CASCADE, NOT NULL | the recipient |
| `notification_type` | String(48) NOT NULL | no CHECK in the database, so a new enum member needs no migration |
| `severity` | String(16) NOT NULL | |
| `title` | String(200) NOT NULL | trimmed by `normalise_title` |
| `body` | Text NOT NULL | |
| `entity_type` / `entity_id` | String(48) / GUID, both nullable | no FK; CHECK `entity_reference_complete` requires both or neither |
| `action_url` | String(512) nullable | CHECK `action_url_is_relative` (`LIKE '/%' AND NOT LIKE '//%'`) |
| `is_read` / `read_at` | Bool default false / timestamptz | CHECK `read_state_consistent`: `(read_at IS NOT NULL) = is_read` |
| `channels` | `ARRAY(String(16))`, server default `'{IN_APP}'` | CHECK `channels_not_empty`; holds the channels actually chosen after preferences |
| `delivered_at` | timestamptz | stamped at insert if `IN_APP` is a channel; otherwise stamped by the worker |
| `email_delivered_at` | timestamptz | idempotency marker for the e-mail worker (migration `d91ec6b47a20`) |
| `group_key` | String(120) | collapse key |
| `occurrence_count` | Integer default 1 | CHECK `>= 1` |

Indexes:
- `ix_notifications_feed` (company_id, user_id, is_read, created_at DESC, id DESC)
- `ix_notifications_unread` (company_id, user_id) WHERE `is_read = false`
- **`uq_notifications_open_group`** UNIQUE (company_id, user_id, notification_type, group_key) WHERE `group_key IS NOT NULL AND is_read = false`
- `ix_notifications_entity` (company_id, entity_type, entity_id) WHERE `entity_id IS NOT NULL`

**`notification_preferences`** (`NotificationPreference(TenantEntity)`, lines 265-294)

Columns: `user_id` (FK CASCADE), `notification_type` String(48), `in_app_enabled` (default true), `email_enabled` (default false). Unique `uq_notification_preferences_key` on (company_id, user_id, notification_type). Rows are sparse: a user with no row gets the module defaults.

### 2. Enums (models.py lines 68-132)

- **`NotificationSeverity`**: INFO, SUCCESS, WARNING, CRITICAL.
- **`NotificationChannel`**: IN_APP, EMAIL. There is no PUSH, SMS or WHATSAPP member.
- **`NotificationType`** has 20 members, in five groups:
  - Approvals: APPROVAL_PENDING, APPROVAL_GRANTED, APPROVAL_REJECTED
  - Delays: PURCHASE_ORDER_DELAYED, PRODUCTION_ORDER_DELAYED, JOB_WORK_OVERDUE, DISPATCH_DELAYED, SALES_ORDER_AT_RISK
  - Stock and quality: STOCK_BELOW_REORDER, STOCK_SHORTAGE, ALLOCATION_CHANGED, QC_INSPECTION_PENDING, QC_FAILED
  - Documents and jobs: GOODS_RECEIPT_POSTED, PAYMENT_DUE, MRP_RUN_COMPLETED, REPORT_READY, BACKGROUND_JOB_COMPLETED, BACKGROUND_JOB_FAILED
  - Platform: SYSTEM_ANNOUNCEMENT

  A grep across `src` for `NotificationType.` finds only `DISPATCH_DELAYED`, `QC_FAILED`, `ALLOCATION_CHANGED` and `REPORT_READY`.

### 3. The API other modules call (`notifications/service.py`)

```python
async def notify(session, *, company_id: UUID, user_ids: Sequence[UUID], notification_type: NotificationType,
                 title: str, body: str, severity=NotificationSeverity.INFO, entity_type=None, entity_id=None,
                 action_url=None, group_key=None, actor_id=None) -> list[Notification]        # :364-516
async def notify_role(session, *, company_id, role_codes: Sequence[str], <same kwargs>) -> list[Notification]  # :519-572
```

- **Runs in the caller's transaction.** It wraps its work in `transaction(session)` (`:471`). It writes one `record_audit(action=CREATE, entity_type="notification")` per fan-out (`:477-498`).
- **Preferences decide the channels; callers cannot choose.** One query loads preferences for all recipients (`repo.preferences_for_type`). Then `resolve_channels(severity, *, in_app_enabled, email_enabled) -> tuple[str, ...]` (`:156-174`) applies them. A recipient with no channel gets no row. **`SEVERITIES_IGNORING_PREFERENCES = {CRITICAL}`** (`:110`) forces IN_APP only, never e-mail. The defaults are `DEFAULT_IN_APP_ENABLED=True` and `DEFAULT_EMAIL_ENABLED=False` (`:103-104`), both marked `ASSUMPTION`.
- **Input clean-up.** `normalise_title` trims to 200 characters. `normalise_action_url` (`:191-217`) accepts only site-relative paths (`/...`, not `//`). `normalise_group_key` rejects anything over 120 characters rather than truncating. `normalise_entity_reference` requires both halves. `dedupe_recipients` exists because `ON CONFLICT` cannot touch the same row twice in one statement.
- **Recipient resolution.** There are only two ways to pick recipients:
  - explicit `user_ids`. These are not checked against `company_id`; the only check is the FK to `users.id`.
  - `NotificationRepository.user_ids_for_roles(role_codes)` (`repository.py:371-418`). It joins users, user_roles and roles, keeping active, non-deleted users and roles. It is capped at `MAX_ROLE_RECIPIENTS=500` and logs a warning when it truncates. **It does not filter on `users.vendor_id`**, so passing a vendor role code reaches every vendor's staff in the company.

  There is no resolver by permission and none by vendor.
- **De-duplication works through `group_key` alone.** `repo.upsert_many(rows)` (`repository.py:236-280`) is `INSERT ... ON CONFLICT (company_id,user_id,notification_type,group_key) WHERE group_key IS NOT NULL AND is_read = false DO UPDATE`. On conflict it:
  - sets title, body, severity, entity, action_url, channels, delivered_at, updated_*
  - sets `occurrence_count + 1`
  - **keeps `created_at`**, because it is the keyset key
  - **keeps `email_delivered_at`**, so a collapsed repeat is never e-mailed again

  It writes in batches of `INSERT_BATCH_SIZE=200`. Once a row is read, the next occurrence inserts a new row (pinned by test `test_a_new_row_is_raised_once_the_alert_has_been_read`).
- **E-mail hand-off.** `queue_email_delivery(session, notifications) -> int` (`:326-356`) stages a payload `{notification_id, company_id, user_id, request_id}` in `sync_session.info["notifications.pending_email"]`. It registers `after_commit` → `_flush_pending_emails` (`:301`) and `after_rollback` → `_drop_pending_emails` (`:321`). On commit, `_send_email_tasks` (`:277`) calls `celery_app.send_task(NOTIFICATION_EMAIL_TASK="notifications.send_email", kwargs=payload)` inside `loop.run_in_executor` and **never raises**; a broker failure is only logged as `notifications.email_enqueue_failed`.
- **Read state.** `NOTIFICATION_READ_MACHINE` (`:133-144`, entity "Notification") has the single edge UNREAD→READ. `NotificationService(session, tenant)` (`:609-748`) provides:
  - `list_feed(filters, params)`
  - `unread_count()`
  - `mark_read(id)`: idempotent, `assert_transition`, `record_status_change`
  - `mark_all_read()`: `MARK_ALL_BATCH=2000` per call, one audit row, returns `remaining_unread`
  - `get_preferences()` / `update_preferences(payload)`: one upsert plus audit `UPDATE` on `notification_preference`
- **System actor.** `NotificationRepository.for_company(session, company_id, actor_id=None)` (`repository.py:89-104`) builds `TenantContext(user_id=SYSTEM_ACTOR_ID)`, where `SYSTEM_ACTOR_ID = UUID(int=0)` (`:75`).

### 4. Feed, keyset paging, unread count (`repository.py`)

- `list_feed` (`:143-193`) orders by `is_read ASC, created_at DESC, id DESC`. The cursor encodes `(created_at, id)`. `_cursor_partition_is_read` (`:127`) recovers which half (unread or read) the previous page ended in, so read rows are not skipped. Only rows with `IN_APP` in `channels` are shown (`:117`).
- `unread_count(user_id, ceiling=UNREAD_COUNT_CEILING=500) -> (int, is_capped)` (`:195-219`) counts through a LIMIT subquery.
- `mark_read_batch` (`:282-307`) is a single UPDATE limited to 2,000 rows.

### 5. Routes (`notifications/router.py`, prefix `/notifications`, tag "Notifications")

Every route uses `dependencies=[require_permission(Permission.NOTIFICATION_VIEW)]` and acts only on the caller's own notifications. The recipient is never a parameter.

| route | handler | response |
|---|---|---|
| `GET /notifications` (filters `unread_only`, `notification_type`, `severity`, cursor params) | `list_notifications` :73 | `CursorPaginatedResponse[NotificationRead]` |
| `GET /notifications/unread-count` | :93 | `ApiResponse[UnreadCountRead]` |
| `GET /notifications/preferences` | :108 | `ApiResponse[list[NotificationPreferenceRead]]` (all types, defaults filled in) |
| `PUT /notifications/preferences` (`PreferencesUpdateRequest{preferences:[{notification_type,in_app_enabled,email_enabled}]}`, partial, no duplicate types) | :125 | same |
| `POST /notifications/read-all` | :142 | `ApiResponse[MarkReadResult{marked, remaining_unread}]` |
| `POST /notifications/{id}/read` | :154 | `ApiResponse[NotificationRead]` |

`notification.view` is held by every internal role preset and by `_VENDOR_BASE` (`core/permissions.py:451-458`). Vendor-portal users can therefore already open the bell.

### 6. E-mail

**Transport, `core/mail.py`.** `OutboundEmail(recipient, subject, text_body, html_body=None, message_id=None)`; `build_message`; `_send_smtp` (blocking, supports STARTTLS, TLS or none); `async deliver_email(mail)` runs it through `asyncio.to_thread`. Nothing logs addresses or message bodies.

**Notification e-mail task, `workers/tasks/notifications.py`.** `send_email(notification_id, company_id, user_id, request_id=None)` (`:409-421`) calls `_deliver_notification_email` (`:157-243`) in two short transactions:
1. Lock the notification joined to an active user (`with_for_update(of=Notification)`). Skip if EMAIL is not a channel or `email_delivered_at` is already set. Claim the row by stamping `email_delivered_at` (and `delivered_at` if it is null).
2. Send over SMTP with no transaction open.
3. On failure, release the claim with an UPDATE guarded by `email_delivered_at == claimed_at`, then re-raise.

Retries come from `_MAIL_RETRY_OPTIONS` (`:401-406`): `autoretry_for=(OSError, smtplib.SMTPException)`, backoff with jitter, `max_retries=5`. The link in the mail is built by `_site_url(action_url)`: `PUBLIC_WEB_URL` plus the path.

**Password-reset outbox, `modules/auth/email_delivery.py`.** `queue_password_reset_email(session, *, company_id, reset_token_id)` uses the same after-commit staging. The durable outbox is `password_reset_tokens.email_token_ciphertext`, Fernet-encrypted with purpose `"password-reset-delivery"` (`core/security.py:436-443`). A sweep, `notifications.dispatch_pending_password_resets` (`:438-446` → `_dispatch_pending_password_resets(limit=100)` `:368-398`), runs every 60 seconds and recovers publishes lost while Redis was down. **No equivalent sweep exists for notification e-mail.**

### 7. Celery (`workers/celery_app.py`)

- **Included modules:** `src.workers.tasks.{mrp,reports,notifications,maintenance}` (`:38-43`).
- **Settings:** `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, `task_time_limit=3600` (soft limit 3300), `task_default_queue="default"`.
- **Routes:** `{"mrp.*": mrp, "reports.*": reports, "notifications.*": notifications, "maintenance.*": default}`.

Beat schedule, verbatim (`:71-109`):

```python
"refresh-inventory-summary": {"task": "maintenance.refresh_summary_views", "schedule": 900.0, "options": {"queue": "default", "expires": 600}},
"release-stale-reservations": {"task": "maintenance.release_stale_reservations", "schedule": 3600.0, "options": {"queue": "default", "expires": 1800}},
# Section 23: a purchase order past its expected delivery date raises a delay alert ...   <-- comment is wrong: task scans dispatches
"dispatch-delay-alerts": {"task": "notifications.scan_dispatch_delays", "schedule": 3600.0, "options": {"queue": "notifications", "expires": 1800}},
"purge-expired-sessions": {"task": "maintenance.purge_expired_sessions", "schedule": 86400.0, "options": {"queue": "default"}},
"purge-temp-files": {"task": "maintenance.purge_temp_files", "schedule": 86400.0, "options": {"queue": "default"}},
"dispatch-pending-password-resets": {"task": "notifications.dispatch_pending_password_resets", "schedule": 60.0, "options": {"queue": "notifications", "expires": 55}},
```

- **Task wrapper.** `async_task(name=..., queue=..., **celery_opts)` (`:137-163`) wraps an async function. `AsyncTask.run_async` reuses the thread's event loop.
- **Signals.** `task_prerun` binds `request_id`, `company_id` and `user_id` from the task kwargs. `task_failure` (`:227-252`): for tasks in `MAIL_TASKS = {"notifications.send_email", "notifications.send_password_reset_email", "notifications.dispatch_pending_password_resets"}` (`:211-217`) that raise `smtplib.SMTPException` or `OSError`, it logs only the exception type.

**How a sweep opens sessions and iterates tenants.** Two patterns exist; the first is the one to copy.

1. **Maintenance pattern** (`workers/tasks/maintenance.py`):
   - One cross-tenant `SELECT DISTINCT company_id` in its own `session_scope()` (for example `_companies_with_stale_reservations` `:159-172`).
   - Then, for each company: `async with session_scope() as session:` and `await owning_service.fn(session, _system_tenant(company_id))`, where `_system_tenant(company_id) = TenantContext(company_id=..., user_id=SYSTEM_ACTOR_ID)` (`:96-103`).
   - Each company runs in a `try/except` that logs and continues.
2. **Dispatch delay pattern** (`notifications.scan_dispatch_delays` `:60-95` → `dispatch.service.scan_delays(session, *, as_of=None, limit=MAX_DELAY_SCAN=500)` `:1846-1902`):
   - One cross-tenant query: `dispatch.repository.scan_delayed_across_tenants` (`:820-861`), using `_delayed_predicate` (`:805-817`).
   - Rows are grouped by company. For each company, `_raise_delay_alert` (`:1905-1969`) runs inside `transaction(session)`:
     - `notify_role(role_codes=DELAY_ALERT_ROLES=("DISPATCH_MANAGER","PLANT_MANAGER","ADMIN"), type=DISPATCH_DELAYED, severity=WARNING, action_url="/dispatch/delayed", group_key=f"dispatch-delay:{company_id}")`
     - `mark_delay_notified` (one UPDATE of `dispatches.delay_notified_at`)
     - a `record_audit(action=DISPATCH_CHANGE, user_id=None, actor_label="notifications.scan_dispatch_delays", context={...})`
   - **The only de-duplication is the unread `group_key` collapse.** `delay_notified_at` and `DelayedDispatchRow.already_notified` are written but never read anywhere (grep).

### 8. Frontend

- **Notifications page**, `apps/web/src/features/notifications/notifications-page.tsx`. It has two tabs, chosen by `?tab=`:
  - **InboxTab** (`:167-391`): `useInfiniteQuery` on `[...queryKeys.notifications.all,'list',filters]` with a cursor; the read-state, severity and type filters live in the URL; "Mark read" calls `POST /notifications/{id}/read`; "Mark all as read" calls `POST /read-all`; links are `<Link to={action_url}>`.
  - **PreferencesTab** (`:395-586`): a table of In app and Email checkboxes, saved with `PUT /notifications/preferences` sending only the changed rows.
  - There is no test file for this page.
- **Types**, `notification-types.ts`. `NotificationRow`, `NotificationPreference`, `UnreadCount` and `MarkAllReadResult` are all written by hand, and none is registered in `tests/unit/test_frontend_contract_types.py` `MIRRORS`. **`NOTIFICATION_TYPES` (`:27-48`) is a hand-maintained copy of the enum; no test compares the two.** `fetchNotificationPage` casts the offset envelope to `CursorEnvelope`.
- **Bell**, `app/app-layout.tsx:83-99, 229-274`:
  - `useQuery({queryKey: queryKeys.notifications.unreadCount, queryFn: api.get<SchemaOf<'UnreadCountRead'>>('/notifications/unread-count'), enabled: can(P.NOTIFICATION_VIEW), refetchInterval: NOTIFICATION_POLL_MS, staleTime: NOTIFICATION_POLL_MS/2})`.
  - `NOTIFICATION_POLL_MS = 60_000` (`lib/constants.ts:76`).
  - The badge shows "99+" above 99 and "!" when the count request fails.
- **Query keys and routes.** `queryKeys.notifications = {all:['notifications'], unreadCount:['notifications','unread-count']}` (`lib/query-client.ts:237-240`). The route is `/notifications`, gated on `P.NOTIFICATION_VIEW` (`app/router.tsx:373`; lazy loader `app/route-loaders.ts:71`). Any unknown path falls through to `*` → `NotFound` (`router.tsx:161, 235`).

### 9. Proposal notifications (PDF p.17): which are implemented

| Proposal event | Implemented? | Raised where | Recipients |
|---|---|---|---|
| New purchase request | No | (hook: `procurement.service.transition_requisition` :774) | — |
| New vendor order | No | (hook: `create_purchase_order` :2071 / SENT branch :2454) | — |
| PO approved | No (type `APPROVAL_GRANTED` unused) | (hook: `transition_purchase_order` APPROVED :2447) | — |
| PO rejected | No (`APPROVAL_REJECTED` unused) | (REJECTED :2451) | — |
| Material received | No (`GOODS_RECEIPT_POSTED` unused) | (hook: `post_goods_receipt` :2954-3113) | — |
| QC pending | No (`QC_INSPECTION_PENDING` unused) | (hook: `_raise_inspections` :3219 / `quality.create_inspection` :1546) | — |
| **QC rejected** | **Yes (internal only)** | `quality/service.py:1950-1965`, called from `decide_inspection` :1871 | `QC_FAILURE_ROLE_CODES=("QC_INSPECTOR","PLANT_MANAGER","STORE_KEEPER")` (:202), WARNING, group `qc-failed:{inspection.id}`, **no action_url**; plus `:1967-1988` CRITICAL to `QC_RETURN_ROLE_CODES=("PURCHASE_MANAGER","PURCHASE_EXECUTIVE")` when `return_required`, group `qc-return:{id}` |
| Job work material sent / received | No | (hooks: `job_work.service.issue_order` :1573, `receive` :1965) | — |
| Sales order created | No | (hooks: `sales.service.create_order` :1023 / `confirm_order` :1608) | — |
| Production shortage | No (`STOCK_SHORTAGE` unused) | — | — |
| Stock reallocation request | **Partly**: the request itself notifies nobody (`create_reallocation_request` :1949-2043); approval notifies the donor | `allocation/service.py:1830-1854` (`_notify_donor_owner`, called :1572) → `ALLOCATION_CHANGED`, WARNING, group `reallocation:{donor.id}`, action_url `/allocation/reservations/{id}` (**no SPA route**) | `donor.created_by_id` |
| Dispatch created | No | (hooks: `dispatch.create_dispatch` :1095, `dispatch_consignment` :1390) | — |
| Courier tracking updated | No | (hooks: `dispatch.add_tracking_event` :1780, `procurement.record_shipment` :2524) | — |
| Payment pending | No (`PAYMENT_DUE` unused) | — | — |
| Vendor delay | No (`PURCHASE_ORDER_DELAYED` unused); only our outbound `DISPATCH_DELAYED` sweep exists | `dispatch/service.py:1927-1940` | `DELAY_ALERT_ROLES` (dispatch only) |
| Vendor: PO assigned / PO approved / order dispatched / courier & tracking ID / delivery expected date / payment reminder / material rejected in QC / job work pending / new order request / approval request | **None.** Nothing anywhere targets a vendor user | — | — |
| (not in proposal) Report ready | Yes | `workers/tasks/reports.py:1000-1014` | requesting user; action_url `/api/v1/reports/jobs/{id}`, an **API path, not a page** |

### 10. Proposal smart alerts (PDF p.21): status and the data to detect them

| Alert | Implemented? | Columns and indexes that exist |
|---|---|---|
| PO not accepted within 24 h | No | `purchase_orders.status='SENT'`, `sent_at` (set `procurement/service.py:2455`), `acknowledged_at` (set :2459). The machine allows SENT→PARTIALLY_RECEIVED without acknowledgement (`service_rules.py:206-209, 274-287`), so the condition must be `status='SENT' AND acknowledged_at IS NULL AND sent_at <= now-24h`. Index `ix_purchase_orders_status_expected(company_id,status,expected_delivery_date)`. Recipients available: `created_by_id`, `assigned_vendor_user_id` |
| Vendor delivery date missed | No | `expected_delivery_date` (models :806), `RECEIVABLE_PO_STATUSES` (:290-294), line status PENDING/PARTIALLY_RECEIVED. **Two definitions already exist:** list `overdue_only` (`procurement/repository.py:945-950`, uses `OPEN_PO_STATUSES`, which includes DRAFT/PENDING_APPROVAL/APPROVED) and `order_tracking.is_delayed` (`service.py:2674-2704`, receivable statuses with open lines) |
| Dispatch not updated | No | Inbound: `status='ACKNOWLEDGED' AND shipped_at IS NULL` against `expected_delivery_date`. Outbound: `dispatches.status IN IN_FLIGHT_STATUSES` plus `MAX(dispatch_tracking_events.event_time)` or `dispatched_at` (index `ix_dispatch_tracking_events_dispatch(company_id,dispatch_id,event_time)`) |
| Tracking ID missing | No | Inbound: `purchase_orders.shipped_at IS NOT NULL AND tracking_reference IS NULL` (`record_shipment` lets either field stay null, `service.py:2567-2576`). Outbound: `dispatches.tracking_id`, `lr_number`, `courier_name`, `dispatched_at` |
| Material received but QC pending | No (`QC_INSPECTION_PENDING` unused) | `qc_inspections.status IN OPEN_STATUSES (PENDING, IN_PROGRESS)`, `created_at`, partial index **`ix_qc_inspections_open_queue(company_id,qc_stage,created_at)`**; `goods_receipt_lines.qc_status='PENDING'`/`qc_inspection_id`; PO status `QC_PENDING`. The dashboard already counts these (`reports/repository.py:1308-1312`) |
| QC rejected | Yes, raised on the decision (see section 9) | — |
| GRN not created | No | Inbound: `purchase_orders.shipped_at`, `expected_delivery_date`, receivable status, open lines; `goods_receipts.status='DRAFT'`, `created_at`, `posted_at`. Outbound: `GRN_OUTSTANDING_STATUSES` (`dispatch/repository.py:88-90`) |
| Vendor invoice pending | No | PO status `GRN_COMPLETED` (→INVOICED, `service_rules.py:351-356`) with no `vendor_invoices.purchase_order_id` (index `ix_vendor_invoices_purchase_order`); `vendor_invoices.status='PENDING_APPROVAL'`, `created_at`; `goods_receipts.posted_at` |
| Payment overdue | No (`PAYMENT_DUE` unused) | `vendor_invoices.due_date`, `paid_amount`, `total_amount`, `PAYABLE_INVOICE_STATUSES` (`finance/models.py:211-214`), partial index `ix_vendor_invoices_open`. The existing query is `VendorInvoiceRepository.outstanding_stmt(as_of, vendor_id, overdue_only=True)` (`finance/repository.py:991`). Customer receivables have **no due date**: they are aged from the posting date (`receivable_ageing_stmt` :674); `customers.credit_days` (`customers/models.py:131`) could supply one |
| (types exist, unraised) Job work overdue | No | `job_work_orders.expected_return_date`, `MATERIAL_AT_VENDOR_STATUSES`, partial index `ix_job_work_orders_material_at_vendor`; `job_work.service.pending_at_vendor(..., overdue_only=True)` :1021 |
| (types exist, unraised) Stock below reorder | No | `inventory/repository.py:389-395` (`Item.reorder_level > 0 AND available_quantity <= reorder_level`) |

### 11. Settings and `.env.example`

`core/config.py:237-253` holds only mail settings: `email_provider` (disabled|smtp), `public_web_url`, `email_from_address`, `email_from_name`, `smtp_host`, `smtp_port`, `smtp_security`, `smtp_username`, `smtp_password`, `smtp_timeout_seconds`, plus `celery_broker_url` and `celery_result_backend`. When deployed, the validators (`:359-431`) require SMTP and an HTTPS `PUBLIC_WEB_URL`. `credential_encryption_key` is the root for Fernet keys derived per purpose (`core/security.py:392-396`).

**There are no settings for push, alerts, thresholds or feature flags.** `.env.example:93-105` lists only the mail keys. In `docker-compose.yml`, `x-api-env` (`:54-106`) passes each variable explicitly.

### 12. PWA and push readiness on the frontend

- **No PWA pieces exist.** `apps/web/public/` contains only `favicon.svg`. `index.html` has no manifest link, no apple-touch-icon and no service worker registration. `vite.config.ts` has no PWA plugin. `package.json` has no workbox or vite-plugin-pwa.
- **Frontend container nginx**, `apps/web/nginx.conf`:
  - `/assets/` → `expires 1y`, `Cache-Control "public, immutable"`
  - `\.(svg|ico|png|jpg|jpeg|webp|woff2?)$` → 30 days
  - `= /index.html` → `no-cache, no-store, must-revalidate`
  - `/` → `try_files $uri $uri/ /index.html`

  A `/sw.js` file would fall into `location /` and get no explicit Cache-Control.
- **Deployment nginx**, `infrastructure/nginx/conf.d/atcraft.conf:115-119`, sets these at server level:
  - `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy "geolocation=(), microphone=(), camera=()"`
  - `CSP "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"`

  The `/assets/` and `/` locations deliberately add no headers of their own (comment at `:179-186`). HSTS is set at the host nginx (`host/atcraft-main.conf:103`). The dev config (`conf.d.dev`) sends no security headers.
- **These headers do not block a same-origin service worker or Web Push:**
  - `worker-src` falls back to `script-src 'self'`.
  - `manifest-src` falls back to `default-src 'self'`.
  - The page never contacts the push service, so `connect-src` does not matter.
  - The Permissions-Policy does not mention notifications or push.

  Requirements: a secure context (deployed processes are forced to HTTPS; localhost and 127.0.0.1 are fine), and on iOS 16.4+ the site must be installed to the Home Screen, which needs a manifest.
- **Service-worker caching would break deploy recovery.** `main.tsx` has a `vite:preloadError` handler that reloads after a deploy. It assumes there is no service-worker cache, so any new service worker must have no `fetch` handler.

### 13. Outbound HTTP and crypto available to the backend

- **Runtime dependencies** (`pyproject.toml:17-54`) contain **no HTTP client**. `httpx` is dev-only (`pyproject:61`; `requirements-dev.lock:930`). `requests` comes in only through pip-audit (dev). `urllib3==2.7.0` and `certifi` appear only as transitive dependencies of `sentry-sdk` (`requirements.lock:1321, 194`). Nothing in `src` imports an HTTP client (grep found none).
- **Crypto is present:** `cryptography==50.0.1` (`requirements.lock:497`) and `pyjwt==2.13.0` (`:1114`).
- **Web Push needs no new dependency.** With these two packages, VAPID (RFC 8292, ES256 via `jwt.encode(..., algorithm="ES256")`) and RFC 8291 `aes128gcm` (ECDH P-256, HKDF-SHA256, AESGCM) can be built. The HTTP POST can use stdlib `http.client.HTTPSConnection` inside `asyncio.to_thread`.

---

## Missing vs proposal

**Gap 2: push**
1. `NotificationChannel.PUSH`, a `push_enabled` preference, and a `notifications.push_delivered_at` idempotency marker.
2. A table for device subscriptions (endpoint, p256dh, auth, owner, last seen, failures).
3. VAPID keys and push settings, plus their `.env.example` and compose plumbing.
4. Outbound sender: payload encryption, a VAPID header, and handling of 201/404/410/413/429 responses.
5. A Celery task `notifications.send_push`, an after-commit queue hook, and a recovery sweep.
6. Routes for the public key and for subscribe, list and unsubscribe.
7. Frontend:
   - `public/sw.js` handling `push` and `notificationclick`
   - `manifest.webmanifest` plus icons, linked from `index.html`
   - a push module and a "this device" toggle
   - unsubscribe on logout
   - a Push column in Preferences
8. nginx: no-cache for `/sw.js`, and a MIME type for `.webmanifest`. Check whether nginx 1.30's `mime.types` already maps `webmanifest`.
9. Vendor-scoped recipient resolution. Today nothing can address one vendor's users safely: `notify_role` with a vendor role code reaches every vendor.

**Gap 9: timed alerts**
1. The eight absent rules in section 10 (plus the overdue job-work, purchase-order and production rules whose types exist).
2. Configurable thresholds, both global and per company. Nothing exists today.
3. Per-rule, per-entity alert state (when first detected, last notified, resolved). Only dispatch has a stamp column, and it is unused.
4. A beat entry and a Celery adapter.
5. Vendor-side recipients for vendor rules.
6. A notification retention sweep. The model docstring at `models.py:38-40` promises one; nothing implements it.

**Event notifications** (section 9): 18 of 26 proposal events have no call site. The 16 unused `NotificationType` members give a ready vocabulary for most of them; new members are needed for "PO not acknowledged", "tracking missing", "GRN missing", "vendor invoice pending" and "PO assigned or shipped".

**Defects in what exists today (verified)**
- **Links that go nowhere.**
  - `/dispatch/delayed` (`dispatch/service.py:1937`): the app has no such page; the handbook (`docs/handbook/content.json`) says so too. The page that exists is `/dispatch?tab=tracking&delayed_only=true`.
  - `/allocation/reservations/{id}` (`allocation/service.py:1849`): the app has no such page; reservations live at `/inventory/reservations`.
  - `REPORT_READY` → `/api/v1/reports/jobs/{id}` (`reports.service.job_status_url`): this is an API path, so in the app `<Link>` lands on `NotFound`, and in e-mail it hits the API without a token.
  - `QC_FAILED` has no link at all.
- **The dispatch delay alert comes back every hour once read.** Only the unread group collapses; `delay_notified_at` is never consulted. Each run also writes two audit rows (the notify `CREATE` and a `DISPATCH_CHANGE`). The cross-tenant `limit=500` is ordered by `company_id`, so later tenants can be starved. The docstring says "successive runs work through a backlog", but the query has no cursor or stamp filter.
- **No recovery sweep for notification e-mail.** A broker outage at commit loses the e-mail permanently; it is only logged.
- **No retention sweep for notifications.**
- **`purchase_orders.assigned_vendor_user_id` is never validated** against the company, the PO's vendor or active status (`procurement/service.py:2159, 2456-2457`); only the FK is checked.
- **The notifications `ASSUMPTION`s are not in `docs/assumptions.md`**, which CLAUDE.md requires: e-mail opt-in, the CRITICAL override, the collapse rule and the meaning of `delivered_at`.

---

## Patterns to copy

| New piece | Closest precedent | Conventions to keep |
|---|---|---|
| Sweep task | `notifications.scan_dispatch_delays` (`workers/tasks/notifications.py:60-95`) plus the maintenance tenant loop (`maintenance.py:175-214`) | `@async_task(name="notifications.<x>", queue="notifications")`; signature `(request_id: str \| None = None, **_)`; list companies in one cross-tenant read; one `session_scope()` per company; `_system_tenant(company_id)`; per-company try/except with logging; return a summary dict; must be safe to run twice (acks_late) |
| Rule logic inside the owning module | `dispatch.service.scan_delays` / `_raise_delay_alert` (`:1846-1969`); `job_work.service.pending_at_vendor` | A predicate written as a SQL expression and shared with the list screen (`_delayed_predicate` `dispatch/repository.py:805`), so the screen and the alert cannot disagree; write inside `async with transaction(session)`; audit with `user_id=None, actor_label="<task name>"` |
| Collapsing repeated alerts | `uq_notifications_open_group` plus `upsert_many` `index_where` (models `:227-235`, repository `:248-275`) | The partial-index predicate must be identical in the model and in `index_where`; `ON CONFLICT DO UPDATE` arbitrates concurrent sweeps |
| Sparse per-company config | `NotificationPreference` plus `effective_preferences` (`service.py:580-606`) plus `upsert_preferences` | Store only overrides; code defaults marked `ASSUMPTION`; the API returns the full effective matrix |
| Delivery channel / outbox | E-mail: `queue_email_delivery` (`service.py:326-356`) → `send_email` → `_deliver_notification_email` (claim, send outside the transaction, release on failure); recovery: `_dispatch_pending_password_resets` | After-commit hook keyed in `sync_session.info`; task message carries ids only; timestamp-guarded claim and release; `autoretry_for` transport errors; confidential failure logging (`MAIL_TASKS`) |
| Transport module | `core/mail.py` (`OutboundEmail`, `deliver_email` via `asyncio.to_thread`) | Settings from `core.config.settings`; never log content or addresses |
| Secret at rest | `_encrypt_credential(value, purpose=...)` (`core/security.py:399-409`) | Fernet key derived per purpose from `CREDENTIAL_ENCRYPTION_KEY` |
| Read state machine | `NOTIFICATION_READ_MACHINE` (`service.py:133-144`) | `StateMachine(entity="<Human name>", initial=..., transitions=(Transition(source, target, label=...),), terminal=...)`; `assert_transition` before assigning; `record_status_change` |
| Router | `notifications/router.py` | `APIRouter(prefix, tags, responses=COMMON_ERROR_RESPONSES)`; `dependencies=[require_permission(Permission.X)]` on every route; `DbSession`, `Tenant` deps; `ApiResponse[T].ok(data, "message")`; `CursorPaginatedResponse`/`PaginatedResponse`; the service is built per request as `NotificationService(session, tenant)` |
| Schemas | `notifications/schemas.py` | Pydantic v2, `ConfigDict(from_attributes=True)`; no `user_id` or `company_id` in request payloads; `field_validator` for duplicates |
| Migration | `20260904_0100_email_delivery_state.py`, `20260906_0902_grant_item_attribute_permissions.py` | File name `YYYYMMDD_HHMM_<slug>.py`; docstring covering locking impact, data migration, rollback and production size; current head **`2f5e08c7ba61`**; nullable `ADD COLUMN` (no rewrite) |
| Unit tests | `test_notifications_channels.py` (pure functions); `test_notifications_email_queue.py` (fake `SimpleNamespace(sync_session=SyncSession())`, monkeypatch `_send_email_tasks`); `test_notification_email_worker.py` (patch `worker.session_scope` and `worker.deliver_email`, compile the release UPDATE with the postgres dialect) | `pytestmark = pytest.mark.unit` |
| Integration tests | `tests/integration/test_notifications_service.py` (helpers `_make_user`, `_make_role`, `_raise`); dispatch sweep test `test_dispatch_service.py:832-868` | Fixtures `session`, `company`, `user`, `tenant` from `tests/conftest.py`; `scan_delays(session, as_of=...)` takes a date rather than freezing the clock |
| Frontend page and tests | `notifications-page.tsx` (URL-driven tabs, `useMutation` then invalidate `queryKeys.notifications.all`, `toast.success(success.saved('Preferences'))`); harness `src/test/harness.tsx` `renderScreen(<Page/>, {permissions, routes:{'GET /x': ok(...)}})` | Types from `SchemaOf<'Model'>`, not hand-written; numbers in `lib/constants.ts`; keys in `queryKeys` |
| Settings and secrets | `email_*`/`smtp_*` in `config.py` plus `_enforce_production_safety` | Each field gets a line in `.env.example`, a `${VAR}` entry in `x-api-env`, a runbook table row, and, if secret, the `deploy/*.env.example` files and `scripts/deploy.sh init` |

---

## Integration points

- **Raising notifications from owning modules.** All callers import `notify`/`notify_role` from `src.modules.notifications.service`, following the precedent set by dispatch, quality and allocation:
  - procurement: `transition_purchase_order` :2419 (APPROVED, REJECTED, SENT, ACKNOWLEDGED), `transition_requisition` :774, `record_shipment` :2524, `post_goods_receipt` :2954
  - quality: `create_inspection` :1546
  - job_work: `issue_order` :1573, `receive` :1965
  - sales: `create_order` :1023, `confirm_order` :1608
  - dispatch: `create_dispatch` :1095, `dispatch_consignment` :1390, `add_tracking_event` :1780
  - finance: `approve_invoice` :2165, `approve_payment` :2598, `allocate_payment` :2895
  - allocation: `create_reallocation_request` :1949
- **Recipient data.**
  - Roles: `users`, `user_roles`, `roles` (`users/models.py:49-185`). `Role.permissions` is a text array; `User.extra_permissions`/`denied_permissions` are arrays.
  - Vendor users: `users.vendor_id` (index `ix_users_vendor`). Vendor and internal roles are kept separate by `assert_vendor_role_consistency` in `users/service.py`.
  - Precedent query: `vendors/repository.py:278-295`.
- **Dependency direction** (`docs/architecture.md:152-170`). notifications is imported by every module, so it must not import procurement, finance and the rest. Rule scanners go in the owning modules; the worker adapter composes them.
- **Auth and session lifecycle.** For push subscription cleanup:
  - `AuthService.logout` (`auth/service.py:681`), `logout_all` (:722), `change_password` (:972), `confirm_password_reset` (:1110), user deactivation in the users service.
  - Refresh **rotates `user_sessions` rows every refresh** (`refresh` :612-622), so a subscription cannot be tied to one session row.
- **Celery.** `celery_app.include`, `beat_schedule`, `task_routes` (`notifications.*` → `notifications` queue, consumed by `celery-notify`), `MAIL_TASKS` and `_failure_is_confidential`.
- **Frontend.**
  - Bell polling: `app-layout.tsx` (keep the push code lazy-loaded).
  - Logout hook: `features/auth/auth-context.tsx:313-324`.
  - Preferences UI: `notifications-page.tsx`.
  - `notification-types.ts`; `lib/constants.ts`; `lib/query-client.ts`.
  - Only if a new page is added: router and route-loaders, plus the e2e sweeps.
- **Infrastructure.** `apps/web/nginx.conf`; `apps/web/public/`; `index.html`; `docker-compose.yml` `x-api-env`; `deploy/*.env.example`; `scripts/deploy.sh`; `ONE-BOX-RUNBOOK.md` settings table.

---

## Traps & constraints

**Specific to this area (found while mapping)**

- **`action_url` must be a page the app actually has.** Today three links go nowhere (see Missing). Add a meta-test, similar to `tests/unit/test_e2e_sweep_routes.py`, that parses `apps/web/src/app/router.tsx` and checks every `action_url` template used by `notify` callers. A push click opens this URL directly.
- **`notify_role` with a vendor role code reaches every vendor in the company.** Vendor rules need a resolver filtered on `vendor_id`. Validate `assigned_vendor_user_id` (same company, same vendor, active) before sending to it. This is CLAUDE.md's "A foreign key to a UUID is not a tenant check" trap.
- **Collapsing does not reset `email_delivered_at`.** A repeat alert is never e-mailed (or pushed, if push copies this) until the row is read. Decide on purpose whether reminders should re-deliver.
- **Each `notify()` call writes an audit row, and `audit_logs` is append-only (database trigger).** A sweep that calls notify on every tick floods the audit trail. Notify only on new detections or when a reminder is due.
- **Dispatch delay re-fires.** Don't copy that sweep's de-duplication (unread collapse only, unused `delay_notified_at`, global `limit` across tenants).
- **Celery queue consumption.** With no `task_queues` configured, a worker started without `-Q` consumes only `default`; the fallback is at `celery/app/amqp.py:281` in the venv.
  - Base compose `celery-worker` (`docker-compose.yml:250`), the dev overlay (`docker-compose.dev.yml:66`) and `make dev-worker` (`Makefile:67-68`) all pass no `-Q`.
  - The `notifications` queue is consumed only by `celery-notify` (`-Q notifications`).
  - `make dev` has **no beat target**, so sweeps never run in hybrid development.
  - In the dev compose stack, `celery-notify` runs the **production image with `src` baked in** (no bind mount, no watchfiles), so new `notifications.*` tasks are "unregistered" there until `docker compose build celery-notify`.
  - The production image installs `requirements.lock` only, so code that imports `httpx` passes tests and then crashes in the image.
- **Two definitions of "PO late" already exist** (`overdue_only` vs `order_tracking.is_delayed`). Pick one and share it between the screen and the alert.
- **`today()` is the UTC date** (`core/clock.py`); companies have `timezone` defaulting to Asia/Kolkata. Date rules roll over at 05:30 IST. Either stay consistent with the screens (UTC) or record an `ASSUMPTION`.
- **Push-specific hazards:**
  - The endpoint URL comes from the client, so posting to it is an **SSRF** risk: allow-list push hosts, https only, no IP literals.
  - The endpoint is a capability URL: encrypt it at rest and keep it out of logs.
  - Push transport exceptions should be logged by type only (extend `_failure_is_confidential`).
  - The encrypted payload is capped at about 4 KB.
  - The browser endpoint belongs to one browser profile. On a shared plant PC, re-registration must move ownership to the new user, and logout must unsubscribe.
  - The service worker has no access token (`api-client.ts:75`: access token in memory, refresh token in an HttpOnly cookie), so push payloads must be self-contained.
- **`ruff S310`** flags `urllib.request.urlopen` on a variable URL; `http.client.HTTPSConnection` avoids it.
- **The `detect-private-key` pre-commit hook** rejects PEM literals. Generate VAPID or EC keys at runtime in tests.
- **`public/sw.js` escapes every quality gate.** It is not covered by eslint (`**/*.{ts,tsx}` only), the TypeScript config (`include: ["src"]`) or the bundle checks, and jsdom has no `serviceWorker`, `PushManager` or `Notification`. Put shared stubs in `src/test/setup.ts`, per the CLAUDE.md trap "the fix existing in 15 files".
- **Bundle budgets** (`scripts/check-bundle-budget.mjs`): entry chunk at most 80 KB gzipped, each route chunk at most 20 KB. Push helpers must be lazy-imported from `app-layout`/`auth-context`.
- **nginx header inheritance.** An `add_header` inside a location of `infrastructure/nginx/conf.d/atcraft.conf` silently drops the server-level CSP and other security headers (comment at `:179-186`). Put the `sw.js` Cache-Control in `apps/web/nginx.conf` instead, which has no server-level `add_header`.

**General (CLAUDE.md and meta-tests that will apply to new code)**

- **`test_env_template.py`:** every `Settings` field needs a `.env.example` line, and every line must be read by something.
- **`test_deployed_secret_placeholders.py`:** if a VAPID private key becomes a secret, add it to `SECRET_KEYS` and to `deploy.sh init`.
- **`test_worker_failure_logging.py`:** `MAIL_TASKS` must equal the registered mail tasks; mirror this for push tasks.
- **`test_state_machine_entity_names.py`:** machine entity names must be human words ("Push subscription", "Alert episode").
- **`test_repository_entity_label.py` / `test_entity_names.py`:** one name per record.
- **`test_schema_identifier_length.py`:** hand-written CHECK names must be 63 bytes or fewer.
- **`test_frontend_contract_types.py`:** derive frontend types from `SchemaOf` or register them in `MIRRORS`.
- **Permission tests.** `test_frontend_permission_gates.py`/`_catalogue.py`/`test_role_presets_reach_roles.py`: a new permission needs the frontend catalogue entry *and* a data migration granting it to the stored `roles.permissions` (precedent `2f5e08c7ba61`). Reusing `NOTIFICATION_VIEW` and `SETTINGS_VIEW`/`SETTINGS_MANAGE` avoids all of this.
- **`test_e2e_sweep_routes.py`:** any new page must appear in the browser, a11y, responsive and permission sweeps.
- **`test_postman_and_contract_targets.py`:** new endpoints require `make contract` (openapi.json, `src/types/openapi.ts`, postman collection) and updating the count in `docs/api.md:28` ("494 requests in 27 folders").
- **Trap list items that apply:**
  - partial unique index checked per statement: resolve before insert
  - order by `(created_at, id)`
  - `if xs:` without `else` inverts an IN filter: an empty current set must resolve every open episode, and an empty recipient list must notify nobody
  - never `session.rollback()` in a service
  - after a rolled-back savepoint, keep ids in plain variables
  - `model_validate` of `lazy="raise"` relationships
  - "idempotence keyed on today": key alert state on the entity, not a date
  - integration fixtures through the ORM or `seed_support`
  - every list endpoint is paginated

---

## Files a change here will touch

**Backend**
- `src/modules/notifications/`
  - `models.py`: PUSH channel; new types; `push_delivered_at`; `push_enabled`; new tables `push_subscriptions`, `notification_alert_rules`, `notification_alert_states`
  - `service.py`: `resolve_channels`, `notify`, `queue_push_delivery`, new resolvers, subscription service, alert reconcile
  - `repository.py`
  - `schemas.py`
  - `router.py`
  - new `alerts.py` (rule registry, candidate dataclass, reconcile)
- `src/core/webpush.py` (new; modelled on `core/mail.py`)
- `src/core/config.py`
- `src/core/security.py`: `encrypt_push_subscription`/`decrypt_*`
- `src/core/logging.py`: add `endpoint`, `p256dh`, `auth` to `SENSITIVE_KEYS`
- `src/workers/celery_app.py`: include list, beat entries, a push equivalent of `MAIL_TASKS`
- `src/workers/tasks/notifications.py`: `send_push`, `dispatch_pending_deliveries`
- new `src/workers/tasks/alerts.py` (or extend notifications)
- `src/workers/tasks/maintenance.py`: notification retention
- Scanners in the owning modules: procurement, quality, finance, job_work and dispatch `service.py`/`repository.py`
- Event hooks in the same modules' services
- `src/modules/auth/service.py`: logout_all, password change cleanup
- `src/modules/users/service.py`: deactivation cleanup

**Migrations**
- `apps/api/migrations/versions/2026MMDD_HHMM_web_push_and_alerts.py` (down_revision `2f5e08c7ba61`)

**Tests**
- unit: `test_notifications_channels.py` (new tuple shapes), new `test_notifications_push_queue.py`, `test_webpush_crypto.py` (RFC 8291 test vector), `test_push_worker.py`, `test_alert_reconcile.py`
- unit: `test_worker_failure_logging.py`, `test_env_template.py`, a beat-task-registered test, an action_url-route test
- integration: `test_notifications_service.py`, a new `test_alerts_service.py`, scanner tests in each owning module's integration file
- api: a vendor-scope push/alert test, modelled on `tests/api/test_vendor_portal_scope_http.py`

**Frontend**
- `apps/web/public/sw.js`, `manifest.webmanifest`, `icons/*`
- `index.html`
- `src/features/notifications/notification-types.ts`, `notifications-page.tsx`, new `push.ts`, new `notifications-page.test.tsx`
- `src/app/app-layout.tsx`, `src/features/auth/auth-context.tsx`
- `src/lib/constants.ts`, `src/lib/query-client.ts`
- `src/test/setup.ts` (service-worker and push stubs)
- `src/types/openapi.ts` (regenerated)
- `apps/web/nginx.conf`

**Infrastructure and docs**
- `.env.example`, `docker-compose.yml` (`x-api-env`; consider bind-mounting `src` into `celery-notify` in the dev overlay), `deploy/main.env.example`, `deploy/dev.env.example`, `scripts/deploy.sh` (if the VAPID key is a secret)
- `openapi.json`, `postman_collection.json`
- `docs/assumptions.md` (notification defaults, push defaults, thresholds), `docs/api.md` (count; `/notifications` line), `docs/architecture.md` (background work), `docs/security.md` (push, SSRF allow-list, CSP note), `docs/database.md` (module table), `ONE-BOX-RUNBOOK.md` (settings table), the handbook (fix the `/dispatch/delayed` note)

---

## Proposed design

### (a) Smart-alert sweep with de-duplication and configurable thresholds

**1. Rule registry in code** (`notifications/alerts.py`, importing nothing from other modules):

```python
class AlertRuleKey(StrEnum): PO_NOT_ACKNOWLEDGED, PO_DELIVERY_MISSED, PO_NOT_DISPATCHED, PO_TRACKING_MISSING,
    GRN_NOT_CREATED, QC_PENDING, VENDOR_INVOICE_PENDING, PAYMENT_OVERDUE, JOB_WORK_OVERDUE, DISPATCH_DELAYED
@dataclass(frozen=True) class AlertRule: key; notification_type; severity; default_threshold: timedelta;
    default_remind_every: timedelta | None; granularity: Literal["ENTITY","DIGEST"]; internal_roles: tuple[str,...];
    vendor_roles: tuple[str,...] = (); notify_creator: bool = False
@dataclass(frozen=True) class AlertCandidate: entity_type; entity_id; code; title; body; action_url;
    vendor_id: UUID | None = None; creator_id: UUID | None = None; assigned_user_id: UUID | None = None
```

- Every default threshold carries an `ASSUMPTION` comment and an entry in `docs/assumptions.md`. Suggested values: 24 h PO acknowledgement, 0 days late, 4 h tracking missing, 24 h QC pending, 2 days GRN after shipment, 7 days invoice after GRN, 0 days payment.
- **Rule key, notification type and deep link per rule:**

  | Rule | Notification type | Deep link |
  |---|---|---|
  | PO not acknowledged | new `PURCHASE_ORDER_NOT_ACKNOWLEDGED` | `/procurement/purchase-orders/{id}` |
  | PO delivery missed | `PURCHASE_ORDER_DELAYED` | `/procurement/purchase-orders?overdue_only=true` |
  | QC pending | `QC_INSPECTION_PENDING` | `/quality?tab=pending` |
  | Job work overdue | `JOB_WORK_OVERDUE` | `/job-work?tab=pending` |
  | Payment overdue | `PAYMENT_DUE` | `/finance?tab=outstanding` |
  | Dispatch delayed | `DISPATCH_DELAYED` | `/dispatch?tab=tracking&delayed_only=true` |

  New types are also needed for tracking missing, GRN missing, vendor invoice pending and PO not dispatched.

**2. Threshold configuration**
- A sparse per-company table `notification_alert_rules(company_id, rule_key String(48), is_enabled bool, threshold_minutes int, remind_every_minutes int null)`. It is a `TenantEntity` with `UniqueConstraint(company_id, rule_key)` and CHECKs `threshold_minutes >= 0` and `remind_every_minutes > 0`. It copies the `NotificationPreference` and `effective_preferences` pattern.
- Routes: `GET`/`PUT /notifications/alert-rules`, gated on `SETTINGS_VIEW`/`SETTINGS_MANAGE`, with audit `CONFIG_CHANGE`. These permissions already exist, so no role migration is needed.
- Global switches in `Settings`: `alerts_enabled: bool = True` and `alerts_scan_interval_seconds: int = 900` (read when the beat schedule is built), each added to `.env.example` and `x-api-env`.

**3. Alert state** (`notification_alert_states`, a `TenantEntity`)
- Columns: `rule_key`, `entity_type`, `entity_id` (no FK), `first_detected_at`, `last_detected_at`, `last_notified_at`, `notified_count` int, `resolved_at` nullable.
- Partial unique index `uq_notification_alert_states_open` on (company_id, rule_key, entity_id) WHERE `resolved_at IS NULL`, with the same predicate passed as `index_where`.
- Optionally an index on (company_id, rule_key) WHERE `resolved_at IS NULL`.
- No status column, so no state machine is required. If one is added, use a machine named "Alert episode", OPEN→RESOLVED.

**4. Scanners live in the owning modules and are tenant-scoped.** Signature: `async def <rule>_candidates(session, tenant, *, as_of: datetime, threshold: timedelta, limit: int) -> list[AlertCandidate]`. Each reuses the module's own predicate (the PO late definition shared with the list filter, `_delayed_predicate`, `OPEN_STATUSES`, `outstanding_stmt(overdue_only=True)`, `pending_at_vendor(overdue_only=True)`). Each is bounded by `MAX_ALERT_CANDIDATES = 500` **per company**, never as a global limit.

**5. Reconcile**, `notifications.alerts.reconcile(session, tenant, rule, config, candidates, *, now)`, inside `transaction(session)` for each (company, rule):
1. Load the open states with one query, then compute `new`, `still` and `gone` sets.
2. Resolve `gone` with a single UPDATE `SET resolved_at=now`. Branch explicitly on the empty set.
3. Insert `new` with `ON CONFLICT DO NOTHING`.
4. `due = new ∪ {still where remind_every and last_notified_at <= now - remind_every}`.
5. If `due` is empty, return. No notify and no audit: a quiet tick writes nothing.
6. ENTITY granularity: one `notify` per entity with `group_key=f"alert:{rule.key}:{entity_id}"`. DIGEST granularity: one notify per company with `group_key=f"alert:{rule.key}"` and a title like "N purchase orders not acknowledged".
7. Recipients:
   - `notify_role(internal_roles)`, or a new `user_ids_with_permission(permission)` for approval-type rules
   - plus the creator (`created_by_id`), checked active and same company
   - plus vendor users through a new `user_ids_for_vendor(vendor_id, role_codes)` (`User.vendor_id == vendor_id`, active), or `assigned_user_id` after validation
8. Update `last_notified_at` and `notified_count` for the `due` rows (one UPDATE).
9. Write one `record_audit(action=STATUS_CHANGE or CONFIG_CHANGE, entity_type="alert_rule", user_id=None, actor_label="notifications.scan_smart_alerts", context={"rule":..., "codes":[...]})` per company per rule, and only when something was notified.

**6. Worker adapter** (`src/workers/tasks/alerts.py`, added to `celery_app.include`):

```python
@async_task(name="notifications.scan_smart_alerts", queue="notifications")
async def scan_smart_alerts(request_id: str | None = None, **_): ...
#   companies = active companies (one cross-tenant read, own session_scope)
#   for company: async with session_scope() as s, transaction(s):
#       if not await try_advisory_xact_lock(s, company_id, "smart-alerts"): continue
#       for rule in enabled rules: async with transaction(s): reconcile(s, _system_tenant(cid), rule, cfg,
#                                      await SCANNERS[rule.key](s, tenant, as_of=utcnow(), threshold=cfg.threshold, limit=500))
#       (per-company and per-rule try/except -> log and continue)
```

Beat entry: `"smart-alerts": {"task": "notifications.scan_smart_alerts", "schedule": float(settings.alerts_scan_interval_seconds), "options": {"queue": "notifications", "expires": 600}}`.

Then:
- Move `scan_dispatch_delays` onto this engine (or at least make it honour reminders and fix its link), and correct the beat comment.
- Add a unit test that every `beat_schedule` task name is registered.
- Add `maintenance.purge_read_notifications`, modelled on `purge_expired_sessions` (`PURGE_BATCH`, `MAX_PURGE_BATCHES`).
- Optionally add `alert_states` retention for resolved rows.

### (b) Web Push channel

**1. Schema.** One migration, all nullable columns or constant defaults, so no table rewrite:
- **Channel and preference.** `NotificationChannel.PUSH = "PUSH"` (no CHECK on `channels`). `notification_preferences.push_enabled BOOLEAN NOT NULL DEFAULT true`. `DEFAULT_PUSH_ENABLED = True` (ASSUMPTION: registering a device is the opt-in; CRITICAL still forces only IN_APP).
- **Delivery marker.** `notifications.push_delivered_at TIMESTAMPTZ NULL`.
- **Recovery index.** Partial index on `created_at` WHERE `push_delivered_at IS NULL AND 'PUSH' = ANY(channels)`, and the same shape for EMAIL, for the recovery sweep.
- **`push_subscriptions` table** (`TenantEntity`, hard delete like `user_sessions`):
  - columns: `user_id` FK CASCADE, `endpoint_hash` String(64) UNIQUE, `endpoint_ciphertext` String(2048) (Fernet purpose `"push-subscription"`), `p256dh` String(128), `auth` String(32), `user_agent` String(512), `device_label` String(120), `expires_at`, `last_seen_at`, `last_success_at`, `last_failure_at`, `failure_count` int default 0
  - index on (company_id, user_id)
  - CHECK `failure_count >= 0`

**2. Settings** (all in `.env.example` and `x-api-env`):
- `web_push_enabled: bool = False`
- `web_push_vapid_private_key: SecretStr = ""`: optional. When empty, derive it deterministically from `CREDENTIAL_ENCRYPTION_KEY` with an HMAC purpose `"web-push-vapid"`, as `_credential_fernet` does. That means no new deploy secret; if the root key rotates, clients simply re-subscribe.
- `web_push_subject: str = ""`: `mailto:`/`https:`; required when deployed and push is enabled; defaults from `EMAIL_FROM_ADDRESS`.
- `web_push_ttl_seconds: int = 86400`, `web_push_timeout_seconds: int = 10`
- `web_push_allowed_hosts: str = "fcm.googleapis.com,push.services.mozilla.com,notify.windows.com,push.apple.com"`, matched as host suffixes, https only.

**3. `src/core/webpush.py`** (no new dependency):
- `vapid_keypair()`, cached; `vapid_public_key_b64()`
- `vapid_header(endpoint) -> str`: ES256 JWT via PyJWT with `aud=scheme://host`, `exp <= 24h`, `sub`; header `vapid t=..., k=...`
- `encrypt_aes128gcm(p256dh, auth, plaintext) -> bytes`: RFC 8291 with `cryptography` ECDH, HKDF and AESGCM; record size 4096; `0x02` padding delimiter
- `assert_allowed_endpoint(url)`
- `async deliver_push(sub, payload: bytes, *, ttl, urgency, topic) -> PushOutcome`: `http.client.HTTPSConnection` in `asyncio.to_thread`; outcomes `DELIVERED` (201/202), `GONE` (404/410), `TOO_LARGE` (413), `RETRY` (429/5xx/OSError)
- `Urgency` from severity (CRITICAL→high, WARNING→normal, otherwise low). `Topic` = base64url(sha256(group_key or id))[:32], so a newer push replaces an undelivered one.
- Payload JSON kept under about 3 KB: `{id, title, body (truncated), url: action_url or "/notifications", tag: group_key or id, severity, count}`.

**4. Service changes**
- `resolve_channels(..., push_enabled, has_push_device)`, with output order IN_APP, EMAIL, PUSH.
- `notify()` loads `users_with_push_devices(recipients)` in **one** query (no N+1).
- `queue_push_delivery(session, written)` copies the e-mail hook with keys `"notifications.pending_push"`/`"notifications.push_hook_installed"` and `NOTIFICATION_PUSH_TASK = "notifications.send_push"`.
- `upsert_many` leaves `push_delivered_at` untouched on collapse, mirroring e-mail; record this as an ASSUMPTION.
- `PreferencesUpdateRequest` gets `push_enabled: bool | None = None`, with `COALESCE` in the upsert, so an old cached SPA does not get a 422 or reset the column.

**5. Worker.** `send_push(notification_id, company_id, user_id, request_id=None)` on the `notifications` queue, retrying on `(OSError, PushRetryableError)`:
1. **Claim.** In one transaction, lock the notification joined to an active user; check PUSH is a channel and `push_delivered_at IS NULL`; load at most 10 subscriptions; stamp `push_delivered_at` (and `delivered_at` if null).
2. **Send** to each device with no transaction open.
3. **Update.** In a second transaction: delete `GONE` devices; update `last_success_at` or `failure_count`. If every device failed with `RETRY`, release the claim with an UPDATE guarded by `push_delivered_at == claimed_at`, then raise.
4. **Recovery and logging.** `notifications.dispatch_pending_deliveries` runs every 60-300 s and re-sends both e-mail and push rows unclaimed for under 24 h, limit 100. Extend `_failure_is_confidential` to cover the push tasks.

**6. Routes** (all `require_permission(NOTIFICATION_VIEW)`, caller's own data, open to vendor users):
- `GET /notifications/push/config` → `{enabled, vapid_public_key}`
- `POST /notifications/push/subscriptions` (upsert by `endpoint_hash`; a different owner takes the endpoint over; audit `CREATE`; never echoes endpoint or keys)
- `GET /notifications/push/subscriptions` (paginated)
- `DELETE /notifications/push/subscriptions/{id}`
- `POST /notifications/push/subscriptions/unregister {endpoint}` (used by logout)
- `POST /notifications/push/test`
- Server-side cleanup on `logout_all`, password change or reset, and user deactivation. Delivery also requires `User.is_active` and `last_seen_at` within `jwt_refresh_token_expire_days`.

**7. Frontend**
- **Service worker.** `public/sw.js` handles only push:
  - `push` → `showNotification(title, {body, tag, data:{url}, icon:'/icons/icon-192.png', badge, renotify})` and `postMessage({type:'notification'})` to open clients
  - `notificationclick` → focus an existing same-origin client and navigate, or `clients.openWindow(url)`
  - no `fetch` handler and no cache
- **Manifest.** `public/manifest.webmanifest` (`display: standalone`, icons 192/512 and maskable, `theme_color` matching `index.html`), linked from `index.html` along with `apple-touch-icon`.
- **Push module.** `src/features/notifications/push.ts`, lazy-loaded:
  - `isPushSupported`, `enablePush()`: `Notification.requestPermission()` from a click, `register('/sw.js')`, `pushManager.subscribe({userVisibleOnly:true, applicationServerKey})`, then POST
  - `disablePush`, `syncPushSubscription()` (on load when permission is granted; re-subscribe if the server key changed), `unregisterOnLogout()`
- **UI.** A "This device" card plus a Push column in PreferencesTab. Types come from `SchemaOf<'PushConfigRead'|...>`. Keys `queryKeys.notifications.pushConfig`/`pushSubscriptions`; `SERVICE_WORKER_URL` in `lib/constants.ts`.
- **Shell and logout.** `app-layout.tsx` lazily syncs and listens for service-worker messages, then invalidates `queryKeys.notifications.unreadCount`. `auth-context.tsx` logout awaits `unregisterOnLogout()` (errors swallowed) before `POST /auth/logout`.
- **nginx.** In `apps/web/nginx.conf`: `location = /sw.js { add_header Cache-Control "no-cache"; try_files $uri =404; }` and `location = /manifest.webmanifest { default_type application/manifest+json; add_header Cache-Control "no-cache"; }`. Leave `infrastructure/nginx/conf.d/atcraft.conf` locations without `add_header`. No CSP change is needed.
- **Tests.** `notifications-page.test.tsx` via the harness, with service-worker, PushManager and Notification stubs in `src/test/setup.ts`; also the vitest suite plus unit tests for `push.ts`.

This gives vendors installable phone push through the existing vendor portal: Android Chrome out of the box, iOS 16.4+ after Add to Home Screen. It needs no native app, no Firebase keys and no new runtime dependency.
