# Security

Master rules sections 8, 38–41, 44, 51–55. This document records what is
implemented, where it lives, and what is deliberately still open.

---

## 1. Authentication

**Administrator-managed recovery identity.** Changing an existing user's login
email requires `user.update` and `user.reset_password`; changing a protected
SUPER_ADMIN or superuser account additionally requires super-administrator
authority. An unchanged normalized email remains a profile edit. Authorized
changes preserve MFA enrollment, invalidate outstanding reset links and their
queued delivery copies, revoke sessions in the same transaction, and update
session-liveness/permission caches after commit. Recovery email is treated as a
credential rather than an ordinary contact field.

**Password storage — Argon2id.** `src/core/security.py`, parameters following
OWASP guidance (19 MiB, t=2, p=1). A verify-only bcrypt path exists so an
inherited user table can be migrated: on a successful login with a legacy hash,
`needs_rehash()` triggers a transparent upgrade to Argon2id. New bcrypt hashes
are never produced.

**Password policy.** Minimum 12 characters, upper, lower, digit, symbol, not a
common password, and must not contain the local part of the user's email.
Enforced server-side in `validate_password_policy` — the frontend checklist is a
convenience, not the gate.

**Tokens.** Short-lived access token (15 min default) plus a rotating refresh
token (7 days). Both carry `jti`, issuer and audience claims and are verified for
all of signature, expiry, `nbf`, issuer and audience. Issuer and audience are
per deployment (`JWT_ISSUER`, `JWT_AUDIENCE`), so a token minted by one
deployment is rejected by the other deliberately rather than by an accident of
key rotation.

Three properties worth stating explicitly:

- **Permissions are not in the token.** They are resolved per request from the
  database, including roles, direct grants and denials. Revoking someone's
  approval right takes effect on the next request rather than at token expiry.
- **Refresh tokens rotate.** Each refresh revokes the presented session and
  issues a successor, linked by `replaced_by_id`. Presenting an already-replaced
  token is treated as replay: the whole session chain is revoked and audited.
- **A password change invalidates earlier tokens.** `deps.py` compares the
  token's `iat` against `password_changed_at`, so reacting to a compromise
  actually ends the attacker's session.

**Storage in the browser.** The access token lives in memory only. The refresh
JWT never appears in JSON: `/auth/login`, `/auth/mfa/verify` and `/auth/refresh`
set it as an `HttpOnly`, `SameSite=Strict` cookie - `atcraft_refresh`, path
`/api/v1/auth`, `Secure` when deployed - in `src/modules/auth/cookies.py`, so
script cannot read it. A second, readable cookie, `atcraft_csrf`, holds an HMAC
of that refresh JWT keyed on `JWT_SECRET`; it is not a secret. `/auth/refresh`,
`/auth/logout` and `/auth/logout-all` require the client to echo it in
`X-CSRF-Token`, and `require_refresh_csrf` compares cookie, header and HMAC in
constant time before anything is rotated or revoked. A cross-site form post
cannot set the header, and cookie tossing from a sibling host cannot forge the
HMAC. A mismatch answers 403 `CSRF_VALIDATION_FAILED` and leaves the session
intact. Both cookies are re-issued on every refresh and cleared on logout.

The SPA (`lib/api-client.ts`) treats the presence of `atcraft_csrf` as the hint
to restore a session after a reload. `localStorage` holds a sign-out marker so a
failed logout request cannot silently restore the session; it never holds a
credential. Recorded as `docs/assumptions.md` section 14.

**Forced password change.** A user whose password was set by an administrator
or the seed (`must_change_password`) may call only `/auth/me`,
`/auth/change-password`, `/auth/logout` and `/auth/logout-all`. Every other
route answers 403 `PASSWORD_CHANGE_REQUIRED` from the shared authentication
dependency in `deps.py`, so a newly added business route cannot forget the
restriction.

