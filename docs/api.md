# API conventions

Default base path `/api/v1`, configured by `API_V1_PREFIX`. Interactive
documentation at `/docs` (disabled in staging and
production — the schema is an accurate map of every endpoint and its
permissions).

## Where the contract lives

| Artefact | What it is |
|---|---|
| `openapi.json` | The contract. Generated from the application by `make contract`, committed, and checked by CI and the pre-push hook, so it cannot drift from the code. |
| `/docs`, `/redoc` | Swagger UI and ReDoc, served from that same schema in development. Both are **off in staging and production**: the schema lists every endpoint and the permission it needs, which is a map worth not publishing. Read it from the committed file instead. |
| `apps/web/src/types/openapi.ts` | The frontend's types, generated from the contract. |
| `postman_collection.json` | A Postman collection, generated from the contract. |

The schema, frontend types and Postman collection are regenerated together;
development documentation renders the same schema:

```bash
make contract          # writes all of them (Windows: .\make.ps1 contract)
make postman           # the collection only (Windows: .\make.ps1 postman)
make contract-check    # fails if any is stale (Windows: .\make.ps1 contract-check)
```

CI and the pre-push hook run the same three checks `contract-check` runs.

### Using the Postman collection

Import `postman_collection.json` (Postman v2.1 format, 782 requests in 33
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

Every JSON API response uses the shared envelope models in
`apps/api/src/core/responses.py`. The frontend derives envelope metadata from
the generated OpenAPI types and validates it at the HTTP boundary. Clients
never have to guess whether a response is wrapped.

Success bodies always contain `success: true`, `data` (which may be `null`),
and a string `message`. Lists also contain `pagination`. Error bodies always
contain `success: false`, `error` and `request_id`; `error.details` and
`request_id` are `null` when unavailable. HTTP status codes still indicate
success or failure.

`make contract` and `make contract-check` reject missing or inconsistent JSON
response schemas, including every alternative of a permission-dependent
response. Routes that hide costs retain their runtime permission filtering
while documenting both permitted response shapes.

File downloads retain their binary content type. HTTP 204/205 responses have
no JSON body. Operational `/health/*` probes, Prometheus `/metrics`, and
successful CORS preflights retain their protocol-specific formats.

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
  "pagination": { "limit": 50, "next_cursor": "eyJ0Ijoi...", "has_next": true },
  "message": "Operation completed successfully"
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

Poll the returned `status_url`, then use the job's `download_url`. A finance
valuation exporter with `inventory.valuation_view` and `report.export` can watch
and download their own valuation job without general report or document rights.
`GET /reports/jobs/{job_id}/download` binds the file to that authorized job;
another creator's job, a catalogue job, and unrelated documents remain outside
this limited access. Internal `report.view` readers retain their existing job
visibility and need `document.view` to download files. Vendor accounts cannot
use these internal job routes.

An internal `job_work.view` holder can likewise poll and download their own
`JOB_WORK_CHALLAN` PDF through its job URL without general report/document
rights. The service checks tenant, creator and the exact job key; this exception
does not grant access to another operator's exports, the report catalogue, or
arbitrary document IDs. Generated `JOB_WORK_COST` files retain `item.cost_view`
protection on document lists, metadata and downloads.

`GET /inventory/batches` accepts `search` and normal `page`/`limit` parameters.
Its optional `batch_id` filter resolves a selected batch outside the current
page; it remains scoped to the verified tenant and any supplied `item_id`.

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
| 202 | accepted, running in the background; the courier webhook's acknowledgement (see Courier integration) |
| 204 | success, no body |
| 400 | malformed request, or an upload refused for its type (`FILE_TYPE_NOT_ALLOWED`) or by the malware scan (`FILE_INFECTED`) |
| 401 | not authenticated, or token invalid/expired/revoked |
| 403 | authenticated but not allowed: a missing permission, an account's scope, or a pending password change or MFA enrolment |
| 404 | not found **in your tenant** |
| 405 | HTTP method not allowed; see the `Allow` header |
| 409 | conflict: concurrent edit, duplicate document, invalid state transition, lock timeout |
| 413 | body too large, including an upload over the size limit (`FILE_TOO_LARGE`) |
| 422 | validation or business-rule failure |
| 429 | rate limited (see `Retry-After`) |
| 500 | unexpected server error — quote `request_id` |
| 503 | dependency unavailable (the malware scanner too: `FILE_SCAN_UNAVAILABLE`), or the request exceeded the statement timeout |

A record belonging to another company returns **404, not 403**. Confirming it
exists elsewhere is itself a disclosure.

A nested read addresses its parent - `GET /warehouses/{id}/locations`,
`/mrp/runs/{id}/suggestions`, `/quality/reports/item/{id}` - so a parent that is
not in your tenant is the same 404. The flat list filtered by that id
(`GET /warehouses/locations?warehouse_id=...`) reads it as a filter instead, and
no match is an empty page.

---

## Error codes

Stable strings the frontend switches on. Renaming one is a breaking change.

| Group | Codes |
|---|---|
| Auth | `UNAUTHENTICATED` `INVALID_CREDENTIALS` `TOKEN_EXPIRED` `TOKEN_INVALID` `TOKEN_REVOKED` `ACCOUNT_LOCKED` `ACCOUNT_INACTIVE` `PASSWORD_POLICY` `MFA_INVALID` `MFA_REQUIRED` `PASSWORD_CHANGE_REQUIRED` |
| Authz | `FORBIDDEN` `PERMISSION_DENIED` `TENANT_MISMATCH` `SCOPE_DENIED` `CSRF_VALIDATION_FAILED` |
| Generic | `VALIDATION_ERROR` `NOT_FOUND` `CONFLICT` `RATE_LIMITED` `PAYLOAD_TOO_LARGE` `SERVICE_UNAVAILABLE` `INTERNAL_ERROR` `HTTP_ERROR` |
| Business | `BUSINESS_RULE_VIOLATION` `INVALID_STATE_TRANSITION` `APPROVAL_REQUIRED` `DUPLICATE_DOCUMENT` `REFERENCED_RECORD_IN_USE` |
| Inventory | `INSUFFICIENT_STOCK` `INSUFFICIENT_AVAILABLE_STOCK` `STOCK_LOCK_TIMEOUT` `NEGATIVE_STOCK_BLOCKED` `BATCH_REQUIRED` `SERIAL_REQUIRED` `RESERVATION_NOT_FOUND` `RESERVATION_ALREADY_CONSUMED` |
| BOM / MRP | `BOM_NOT_FOUND` `BOM_CYCLE_DETECTED` `BOM_DEPTH_EXCEEDED` `BOM_NOT_APPROVED` `MRP_RUN_IN_PROGRESS` |
| Files | `FILE_TYPE_NOT_ALLOWED` `FILE_TOO_LARGE` `FILE_NOT_FOUND` `FILE_INFECTED` `FILE_SCAN_UNAVAILABLE` |
| Jobs | `JOB_NOT_FOUND` `JOB_FAILED` |

Reserved, not emitted: `NEGATIVE_STOCK_BLOCKED` `RESERVATION_NOT_FOUND`
`JOB_FAILED`. They are members of `ErrorCode`, so a client may switch on them,
but no route raises them today - a shortfall is `INSUFFICIENT_STOCK`, a
missing reservation `NOT_FOUND`, and a failed job reads back as a job whose
status is `FAILED`, not as an error. This table and the reserved list are checked against
`ErrorCode` and the source by `tests/unit/test_api_doc_contract.py`.

Five of these are easy to mishandle:

- `PASSWORD_CHANGE_REQUIRED` (403) is raised by the shared authentication
  dependency on every route outside `/auth/me`, `/auth/change-password`,
  `/auth/logout` and `/auth/logout-all` while `must_change_password` is set.
- `MFA_REQUIRED` (403) is raised by the same dependency when the account's role
  is listed in `MFA_REQUIRED_ROLES` and has not enrolled a second factor, on
  every route outside the enrolment surface (`mfa_enrolment_route_allowed`:
  `/auth/me`, `/auth/change-password`, `/auth/logout`, `/auth/logout-all`,
  `/auth/mfa/setup`, `/auth/mfa/confirm` and `/auth/sessions`). With the setting
  empty it is never sent. `MFA_INVALID` (401) is a wrong, replayed or
  unavailable authenticator code during the MFA step, which itself is signalled
  by `mfa_required: true` in a 200 login response (see Authentication below).
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
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Some fields need attention.",
    "details": { "fields": { "lines.0.quantity": ["Input should be greater than 0"] } }
  },
  "request_id": "9f2c4a1b8e7d..."
}
```

The frontend uses `api.get`, `post`, `put`, `patch` and `delete` for unwrapped
`data`, `api.getPage` for offset lists, and `api.getCursorPage` for cursor
lists. `api.download` and `api.blobUrl` read binary responses. Malformed JSON
success bodies raise `ApiError` with code `INVALID_RESPONSE`; server errors
use their `error.code`, `error.message`, `error.details` and request ID.

Report lists use `api.getPageOrAccepted` through
`useListQuery({ allowAccepted: true })` because the same operation may return
an inline page or an HTTP 202 job. The hook exposes queued work as
`acceptedJob`; ordinary lists remain arrays with pagination.

## Request validation and naming

Routes accept typed Pydantic request bodies and typed query/path parameters.
Dependencies that assemble filter or period models from query parameters use
`bind_query_filters`, which applies the same field-error formatter as body
validation. Manually assembled multipart body metadata uses
`bind_request_model` at the input boundary. Invalid JSON returns **400**;
valid JSON, multipart metadata or query parameters that
fail schema or business validation return **422**. A server-side model failure
remains **500**.

Validation details contain only field paths and messages. Body paths match
form controls (`lines.0.quantity`); query, path, header and cookie errors retain
their transport prefix (`query.search`, `path.item_id`). Whole-body failures
use `_root`. Raw submitted values, parser offsets and validator context are not
copied into error metadata.

Every request body refuses a key it does not define: a misspelt or retired
field is a **422** `VALIDATION_ERROR` naming that key, never dropped while the
rest of the body is saved with its defaults. Each body model sets
`model_config = ConfigDict(extra="forbid")`, and
`tests/unit/test_request_bodies_refuse_unknown_keys.py` fails for one that does
not.

A partial update applies only the keys a request sends: an omitted key keeps
the stored value. An explicit `null` on a field that cannot be empty is refused
before any service runs, as a **422** naming the field (`reject_null` in
`core/validation.py`), never written through to a NOT NULL column or handed to
a calculation. Assembly and job-work draft edits instead ignore such a null and
keep the stored value, as their services document. The two rules are kept
apart on purpose: the assembly edit form sends `output_warehouse_id: null` to
mean "keep the stored one", so refusing that null would break the screen.

A `PATCH` that names no field at all (`{}`) is refused on every route before
anything is read, as a **422** `BUSINESS_RULE_VIOLATION` with the message "No
fields were supplied to update." (`require_changes` in `core/validation.py`,
docs/assumptions.md §PATCH-1) - never applied as an edit that changed nothing,
which would bump the record's version and write an empty audit row. A body
that is a full replacement (an adjustment's header and lines, a consignment's
details) fails on its required fields first, as `VALIDATION_ERROR`. `PUT`
settings are whole records and keep their own rule.

The body-size ceiling applies to bytes actually received, including chunked
requests and bodies larger than their declared `Content-Length`. Exceeding it
returns **413** with the standard error envelope; partially parsed upload files
are closed when parsing stops. Unexpected server errors omit technical details
even with development debug logging enabled. Database connection/pool failures
and statement timeouts return **503**; concurrent transaction conflicts and lock
timeouts return **409**.

Paths use lowercase words separated by hyphens, with plural resource names
where appropriate. Path and query parameter names and JSON fields use
`snake_case`. Existing action paths (such as `/approve`) describe explicit
business transitions. Every operation has a tag, a unique operation ID and a
summary of its own in OpenAPI - written in the route, never FastAPI's title of
the function name, and no two alike, because the summary is what Swagger and
the Postman collection name the request by
(`tests/unit/test_http_route_conventions.py`). HTTP 201 identifies creation,
200 identifies reads and completed actions, and 202 identifies queued work;
successful updates and removals that return an envelope use 200.

## Message conventions

Shared backend copy lives in `src/core/messages.py`; shared frontend copy lives
in `src/lib/messages.ts`. Standard success titles name the subject and use a
past-tense outcome, such as `Purchase order created`, without exclamation marks
or redundant “successfully”. Backend routes use `success_message`, and frontend
notifications use the `success` builders. Document codes and useful
workflow-specific details remain available.

Validation and error descriptions use clear sentences that explain the problem
and, where possible, the next action. Repeated validation headlines and generic
failure messages come from the shared catalogues. Stable error codes remain
machine-readable; the UI preserves API business messages and field errors.
Notifications use consistent titles and descriptions, and server failures
include the request reference for support.

---

## Authentication

Sign in with an email address and password:

```http
POST /api/v1/auth/login
{ "email": "planner@atcraft.example", "password": "...", "device_label": "Chrome on the planning PC" }
```

Add `company_code` when the same email belongs to more than one company. It
disambiguates the login; tenant IDs on authenticated requests still come from
the verified principal.

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

Access tokens last 15 minutes by default, and refresh tokens seven days;
deployments can configure both lifetimes. Refresh with a body-less `POST /auth/refresh`,
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

You may supply your own using 1–64 ASCII letters, digits, hyphens, underscores
or full stops. Other values are replaced with a generated identifier so a
correlation header cannot break an audit write or inject response/log content.
Request IP provenance accepts valid IPv4/IPv6 addresses; a malformed
`X-Real-IP` falls back to the valid peer address, or no address when neither is
valid.

---

## Rate limits

```http
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 117
Retry-After: 43          # on 429 only
```

Tighter budgets apply to login, MFA verification, password reset, refresh, phone
verification, MRP runs, exports, document PDFs and uploads; the last three are
per user (`docs/security.md` section 8).

---

## Long-running operations

Large calculations return 202 and a job ID. For MRP, the server compares the
demand-line count with `MRP_SYNC_THRESHOLD_DEMAND_LINES`: smaller runs finish
inline with 200, while runs above the threshold are queued with 202.

```
POST /api/v1/mrp/runs                → 200 completed run, or 202 {job_id, status_url}
GET  /api/v1/mrp/runs/{id}           → status: QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED
GET  /api/v1/mrp/runs/{id}/suggestions
GET  /api/v1/mrp/schedule-settings   → tenant's nightly planning defaults and scope
PUT  /api/v1/mrp/schedule-settings   → replace them (requires mrp.run)
```

Nightly runs are dispatched by Celery beat at `MRP_SCHEDULE_CRON` (default
`30 20 * * *`, or 02:00 Asia/Kolkata). A tenant with no saved settings is
enabled by default with a 90-day horizon, all active warehouses and safety stock
respected.

Report queries likewise return an inline page or an accepted job according to
their bounded-query rules. Queued exports finish with a document ID to
download; long work runs in a worker rather than holding the HTTP request open.

---

## Planning demand provenance

MRP requirement and suggestion responses include `demand_provenance`, either
`null` when no source facts were recorded or an object with
`root_demand_refs` and `root_demand_refs_complete`. Each reference contains
`demand_type`, `demand_id` and nullable `demand_line_id`. Sales demand uses
`SALES_ORDER`; manual planning uses `MRP_DEMAND` and the persisted input-row ID.
These references survive BOM expansion independently of the capped display
facts in `source_demands`.

Converting a purchase suggestion requires complete, nonempty demand links,
with at most fifty distinct references. A refused conversion returns the
standard 422 business-rule envelope with guidance to rerun the plan, leaving
the suggestion open and creating no requisition or conversion audit. Legacy
plans remain readable; incomplete historical links are reported explicitly.
References describe why a shared requirement exists and do not allocate its
aggregate quantity to individual sales lines.

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
                      prices · vendor links · specification masters/templates · accessories
/customers  /vendors  business partners · addresses · contacts · credit status ·
                      vendor approval, categories and rating
/transporters         carriers a dispatch or shipment names · tracking URL
/inventory            balances · ledger · transfers · adjustments · physical counts
                      batches · serials · bin moves · scrap disposals · internal issues
/allocation           reservations · release · available-to-promise · reallocation
/bom                  BOMs · versions · approve · explode · cost · where-used
/routing              routings · operations · work centres · machines
/sales                sales orders · confirm · allocation status · demand summary
                      planning settings
/sales-returns        returns · receive · serial dispositions · credit note · close
/mrp                  runs · suggestions · convert · shortage report · demand
                      sales-order expected availability
/procurement          requisitions · close · convert to POs by category · RFQs · quotations
                      POs · vendor assignees · GRNs · returns · tracking
/production           orders · components · work orders · issues · entries (per order
                      and company-wide) · capacity · cost
/assembly             assembly orders · components · availability · board · verdict
                      ready-for-packing · complete · pack handover
/job-work             orders · challans (per order and company-wide register) ·
                      issue · receive · pending-at-vendor · vendor ledger · charges
/quality              templates · inspections · results · decide · reports
/packing              packing orders · cartons · packing list · from-assembly
/dispatch             dispatches · tracking events · tracking board · delayed
/integrations/courier courier accounts · webhook secret · connection test · refresh ·
                      event history · the signed provider webhook
/finance              ledgers · vendor/sales invoices · credit notes · match · payments
                      return-debit allocations · ageing · GST · valuation
/collections          overdue worklist · follow-up register · notes · promises · escalation
/reports              catalogue · dashboard · background report jobs · one route per
                      report (e.g. /reports/cost-centre-expense, the cost-centre statement)
/documents            upload · download · metadata
/notifications        list · unread count · read · preferences · push devices ·
                      alert rules (company smart-alert thresholds and reminders) ·
                      messaging channel status · tenant templates · delivery history
/vendor-portal        orders · consignments · shared documents · bills · payments · QC
/dealer-portal        catalogue · pricing · cart · wishlist · checkout · orders · statement
                      own invoices/credits · return requests · banners/offers · item images
/promotions           staff banner/offer management · lifecycle actions · eligibility preview
/audit                logs · entity history · approvals (open requests; decisions stay
                      on their documents) · approval settings
```

