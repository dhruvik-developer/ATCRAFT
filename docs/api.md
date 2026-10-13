# API conventions

Base path `/api/v1`. Interactive documentation at `/docs` (disabled in
production — the schema is an accurate map of every endpoint and its
permissions).

## Where the contract lives

| Artefact | What it is |
|---|---|
| `openapi.json` | The contract. Generated from the running app by `make contract`, committed, and checked by CI and the pre-push hook, so it cannot drift from the code. |
| `/docs`, `/redoc` | Swagger UI and ReDoc, served from that same schema in development. Both are **off in staging and production**: the schema lists every endpoint and the permission it needs, which is a map worth not publishing. Read it from the committed file instead. |
| `apps/web/src/types/openapi.ts` | The frontend's types, generated from the contract. |
| `postman_collection.json` | A Postman collection, generated from the contract. |

All four are regenerated together:

```bash
make contract          # writes all of them (Windows: .\make.ps1 contract)
make postman           # the collection only (Windows: .\make.ps1 postman)
make contract-check    # fails if any is stale (Windows: .\make.ps1 contract-check)
```

CI and the pre-push hook run the same three checks `contract-check` runs.

### Using the Postman collection

Import `postman_collection.json` (Postman v2.1 format, 533 requests in 27
folders, one per tag). Then:

1. Set the `base_url` variable if it is not `http://127.0.0.1:8000/api/v1`.
2. Send **Sign in** under *Authentication*. Its post-response script stores the
   access token and the CSRF value in collection variables.
3. Send anything else — bearer auth is inherited from the collection root.

The refresh token is deliberately absent from every response body: it is an
HttpOnly cookie, and Postman's cookie jar replays it on `/auth/refresh`, which
is why that request carries the `X-CSRF-Token` header. Request bodies are
generated from each endpoint's own schema with required fields first, so they
show the shape; the values are placeholders, not valid business data.

Do not edit the file by hand — `make contract` overwrites it.

---

## Response envelope

Every response has the same shape. Clients never have to guess.

**Success**

```json
{
  "success": true,
  "data": { "id": "0193f2a1-...", "code": "PO/2026-27/00042" },
  "message": "Purchase order created"
}
```

**Offset-paginated list**

```json
{
  "success": true,
  "data": [ ... ],
  "pagination": {
    "page": 1,
    "limit": 50,
    "total": 1284,
    "total_pages": 26,
    "has_next": true,
    "has_previous": false
  },
  "message": "Operation completed successfully"
}
```

`total` may be `null`. Counting a large filtered set is a second scan, and past
`COUNT_CEILING` rows the exact number is not worth paying for.

**Cursor-paginated list** — used for the inventory ledger, audit log and
notifications, which grow without bound.

```json
{
  "success": true,
  "data": [ ... ],
  "pagination": { "limit": 50, "next_cursor": "eyJ0Ijoi...", "has_next": true }
}
```

**Error**

```json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_AVAILABLE_STOCK",
    "message": "Insufficient available stock for LOCK-BODY-01 at WH-RM: requested 120, available 45.",
    "details": { "item_code": "LOCK-BODY-01", "requested": "120", "available": "45" }
  },
  "request_id": "9f2c4a1b8e7d..."
}
```

**Accepted background job** — HTTP 202.

```json
{
  "success": true,
  "data": { "job_id": "…", "status": "queued", "status_url": "/api/v1/mrp/runs/…" },
  "message": "Job queued for background processing"
}
```

---

## Status codes

| Code | Meaning here |
|---|---|
| 200 | success |
| 201 | resource created |
| 202 | accepted, running in the background |
| 204 | success, no body |
| 400 | malformed request |
| 401 | not authenticated, or token invalid/expired/revoked |
| 403 | authenticated but missing the required permission |
| 404 | not found **in your tenant** |
| 409 | conflict: concurrent edit, duplicate document, invalid state transition, lock timeout |
| 413 | body too large |
| 422 | validation or business-rule failure |
| 429 | rate limited (see `Retry-After`) |
| 500 | unexpected server error — quote `request_id` |
| 503 | dependency unavailable, or the request exceeded the statement timeout |

A record belonging to another company returns **404, not 403**. Confirming it
exists elsewhere is itself a disclosure.