**Multi-factor authentication (TOTP).** Optional per account, RFC 6238 via
`pyotp`, enrolled and disabled by the user through `/auth/mfa/setup`,
`/auth/mfa/confirm` and `/auth/mfa/disable` (`src/modules/auth/service.py`).

- Enrolment re-checks the password, stages an encrypted seed and returns the
  secret and a provisioning URI once. The seed is inactive until `confirm`
  proves a code from the authenticator; confirming ends the user's other
  sessions.
- Disabling needs the password _and_ a current code, because success turns a
  stolen access token into a permanently MFA-free account. A wrong password
  here is throttled and counted like a failed sign-in.
- With MFA enabled, `POST /auth/login` issues no tokens. It returns
  `mfa_required: true` with a signed five-minute challenge token (type `mfa`,
  `amr: ["pwd"]`), and no session exists until `POST /auth/mfa/verify` accepts
  a code. Verification is rate-limited per account
  (`RateLimitPolicy.MFA_VERIFY`, 5 per 5 minutes), a wrong code is recorded as
  a failed attempt, and the last accepted time-step is persisted
  (`users.mfa_last_used_step`) so a code is single-use even when concurrent
  requests reach different API workers.
- Seeds are stored as Fernet ciphertext. The key is derived per purpose by
  HMAC-SHA256 from `CREDENTIAL_ENCRYPTION_KEY` (`_credential_fernet` in
  `core/security.py`), which is deliberately independent of `JWT_SECRET`:
  rotating the signing key does not touch enrolled authenticators, and rotating
  `CREDENTIAL_ENCRYPTION_KEY` invalidates every one of them - those users
  answer `MFA_INVALID` until they re-enrol. Back the key up with the database.
- `decrypt_totp_secret` still accepts the original plaintext Base32 shape and
  re-encrypts it on the next successful verification. The original schema change
  is included in the squashed baseline; see the
  [migration bridge](implementation/migration-squash-20261021.md) for older
  installations. A legacy plaintext row is handled as an upgrade case.
- A completed password reset disables MFA: mailbox recovery must also recover
  an account whose authenticator was lost. The user enrols again afterwards.
- `mfa_secret` is on the log-redaction list in `core/logging.py`.

**Password reset by email.** `POST /auth/password-reset/request` answers
identically whether or not the address belongs to an account. The service
stores only a SHA-256 digest of a 32-byte token in `password_reset_tokens`
(60-minute expiry, earlier tokens superseded), encrypts a delivery copy under
the `password-reset-delivery` purpose of `CREDENTIAL_ENCRYPTION_KEY`, and hands
it to a Celery task; the worker decrypts it and sends a link built on
`PUBLIC_WEB_URL`. The token travels in that link's URL _fragment_
(`/reset-password#token=...`), which a browser never sends to the server, so it
reaches neither the API's request log nor either nginx's access log; the reset
page reads it from the fragment and scrubs it from history. Redeeming it
through `/auth/password-reset/confirm` applies the password policy, lifts any
lockout, and revokes every session - a reset is the response to a suspected
compromise, not a routine edit.

Delivery is `EMAIL_PROVIDER=disabled` on a developer checkout, and `Settings`
refuses to boot a deployed process without `EMAIL_PROVIDER=smtp`, an HTTPS
`PUBLIC_WEB_URL`, `SMTP_HOST`, `EMAIL_FROM_ADDRESS` and STARTTLS or TLS: the
public endpoint must never claim a link was sent when nothing can deliver it.

**Lockout and rate limiting.** Five failed attempts lock an account for 15
minutes (configurable). Login is additionally rate-limited per email _and_ per IP
— per-IP alone is useless against a credential-stuffing run behind rotating
proxies, and per-email alone lets one host walk the whole user list.

**Enumeration resistance.** An unknown email still performs a dummy Argon2
verify against a constant hash, so response timing does not distinguish "no such
user" from "wrong password", and both return the identical error. Password reset
returns the same generic success message either way.

---

## 2. Authorisation

```
User → Roles → Permissions → Resource.Action
```

