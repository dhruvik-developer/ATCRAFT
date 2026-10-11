# WP C1b: Web Push notification channel (backend)

Lane C (portals), round 2, step 2 of 2. It follows C1a in the same lane. Placeholder prefix `§C1-n` (continue numbering after C1a's).

**Proposal.** The Notification System channels are "In-app notification, Push notification, Email notification, WhatsApp / SMS (future)". "Mobile push notification" is listed as a **Must Have**.

**Audit.** "phone push notifications" are absent.

**Delivery.** Standard **Web Push** (RFC 8030/8291/8292) to the installable PWA:
- Android Chrome works out of the box.
- iOS 16.4+ works after "Add to Home Screen".
- No Firebase account and no new runtime dependency.

The frontend arrives in round 3 (WP C2). WhatsApp and SMS stay **future**, as the proposal says. Design the channel enum and preferences so they can be added later, but implement nothing for them.

**Background.** Read `reports/notifications-alerts.md`: sections 1–8, "Missing", "Traps" (especially the push-specific hazards), and "Proposed design (b)" items 1–6. Follow (b), with the decisions below.

## Decisions (binding)

### 1. Schema (one migration; nullable or constant defaults, no table rewrite)

- **Channel enum.** Add `NotificationChannel.PUSH`. There is no CHECK on `channels`.
- **Preference.** Add `notification_preferences.push_enabled BOOLEAN NOT NULL DEFAULT true`. Registering a device is the opt-in (`§C1-n`). CRITICAL severity still forces IN_APP only, as today.
- **Delivery marker.** Add `notifications.push_delivered_at TIMESTAMPTZ NULL`.
- **Recovery indexes.** Partial indexes on `created_at` for the recovery sweep:
  - WHERE `push_delivered_at IS NULL AND 'PUSH' = ANY(channels)`
  - the same shape for EMAIL
- **New table `push_subscriptions`** (`TenantEntity`, hard delete):
  - `user_id` FK CASCADE
  - `endpoint_hash` String(64), UNIQUE
  - `endpoint_ciphertext` (Fernet, purpose `"push-subscription"`, via `core/security.py` helpers)
  - `p256dh` String(128), `auth` String(32)
  - `user_agent` String(512), `device_label` String(120)
  - `expires_at`, `last_seen_at`, `last_success_at`, `last_failure_at`
  - `failure_count` int, default 0, CHECK `>= 0`
  - index `(company_id, user_id)`
  - add `endpoint`, `p256dh` and `auth` to `core/logging.SENSITIVE_KEYS`

### 2. Settings

Each setting needs `.env.example`, compose `x-api-env`, and the runbook settings table. `test_env_template.py` must pass.

| Setting | Default | Notes |
|---|---|---|
| `web_push_enabled` | `False` | `True` in the dev overlay env |
| `web_push_vapid_private_key` | empty `SecretStr` | Optional. When empty, derive the key deterministically from `CREDENTIAL_ENCRYPTION_KEY` with HMAC purpose `"web-push-vapid"`. No new deploy secret is needed; if the root key rotates, clients re-subscribe (`§C1-n`). |
| `web_push_subject` | empty | `mailto:` or `https:`. Defaults from `EMAIL_FROM_ADDRESS`. Required when deployed with push enabled; extend `_enforce_production_safety`. |
| `web_push_ttl_seconds` | `86400` | |
| `web_push_timeout_seconds` | `10` | |
| `web_push_allowed_hosts` | see below | Matched as host suffixes, https only, no IP literals. |

Default `web_push_allowed_hosts`: `"fcm.googleapis.com,updates.push.services.mozilla.com,push.services.mozilla.com,notify.windows.com,web.push.apple.com,push.apple.com"`.

### 3. `src/core/webpush.py` (new; modelled on `core/mail.py`; no new dependency)

**VAPID**
- `vapid_keypair()` is cached. `vapid_public_key_b64url()` returns the uncompressed P-256 point, base64url, no padding.
- `vapid_authorization(endpoint) -> str` builds an ES256 JWT with PyJWT: `aud` = scheme://host, `exp` ≤ 12 h, `sub`. The header value is `vapid t=<jwt>, k=<pub>`.

**Encryption**
- `encrypt_aes128gcm(p256dh_b64url, auth_b64url, plaintext, *, salt=None, ephemeral_private=None) -> bytes`: RFC 8291 + RFC 8188.
  - ECDH P-256, HKDF-SHA256, AES-128-GCM, record size 4096, padding delimiter `0x02`.
  - `salt` and `ephemeral_private` are injectable **for the RFC 8291 Appendix A test vector**. A unit test must reproduce the RFC's exact ciphertext bytes.

**Endpoint and delivery**
- `assert_allowed_endpoint(url)`: https only, host-suffix allow-list, no IP literal, no userinfo, default port only. This is the SSRF guard.
- `async deliver_push(sub, payload: bytes, *, ttl, urgency, topic) -> PushOutcome`:
  - transport: `http.client.HTTPSConnection` inside `asyncio.to_thread`, with the timeout setting. Do not use `urlopen`, because of ruff S310.
  - outcomes:

    | Response | Outcome |
    |---|---|
    | 201 or 202 | DELIVERED |
    | 404 or 410 | GONE |
    | 413 | TOO_LARGE |
    | 429, 5xx, OSError | RETRY |
    | other 4xx | FAILED |

- `Urgency` follows severity: CRITICAL → high, WARNING → normal, otherwise low.
- `Topic` = `base64url(sha256(group_key or id))[:32]`.
- Payload JSON stays ≤ 3 KB: `{id, title, body (truncated), url: action_url or "/notifications", tag: group_key or id, severity, count}`.
- Never log the endpoint, keys or payload.

### 4. Service changes (`notifications/service.py`)

- `resolve_channels(...)` gains `push_enabled` and `has_push_device`. Output order is IN_APP, EMAIL, PUSH.
- `notify()` loads `users_with_push_devices(recipient_ids)` in **one** query.
- `queue_push_delivery` mirrors the email after-commit hook, with its own session-info keys and task name `notifications.send_push`.
- `upsert_many` leaves `push_delivered_at` untouched on collapse, mirroring email (`§C1-n`).
- `PreferencesUpdateRequest` gains `push_enabled: bool | None = None`, with COALESCE in the upsert, so an old SPA neither gets a 422 nor resets the value.

### 5. Worker (`workers/tasks/notifications.py`)

**`send_push(notification_id, company_id, user_id, request_id=None)`**, on the `notifications` queue, with `autoretry_for` the retryable error:
1. Claim in one transaction:
   - lock the notification joined to its active user;
   - check that PUSH is a channel and `push_delivered_at` is NULL;
   - load at most 10 subscriptions;
   - stamp `push_delivered_at` (and `delivered_at` if it is NULL).
2. Send to each device with no transaction open.
3. In a second transaction:
   - delete GONE devices;
   - update `last_success_at` or `failure_count`;
   - if every device ended in RETRY, release the claim with an UPDATE guarded by `push_delivered_at == claimed_at`, then raise.

**Recovery sweep `notifications.dispatch_pending_deliveries`**
- Beat entry every 120 s, `expires` 110.
- Re-queues EMAIL and PUSH rows that are still unclaimed, under 24 h old, limit 100.
- This also closes the existing "no recovery sweep for notification e-mail" defect.

**Celery wiring**
- Extend `MAIL_TASKS` / `_failure_is_confidential` to cover the push tasks, and update `test_worker_failure_logging.py`.
- Add a unit test that every `beat_schedule` task name is a registered task.

### 6. Routes (`/notifications/push/...`)

All use `require_permission(Permission.NOTIFICATION_VIEW)`, act on the caller's own data, and are open to vendor users and later dealer users. The C1a allow-list already covers `/notifications/**`.

| Method and path | Behaviour |
|---|---|
| `GET /notifications/push/config` | Returns `{enabled, vapid_public_key}`. |
| `POST /notifications/push/subscriptions` | Body `{endpoint, keys:{p256dh, auth}, expiration_time?, device_label?}`. Upserts by `endpoint_hash`; a different owner takes the endpoint over, which is the shared plant PC case. Validates the endpoint (allow-list) and the key lengths. Audits CREATE. Never echoes the endpoint or keys. |
| `GET /notifications/push/subscriptions` | Paginated list: id, device_label, user_agent, created_at, last_success_at, failure_count. |
| `DELETE /notifications/push/subscriptions/{id}` | Own subscriptions only; a foreign id answers 404. |
| `POST /notifications/push/subscriptions/unregister` | Body `{endpoint}`, used at logout. |
| `POST /notifications/push/test` | Sends a test push to the caller's devices through the normal path. |

**Server-side cleanup.** Delete the user's subscriptions on `logout_all`, on password change or reset, and on user deactivation.

**Delivery eligibility.** Deliver only to a user who is active and has had a session within `jwt_refresh_token_expire_days`.

### 7. Docs and nginx

**Docs**
- `docs/security.md`: the push section, covering the SSRF allow-list, encryption at rest, why the CSP needs no change, and the "no fetch handler" rule for the future service worker.
- `docs/architecture.md`: background work.
- `docs/database.md`: §2.
- `docs/assumptions.md`: `§C1-n` covering the device-is-opt-in default, collapse behaviour and VAPID derivation.
- `ONE-BOX-RUNBOOK.md`: the settings table.
- `docs/api.md`.

**nginx.** In `apps/web/nginx.conf` add:
- `location = /sw.js { add_header Cache-Control "no-cache"; try_files $uri =404; }`
- `location = /manifest.webmanifest { default_type application/manifest+json; add_header Cache-Control "no-cache"; try_files $uri =404; }`

Do not add `add_header` inside locations of `infrastructure/nginx/conf.d/atcraft.conf`: it would drop the server-level security headers. The files themselves arrive in C2; nginx is configured now so C2 only adds files.

## Tests (minimum)

**Unit**
- The RFC 8291 Appendix A vector, byte for byte.
- A VAPID JWT decodes with the public key, with the correct `aud` and `exp`.
- `assert_allowed_endpoint`: accepts the allowed hosts; refuses http, IP literals, foreign hosts and userinfo.
- `resolve_channels` matrix.
- Push queue staging and the after-commit hook.
- Worker claim / release logic, compiled SQL, mirroring the email worker tests.
- Outcome mapping.
- Every beat task is registered.
- Failure logging is confidential.

**Integration**
- Subscribe, list and delete; takeover by another user.
- `notify()` sets the PUSH channel only for users with a device and `push_enabled`.
- A collapsed repeat does not re-push.
- Logout-all removes the user's devices.

**API**
- Own subscriptions only (404 for foreign ids).
- A vendor user can register.
- An endpoint outside the allow-list answers 422.

Use a fake transport in the worker tests (monkeypatch `deliver_push`). Never call real push services from tests.