---

## Error codes

Stable strings the frontend switches on. Renaming one is a breaking change.

| Group | Codes |
|---|---|
| Auth | `UNAUTHENTICATED` `INVALID_CREDENTIALS` `TOKEN_EXPIRED` `TOKEN_INVALID` `TOKEN_REVOKED` `ACCOUNT_LOCKED` `ACCOUNT_INACTIVE` `PASSWORD_POLICY` `MFA_INVALID` `PASSWORD_CHANGE_REQUIRED` |
| Authz | `FORBIDDEN` `PERMISSION_DENIED` `TENANT_MISMATCH` `SCOPE_DENIED` `CSRF_VALIDATION_FAILED` |
| Generic | `VALIDATION_ERROR` `NOT_FOUND` `CONFLICT` `RATE_LIMITED` `PAYLOAD_TOO_LARGE` `SERVICE_UNAVAILABLE` `INTERNAL_ERROR` |
| Business | `BUSINESS_RULE_VIOLATION` `INVALID_STATE_TRANSITION` `APPROVAL_REQUIRED` `DUPLICATE_DOCUMENT` `REFERENCED_RECORD_IN_USE` |
| Inventory | `INSUFFICIENT_STOCK` `INSUFFICIENT_AVAILABLE_STOCK` `STOCK_LOCK_TIMEOUT` `NEGATIVE_STOCK_BLOCKED` `BATCH_REQUIRED` `SERIAL_REQUIRED` `RESERVATION_NOT_FOUND` `RESERVATION_ALREADY_CONSUMED` |
| BOM / MRP | `BOM_NOT_FOUND` `BOM_CYCLE_DETECTED` `BOM_DEPTH_EXCEEDED` `BOM_NOT_APPROVED` `MRP_RUN_IN_PROGRESS` |
| Files | `FILE_TYPE_NOT_ALLOWED` `FILE_TOO_LARGE` `FILE_NOT_FOUND` |
| Jobs | `JOB_NOT_FOUND` `JOB_FAILED` |

Five of these are easy to mishandle:

- `PASSWORD_CHANGE_REQUIRED` (403) is raised by the shared authentication
  dependency on every route outside `/auth/me`, `/auth/change-password`,
  `/auth/logout` and `/auth/logout-all` while `must_change_password` is set.
- `MFA_INVALID` (401) is a wrong, replayed or unavailable authenticator code
  during the MFA step. `MFA_REQUIRED` exists in `ErrorCode` but is never sent:
  the MFA step is signalled by `mfa_required: true` in a 200 login response
  (see Authentication below).
- `INVALID_CREDENTIALS` and `MFA_INVALID` change status with the route. They
  are 401 on the unauthenticated auth routes (`/auth/login`, `/auth/mfa/verify`,
  `/auth/refresh`) and 422 with `details.fields` on the authenticated
  self-service routes (`/auth/change-password`, `/auth/mfa/setup`,
  `/auth/mfa/confirm`, `/auth/mfa/disable`), naming the control that was wrong
  (`current_password`, `code`). The SPA treats any 401 on an authenticated route
  as a dead session and signs the user out, which would hide the message.
- `CSRF_VALIDATION_FAILED` (403) is the refresh double-submit check failing.
  The session is retained; reload the CSRF cookie and retry.
- `INSUFFICIENT_STOCK` (422) is an on-hand or pool shortfall at the moment of
  an issue, an assembly consume or a job-work return. It is distinct from
  `INSUFFICIENT_AVAILABLE_STOCK`, the reservation-aware check on allocation.