`src/core/permissions.py` holds the `resource.action` registry, mirrored by the
frontend permission catalogue and checked by unit tests. Roles bundle its
permissions; users may additionally carry direct grants and
direct denials, and **denials always win**, so "this buyer may not approve
payments" cannot be undone by adding another role.

Every route is gated:

```python
@router.post(
    "/purchase-orders/{po_id}/approve",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_APPROVE)],
)
```

**Privilege escalation guard.** A user cannot grant a permission they do not
themselves hold. Superusers are exempt; the check is unit-tested.

**Segregation of duties.** The approver of a BOM or an approval request may not
be its creator unless they are a superuser. Recorded as an assumption in
`docs/assumptions.md` because the master document did not fix the rule.

**Frontend permissions are UX only.** They hide controls a user cannot use. The
API re-checks every request. Anyone can edit the browser's copy; the server does
not consult it.

---

## 3. Tenant isolation

Every tenant-owned table carries `company_id`. `TenantContext` is derived
**only** from the verified token — there is no code path that reads a
client-supplied `company_id`, and `BaseRepository._scoped()` applies the filter
structurally, so forgetting it is not something an individual query can do.

Cross-tenant access returns `NOT_FOUND`, never `FORBIDDEN`. Confirming that a
record exists in another tenant is itself a disclosure.

A foreign key to a UUID is **not** a tenant check: it is satisfied by another
company's row. Every reference a write names is therefore read through the
tenant-scoped repository before the row is written. Cost centres are the
widest-reaching case - eight tables carry one - so every carrier goes through
one seam, `organization.service.require_cost_centre` / `require_cost_centres`,
which answers another company's id exactly as it answers an unknown one (404).

The application is single-company today. Nothing in the schema prevents adding
another.

---

## 4. Vendor portal isolation

Vendor users share `users` and carry `vendor_id` (and, optionally,
`allowed_category_ids`). Their isolation is structural - nothing depends on a
module remembering to filter by vendor:

1. **Route allow-list.** `core.deps.get_current_principal` lets a vendor
   principal reach only `/vendor-portal/**`, its own `/notifications/**` and auth
   self-service (`/auth/me`, logout, logout-all, change-password, sessions, MFA).
   Every other authenticated route answers 403 `SCOPE_DENIED` before a handler
   runs, whatever permissions the account's roles carry - a vendor role granted
   `dispatch.view` by mistake still reaches no dispatch. The predicate
   (`vendor_route_allowed`) matches whole path segments, so
   `/vendor-portal-x` is not `/vendor-portal`. The temporary-password gate is
   applied first.
2. **Portal rights.** The five system vendor roles hold only
   `vendor_portal.access`, `.respond`, `.ship`, `.invoice`, `.quality` and
   `notification.view` (none ends in `.view`, so the AUDITOR bundle does not
   absorb them). Each portal route names its right literally.
3. **Vendor principals only.** Every portal route also requires a vendor
   principal (`require_vendor_user`): an internal administrator holds every
   permission string and still gets 403 - nobody acts "as" a vendor by naming
   an order.
4. **Ownership is "not found".** The vendor comes from the verified token; no
   portal request carries a vendor id. Purchase-order reads and row locks are
   confined in `PurchaseOrderRepository._scoped` to the vendor's own orders that
   have been sent (a cancelled one only if it had been sent), and, for a
   category-scoped account, to its categories and their subtrees plus orders
   assigned to it by name (docs/assumptions.md §56). Receipts, consignments,
   files and PDF jobs are reached through those orders; bills and payments carry
   the vendor's id, and the finance repositories add the vendor filter for a
   vendor principal. Another vendor's order, file, receipt, job or bill answers 404.
5. **Allow-listed responses.** Portal read models name their fields. The buyer's
   notes, duplicate-override reasons, approval comments, source links, cost
   centres and invoice-approval notes are not in them.
6. **Shared files only.** A vendor lists and downloads only the files filed
   through the order's documents route and the generated PO PDF; anything a
   buyer filed against the order from the Documents screen stays internal
   (§C1-3).