Every prefix in this map is one the API serves, and every prefix the API
serves is in it (`tests/unit/test_api_doc_contract.py`).

Full request and response schemas are generated from the code in
`openapi.json` and, in development, at `/docs`.

## Collections

The collections API is tenant-scoped and exposes balances by customer and
currency; it never combines currencies. Overdue means an ISSUED or
PARTIALLY_PAID sales invoice whose `due_date` is before the requested business
date, less paid amounts and issued credit notes. All lists are paginated. Notes
are append-only and cursor-paginated by `(created_at, id)`.

| Route | Operation | Permission |
|---|---|---|
| `GET /collections/worklist` | Read overdue customers and live follow-up summaries | `collection.view` |
| `GET /collections/follow-ups` | List and filter follow-ups | `collection.view` |
| `POST /collections/follow-ups` | Open a follow-up for an overdue customer/currency | `collection.manage` |
| `GET /collections/follow-ups/{id}` | Read a follow-up and its overdue invoices | `collection.view` |
| `PATCH /collections/follow-ups/{id}` | Reassign or reschedule a follow-up | `collection.manage` |
| `GET /collections/follow-ups/{id}/notes` | Read keyset-paginated history | `collection.view` |
| `POST /collections/follow-ups/{id}/notes` | Append a contact note | `collection.manage` |
| `POST /collections/follow-ups/{id}/promise` | Record a promise within the overdue balance | `collection.manage` |
| `POST /collections/follow-ups/{id}/escalate` | Escalate a chase | `collection.manage`; `customer.update` if blocking the customer |
| `POST /collections/follow-ups/{id}/resolve` | Resolve a chase | `collection.manage` |
| `POST /collections/follow-ups/{id}/cancel` | Cancel a chase opened in error | `collection.manage` |