Validation failures carry field paths so a form can map them onto controls:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Some fields need attention.",
    "details": { "fields": { "lines.0.quantity": ["Input should be greater than 0"] } }
  }
}
```

---

## Authentication

Sign in with an email address and password:

```http
POST /api/v1/auth/login
{ "email": "planner@atcraft.example", "password": "...", "device_label": "Chrome on the planning PC" }
```

`data` takes one of two shapes. Without MFA on the account the sign-in
completes:

```json
{
  "access_token": "...",
  "token_type": "bearer",
  "expires_in": 900,
  "refresh_expires_in": 604800,
  "session_id": "...",
  "mfa_required": false,
  "user": { "id": "...", "email": "...", "full_name": "...", "company_id": "...",
            "is_superuser": false, "must_change_password": false, "roles": ["planner"] }
}
```

With MFA enabled, the password stage succeeded but no session exists yet:

```json
{ "mfa_required": true, "challenge_token": "...", "expires_in": 300 }
```

Complete it within five minutes with the six-digit code from the authenticator.
The response is the first shape above:

```http
POST /api/v1/auth/mfa/verify
{ "challenge_token": "...", "code": "123456" }
```

A wrong or replayed code answers 401 `MFA_INVALID`; an expired or malformed
challenge answers 401 `TOKEN_INVALID`. Then on every request:

```http
Authorization: Bearer <access_token>
```

**No refresh token is ever in JSON.** Alongside either successful response the
server sets two cookies:

| Cookie | Attributes | Purpose |
|---|---|---|
| `atcraft_refresh` | `HttpOnly`, `SameSite=Strict`, `Path=/api/v1/auth`, `Secure` when deployed | the refresh JWT itself; script cannot read it |
| `atcraft_csrf` | readable, `SameSite=Strict`, `Path=/` | an HMAC of that JWT, not a secret; the double-submit value |

Access tokens last 15 minutes. Refresh with a body-less `POST /auth/refresh`,
sending the cookies plus the header `X-CSRF-Token: <value of atcraft_csrf>`.
`data` is `{ "access_token", "token_type", "expires_in", "refresh_expires_in",
"session_id" }` and both cookies are re-set: the refresh token **rotates** on
every use, and presenting an already-replaced one is treated as replay and
revokes the entire session chain. A missing refresh cookie answers 401
`TOKEN_INVALID`; a missing or mismatched header answers 403
`CSRF_VALIDATION_FAILED` and leaves the session intact.

`POST /auth/logout` and `POST /auth/logout-all` take the bearer token, require
the same `X-CSRF-Token` header whenever the refresh cookie is present, and clear
both cookies.

A non-browser client keeps a cookie jar and echoes `atcraft_csrf` in
`X-CSRF-Token`. There is no body-based refresh.

A user whose password was set by an administrator (`must_change_password:
true`) may call only `/auth/me`, `/auth/change-password`, `/auth/logout` and
`/auth/logout-all` until it is replaced; every other route answers 403
`PASSWORD_CHANGE_REQUIRED`.

MFA is enrolled by the signed-in user: `POST /auth/mfa/setup` (current
password) returns the secret and a provisioning URI once, `POST
/auth/mfa/confirm` (a code) activates it, `POST /auth/mfa/disable` (password
and a code) removes it. Password reset is `POST /auth/password-reset/request`
(email; always 200 with the same message) followed by `POST
/auth/password-reset/confirm` (token from the emailed link, new password).

Permissions are not carried in the token. `GET /auth/me` returns the current
effective set, and the server re-resolves it per request so a revoked right takes
effect immediately.

---

## Pagination, sorting, filtering

```http
GET /api/v1/inventory/balances
      ?page=1&limit=50
      &sort_by=available_quantity&sort_dir=desc
      &search=lock
      &warehouse_id=…&below_reorder=true
      &with_total=false
