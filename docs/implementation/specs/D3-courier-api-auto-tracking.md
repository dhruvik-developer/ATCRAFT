# WP D3: Courier API integration and automatic tracking status

Lane A, round 4, step 2. It runs after A4 in the same lane. Assumption placeholders use the prefix `§D3-n`.

**Proposal:** Future Enhancement lists "Courier API integration" and "Auto tracking status". The client now wants both present.

**Approach:** a provider-agnostic tracking integration, disabled until configured. It has two halves:
- **(a) Inbound webhooks.** Couriers or aggregators push updates to us.
- **(b) Polling.** A sweep pulls tracking for in-flight consignments from providers with a pull API.

One concrete provider is implemented end to end: **Shiprocket**, an Indian aggregator covering Blue Dart, Delhivery, DTDC and others. It exposes a tracking API and webhooks. Alongside it, a **generic normalised webhook** lets any other courier or aggregator integrate without code.

There is no new dependency. Use the stdlib HTTPS transport pattern from C1b (`http.client` in a thread, a host allow-list, confidential logging).

**Background:** dispatch tracking events (`dispatch_tracking_events`, `add_tracking_event`, `DISPATCH_LIFECYCLE`) in `reports/returns-inventory-qc.md` §4 and `reports/notifications-alerts.md`. Inbound `purchase_order_shipments` and `delivered_at` come from C1a (on main).

## Decisions (binding)

### 1. Module

Create a new downstream module **`courier_tracking`**, mounted after `dispatch` and `procurement`. Prefix `/integrations/courier`, tag "Courier Integration".

### 2. Configuration table `courier_accounts`

Base: TenantMasterEntity.

| Column | Notes |
|---|---|
| `code` | |
| `provider` | `SHIPROCKET \| GENERIC_WEBHOOK` (CHECK) |
| `display_name` | |
| `credentials_ciphertext` | Fernet, purpose `"courier-credentials"`. Shiprocket API user email and password. Never returned by the API. |
| `webhook_token_hash` | SHA-256 of an opaque random token |
| `webhook_secret_ciphertext` | for the HMAC signature |
| `is_active` | |
| `last_poll_at` | |
| `last_error` | |

Lifecycle: ACTIVE/INACTIVE via a state machine. Optionally map each `transporters` master row to the courier account that tracks it: add a nullable `transporters.courier_account_id`.

### 3. Tenant resolution for webhooks

`POST /integrations/courier/webhook/{token}` is unauthenticated.
- The **token** in the path identifies the account and therefore the company. Look it up by hash, server side. This complies with "tenant never from request content": the token is a server-issued capability, not a client-chosen id.
- It also requires a valid HMAC-SHA256 signature header computed with the account's webhook secret, compared in constant time.
- Shiprocket's webhook uses a configured token header instead. Support that per provider.
- Reject anything stale or replayed: keep a timestamp window where the provider sends one, and dedupe event ids.
- Payloads are limited to `MAX_WEBHOOK_BYTES` (64 KB).
- Respond fast. Store the raw event, then process it inline (bounded) or queue it.

Add the path to the auth exemptions using the same mechanism as other public routes. Check the per-IP flood guard.

### 4. Normalised event model

Table `courier_tracking_events`, an ImmutableLedgerEntity.

| Column | Notes |
|---|---|
| `courier_account_id` | |
| `tracking_number` | the AWB or tracking id |
| `normalized_status` | `BOOKED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, FAILED_DELIVERY, RETURNED, EXCEPTION`; reporting enum with a CHECK |
| `raw_status` | |
| `event_time` | |
| `location` | |
| `description` | |
| `provider_event_id` | |

Unique key `(company_id, courier_account_id, tracking_number, normalized_status, event_time)` for idempotency.

Each provider's status mapping is a pure function with a fixture-based test. Shiprocket's `current_status` / `shipment_status` codes map to the normalised set.

### 5. Matching and automatic status (the "Auto tracking status")

Match a tracking number to:
- outbound `dispatches.tracking_id` and/or `lr_number`, through the dispatch's transporter's courier account or any active account of the company;
- inbound `purchase_order_shipments.tracking_reference` and/or `lr_number`.

Batch the lookup: one query per event batch, never one per event.

Effects, all inside the owning module's services (call their public functions; do not write their tables directly):