The daily `notifications.scan_overdue_receivables` task opens missing chases,
refreshes balances, returns broken promises to OPEN, resolves settled balances,
and sends reminders on a three-day cadence. Status transitions
and every write are audited. `GET /reports/dealer-performance` provides period
aggregates without combining currencies; it is available through the standard
`report.view` catalogue.

## Procurement reports

The reports catalogue includes purchase-order register and summary, delayed
orders, duplicate-warning overrides, vendor acceptance, inbound shipments,
inbound freight and vendor inbound history. Each report is tenant-scoped,
paginated, requires `report.view`, and accepts the common bounded
`date_from`/`date_to` period. New report keys are included in the literal
`report_snapshots.report_key` check constraint by Alembic migration.

| Route | What it reports | Relevant filters |
|---|---|---|
| `GET /reports/purchase-order-register` | Purchase orders raised in the period, with vendor/category, status, assigned vendor user and receipt progress | `vendor_id`, `category_id`, `status`, `search` |
| `GET /reports/purchase-order-summary` | Period totals by vendor or category | `group_by=vendor\|category`; vendor totals use PO header amounts including freight, while category totals sum categorized lines and do not assign unallocated freight |
| `GET /reports/delayed-purchase-orders` | Open PO quantities whose line due date falls in the period and remains late at its end | `vendor_id`, `category_id`, `status`, `search` |
| `GET /reports/duplicate-po-overrides` | POs raised after a duplicate warning was acknowledged, including the recorded reason and user evidence | `vendor_id`, `category_id`, `status`, `search` |
| `GET /reports/vendor-acceptance` | Sent, acknowledged and declined counts, acknowledgement times and acknowledgement-within-24-hours rate | `vendor_id`, `status`, `search` |
| `GET /reports/inbound-shipments` | Vendor consignments with carrier, tracking/LR details, dates, status and lateness | `vendor_id`, `category_id`, `transporter_id`, `status`, `in_transit_only`, `expected_today`, `late_only`, `search` |
| `GET /reports/inbound-freight` | Freight totals by transporter/courier and UTC month | `vendor_id`, `category_id`, `transporter_id`, `search` |
| `GET /reports/vendor-inbound-history` | Shipment counts, freight and on-time delivery by vendor | `vendor_id`, `category_id`, `transporter_id`, `search` |