```

| Parameter | Notes |
|---|---|
| `page` | 1-based |
| `limit` | max 200 |
| `sort_by` | validated against a per-endpoint allow-list; anything else is a 422 |
| `sort_dir` | `asc` \| `desc` |
| `search` | trigram search across the endpoint's declared searchable columns |
| `with_total` | `false` skips the count on very large tables |
| `cursor` | keyset endpoints only; opaque — do not parse it |

Filtering is server-side, always. There is no endpoint that returns everything
for the client to filter.

---

## Numbers

**Money and quantities are transported as decimal strings**, never JSON numbers:

```json
{ "quantity": "1250.0000", "unit_price": "184.750000", "line_total": "230937.50" }
```

A JSON number is an IEEE-754 double. `0.1 + 0.2 !== 0.3`, and a four-decimal
quantity does not round-trip. The API serialises `Decimal` as a string; clients
parse with a decimal library before computing.

| Kind | Precision |
|---|---|
| Money | `NUMERIC(18,4)`, presented at 2 places |
| Unit price | `NUMERIC(18,6)` |
| Quantity | `NUMERIC(18,4)` |
| Percentage | `NUMERIC(9,4)` |

Rounding is `ROUND_HALF_UP` — the convention Indian GST invoicing expects.

---

## Dates

All timestamps are UTC ISO-8601 with an offset: `2026-08-31T09:14:22.481Z`.
Business dates (`order_date`, `required_date`) are plain `YYYY-MM-DD`.

---

## Correlation id

Every request carries `X-Request-ID`, minted by Nginx or by the API, returned in
the response, stamped on every log line and audit row, and propagated into Celery
tasks. Quote it in a support ticket and the whole story is findable.

You may supply your own; it will be honoured.

---

## Rate limits

```http
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 117
Retry-After: 43          # on 429 only
```

Tighter budgets apply to login, MFA verification, password reset, refresh, MRP
runs, exports, uploads and bulk imports.

---

## Long-running operations

Anything that can outlive a request returns 202 and a job id:

```
POST /api/v1/mrp/runs                → 202 {job_id, status_url}
GET  /api/v1/mrp/runs/{id}           → status: QUEUED | RUNNING | COMPLETED | FAILED
GET  /api/v1/mrp/runs/{id}/suggestions
```

Reports and exports follow the same pattern and finish with a document id to
download. No endpoint holds a connection open for a calculation.

---

## Endpoint map

```
/auth                 login · mfa/verify · refresh · logout · logout-all · me · sessions
                      mfa/setup · mfa/confirm · mfa/disable · change-password
                      password-reset/request · password-reset/confirm
/users  /roles        users, roles, permission catalogue
/organization         company (incl. bank details) · branches · plants · departments
                      cost centres · financial years
/warehouses           warehouses · storage locations
/products             items · categories · UoM · attributes · brands · manufacturers
                      prices · vendor links
/customers  /vendors  business partners · transporters
/inventory            balances · ledger · transfers · adjustments · physical counts
/allocation           reservations · release · available-to-promise · reallocation
/bom                  BOMs · versions · approve · explode · cost · where-used
/routing              routings · operations · work centres · machines
/sales                sales orders · confirm · allocation status · demand summary
/mrp                  runs · suggestions · convert · shortage report
/procurement          requisitions · close · convert to POs by category · RFQs · quotations
                      POs · vendor assignees · GRNs · returns · tracking
/production           orders · components · work orders · issues · entries · cost
/assembly             assembly orders · components · complete
/job-work             orders · challans · issue · receive · pending-at-vendor
/quality              templates · inspections · results · decide · reports
/packing              packing orders · cartons · packing list
/dispatch             dispatches · tracking events · tracking board · delayed
/finance              ledgers · vendor invoices · match · payments · ageing · GST · valuation
/reports              catalogue · dashboard · background report jobs · one route per
                      report (e.g. /reports/cost-centre-expense, the cost-centre statement)
/documents            upload · download · metadata
/notifications        list · unread count · read · preferences
/audit                logs · entity history · approvals
```

Full request and response schemas are generated from the code at `/docs`.

## Inventory operation documents

`/inventory/bin-moves`, `/inventory/scrap-disposals` and `/inventory/internal-issues` each provide paginated lists, detail reads and creation. All reads require `inventory.view` and apply tenant and warehouse scope. Bin moves use `from_location_id` and `to_location_id`; their date is `move_date`. Scrap disposal uses `disposal_date`; internal issue uses `issue_date`.

Bin moves post on creation with `inventory.transfer`; scrap disposal posts with `inventory.scrap_dispose`. Internal issues start in DRAFT with `internal_issue.create`, which also authorises submit, post and cancel. Approval and rejection require `internal_issue.approve`. Cancellation and rejection record a reason. Higher-value issues need a separate approver.

Responses use each document's Read or ReadWithCost schema. FIFO unit cost, line cost and total value are omitted unless the principal has `item.cost_view` or `inventory.valuation_view`. Disposal proceeds remain part of the visible document. Report endpoints are `/reports/scrap-register` and `/reports/consumption-by-department`, with the standard bounded-period filters and inline/background report contract.

`PATCH /inventory/adjustments/{id}` replaces a draft's header and lines. Explicit write-up costs require a cost-view right. Posted or submitted adjustments cannot be edited.