**Outbound**
- Append a `dispatch_tracking_events` row with source `COURIER_API`. Add a `source` column if one is missing; the default MANUAL keeps old rows correct.
- Status moves through the dispatch machine:
  - DISPATCHED → IN_TRANSIT on PICKED_UP or IN_TRANSIT.
  - IN_TRANSIT → DELIVERED on DELIVERED, through the dispatch service's delivery-confirmation function, acting as the system actor with reason "Delivered per <courier> API". Making this automatic is an ASSUMPTION (`§D3-n`). Put a company switch `AUTO_CONFIRM_DELIVERY` on the courier account (default **true**). When it is off, only record the event and notify, and staff confirm.
- RETURNED and EXCEPTION are recorded and raise a WARNING notification to the dispatch roles.

**Inbound**
- Set `shipment.delivered_at` on DELIVERED, through the C1a service function. This makes the derived tracking status DELIVERED.
- Record the event for the timeline.

**Notifications**
- Raise "Courier tracking updated" through the B4 event notification, if it exists.
- Raise the dealer-facing notifications (A5 adds those next round); keep a clean seam for them.

### 6. Polling

Task `maintenance.poll_courier_tracking`, beat entry every `COURIER_POLL_INTERVAL_SECONDS` (1800) with `expires`.
- Per company, per active SHIPROCKET account, take in-flight consignments: outbound dispatches in DISPATCHED/IN_TRANSIT with a tracking id, and inbound shipments without `delivered_at` and with a tracking reference.
- Bound it with `MAX_POLL_PER_ACCOUNT` (100), oldest first.
- Shiprocket auth: POST `/v1/external/auth/login` gives a token, cached in Redis respecting `settings.cache_enabled`. Then GET `/v1/external/courier/track/awb/{awb}`.
- Run per company in its own transaction and isolate failures. Record `last_poll_at` and `last_error`.

### 7. Routes

| Route | Access | Notes |
|---|---|---|
| `GET /integrations/courier/accounts` | `settings.view` | |
| `POST /integrations/courier/accounts` | `settings.manage` | |
| `PATCH /integrations/courier/accounts/{id}` | `settings.manage` | |
| `POST /integrations/courier/accounts/{id}/rotate-webhook` | `settings.manage` | Returns the new webhook URL and secret **once**. |
| `POST /integrations/courier/accounts/{id}/test` | `settings.manage` | Dry-run login. |
| `POST /integrations/courier/refresh` | `dispatch.update` | Body `{dispatch_id}` or `{shipment_id}`. Polls that one consignment now, bounded. |
| `GET /integrations/courier/events` | `dispatch.view` | Filters: `tracking_number`, `dispatch_id`, `shipment_id`. |
| `POST /integrations/courier/webhook/{token}` | public | See §3. |

Every route uses `require_permission` literally, except the public webhook. Staff routes also use `forbid_vendor_users`; the C1a gate already blocks vendors.

### 8. Settings

`COURIER_TRACKING_ENABLED` (default false), `COURIER_POLL_INTERVAL_SECONDS`, `COURIER_ALLOWED_HOSTS` (default `apiv2.shiprocket.in`). Each goes in `.env.example` and compose.

### 9. Frontend

**New admin page `/admin/courier-integration`** ("Courier integration", gate `[P.SETTINGS_VIEW]`):
- the account list;
- an add/edit dialog with provider, display name, and credentials (write-only, masked);
- rotate webhook, which shows the URL and secret once with copy buttons;
- test connection;
- last poll and last error;
- it goes in the sweeps and the handbook.

**Dispatch tracking timeline:** a source badge (Manual / Courier API) and a "Refresh tracking" button (`P.DISPATCH_UPDATE`).

**Inbound shipments** (PO detail and inbound board): a delivered-by-courier marker and the same refresh button.

### 10. Docs

- `security.md`: unauthenticated webhook hardening (capability token, HMAC, replay, size limit) and credential encryption.
- `assumptions.md` (`§D3-n`): auto-confirm delivery, status mapping, polling bounds.
- `ONE-BOX-RUNBOOK.md`: how to connect Shiprocket.
- `api.md`, handbook.

## Tests

- **Unit:** the Shiprocket status mapping table; signature verification (valid, invalid, constant time); token hashing; the idempotency key; request building for login and track; outcome handling with a fake transport.
- **Integration:**
  - a webhook with a valid token and signature records the event, moves the outbound dispatch to IN_TRANSIT and then DELIVERED, and sets the inbound shipment's `delivered_at`;
  - a duplicate event is ignored;
  - a bad signature answers 401 and writes nothing;
  - an unknown token answers 404 and writes nothing;
  - with `AUTO_CONFIRM_DELIVERY` off, the event is recorded but the status is unchanged;
  - polling with a fake provider;
  - tenant isolation: one company's token can never touch another company's dispatches.
- **Frontend:** the admin page request bodies (credentials never echoed), and the refresh button.