`expected_today=true` means expected on the selected period's inclusive end,
which keeps past-period exports reproducible. Shipment state comes from
`delivered_at`. Inbound history's on-time percentage uses shipments with both an
expected date and a delivery/late outcome; shipments missing either date are
excluded rather than counted as on-time. UTC defines freight month boundaries.
Filters that do not apply to a report are ignored by its query and do not affect
its snapshot identity.

## Smart alerts

Timed operational alerts are scanned by Celery at the configured interval
(900 seconds by default). Each rule keeps a company-scoped episode so repeated
scans update the same alert instead of raising duplicate events. The
`/admin/alerts` page reads and updates the effective rule matrix:

| Route | Operation | Permission |
|---|---|---|
| `GET /notifications/alert-rules` | Read all rules, including defaults | `settings.view` |
| `PUT /notifications/alert-rules` | Save changed company overrides | `settings.manage` |

QC inspection creation also raises an immediate `QC_INSPECTION_PENDING`
notification for eligible internal users with `qc.inspect`. Unread
notifications collapse into one group per inspection stage and link to the
quality pending queue.

## WhatsApp and SMS

Messaging is disabled by default and notifications are sent only when the
company channel is enabled, the recipient has a verified phone and current
consent, the per-type preference is enabled, and the tenant has an active
provider template. `CRITICAL` notifications remain in-app only.

