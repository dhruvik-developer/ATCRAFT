# WP A4: Payment follow-up (collections) and dealer-performance report

Lane A (commercial), round 4. Assumption placeholder prefix: `§A4-n`.

**What the proposal promised.** Finance: "Payment follow-up", "Payment collection", "Outstanding report". Reports: "Dealer performance".

**What the audit found.** "A dealer-performance report and a payment-follow-up workflow. Outstanding and ageing are there; chasing overdue invoices is not a separate screen."

**Background.** Read `reports/dealers-reports-docs.md` §1.5–§1.7 and §8. Sales invoices (A2), credit notes and returns (A3) are on main, so "overdue" is now measured **per invoice, against its due date**.

## Part 1: Collections module (new downstream module `collections`)

Mount it after `finance` in `MODULE_ORDER`. Prefix `/collections`, tag "Collections".

### 1. Tables

**`payment_follow_ups`** (`TenantEntity`, `OptimisticLockMixin`)

| Column | Notes |
|---|---|
| `code` | `DocTypes.PAYMENT_FOLLOW_UP`, prefix "FU" |
| `customer_id` | |
| `currency` | |
| `status` | `FollowUpStatus`: OPEN, PROMISED, ESCALATED, RESOLVED, CANCELLED |
| `assigned_to_id` | FK users, SET NULL. Defaults to `customers.assigned_sales_user_id`; otherwise unassigned |
| `opened_overdue_amount` | |
| `current_overdue_amount` | Refreshed by the sweep |
| `oldest_due_date` | |
| `next_action_date` | |
| `promised_amount` / `promised_date` | Pair CHECK: both or neither |
| `last_reminded_at` | |
| `escalated_at` | |
| `resolution_note` | |
| `resolved_at`, `resolved_by_id` | |
| `cancellation_reason` | |

Constraints:
- Partial unique index: **one live row per (company, customer, currency)**, WHERE status is OPEN, PROMISED or ESCALATED. Demote before promote (CLAUDE.md trap).

**`payment_follow_up_notes`** (`ImmutableLedgerEntity`)
- `follow_up_id`
- `channel`: CALL, EMAIL, VISIT, MESSAGE, SYSTEM
- `outcome` (optional): PROMISED, DISPUTED, NO_RESPONSE, PARTIAL_PAYMENT, OTHER
- `note` (Text)
- `next_action_date`

Notes are paged with keyset, ordered by `(created_at, id)`. Add an immutability trigger like the other append-only tables (anatomy trap 5).

### 2. Machine

`FOLLOW_UP_LIFECYCLE`, entity "Payment follow-up":
- OPEN → PROMISED, ESCALATED, RESOLVED, CANCELLED
- PROMISED → OPEN (broken promise), ESCALATED, RESOLVED
- ESCALATED → OPEN, RESOLVED

Every status needs a badge. Use `PROMISED` and `ESCALATED` as new badge keys; check which already exist.

### 3. Worklist: `GET /collections/worklist` (`collection.view`)

- **Rows:** one per (customer, currency) with overdue invoices, meaning ISSUED or PARTIALLY_PAID invoices with `due_date < today` and an unsettled balance (total − paid − credited).
- **Columns:** customer code and name, channel (customer type), assigned to, overdue amount, number of overdue invoices, oldest overdue days, ageing bucket amounts (0-30, 31-60, 61-90, 90+, using the existing boundaries), last follow-up note date, next action date, promise (amount and date), follow-up status.
- **Implementation:** one SQL statement over `sales_invoices`, joined to `customers` and left-joined to the live follow-up. Paginated.
- **Filters:** `assigned_to_me`, `min_overdue_days`, `customer_type IN (dealer channels)`, `customer_id`, `status`, search.
- **Sortable:** `overdue_amount`, `oldest_overdue_days`, `next_action_date`, `customer_code`.

### 4. Routes

All take `collection.view` or `collection.manage`, written literally.

| Route | Notes |
|---|---|
| `GET /collections/follow-ups` | Paginated; filters status, assignee, customer |
| `POST /collections/follow-ups` | Open manually for a customer with overdue invoices. A duplicate live follow-up answers 409 |
| `GET /collections/follow-ups/{id}` | Includes the customer's overdue invoices list |
| `PATCH /collections/follow-ups/{id}` | `assigned_to_id`, `next_action_date` |
| `POST /collections/follow-ups/{id}/notes` | Notes are appended (`channel`, `outcome`, `note`, `next_action_date`) |
| `GET /collections/follow-ups/{id}/notes` | Keyset paged |
| `POST /collections/follow-ups/{id}/promise` | `{promised_amount, promised_date}`. Date must be ≥ today; amount > 0 and ≤ overdue |
| `POST /collections/follow-ups/{id}/escalate` | `{reason, block_customer: bool}`. When `block_customer` is true, call `CustomerService.block` through its service seam. That requires the caller to also hold `customer.update`; check with `principal.require`. This puts the customer on credit hold |
| `POST /collections/follow-ups/{id}/resolve` | `{note}` |
| `POST /collections/follow-ups/{id}/cancel` | `{reason}` |

Every change is audited, and every status change goes through the machine.

### 5. Daily sweep

Logic lives in `collections.service.scan_overdue_receivables(session, tenant, *, as_of)`. The Celery adapter is `notifications.scan_overdue_receivables` on the `notifications` queue, with a beat entry once a day plus `expires`. Use one per-company nested transaction, as the maintenance tenant loop does.

