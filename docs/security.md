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
all of signature, expiry, `nbf`, issuer and audience.

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
token is persisted in `localStorage` so a reload does not sign the user out.

> **Known gap.** An httpOnly, `SameSite=Strict` refresh cookie would put the
> refresh token beyond the reach of script entirely. That requires the API to set
> cookies and a CSRF strategy on the refresh endpoint. It is the recommended
> hardening step before this is exposed to the public internet.

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
  Handles" is expressed;
- can be blocked wholesale from internal endpoints with `forbid_vendor_users`,
  a second line of defence behind the permission check.

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
is committed. Staging and production inject real environment variables through
the compose overlays rather than shipping a file, and nothing is baked into an
image.

`Settings` refuses to start a production process with a weak or missing
`JWT_SECRET`, with `APP_DEBUG=true`, or with a wildcard CORS origin alongside
credentials. A misconfigured production deployment is an incident, not a warning.

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
- [ ] `APP_DEBUG=false`, `APP_ENV=production`, `LOG_JSON=true`
- [ ] `CORS_ORIGINS` set to the real frontend origin — no wildcard
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