| Route | Operation | Permission |
|---|---|---|
| `PUT /auth/me/phone` | Set the caller's E.164 mobile number; a changed number clears verification and both consents | Signed-in caller, own account |
| `POST /auth/me/phone/verification` | Send a short-lived SMS verification code | Signed-in caller, own account |
| `POST /auth/me/phone/verification/confirm` | Verify the caller's number | Signed-in caller, own account |
| `POST /auth/me/messaging-consent` | Record or withdraw WhatsApp/SMS consent | Signed-in caller, own account |
| `GET /notifications/channels` | Read globally enabled channel status | `notification.view` |
| `GET /notifications/channel-templates` | List this company's provider templates | `settings.view` |
| `PUT /notifications/channel-templates` | Upsert this company's provider templates | `settings.manage` |
| `POST /notifications/channel-templates/test` | Send a test to the caller's own verified, opted-in number | `settings.manage` |
| `GET /notifications/deliveries` | Read paginated provider delivery attempts | `settings.view` |

Phone verification uses SMS OTP even when only WhatsApp notifications are
enabled. Provider credentials remain in deployment environment settings and are
never stored in the database.

## Courier integration

Courier integrations are disabled until `COURIER_TRACKING_ENABLED=true` is set
for the deployment. Account configuration uses `settings.view` /
`settings.manage`; consignment refresh and event history use
`dispatch.update` / `dispatch.view`. Account and event resources remain scoped
to the company resolved from the signed-in principal.

