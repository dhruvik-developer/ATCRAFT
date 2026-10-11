# WP D2: WhatsApp and SMS notification channels

Lane B, round 4, step 2. It runs after B4 in the same lane. Placeholder prefix: `§D2-n`.

**Proposal.** Channels list "WhatsApp / SMS (future)"; Future Enhancement lists "WhatsApp notification". The client now wants them present.

**Approach.** Build real, working channel integrations behind configuration, **disabled by default**. They activate once Atcraft supplies its own accounts. The proposal says third-party SMS/WhatsApp charges are borne by the client. Accounts needed:
- a Meta WhatsApp Business (Cloud API) account with approved message templates;
- an Indian SMS gateway with TRAI DLT-registered sender and templates.

**No new dependency.** Use the stdlib HTTPS transport pattern from `src/core/webpush.py` (C1b): `http.client` in a thread, a host allow-list, and confidential logging.

**Background.** `reports/notifications-alerts.md` sections 1–7 and the C1b push design now on main. Copy the push channel's structure exactly: channel enum, preference column, delivery marker, worker claim/send/release, and the recovery sweep.

## Decisions (binding)

### 1. Channels

- Add `NotificationChannel.WHATSAPP` and `NotificationChannel.SMS`.
- `notification_preferences` gains `whatsapp_enabled` and `sms_enabled` (default **false**, explicit opt-in per type).
- `notifications` gains `whatsapp_delivered_at` and `sms_delivered_at`.
- `resolve_channels` adds a channel only when **all** of these hold:
  - the company has the channel configured and enabled;
  - the user has a verified phone and consent for that channel;
  - the per-type preference is on.
- CRITICAL severity keeps the IN_APP-only rule; record it as `§D2-n`.

### 2. Consent and phone

On `users`:
- `phone_e164` String(16): E.164, validated, India `+91` normalisation helper.
- `whatsapp_opt_in_at` / `sms_opt_in_at` timestamptz: the consent records.

Self-service routes:
- `PUT /auth/me/phone` (body `{phone}`, own record): changing the phone clears both opt-ins.
- `POST /auth/me/messaging-consent` (body `{channel, opt_in: bool}`): audited, since this is consent evidence.

Never send to a user without consent. Document the TRAI/WhatsApp consent basis in `security.md`.

### 3. Configuration

**Settings:**

| Setting | Notes |
|---|---|
| `WHATSAPP_ENABLED` | false |
| `WHATSAPP_API_BASE` | default `https://graph.facebook.com` |
| `WHATSAPP_API_VERSION` | default `v20.0` |
| `WHATSAPP_PHONE_NUMBER_ID` | |
| `WHATSAPP_ACCESS_TOKEN` | SecretStr; secret |
| `SMS_ENABLED` | false |
| `SMS_PROVIDER` | `msg91` \| `generic_json` |
| `SMS_API_URL` | |
| `SMS_AUTH_KEY` | SecretStr; secret |
| `SMS_SENDER_ID` | |
| `SMS_DLT_ENTITY_ID` | |
| `MESSAGING_ALLOWED_HOSTS` | default covers graph.facebook.com and the configured SMS host only |

Each goes in `.env.example`, compose `x-api-env`, `deploy/*.env.example`, `scripts/deploy.sh` for the secrets, and the runbook table. `_enforce_production_safety` requires the credentials when a channel is enabled.

**Per-company template mapping** (new table `notification_channel_templates`, TenantEntity):
- columns: `channel`, `notification_type`, `template_name` (WhatsApp) or `template_id` (SMS DLT), `language` (default `en`), `parameters` (JSONB ordered list drawn from a closed vocabulary: `title`, `body`, `code`, `link`, `amount`, `date`), `sms_text` (the registered DLT text with `{#var#}` placeholders), `is_active`;
- unique `(company_id, channel, notification_type)`;
- a notification type with no active template for a channel is simply not sent on that channel (`§D2-n`).

Routes: `GET/PUT /notifications/channel-templates` (`settings.view` / `settings.manage`, audited CONFIG_CHANGE), plus `POST /notifications/channel-templates/test` (`settings.manage`), which sends one test message to the caller's own phone.

`GET /notifications/channels` (`notification.view`) returns which channels are enabled, so the UI can show only the usable columns.

### 4. Providers (`src/core/messaging.py`)

- `WhatsAppCloudProvider.send_template(to_e164, template_name, language, parameters)` → POST `/{version}/{phone_number_id}/messages` with `{"messaging_product":"whatsapp","to":...,"type":"template","template":{"name":...,"language":{"code":...},"components":[{"type":"body","parameters":[{"type":"text","text":...}]}]}}` and a bearer token.
- `Msg91SmsProvider` (Flow API shape) and `GenericJsonSmsProvider` (configurable JSON body) → send DLT template id, sender, entity id, recipient and variables.
- Outcome mapping:
  - 2xx → DELIVERED (store the provider message id in a delivery log if returned);
  - 4xx permanent → FAILED;
  - 429/5xx/OSError → RETRY.
- Never log phone numbers, tokens or bodies. Add them to `SENSITIVE_KEYS`.

### 5. Workers

- `notifications.send_whatsapp` and `notifications.send_sms`: claim / send outside the transaction / release on retry, as in C1b.
- The `dispatch_pending_deliveries` recovery sweep covers both channels.
- Extend `MAIL_TASKS` / confidential failure logging and `test_worker_failure_logging.py`.
- Add a lightweight `notification_deliveries` log table (ImmutableLedgerEntity: `notification_id`, `channel`, `outcome`, `provider_message_id`, `attempted_at`, `error_code`) so the admin can see what was sent. Index `(company_id, notification_id)`.

### 6. Frontend

- **Profile (staff, vendor app, dealer app):** a "Mobile number & messaging" card with the E.164 input and WhatsApp / SMS consent toggles. Show it only if `/notifications/channels` says the channel is enabled.
- **Notification preferences:** WhatsApp and SMS columns, shown only when enabled.
- **New admin page `/admin/messaging`** ("Messaging templates", gate `[P.SETTINGS_VIEW]`):
  - per notification type: WhatsApp template name, language and parameter order; SMS DLT template id and text;
  - test-send button;
  - status banner saying whether the server channel is configured;
  - add it to the sweeps and the handbook.

### 7. Docs

- `security.md`: third-party data processors, consent, and that no credentials are stored in the DB.
- `ONE-BOX-RUNBOOK.md`: a "How to enable WhatsApp/SMS" step list (Meta Business verification, template approval, DLT registration).
- `assumptions.md` (`§D2-n`), `api.md`, handbook.

## Tests

- **Unit:**
  - exact provider request bodies (WhatsApp JSON, MSG91 JSON) from fixtures;
  - outcome mapping;
  - E.164 normalisation;
  - `resolve_channels` matrix (consent, preference, config, CRITICAL);
  - template parameter rendering;
  - host allow-list.
- **Integration:**
  - consent routes are audited;
  - a notification gets the WHATSAPP channel only with consent + template + preference;
  - the worker claims and releases using a fake transport;
  - the delivery log is written;
  - template CRUD;
  - test-send uses the caller's own phone only.
- **Frontend:** the profile card's request bodies, the preferences columns appearing only when enabled, and the messaging admin page.