7. **Bills.** A portal bill rejects unknown fields (no `vendor_id`, no
   `cost_centre_id`), must name one of the vendor's own orders, and may name
   only that order's lines and items and the vendor's own receipts against it -
   anything else is "not found", so finance's messages never name another
   vendor's receipt.
8. **Notifications** reach a vendor user only when the user holds the event's
   right and could open the order it links to (§C1-11); `notify_role` is never
   used with vendor role codes, which would reach every vendor of the company.

A PO's assignee must be active, in the tenant and the order's vendor, cover the
order's category (or be unrestricted) and hold `vendor_portal.respond`. The
buyer-side routes that act on orders - including acknowledge and shipment kept
for a buyer recording what a vendor phoned in, and the inbound board and
consignment companions - also carry `forbid_vendor_users` as a second line.

---

## Dealer portal isolation

Dealer users share `users` and carry `customer_id` pointing at a DEALER or
DISTRIBUTOR customer - never a vendor id alongside it. Their isolation mirrors
the vendor portal's, decision for decision:

1. **Route allow-list.** `core.deps.get_current_principal` lets a dealer
   principal reach only `/dealer-portal/**`, its own `/notifications/**` and
   auth self-service. Every other authenticated route answers 403
   `SCOPE_DENIED` before a handler runs, whatever permissions the account's
   roles carry. The predicate (`dealer_route_allowed`, docs/assumptions.md
   §C3-1) matches whole path segments.
2. **Portal rights.** The three system dealer roles hold only
   `dealer_portal.browse`, `.order`, `.account`, `.return` and
   `notification.view` (none ends in `.view`, so the AUDITOR bundle does not
   absorb them). Each portal route names its right literally.
3. **Dealer principals only.** Every portal route also requires a dealer
   principal (`require_dealer_user`): an internal administrator holds every
   permission string and still gets 403.
4. **Ownership is "not found".** The dealer comes from the verified token; no
   portal request carries a customer id. `SalesOrderRepository._scoped` and
   `DispatchRepository._scoped` confine a dealer principal to its own
   customer's rows, and the portal checks ownership before reading an order,
   its consignments, its tracking or its statement. Another dealer's order,
   consignment, tracking or statement answers 404.
5. **Allow-listed responses.** Portal read models name their fields. Standard
   cost, exact stock quantities, approval evidence, driver mobile numbers and
   freight costs are not in them (docs/assumptions.md §C3-2, §C3-7).
6. **Notifications** about a dealer order go to the assigned salesperson, else
   `sales_order.approve` holders - internal users only, never the dealer
   (§C3-6).

---

## 5. Input validation

Pydantic v2 validates every request body, query parameter and path parameter.
Constraints are declared on the schema, not checked ad hoc in handlers.

Shared query binding translates model-level failures into the same typed error
envelope as body validation. Client errors contain safe field messages without
echoing rejected input values or validator internals; unknown HTTP errors and
unexpected exceptions use the shared public messages. See
[API conventions](api.md) for status codes and response contracts.

Beyond that, the database carries the invariants that must hold regardless of
which code path writes: non-negative stock, `reserved <= on_hand`,
`allocated <= reserved`, balanced journal entries, quantity dispositions summing
to the inspected quantity.

SQL injection is structurally excluded: all queries go through SQLAlchemy with
bound parameters. The handful of raw `text()` statements (advisory locks,
sequence claims) use bound parameters too. Sortable columns are resolved against
an explicit allow-list — a client-supplied column name is never interpolated
into `ORDER BY`.

---

## 6. File upload and download

`src/modules/documents`. Every one of these is required, not optional:

- MIME type validated against an allow-list **and** the leading magic bytes
  sniffed, so a renamed executable is rejected;
- size enforced while streaming, not after buffering;
- storage key generated server-side as
  `<category>/<company>/<yyyy>/<mm>/<uuid7><ext>` — the client filename is kept
  for display and never used as a path;