| Route | Operation | Permission |
|---|---|---|
| `GET /integrations/courier/accounts` | List this company's provider accounts | `settings.view` |
| `POST /integrations/courier/accounts` | Create an account with encrypted Shiprocket credentials | `settings.manage` |
| `PATCH /integrations/courier/accounts/{id}` | Edit or deactivate an account; credentials are write-only | `settings.manage` |
| `POST /integrations/courier/accounts/{id}/rotate-webhook` | Rotate webhook token and secret; returns them once | `settings.manage` |
| `POST /integrations/courier/accounts/{id}/test` | Test provider connectivity | `settings.manage` |
| `POST /integrations/courier/refresh` | Refresh one outbound dispatch or inbound shipment | `dispatch.update` |
| `GET /integrations/courier/events` | Cursor-paginated normalized event history | `dispatch.view` |
| `POST /integrations/courier/webhook/{token}` | Accept a bounded provider event payload | Public, authenticated by capability token and provider signature/key |

Generic webhook signatures are HMAC-SHA256 over `<unix-timestamp>.<exact-body>`
in `X-Courier-Signature`, with the timestamp in `X-Courier-Timestamp`.
Shiprocket uses `X-Api-Key`. Webhook bodies are limited to 64 KiB, generic
timestamps have a five-minute acceptance window, and duplicate provider events
are ignored. The webhook answers **202** with `events_recorded` after it has
recorded the events, inside the request: the 202 is the acknowledgement
providers expect, not a queued job, so there is no `status_url` to poll. The scheduled Shiprocket poll runs every
`COURIER_POLL_INTERVAL_SECONDS` (1,800 by default), processes at most 100 oldest
eligible consignments per account, and isolates failures by account.

