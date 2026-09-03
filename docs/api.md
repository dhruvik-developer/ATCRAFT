# API conventions

Base path `/api/v1`. Interactive documentation at `/docs` (disabled in
production — the schema is an accurate map of every endpoint and its
permissions).

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
| Auth | `UNAUTHENTICATED` `INVALID_CREDENTIALS` `TOKEN_EXPIRED` `TOKEN_INVALID` `TOKEN_REVOKED` `ACCOUNT_LOCKED` `ACCOUNT_INACTIVE` `PASSWORD_POLICY` |
| Authz | `FORBIDDEN` `PERMISSION_DENIED` `TENANT_MISMATCH` `SCOPE_DENIED` |
| Generic | `VALIDATION_ERROR` `NOT_FOUND` `CONFLICT` `RATE_LIMITED` `PAYLOAD_TOO_LARGE` `SERVICE_UNAVAILABLE` `INTERNAL_ERROR` |
| Business | `BUSINESS_RULE_VIOLATION` `INVALID_STATE_TRANSITION` `APPROVAL_REQUIRED` `DUPLICATE_DOCUMENT` `REFERENCED_RECORD_IN_USE` |
| Inventory | `INSUFFICIENT_AVAILABLE_STOCK` `STOCK_LOCK_TIMEOUT` `NEGATIVE_STOCK_BLOCKED` `BATCH_REQUIRED` `SERIAL_REQUIRED` `RESERVATION_NOT_FOUND` `RESERVATION_ALREADY_CONSUMED` |
| BOM / MRP | `BOM_NOT_FOUND` `BOM_CYCLE_DETECTED` `BOM_DEPTH_EXCEEDED` `BOM_NOT_APPROVED` `MRP_RUN_IN_PROGRESS` |
| Files | `FILE_TYPE_NOT_ALLOWED` `FILE_TOO_LARGE` `FILE_NOT_FOUND` |
| Jobs | `JOB_NOT_FOUND` `JOB_FAILED` |

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

```http
POST /api/v1/auth/login
{ "email": "planner@atcraft.example", "password": "..." }

→ { "access_token": "...", "refresh_token": "...", "expires_in": 900, "user": {...} }
```

Then on every request:

```http
Authorization: Bearer <access_token>
```

Access tokens last 15 minutes. Refresh with `POST /auth/refresh`; the refresh
token **rotates** on every use — store the new one and discard the old. Reusing a
replaced refresh token is treated as replay and revokes the entire session chain.

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

Tighter budgets apply to login, password reset, refresh, MRP runs, exports,
uploads and bulk imports.

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
/auth                 login · refresh · logout · me · sessions · password
/users  /roles        users, roles, permission catalogue
/organization         company · branches · plants · departments · financial years
/warehouses           warehouses · storage locations
/products             items · categories · UoM · prices · vendor links
/customers  /vendors  business partners · transporters
/inventory            balances · ledger · transfers · adjustments · physical counts
/allocation           reservations · release · available-to-promise · reallocation
/bom                  BOMs · versions · approve · explode · cost · where-used
/routing              routings · operations · work centres · machines
/sales                sales orders · confirm · allocation status · demand summary
/mrp                  runs · suggestions · convert · shortage report
/procurement          requisitions · RFQs · quotations · POs · GRNs · returns · tracking
/production           orders · components · work orders · issues · entries · cost
/assembly             assembly orders · components · complete
/job-work             orders · challans · issue · receive · pending-at-vendor
/quality              templates · inspections · results · decide · reports
/packing              packing orders · cartons · packing list
/dispatch             dispatches · tracking events · tracking board · delayed
/finance              ledgers · vendor invoices · match · payments · ageing · GST · valuation
/reports              catalogue · dashboard · background report jobs
/documents            upload · download · metadata
/notifications        list · unread count · read · preferences
/audit                logs · entity history · approvals
```

Full request and response schemas are generated from the code at `/docs`.