- the resolved absolute path is asserted to be inside `STORAGE_PATH` before any
  read or write (path traversal), with unit tests covering `../../etc/passwd`
  and absolute-path inputs;
- downloads are authenticated and tenant-scoped, served through the API with
  `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`;
- SHA-256 checksum stored on upload.

The ASGI request-body limit counts received bytes even when `Content-Length`
is absent or incorrect. Multipart resources and partially written files are
closed or removed on validation failure, transaction failure and request
cancellation. Upload verification covers allowed content types; it is not an
antivirus scan.

Filesystem paths are never exposed. Nothing is served from a public directory.

> **Known gap.** No malware scanning. Authorized vendor portal uploads use the
> same validation and private storage, but file-content validation does not
> provide malware detection.

---

## 7. Transport and headers

Nginx terminates TLS (1.2/1.3), redirects HTTP, and sets HSTS,
`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
`Permissions-Policy` and a restrictive CSP with `frame-ancestors 'none'`.

The API sets the same headers itself. A header configured in exactly one place is
one that silently disappears when the topology changes.

CORS is an explicit origin list. A wildcard origin combined with credentials is
rejected at startup in production.

When deployed, `TrustedHostMiddleware` rejects any request whose `Host` is not
in `ALLOWED_HOSTS` - bare hostnames only; `Settings` refuses URLs, ports and
wildcards - plus `localhost`, `127.0.0.1` and `backend` for internal probes and
the hostnames of `CORS_ORIGINS`. It is registered last in `main.py` so it runs
ahead of every other middleware.

---

## 8. Rate limiting

Two layers, because they see different things.

| Layer       | Keyed on         | Catches                                     |
| ----------- | ---------------- | ------------------------------------------- |
| Nginx       | client IP        | volumetric floods, before they reach Python |
| Application | account, then IP | targeted attacks that look fine per-IP      |

Named policies (`RateLimitPolicy`) give expensive operations their own budget:
login, password reset, refresh, MRP runs, report exports, uploads, bulk imports.

The application limiter uses a sliding window over a Redis sorted set, evaluated
in a single Lua script — a fixed window lets an attacker send double the limit
across a boundary, and separate round-trips let two concurrent requests both
observe "under the limit".

It **fails open** if Redis is unavailable. A cache outage must not take
authentication offline, and Nginx still provides a floor.

### Sizing, and why the two layers are set so differently

A client address is a _site_, not a person: every user in the plant reaches the
ERP from one NAT address. So the Nginx layer cannot be a fair per-user budget,
and sizing it as though it were is how you end up refusing the busiest, most
legitimate office on the network. Fairness belongs to the application layer,
which knows the authenticated user and can be both strict and fair.

| Budget                             | Value                     | Keyed on           | Reasoning                                                                                                                              |
| ---------------------------------- | ------------------------- | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `api_general` (Nginx, both layers) | 3000 r/m, burst 300       | address            | An office of 50 doing ~30 req/min of real navigation is 1500 r/m; this is twice that, and still an order of magnitude below a scraper. |
| `api_auth` (Nginx)                 | 60 r/m, burst 30          | address            | Deliberately looser than the application's own login budget, so that budget is the one that binds.                                     |
| `RATE_LIMIT_DEFAULT_PER_MINUTE`    | 300                       | authenticated user | A detail page fires ~5 queries and a dialog on it ~5 more, so a clerk moving through 15 documents a minute spends ~150.                |
| `RATE_LIMIT_AUTH_PER_MINUTE`       | 20 (100 per 5-min window) | address            | A shift change is 50 people signing in inside one window, some twice. Caps account _spraying_ at 20 attempts a minute.                 |
| `LOGIN_PER_EMAIL`                  | 5 per 5 min               | account            | Fixed security constant. This, with the lockout, is what stops credential stuffing — not the per-address budget.                       |

Two of these were previously set as though the address budget were the fairness
control: `api_general` at 120 r/m refused a single operator working properly, and
`RATE_LIMIT_AUTH_PER_MINUTE` at 4 refused the twenty-first person at a shift
change — before their password was ever checked, which from the floor is
indistinguishable from the ERP being down.

When raising the per-address login budget, note what it does and does not
defend. It bounds how many _different_ accounts one address may try. It is not
what stops one account being guessed repeatedly; `LOGIN_PER_EMAIL` and the
account lockout do that, and both are fixed constants that no deployment tunes.

---

## 9. Audit trail

`audit_logs` is append-only by construction: it inherits
`ImmutableLedgerEntity` (no `updated_at`, no `deleted_at`, no version column),
and the migration installs a trigger that raises on `UPDATE` and `DELETE`. There
is no API endpoint that writes, edits or deletes an audit row.

Every entry carries who, what, when, before/after values for changed fields only,
the request id, IP address and user agent. Audit writes happen in the same
transaction as the change they describe — if the business write rolls back the
audit rolls back with it, and if the audit write fails the business change is not
committed.

Bank account numbers - a vendor's, and the company's own that is printed on its
invoices - are audited masked to their last four characters
(`src/common/banking.py`). Redirecting a payee account is how diverted-payment
and invoice-redirection fraud start, so the change must be provable without the
audit log becoming a second copy of the number. Two numbers ending in the same
digits mask alike, so a change to the company's account is also flagged
explicitly in the entry's context.

Reading requires `audit_log.view` and a bounded date range (default 30 days, max
366). An unbounded query against the largest table in the system is a denial of
service.

---

## 10. Logging

Structured JSON in production, with the request id, user id and company id bound
to the execution context and stamped on every record.

A redaction processor blanks a fixed key list — passwords, tokens, cookies,
secrets, PAN, Aadhaar, bank and card fields — recursively, before any record is
emitted. It does not rely on call sites remembering.

Errors returned to clients carry a stable code, a usable message and the
correlation id. Never a stack trace, SQL fragment, constraint or table name.
Constraint violations and transient PostgreSQL failures are translated into
safe business or retryable errors by `exception_handlers.py`; unexpected errors
are logged with their context and receive the shared generic response.

Incoming request IDs are accepted only when they are at most 64 ASCII
alphanumeric, hyphen, underscore or dot characters; otherwise the server
generates an ID. Client IP candidates must parse as an IP address before they
can enter logs or the audit table. Nginx overwrites proxy headers at the trusted
edge; these format checks do not establish the identity of an untrusted proxy.

---

## 11. Secrets

Environment variables only. The local `.env` is git-ignored; only `.env.example`
is committed. Each deployment keeps its values in `deploy/<name>.env` (mode 600,
generated by `scripts/deploy.sh init`, git-ignored), which Compose interpolates
into real environment variables for the containers; the application never
reads that file itself, and nothing is baked into an image.

Three secrets, three jobs: `JWT_SECRET` signs tokens and keys the CSRF HMAC,
`CREDENTIAL_ENCRYPTION_KEY` encrypts stored credentials (TOTP seeds, the
delivery copy of a reset token), `SMTP_PASSWORD` authenticates outbound mail.

`Settings` refuses to start a staging or production process with a weak or
missing `JWT_SECRET` or `CREDENTIAL_ENCRYPTION_KEY`, with the two keys equal,
with `APP_DEBUG=true`, with plaintext SMTP, or with an ambiguous/wildcard host.
A misconfigured deployment is an incident, not a warning.

Development generates an ephemeral per-process signing key when none is set, so
nobody is tempted to commit one.

---

## 12. Containers

Non-root users in both images. Multi-stage builds; the production backend image
contains no test or lint tooling and no source bind mounts. Pinned base images.
Health checks on every service. Only Nginx publishes ports to the host in
production — Postgres and Redis are reachable only on the internal network.

---

## Owned valuation export files (A07)

An internal user with `inventory.valuation_view` and `report.export` can poll and
download their own finance valuation job through `/reports/jobs/{id}` and
`/reports/jobs/{id}/download`. The service checks tenant, creator and the
server-set valuation task provenance. This grants no general report catalogue or
document access. Vendor and dealer users are refused, including export queuing.
Existing report readers retain
`report.view` for status and `document.view` for the file.

The queue captures the verified warehouse assignments on the snapshot; the worker
uses that scope and the stored filters rather than broker arguments. Legacy queued
jobs capture their active internal creator's current assignments before reading
stock. A malformed capture fails closed. Restricted readers must still have access
to every captured warehouse; a whole-company or untracked file is refused. These
checks also apply to readers with general report/document permissions, document
metadata and paginated lists, and direct downloads by a known file ID.

Valuation snapshots retain the scope evidence while their workbook exists, even
after the job's normal expiry. New workbooks carry an inventory-valuation entity
attachment. Recognizable legacy valuation files without scope evidence are hidden
from restricted readers. Empty assignments and platform superusers retain access
within the verified company.

The catalogue-backed **Inventory valuation** report additionally requires
`report.view` and `inventory.valuation_view`; **Product profitability** requires
`report.view` and `item.cost_view`. The same definition permission is checked
when running a report and when reading its generic job. The finance-owned
valuation export exception above remains limited to its verified creator and
warehouse scope.

## Owned job-work challan files (E7b)

An internal `job_work.view` holder may read and download a challan job they
created. Tenant, creator and the exact `JOB_WORK_CHALLAN` key must match; the
download accepts only the authorized job ID and opens its bound file. This
exception grants no report catalogue or general document access. Vendor and
dealer accounts cannot use these routes. Other report readers retain their
existing document-view requirement.

`JOB_WORK_COST` exports also require `item.cost_view` through the generic
document list, metadata and download routes. Knowing a generated file ID does
not bypass the report's cost permission.

## Pre-production checklist

- [ ] `JWT_SECRET` generated with `secrets.token_urlsafe(64)`, unique per environment
- [ ] `CREDENTIAL_ENCRYPTION_KEY` independently generated and securely backed up
- [ ] `APP_DEBUG=false`, `APP_ENV=production`, `LOG_JSON=true`
- [ ] `CORS_ORIGINS` set to the real frontend origin — no wildcard
- [ ] `ALLOWED_HOSTS` lists the public hostname; `JWT_ISSUER` and
      `JWT_AUDIENCE` differ from every other deployment
- [ ] `EMAIL_PROVIDER=smtp`, HTTPS `PUBLIC_WEB_URL`, and a reset link actually
      arrives from the configured relay
- [ ] MFA enrolled for every administrator account
- [ ] TLS certificates in place; HTTP redirects to HTTPS
- [ ] Database password rotated off the development default
- [ ] Postgres and Redis not published to the host
- [ ] Seeded superuser password changed; `must_change_password` verified
- [ ] Automated backups running **to external storage**, and a restore tested
- [ ] `pip-audit` and `npm audit` clean, or every finding triaged
- [ ] Log retention and rotation configured; disk usage alerted
- [ ] Rate limits reviewed against expected legitimate traffic
- [ ] `/docs`, `/redoc` and `/openapi.json` confirmed disabled in production
- [ ] Audit-log immutability trigger verified present in the deployed schema

Sales invoices are tenant scoped in every source lookup, list, detail, issue,
cancellation and PDF operation. Viewing/preview/PDF require sales_invoice.view;
issue requires sales_invoice.create; cancellation requires sales_invoice.cancel.
System ADMIN/SUPER_ADMIN roles receive cancellation through a grant migration;
new Accounts presets include it, while client-managed roles remain client owned.
Receipt allocation uses payment.record and verifies customer and currency under
payment/invoice locks. Customer receipt cancellation releases allocations and
reverses its journal atomically. Permanent PDF downloads use the existing
report-job/document authorisation and tenant checks. No public surface is added.

## Web Push

Push endpoints are bearer capabilities. Store their SHA-256 digest for unique
registration and their URL encrypted with purpose-separated Fernet
(`push-subscription`); never return or log the endpoint, keys or payload. Browser
registration is opt-in. An endpoint can transfer to the new signed-in owner on a
shared plant PC; later device updates are guarded by owner and registration stamp.
Logout-all, password changes/resets and user deactivation remove registrations.
Delivery requires an active account with a session within the refresh-token window.
Recovery applies the same account and device eligibility before its batch limit;
inactive users and abandoned devices cannot occupy the first 100 rows indefinitely.

The server accepts HTTPS on port 443 only, without userinfo, fragments or IP
literals. Hosts must match a configured suffix on a dot boundary. Delivery
revalidates the decrypted endpoint, verifies TLS through HTTPSConnection and
never follows redirects. No CSP change is needed: provider calls originate from
the notification worker, and browser registration uses the browser's Push API.
The shipped `/sw.js` has **no fetch handler or cache**. It never reads tokens,
credentials or authenticated API responses. Notification links accept only
same-origin relative URLs; other targets fall back to `/notifications`.

Staff, vendor and dealer shells refresh an existing granted subscription without
requesting permission. Opt-in starts directly from the device card's Enable
button. Key changes replace the provider subscription, and API registration
refreshes the device's last-seen time. Device controls also appear on the shared
profile and notification preferences screens. Event preferences include a
separate Push channel; only changed preference rows are submitted.

Logout cancels pending setup before revoking the session. Browser-provider
cleanup has a two-second foreground budget, and a late native subscription is
disposed before a subsequent account can register this device. Stalled browser
promises cannot keep the logout screen waiting indefinitely. Server-side
revocation remains the enforcement point if browser or network cleanup fails.

Password and MFA sign-in share a cancellable authentication attempt. Replacing
an attempt, returning from MFA to password sign-in, or logging out aborts the
older request before a replacement can set session cookies. Sign-in also aborts
an earlier logout before sending credentials; ignoring its eventual JSON would
not prevent a late cookie-expiration header. If a replacement follows a pending
logout, old user data and query caches are cleared even when the new credentials
fail. MFA submissions have pending and duplicate guards. Regression tests assert
fetch-abort ordering as well as the visible account state.

Encryption and VAPID follow [RFC 8291](https://www.rfc-editor.org/rfc/rfc8291.html)
and [RFC 8292](https://www.rfc-editor.org/rfc/rfc8292.html). The deterministic VAPID
key uses a separate HMAC purpose from the root credential key. An explicit VAPID
key keeps that browser-facing identity independent, but stored subscription
payloads are still encrypted under the root. Root-key rotation therefore also
requires a deliberate re-encryption procedure or new device subscriptions; a
stable VAPID key alone does not recover old ciphertext.

## Warehouse assignments on stock documents and movements (A02/A03)

Warehouse assignments use the shared policy in `src/common/warehouse_scope.py`.
An empty assignment means unrestricted warehouse access within the verified
company. A platform superuser is also exempt from warehouse assignments;
neither case bypasses company isolation.

Transfer visibility and actions require **both** source and destination to be
assigned. Adjustment and physical-count list/detail/line/action lookups use the
same warehouse restriction before pagination or locking. The policy also
applies to procurement and production warehouse destination lookups.

Posting rechecks documents created before a restriction was assigned. The
movement engine checks every movement, additional transit grain and prelocked
grain before it locks or creates a balance. Trusted background/system contexts
retain their existing empty assignment while still requiring tenant-scoped
master records. A user's request context is never widened for posting.

## PDF text and external resources

Every untrusted value passed to a ReportLab `Paragraph` is XML-escaped,
including party names, addresses, item descriptions, packing notes and report
headings. This prevents stored text from becoming markup or an external image
request during background PDF generation. The PDF regression suite injects
file and HTTP image tags across all six document renderers and refuses any
image-loader call. Generated document access still uses its tenant and report
permissions; escaping does not replace those checks.