## Product specifications and accessories

All paths below retain `/api/v1`, the standard response envelope and tenant
scope derived from the verified token. Lists are paginated. Creates return 201,
updates/removals return 200, invalid typed values return 422, and foreign IDs
return the same scoped 404 as missing IDs.

| Resource | Operations | Permission |
|---|---|---|
| `/products/specification-attributes` and `/{id}` | GET list/detail; POST; PATCH; DELETE with a 3–255 character reason | Reads: `item_attribute.view`; writes: `item_attribute.manage` |
| `/products/specification-categories` | GET narrow category picker | `item_attribute.view` or `item_attribute.manage` |
| `/products/categories/{id}/specifications` | GET template; PUT complete replacement | Read: either attribute right; write: `item_attribute.manage` |
| `/products/items/{id}/specification-attributes` | GET active attribute picker, authorized for that item | `item.view` or `item.update` |
| `/products/items/{id}/specifications` | GET values, inherited template and missing-required count; PUT complete replacement | Read: `item.view`; write: `item.update` |
| `/products/items/{id}/accessories` | GET associations; POST association | Read: `item.view`; write: `item.update` |
| `/products/item-accessories/{id}` | PATCH quantity/relation/order/notes; DELETE with reason | `item.update` |
| `/dealer-portal/catalog/specifications` | GET eligible OPTION/BOOLEAN filter metadata | `dealer_portal.browse` and a valid dealer binding |

The item picker does not require master-view permission, and the template's
category picker does not require `category.view`. These narrow endpoints expose
the data needed for the caller's authorized edit without widening master access.
Master retirement and accessory deletion require a meaningful reason.

An item specification PUT has shape:

```json
{
  "values": [
    {"specification_attribute_id": "<boolean-attribute-id>", "value_bool": false},
    {"specification_attribute_id": "<number-attribute-id>", "value_number": "0"}
  ]
}
```

Exactly one typed value is required per attribute. Omitted attributes are removed
and an empty `values` list deliberately clears the set. Requests may contain at
most 200 values; numbers use finite Decimal strings with up to 14 whole digits
and four decimal places. Unknown fields and a client-supplied `value_type` are
refused. A category replacement similarly uses
`{"specifications":[{"specification_attribute_id":"…","is_required":true,"sort_order":10}]}`.
Required fields are completeness warnings and do not prevent ordinary item use.

