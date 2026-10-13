# Security

Master rules sections 8, 38–41, 44, 51–55. This document records what is
implemented, where it lives, and what is deliberately still open.

---

## 1. Authentication

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
  database, Redis-cached with explicit invalidation. Revoking someone's approval
  right takes effect immediately rather than at the next token expiry.
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
- Disabling needs the password *and* a current code, because success turns a
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
  re-encrypts it on the next successful verification (revision
  `a4f7c29d8e11`). A plaintext row during a rolling upgrade is expected, not
  corruption.
- A completed password reset disables MFA: mailbox recovery must also recover
  an account whose authenticator was lost. The user enrols again afterwards.
- `mfa_secret` is on the log-redaction list in `core/logging.py`.

**Password reset by email.** `POST /auth/password-reset/request` answers
identically whether or not the address belongs to an account. The service
stores only a SHA-256 digest of a 32-byte token in `password_reset_tokens`
(60-minute expiry, earlier tokens superseded), encrypts a delivery copy under
the `password-reset-delivery` purpose of `CREDENTIAL_ENCRYPTION_KEY`, and hands
it to a Celery task; the worker decrypts it and sends a link built on
`PUBLIC_WEB_URL`. The token travels in that link's URL *fragment*
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
minutes (configurable). Login is additionally rate-limited per email *and* per IP
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

`src/core/permissions.py` holds the registry — roughly 180 `resource.action`
strings. Roles bundle them; users may additionally carry direct grants and
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

Vendor users share the `users` table but carry `vendor_id`, and:

- may hold only vendor roles, and vendor roles may only be assigned to them —
  enforced in both directions;
- are scoped to their own vendor's records;
- may be restricted further to specific item categories
  (`allowed_category_ids`), which is how "Manager 1 → Lock Body, Manager 2 →
  Handles" is expressed. The list must name live categories of the tenant that
  the vendor is qualified for (`vendor_categories`); the users service refuses
  anything else;
- can be blocked wholesale from internal endpoints with `forbid_vendor_users`,
  a second line of defence behind the permission check.

**Purchase orders.** `PurchaseOrderRepository._scoped` applies the vendor scope
to every read and lock, so a vendor cannot reach an order by any path the rule
does not allow, and an order outside it is "not found", never "forbidden":

- only its own vendor's orders;
- only orders it has been sent - SENT and every later status, and a CANCELLED
  order only if it had been sent. A DRAFT, PENDING_APPROVAL, APPROVED or
  REJECTED order is the buyer's working document, with unapproved prices;
- when `allowed_category_ids` is not empty, only orders whose
  `item_category_id` is one of those categories or beneath one, plus any order
  assigned to that user by name. An empty list is unrestricted.

The buyer-side routes that would otherwise reveal other vendors' orders or the
requisition book are closed to vendor accounts with `forbid_vendor_users`:
`POST /procurement/purchase-orders/check-duplicates` (it answers with open
orders for whichever vendor the payload names), the requisition conversion
preview and convert routes, and `GET /purchase-orders/{id}/vendor-assignees`.
An order's `assigned_vendor_user_id` is validated wherever it is set - tenant,
active, the order's vendor, the vendor-side order right, and the order's
category or unrestricted - because a UUID foreign key checks none of that.

---

## 5. Input validation

Pydantic v2 validates every request body, query parameter and path parameter.
Constraints are declared on the schema, not checked ad hoc in handlers.

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

Filesystem paths are never exposed. Nothing is served from a public directory.

> **Known gap.** No malware scanning. Worth adding (ClamAV sidecar) if vendors
> are given upload access from outside the network.

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

| Layer | Keyed on | Catches |
|---|---|---|
| Nginx | client IP | volumetric floods, before they reach Python |
| Application | account, then IP | targeted attacks that look fine per-IP |

Named policies (`RateLimitPolicy`) give expensive operations their own budget:
login, password reset, refresh, MRP runs, report exports, uploads, bulk imports.

The application limiter uses a sliding window over a Redis sorted set, evaluated
in a single Lua script — a fixed window lets an attacker send double the limit
across a boundary, and separate round-trips let two concurrent requests both
observe "under the limit".

It **fails open** if Redis is unavailable. A cache outage must not take
authentication offline, and Nginx still provides a floor.

### Sizing, and why the two layers are set so differently

A client address is a *site*, not a person: every user in the plant reaches the
ERP from one NAT address. So the Nginx layer cannot be a fair per-user budget,
and sizing it as though it were is how you end up refusing the busiest, most
legitimate office on the network. Fairness belongs to the application layer,
which knows the authenticated user and can be both strict and fair.

| Budget | Value | Keyed on | Reasoning |
|---|---|---|---|
| `api_general` (Nginx, both layers) | 3000 r/m, burst 300 | address | An office of 50 doing ~30 req/min of real navigation is 1500 r/m; this is twice that, and still an order of magnitude below a scraper. |
| `api_auth` (Nginx) | 60 r/m, burst 30 | address | Deliberately looser than the application's own login budget, so that budget is the one that binds. |
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | 300 | authenticated user | A detail page fires ~5 queries and a dialog on it ~5 more, so a clerk moving through 15 documents a minute spends ~150. |
| `RATE_LIMIT_AUTH_PER_MINUTE` | 20 (100 per 5-min window) | address | A shift change is 50 people signing in inside one window, some twice. Caps account *spraying* at 20 attempts a minute. |
| `LOGIN_PER_EMAIL` | 5 per 5 min | account | Fixed security constant. This, with the lockout, is what stops credential stuffing — not the per-address budget. |

Two of these were previously set as though the address budget were the fairness
control: `api_general` at 120 r/m refused a single operator working properly, and
`RATE_LIMIT_AUTH_PER_MINUTE` at 4 refused the twenty-first person at a shift
change — before their password was ever checked, which from the floor is
indistinguishable from the ERP being down.

When raising the per-address login budget, note what it does and does not
defend. It bounds how many *different* accounts one address may try. It is not
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
correlation id. Never a stack trace, never a SQL fragment, never a constraint or
table name. Constraint violations are translated into business language by
`exception_handlers.py`.

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