The sweep:
- **(a)** For each customer and currency with overdue invoices and no live follow-up, opens one: OPEN, assigned to the customer's sales user, with a SYSTEM note.
- **(b)** Refreshes `current_overdue_amount` and `oldest_due_date`.
- **(c)** Detects broken promises: PROMISED and `promised_date < today` while still overdue. These go to OPEN, with a SYSTEM note "Promise of <amount> due <date> was not met", audited.
- **(d)** Auto-resolves follow-ups whose overdue amount is now 0, with a SYSTEM note.
- **(e)** Notifies the assignee (or the ACCOUNTS role users when unassigned) with `NotificationType.PAYMENT_DUE`:
  - `group_key=f"payment-due:{customer_id}:{currency}"`
  - `action_url` = the collections page filtered to that customer
  - at most once per `FOLLOW_UP_REMINDER_DAYS` (3, an ASSUMPTION), tracked by `last_reminded_at`
- Writes one audit row per company, and only when something changed. A quiet run writes nothing.

### 6. Dashboard

Add fields to `DashboardRead`, each **with a default**, and bump the dashboard cache key, because cached JSON has the old shape (dealers report trap 17):
- `receivables_outstanding`
- `receivables_overdue`
- `follow_ups_due` (`next_action_date <= today` among live ones)

Add matching tiles on the dashboard, gated on `collection.view` or `finance.view` as appropriate.

### 7. Permissions

- New: `collection.view`, `collection.manage`, in group "Finance".
- Presets: `_ACCOUNTS` gets both. `_SALES` gets both, because sales executives chase their dealers (`§A4-n`).
- Add a grant migration.

## Part 2: Dealer performance report

### 1. Report definition

- Add `ReportKey.DEALER_PERFORMANCE`, with a CHECK-swap migration listing **all** keys.
- Add the catalogue entry, the route `GET /reports/dealer-performance` (`report.view`) and the frontend union.
- Period required; not cacheable.

### 2. Row (per customer and currency; customers whose type is in the dealer channels, plus CUSTOMER as an option through a filter)

| Group | Columns |
|---|---|
| Identity | `customer_id`, `customer_code`, `customer_name`, `customer_type`, `price_list_type`, `currency` |
| Credit | `credit_limit`, `outstanding_amount` |
| Orders | `order_count`, `order_value_amount` (excluding non-revenue statuses) |
| Invoicing | `invoiced_amount` (issued invoices in the period, net of credit notes), `credit_note_amount`, `returns_amount` (credit notes of reason RETURN) |
| Fulfilment | `dispatched_quantity`, `ordered_quantity`, `fulfilment_percent`, `on_time_dispatch_percent` (dispatch date ≤ required date) |
| Collections | `receipts_amount`, `average_days_to_pay` (receipt allocation date − invoice date, weighted by allocated amount), `overdue_amount` (as of the period end), `credit_utilisation_percent` (None when the limit is unlimited), `collection_ratio_percent` |
| Derived | `average_order_value_amount` |

Name every column with the frontend's formatting suffixes: `_amount`, `_percent`, `_quantity`.

### 3. Query and filters

- One builder with grouped subqueries; no N+1. Keep currencies separate.
- Filters: `customer_id`, `customer_type` (extend `ReportFilters` only if needed, as an optional field), search.
- Order by `invoiced_amount DESC`, then `customer_code`, then `customer_id`.

### 4. UI hooks

The reports runner picks the report up automatically. Also add a "Performance" link from the Dealers page and a row action on a dealer that opens the report filtered to that customer.

## Frontend

- **New page `/finance/collections`** ("Collections", in Finance nav, gated `P.COLLECTION_VIEW`):
  - worklist `DataTable`, with filters in the URL and a "My follow-ups" toggle;
  - a row opens the follow-up drawer: the overdue invoices (links to invoice detail), notes timeline (infinite keyset), Log note dialog, Promise dialog, Escalate dialog (block-customer checkbox shown only when `can(P.CUSTOMER_UPDATE)`), Resolve and Cancel;
  - "Open follow-up" action for rows without one;
  - responsive, with mobile cards;
  - add the page to the sweeps and the handbook.
- **Customer detail:** a Collections panel showing the live follow-up and its last notes.
- **Dashboard:** the new tiles.
- Add messages and badges.

## Demo data

- Make two demo invoices overdue by dating them earlier. Do it with the anchor and identity-keyed, never with "today".
- One follow-up with notes and a promise.
- One broken promise, so the sweep demonstrates it.

## Docs

- `database.md`: the new module tables, §7 machine, §8 sweep.
- `assumptions.md` (`§A4-n`): what "overdue" means (invoice `due_date`, settlement including credits); one live follow-up per customer and currency; auto-open; reminder cadence; broken-promise rule; sales executives may chase; dealer-performance definitions.
- `architecture.md` (new module), `api.md`, and the handbook (Collections screen, Dealer performance report, dashboard tiles).

## Tests

- **Unit:** lifecycle `validate()`; the pure rules for promise validation and the reminder cadence; dealer-performance derive functions.
- **Integration:**
  - worklist sums and buckets;
  - one live follow-up per customer and currency;
  - each sweep branch (open, refresh, broken promise, auto-resolve, reminder cadence, quiet run writes nothing);
  - escalate with block requires the right;
  - notes keyset paging order;
  - the dealer-performance numbers on seeded invoices, receipts and credit notes;
  - list lists.
- **Frontend:** collections page gating and filters; drawer actions' request bodies; dashboard tiles.