Staff and dealer item lists accept repeated query filters such as
`?spec=DM-SPEC-MATERIAL=Brass&spec=DM-SPEC-FIRE-RATED=false` (URL-encoded by the
client) and optional `has_accessories=true|false`. At most 20 expressions are
allowed; they intersect, use active filterable OPTION/BOOLEAN masters and
return no rows for an explicit empty value. Dealer item detail exposes formatted
specifications and its own channel-priced OPTIONAL accessories. INCLUDED entries
are informational: they do not expose a price or add an automatic cart/stock line.
Price-on-request accessories cannot be added through a fabricated price.

## Purchase-order duplicate resolution and revisions

These staff endpoints retain the response/error envelope and tenant scope.
Vendor and dealer accounts cannot execute buyer duplicate actions.

| Path | Contract and permission |
|---|---|
| `POST /procurement/purchase-orders/duplicate-resolution` | `action`, a discriminated `MANUAL` or `REQUISITION` draft and reason. MERGE/INCREASE require `purchase_order.update`, candidate ID and current `expected_candidate_version`; INCREASE also names the candidate line. CREATE_WITH_APPROVAL requires `purchase_order.create`; CANCEL_REQUEST requires `purchase_requisition.create`. Returns 200 with affected orders or cancelled demand IDs. |
| `POST /procurement/purchase-orders/{id}/amend` | `purchase_order.update`; `expected_version`, reason and complete desired lines. Retained lines name `line_id`; new lines name item and price. Optional source allocations explicitly retain requisition quantities. Returns the revised detail with a new version and revision number. |
| `GET /procurement/purchase-orders/{id}/amendments` | `purchase_order.view`; cursor/limit pagination over immutable full revision snapshots. No separate audit-log permission is required. |
| `POST /procurement/purchase-orders/{id}/approve` | `purchase_order.approve`; flagged duplicates additionally require `confirm_duplicate: true` and a meaningful reason. Records approval-time confirmation evidence and enforces separation of duties. |
| `GET /vendor-portal/purchase-orders/pending-amendments` | `vendor_portal.access`; paginated, scoped status notices for previously sent amended orders awaiting reapproval/resend. No commercial fields or actions. |

A requisition draft names explicit requisition and line IDs plus the existing
conversion options. Unselected demand remains available. Merge/increase
rechecks the comparison under locks, refuses different commercial terms and
any GRN history, and routes approved/sent/acknowledged candidates through
amendment. Decreases below allocated quantity require explicit retained source
allocations. Omit unchanged price, unit, item or QC fields on amendment;
explicit nulls for these fields return 422. Stale versions return 409, missing
or foreign sources return 404 and incompatible demand/terms return a controlled
422. Existing submit remains draft-only.

Vendor detail returns a typed union: normal issued detail, or
`VendorPortalPendingAmendmentRead` with only identity/code/status/revision/time,
`pending_amendment: true` and a message. Revised lines, prices, terms, files and
actions remain private until resend. Normal detail has
`pending_amendment: false`. New unsent orders remain invisible.

## Inventory operation documents

`/inventory/bin-moves`, `/inventory/scrap-disposals` and `/inventory/internal-issues` each provide paginated lists, detail reads and creation. All reads require `inventory.view` and apply tenant and warehouse scope. Bin moves use `from_location_id` and `to_location_id`; their date is `move_date`. Scrap disposal uses `disposal_date`; internal issue uses `issue_date`.

Bin moves post on creation with `inventory.transfer`; scrap disposal posts with `inventory.scrap_dispose`. Internal issues start in DRAFT with `internal_issue.create`, which also authorises submit, post and cancel. Approval and rejection require `internal_issue.approve`. Cancellation and rejection record a reason. Higher-value issues need a separate approver.

Responses use each document's Read or ReadWithCost schema. FIFO unit cost, line cost and total value are omitted unless the principal has `item.cost_view` or `inventory.valuation_view`. Disposal proceeds remain part of the visible document. Report endpoints are `/reports/scrap-register` and `/reports/consumption-by-department`, with the standard bounded-period filters and inline/background report contract.

`PATCH /inventory/adjustments/{id}` replaces a draft's header and lines. Explicit write-up costs require a cost-view right. Posted or submitted adjustments cannot be edited.
