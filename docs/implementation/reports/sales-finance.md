# Area report: sales, finance and GST (gaps 3, 7, 10, and the credit side of gap 4)

> Historical implementation research from the proposal build. Source references
> and status descriptions apply to that checkpoint. Consult the
> [current guides](../../README.md) and source before following operational advice.

All four audit claims in this area hold. Several pieces already exist that the audit did not mention, and they are listed under each gap below. This was a read-only review: I changed no files and ran no tests.

| Gap | Audit claim | Verified status |
|---|---|---|
| 3. Sales invoice and PDF | Absent | **No invoice document.** Pieces are built but nothing uses them (§11): `DocTypes.SALES_INVOICE` ("INV"), permissions `sales_invoice.view` and `sales_invoice.create`, sales order status `INVOICED` and its transition, the column `sales_order_lines.invoiced_quantity`, `dispatches.invoice_document_id` (an uploaded file), and the badge and "Invoiced" column on screen. Customer charges are journals written by hand. |
| 4. Customer return (credit side) | Absent | **No credit-note document.** The only option is `POST /finance/customer-adjustments` with type `CREDIT`: two ledger lines, no GST, no item lines, no stock movement, and no screen. The inventory movement type `CUSTOMER_RETURN` exists but nothing posts it. |
| 7. Job-work invoice | Absent | **No job-work invoice.** Charges build up on job-work receipts, and `GET /job-work/charges` describes itself as "the figures finance posts", but no finance code and no screen calls it. A job worker's bill is entered as a vendor invoice with no purchase order. The three-way match then blocks it, so it can only be approved with an override reason. |
| 10. Dealer performance and payment follow-up | Absent | **Neither exists.** What does exist: a list of dealers' credit positions, customer ageing worked out from the ledger, and a customer ledger statement. The notification type `PAYMENT_DUE` exists but nothing sends it. Customer receivables have no due dates, because the customer's `credit_days` is never used for receivables. |

---

## Exists today

### 1. Sales orders (`apps/api/src/modules/sales`)

**`SalesOrderStatus`** (`sales/models.py:81-98`) has these values: DRAFT, CONFIRMED, ALLOCATED, PARTIALLY_DISPATCHED, DISPATCHED, **INVOICED**, CLOSED, CANCELLED. Two status groups are defined:
- `OPEN_DEMAND_STATUSES` = CONFIRMED, ALLOCATED, PARTIALLY_DISPATCHED (`:121`)
- `RESERVING_STATUSES` (`:129`)

**`SalesOrderLineStatus`** (`:101-114`) runs PENDING … DISPATCHED, CANCELLED and has no invoiced value. Line status is worked out from the quantities by `service.line_status_for` (`service.py:383`).

**`SalesOrder(TenantEntity, OptimisticLockMixin)`**, table `sales_orders` (`models.py:135-287`):
- **Identity:** `code` String(64), unique on (company, code); `customer_id` (FK RESTRICT); `customer_po_number`.
- **Dates:** `order_date`, `required_date`, `promised_date`.
- **Location:** `branch_id` (the seller's state comes from the branch when one is set); `warehouse_id` NOT NULL.
- **Money** (`:185-203`), all `Money` = NUMERIC(18,4):
  - `currency` (3 characters, default `DEFAULT_CURRENCY`)
  - `subtotal` (gross, before discount), `discount_amount`, `tax_amount`, `total_amount`
  - The rule total = subtotal − discount + tax is applied in code (`sum_order_totals`, `service.py:365-380`), not by a database CHECK.
- **GST, fixed when the order is priced:**
  - `place_of_supply_state_code` String(4) (`:209`)
  - `is_interstate` (`:212`)
- **Parties:** `billing_address_id`, `shipping_address_id` (FK to `customer_addresses`, RESTRICT); `transporter_id` (plain UUID, no FK).
- **Approval:** `requires_approval`, `approved_by_id`, `approved_at`, plus `cancellation_reason` and `notes`.
- There is no round-off, freight or invoice reference on the order.

**`SalesOrderLine`** (`models.py:290-431`):
- `line_number`, `item_id`, `item_variant_id`, `description` (a copy), `quantity`, `uom_id`
- `unit_price` (NUMERIC 18,6), `discount_percentage`, `discount_amount`, `tax_rate`
- `cgst_amount`, `sgst_amount`, `igst_amount`, `tax_amount`, `line_total`
- `required_date`, `allocated_quantity`, `dispatched_quantity`, **`invoiced_quantity`** (`:365`), `status`, `reservation_id`
- CHECKs:
  - `invoiced_quantity >= 0` and `invoiced_quantity <= quantity` (`:416-419`)
  - `tax_amount = cgst + sgst + igst` (`tax_split_sums_to_total`)
  - `igst = 0 OR (cgst = 0 AND sgst = 0)` (`gst_treatment_is_exclusive`, `:422-430`)
- **There is no `hsn_code` copy on the line.**

**Service (`sales/service.py`)**

- **`SALES_ORDER_LIFECYCLE`** (`:174-245`):
  - `DISPATCHED -> INVOICED`: `permission=Permission.SALES_INVOICE_CREATE`, label "Invoice", irreversible (`:207-212`)
  - `INVOICED -> CLOSED`: `SALES_ORDER_UPDATE` (`:213-218`)
  - Terminal states: CLOSED and CANCELLED.
  - **No code ever moves an order to `INVOICED` or `CLOSED`.** A grep finds these values only in the machine definition, and there is no close function or route.
- **Tax functions**, which are pure:
  - `resolve_tax_treatment(seller_state_code, place_of_supply) -> bool` (`:260-271`). It returns IGST (True) unless both state codes are known and equal.
  - `compute_line_amounts(*, quantity, unit_price, tax_rate, is_interstate, discount_percentage=None, discount_amount=None) -> LineAmounts` (`:288-355`). It rounds CGST and SGST separately, using the same half-rate on each:
    ```python
    half = to_money(taxable * (rate / Decimal(2)) / Decimal(100)); cgst = sgst = half; tax = cgst + sgst
    ```
- **Seller state:** `_resolve_shipping_context` (`:767-802`) calls `OrderPartyRepository.seller_state_code(branch_id)` (`repository.py:291-311`). That uses the branch's `state_code`, otherwise `companies.state_code`. Place of supply is the shipping address `state_code`. `_assert_seller_state_known` is at `:805`.
- **Main flows:**
  - `create_order` (`:1023`) numbers the order with `next_document_number(session, tenant.company_id, DocTypes.SALES_ORDER, on=order_date)`.
  - `confirm_order` (`:1608`) runs the credit check `assert_customer_can_order(..., lock=True)` and takes advisory locks per item.
  - `cancel_order` (`:1755`).
  - `_transition` (`:1825-1856`) is the only code that writes the status, and it calls `record_status_change`.

**Routes** (`sales/router.py:42`, prefix `/sales`):

| Method and path | Permission |
|---|---|
| GET `/demand-summary` | `sales_order.view` |
| GET, POST `` | view / `sales_order.create` |
| GET, PATCH, DELETE `/{order_id}` | view / update / cancel |
| POST `/{id}/confirm` | `sales_order.update` |
| POST `/{id}/cancel` | `sales_order.cancel` |
| POST `/{id}/approve` | `sales_order.approve` |
| GET `/{id}/allocation-status` | `sales_order.view` |

### 2. How dispatch moves a sales order

**`dispatch_consignment`** (`dispatch/service.py:1390-1631`):
1. Locks the order header (`dispatch.repository.SalesOrderRepository.lock_header`, `:426`).
2. Locks the order lines in primary-key order (`SalesOrderLineRepository.lock_lines`, `:442-458`).
3. Consumes the reservations and posts the DISPATCH movements.
4. Writes `order_line.dispatched_quantity` and recomputes `line_status_for`.
5. Calls `_advance_sales_order` (`:1634-1667`), which steps through `SALES_ORDER_LIFECYCLE` using `sales_order_status_path` (`:398-420`), writing `record_status_change(action=AuditAction.DISPATCH_CHANGE)` for each step.

**Dispatch writes nothing to finance**: no ledger entry and no change to the customer's outstanding balance.

**`Dispatch.invoice_document_id`** (`dispatch/models.py:297-302`) is a foreign key to `documents` (ON DELETE SET NULL), described as "The invoice travelling with the goods". It holds an *uploaded* file. The service only checks that the document belongs to the tenant (`service.py:942-945`). On the frontend it exists only in the type (`dispatch-types.ts:158`); there is no screen to attach one.

Dispatch also carries `freight_amount`, `freight_paid_by`, `eway_bill_number` (free text) and `dispatch_date`.

### 3. Chart of accounts

**`LedgerAccount(TenantMasterEntity)`** (`finance/models.py:302-359`):
- Columns: `code` (unique per company), `name`, `account_type` (ASSET, LIABILITY, INCOME, EXPENSE, EQUITY), `parent_id`, `is_active`, `is_system`, `system_code`.
- A partial unique index `uq_ledger_accounts_system_code` (on `system_code IS NOT NULL AND deleted_at IS NULL`) allows only one account per system code.

**`SystemAccount`** (`:164-180`) lists the accounts the automatic postings use: ACCOUNTS_RECEIVABLE, ACCOUNTS_PAYABLE, PURCHASE_EXPENSE, INPUT_TAX, OUTPUT_TAX, BANK, CASH, ROUND_OFF.
- **There is no sales revenue, sales returns, job-work expense or freight account.**

**`DEFAULT_CHART`** (`finance/service.py:477-492`):

| Code | Name | Type |
|---|---|---|
| 1100 | Accounts Receivable | ASSET |
| 1200 | Bank | ASSET |
| 1210 | Cash | ASSET |
| 1300 | Input GST Credit | ASSET |
| 2100 | Accounts Payable | LIABILITY |
| 2200 | Output GST Payable | LIABILITY |
| 5100 | Purchases and Services | EXPENSE |
| 5900 | Rounding Differences | EXPENSE |

**`ensure_system_accounts(session, tenant) -> dict[str, LedgerAccount]`** (`:920-980`):
- Creates the default accounts the first time they are needed, not at seed time.
- Is idempotent: it takes the advisory lock `"finance_chart"` and re-reads after locking.
- Audits each account it creates.
- Returns the accounts keyed by `str(SystemAccount.X)`.

**Routes:** `/finance/accounts` (`router.py:119-200`). GET needs `ledger.view`; POST, PATCH and DELETE need `settings.manage`.

**Demo data** creates three revenue accounts that are *not* system accounts: 4000 Revenue, 4100 Sales, 4200 Freight (`core/demo_data/finance.py:300-335`).

### 4. Ledger posting API

**`LedgerEntry(ImmutableLedgerEntity)`** (`models.py:368-480`):
- Columns: `journal_id`, `line_number`, `account_id`, `party_type` (CUSTOMER, VENDOR or NONE), `party_id` (plain UUID), `entry_date`, `debit_amount`, `credit_amount`, `currency`, `reference_type` String(48) with **no CHECK**, `reference_id`, `reference_code`, `narration`, `financial_year_id`, `is_reversal`, `reverses_entry_id`.
- CHECKs:
  - exactly one of debit and credit is non-zero
  - `(party_type = 'NONE') = (party_id IS NULL)`
  - `is_reversal = (reverses_entry_id IS NOT NULL)`
- The party index runs on (company, party_type, party_id, entry_date, id).

**Building and posting a journal:**
- `JournalLine(account_id, debit=0, credit=0, party_type=NONE, party_id=None, narration=None, reverses_entry_id=None)` (`service.py:501-517`).
- `assert_balanced(lines) -> JournalTotals` (`:536-590`), a pure function: at least 2 lines, at most `MAX_JOURNAL_LINES` = 200 (`models.py:102`), one side per line, balanced to the paisa.
- **`post_journal(session, tenant, lines, *, entry_date, reference_type, reference_id=None, reference_code=None, narration=None, currency=None, journal_id=None) -> list[LedgerEntry]`** (`:1158-1271`):
  - It is the only code that writes ledger entries.
  - It checks every account exists and is active (`existing_ids`, `repository.py:292-306`).
  - It refuses an `entry_date` that falls in a closed `financial_years` row.
  - It writes an audit row with entity type `ledger_journal`.
  - If `currency` is omitted it uses `company_currency()`.
- **`reverse_journal(session, tenant, journal_id, *, reason, entry_date=None)`** (`:1274-1324`) posts the mirror image. It refuses to reverse a reversal, and dates the reversal today unless told otherwise.

**Voucher types** are just strings in `reference_type`:
- `REFERENCE_VENDOR_INVOICE = "vendor_invoice"`, `REFERENCE_PAYMENT = "payment"`, `REFERENCE_CUSTOMER_ADJUSTMENT = "customer_adjustment"` (`service.py:210-215`)
- The demo data also uses `"customer_charge"`.

There is **no HTTP route for posting a manual journal.**

**Party ledger:**
- `party_statement` (`:1332-1398`) is served as `GET /finance/ledger/customer/{id}` and `GET /finance/ledger/vendor/{id}`, both needing `ledger.view` (`router.py:227-276`).
- The running balance is a window function in `statement_stmt` (`repository.py:405`).
- The response is an `ApiResponse`; the pagination block is computed and then thrown away.

### 5. How money owed by customers is recorded today

There are three paths, and **none of them is triggered by a dispatch or a sales order.**

**(a) Manual debit or credit note**
- Route: `POST /finance/customer-adjustments` (`router.py:674-703`), gated on **`payment.approve`**.
- Function: `post_customer_adjustment(session, tenant, principal, customer_id, payload: CustomerAdjustmentCreate)` (`service.py:3189-3315`).
- The request body is `{customer_id, adjustment_type CHARGE|CREDIT, amount>0, account_id (the other side of the entry), entry_date?, reason 3-255}` (`schemas.py:652-668`).
- Steps:
  1. Locks the customer with `get_customer_for_order(lock=True)`.
  2. Calls `ensure_system_accounts` and refuses `account_id == AR`.
  3. Numbers it with `next_document_number(..., DocTypes.CUSTOMER_ADJUSTMENT, on=entry_date)`, prefix "CADJ".
  4. Calls `post_journal(customer_adjustment_lines(...))` (`:3150-3186`). That writes **two lines only**: a charge is Dr AR (with the customer as party) and Cr the named account; a credit is the reverse. `reference_type` is `"customer_adjustment"` and `reference_id` is the journal id.
  5. Calls `apply_outstanding_delta`.
  6. Audits it as `customer_adjustment`.
- There are no GST lines, no item lines, and the currency is always the company's (assumption §38).
- **There is no screen.** The only frontend trace is the generated type at `types/openapi.ts:1616`. The handbook says so: "a debit-note and credit-note facility … has no screen" (`docs/handbook/content.json`).

**(b) Customer receipt**
- A `Payment` with `payment_type=RECEIPT` and `party_type=CUSTOMER`.
- `approve_payment` (`:2598-2666`) calls `_post_payment_journal` (`:2669-2726`), which posts Dr BANK or CASH and Cr AR (with the customer as party), then `apply_outstanding_delta(-amount)`.
- `cancel_payment` (`:2729-2812`) reverses the journal and adds the amount back to the outstanding balance.
- `allocate_payment` refuses receipts (`:2926-2933`): "A customer receipt has no invoice to allocate against".

**(c) Demo data only.** `core/demo_data/finance.py:587-605` and `:1240-1310` post journals directly through `post_journal`:
- Dr AR (customer), Cr 4100 Sales, Cr 4200 Freight, Cr OUTPUT_TAX, with `reference_type="customer_charge"`, then `apply_outstanding_delta(+total)`.
- **This is the journal shape a sales invoice should post.**

**Cached balance:**
- `customers.outstanding_amount` (`customers/models.py:131-137`) has no `>= 0` CHECK.
- It is written only by `customers.service.apply_outstanding_delta(session, tenant, customer_id, OutstandingAdjustment) -> Customer` (`customers/service.py:1414-1468`), which locks the row and audits the change.
- `OutstandingAdjustment` (`customers/schemas.py:217-243`): `delta` (must be non-zero), `reason` (3-255), `reference_type` (required, 1-48), `reference_id` (optional), `reference_code` (required, 1-64).
- The credit check `evaluate_credit` (`customers/service.py:380-441`) uses exposure = outstanding + value of the order being confirmed.

### 6. Vendor invoices (the closest model to copy)

**`VendorInvoice(TenantEntity, OptimisticLockMixin)`** (`models.py:487-648`):
- Identity: `code` ("VINV"), `vendor_id`, `vendor_invoice_number`, unique on (company, vendor, number).
- Dates: `invoice_date`; `due_date`, which is fixed at creation by `_resolve_due_date` (`service.py:1762`) as invoice date + vendor `payment_terms_days`.
- Links: `purchase_order_id` and `goods_receipt_id`, both nullable FKs.
- Money: `currency`, `subtotal` (taxable value), `tax_amount`, `round_off_amount`, `total_amount`, `paid_amount`.
- `status`.
- Match evidence: `match_status`, `matched_at`, `match_variance_amount`, `variance_override_reason`.
- `approved_by_id`, `approved_at`, `dispute_reason`, `cancellation_reason`.
- CHECKs: `total = subtotal + tax + round_off`, `paid <= total`, `due >= invoice`, and complete approval, dispute, cancellation and override evidence.
- A partial index `ix_vendor_invoices_open` covers `OPEN_INVOICE_STATUSES` (`:201`).

**`VendorInvoiceLine`** (`:651-752`):
- Links: `purchase_order_line_id`, `goods_receipt_line_id`.
- `item_id` (nullable, so freight or service lines are allowed), `description`, **`hsn_code` copied onto the line** (String 16).
- `quantity`, `uom_id`, `unit_price`, `discount_amount`, `taxable_amount`, `tax_rate`, `cgst/sgst/igst/tax_amount`, `line_total`.
- CHECKs: the tax split adds up, `line_total = taxable + tax`, and IGST excludes CGST/SGST. Index `ix_vendor_invoice_lines_hsn`.

**Statuses** (`:183-197`): DRAFT, PENDING_APPROVAL, APPROVED, PARTIALLY_PAID, PAID, DISPUTED, CANCELLED. Groups: `PAYABLE_INVOICE_STATUSES` (`:211`), `INPUT_TAX_STATUSES` = APPROVED, PARTIALLY_PAID, PAID (`:217`), `EDITABLE_INVOICE_STATUSES` (`:224`). The machine is `VENDOR_INVOICE_LIFECYCLE` (`service.py:340-416`).

**Pricing:** `price_invoice_line(*, quantity, unit_price, discount_amount, tax_rate, is_interstate) -> LinePricing` (`:605-644`):
```python
tax = to_money(taxable * rate / 100); cgst = to_money(tax / 2); sgst = tax - cgst   # remainder
```
`is_interstate` is set **per line by the caller** (`schemas.py:233-236`).

**Flow:**
1. `create_invoice` (`:1675`): DRAFT, with a duplicate-number check (409 `DUPLICATE_DOCUMENT`).
2. `update_invoice` (`:1800`): any edit resets the match result.
3. `submit_invoice` (`:1983`): runs the match.
4. `approve_invoice(session, tenant, principal, invoice_id, payload: InvoiceApproval)` (`:2165-2247`):
   - re-runs the match;
   - a variance needs `override_reason` and `assert_may_override_variance`;
   - calls **`_post_invoice_journal`** (`:2250-2305`), which posts Dr PURCHASE_EXPENSE (subtotal), Dr INPUT_TAX (tax), Dr or Cr ROUND_OFF, and Cr AP (with the vendor as party) for the total, using `reference_type="vendor_invoice"` and `entry_date=invoice_date`.
5. `dispute_invoice` (`:2308`), `resolve_dispute` (`:2330`), `cancel_invoice` (`:2357-2389`).

**Three-way match:** `evaluate_match(...)` (`:703-864`) is pure. It compares price against the purchase order net price (2% tolerance) and quantity against the accepted goods-receipt quantity (0%), using `PRICE_VARIANCE_TOLERANCE` and `QUANTITY_VARIANCE_TOLERANCE` (`models.py:96-97`). **An invoice with no purchase order is blocked.**

**Routes** (`router.py:310-496`):

| Route | Permission |
|---|---|
| GET list and detail, GET `/match` | `vendor_invoice.view` |
| POST, PATCH, DELETE, POST `/submit` | `vendor_invoice.create` |
| POST `/approve`, `/dispute`, `/resolve-dispute`, `/cancel` | `vendor_invoice.approve` |

Finance never advances the purchase order to `INVOICED` or `PAID`. Those edges exist (`procurement/service_rules.py:351-372`) but are never used.

### 7. Payments and allocations

**`Payment(TenantEntity, OptimisticLockMixin)`** (`models.py:760-842`):
- `code` ("PAY"), `payment_type` PAYMENT or RECEIPT, `party_type` CUSTOMER or VENDOR (CHECK), `party_id` (plain UUID), `payment_date`, `amount`, `allocated_amount` (≤ amount), `currency`.
- `payment_method`: CASH, BANK_TRANSFER, CHEQUE, UPI, CARD, ADJUSTMENT.
- `reference_number`, `bank_name`, `instrument_date`.
- `status`: DRAFT, APPROVED, PARTIALLY_ALLOCATED, ALLOCATED, CANCELLED.

**`PaymentAllocation`** (`:845-883`): `payment_id` (CASCADE), **`vendor_invoice_id` NOT NULL** (RESTRICT), `amount`, unique on (payment, invoice). An ASSUMPTION comment says allocations are vendor-only "until a sales-invoice document exists".

**Logic:**
- `PAYMENT_LIFECYCLE` (`service.py:419-451`).
- `plan_allocation` (`:2864-2892`, pure).
- `allocate_payment` (`:2895-3031`) locks the payment, then locks the invoices with `lock_many` ordered by id, and checks two limits.
- `_assert_allocatable` (`:3034`), `_apply_allocation_plan` (`:3072-3131`).
- Status follows the money: `invoice_status_after_payment` (`:871-888`) and `payment_status_after_allocation` (`:891-899`).

**Routes** (`router.py:532-671`):

| Route | Permission |
|---|---|
| GET | `payment.view` |
| POST, PATCH, DELETE, `/allocate` | `payment.record` |
| `/approve`, `/cancel` | `payment.approve` |

### 8. Reports: outstanding, ageing, GST, valuation

**Outstanding.** `GET /finance/outstanding` (`router.py:706-745`, `finance.view`) calls `list_outstanding` (`service.py:3424-3474`), which uses `VendorInvoiceRepository.outstanding_stmt` (`repository.py:991`). It covers **payables only** and has no party type parameter.

**Ageing.** `GET /finance/ageing?party_type=CUSTOMER|VENDOR` (`router.py:748-804`) calls `ageing_report` (`service.py:3477-3601`):
- **Vendors:** aged from invoice due dates (`payable_ageing_stmt`, `repository.py:1050`).
- **Customers:** `receivable_ageing_stmt` (`:674-746`) over `_open_receivable_items` (`:549-672`). Receipts are set against charges oldest-first, per (party, currency), and **each AR debit is aged from its `entry_date`.** The accounts come from `system_account_ids(ACCOUNTS_RECEIVABLE)`; an empty set gives an empty report (assumption §28). Because there is no due date, customer `not_due` is always 0, and `customers.credit_days` is never read for receivables.
- Buckets come from `AGEING_BUCKET_BOUNDARIES` = (30, 60, 90) (`models.py:114`) and are reported per currency block.

**GST summary.** `GET /finance/gst-summary?date_from&date_to` (`router.py:865-884`, `gst_report.view`) calls `gst_summary` (`service.py:3758-3790`):
- **Output tax** comes from `GstRepository.output_rows_stmt` (`repository.py:1719-1764`): each sales order line's tax multiplied by `DispatchLine.quantity / SalesOrderLine.quantity`, for dispatches in `TAXABLE_DISPATCH_STATUSES` (`:145`), grouped by **`Item.hsn_code` as it is in the item master today** and `SalesOrderLine.tax_rate`.
- **Input tax** comes from `input_rows_stmt` (`:1766-1802`): vendor invoice lines in `INPUT_TAX_STATUSES`.
- Results are capped by `MAX_GST_ROWS` = 1000; a larger period is refused rather than cut off.
- **Customer credit adjustments and purchase returns are not deducted.**

**Inventory valuation.**
- `GET /finance/inventory-valuation` (`router.py:807`, `inventory.valuation_view`) calls `inventory_valuation` (`service.py:3626`), which values stock from the FIFO cost layers.
- `POST /finance/inventory-valuation/export` (`router.py:835`) calls `queue_inventory_valuation` (`service.py:3709-3755`). This creates a `report_snapshots` job, writes a `JOB_STARTED` audit row, and hands Celery task `"reports.inventory_valuation"` to queue `"reports"`.

### 9. GST helpers

- **`src/common/tax_ids.py`:** `GSTIN_PATTERN`, `gstin_check_digit`, `validate_gstin` and `validate_optional_gstin` (check digit included), `validate_pan`, `pan_from_gstin`, `state_code_from_gstin`.
- **Customers:**
  - `gstin_state_code` and `assert_gstin_matches_state` (`customers/service.py:503-526`)
  - `CustomerAddress.state_code` CHECK `^[0-9]{2}$`; an address in India must have a state code (`customers/models.py:320-331`); each address can have its own `gstin` (`:281-284`)
  - `Customer.legal_name`, `gstin`, `credit_days`
- **Company** (`organization/models.py:28-62`): `legal_name`, `gstin`, `pan`, `cin`, address, `state_code`, `currency`, `financial_year_start_month`, `logo_document_id`. **It has no bank details.** `Branch` has `gstin` and `state_code` (`:69-87`).
- **Item:** `hsn_code` String(16) and `gst_rate` (`products/models.py:359-361`). There is no SAC column; `hsn_code` can hold one.
- **Rounding:** `to_money` uses HALF_UP to 2 decimal places (`core/types.py:71-77`) even though the column is NUMERIC(18,4).
- **Frontend `apps/web/src/lib/statutory.ts`:** `GSTIN_PATTERN`, `PAN_PATTERN`, `IFSC_PATTERN`, `PINCODE_PATTERN`, `STATE_CODE_PATTERN`. These check the format only.

### 10. PDF generation and storage

**Only the packing-list PDF exists.** There is no purchase-order PDF, even though `apps/api/pyproject.toml:53` says "PDF generation (packing lists, POs)".

- **Renderer:** `render_packing_list_pdf(data: dict) -> bytes` (`workers/tasks/reports.py:248-303`).
  - It is pure and synchronous, built with reportlab: `SimpleDocTemplate` on A4, `PAGE_MARGIN = 15*mm`.
  - Reusable pieces: `_styles()` (`:121-134`), `_TABLE_STYLE` (`:137-149`), and the two-column key/value header layout in `_header_block` (`:152-189`).
- **Celery task:** `@async_task(name="reports.generate_packing_list", queue="reports")` (`:328-460`). In order:
  1. `reports_service.start_snapshot`
  2. `packing_service.packing_list_data` (`packing/service.py:2089-2163`, which returns a plain dictionary of strings)
  3. `anyio.to_thread.run_sync(render)`, then a sha256 checksum
  4. `build_storage_key(category=DocumentCategory.REPORT, ...)`, then `_write_bytes` (creates the file exclusively)
  5. A `Document(... entity_type="packing_order", entity_id=...)` row
  6. An audit row with `AuditAction.DATA_EXPORT`
  7. `complete_snapshot(document_id=...)`
  - If writing the row fails it deletes the file; if the task fails it calls `fail_snapshot` on a fresh session.
- **Queueing:** `packing.service.queue_packing_list` (`:2032-2086`). Inside a transaction it calls `reports_service.new_job_snapshot(ReportKey.PACKING_LIST, params, now=utcnow())` and writes a `JOB_STARTED` audit row. After the transaction it calls `reports_service.dispatch_job(session, tenant, job, task_name=..., queue=..., task_kwargs=..., subject=...)`.
- **Route:** `GET /packing/{id}/packing-list` returns 202 with `AcceptedJobResponse.create(job_id=..., status_url=reports_service.job_status_url(job_id), message=...)` (`packing/router.py:474-494`).
- **Polling and download:** the client polls `GET /reports/jobs/{id}` and downloads from `GET /documents/{id}/download`.
- **Constraints on report jobs:** `ReportKey.PACKING_LIST` is in `DOCUMENT_ONLY_REPORT_KEYS` (`reports/models.py:95-107`). `report_snapshots` has a **CHECK `report_key IN (...)`** (`:204`); see migration `20260905_0100_report_snapshots_packing_list_key.py`.
- **Retention:** `DEFAULT_RETENTION` covers only TEMP (24 hours) and EXPORT (7 days) (`documents/service.py:323-326`). REPORT and DOCUMENT files never expire.
- **Frontend:** `PackingListAction` (`features/packing/packing-order-dialog.tsx:913-1010`) and the `useReportJob` hook (`features/reports/report-jobs.ts`).
- **Missing for an invoice:** there is no backend helper for amount in words or Indian lakh/crore digit grouping, and no TTF font is registered, so the built-in Helvetica cannot draw "₹".

### 11. Every existing piece of invoice, credit-note and debit-note groundwork

| What | Where | Used by anything? |
|---|---|---|
| `DocTypes.SALES_INVOICE = DocType("sales_invoice", "INV")` | `common/codes.py:67` | No |
| `DocTypes.CUSTOMER_ADJUSTMENT` ("CADJ"), the manual debit or credit note | `codes.py:89` | Yes, `post_customer_adjustment` |
| `Permission.SALES_INVOICE_VIEW` / `SALES_INVOICE_CREATE` | `core/permissions.py:116-117`; group "Sales" (`:218`); `_SALES` has view (`:409`); `_ACCOUNTS` has view and create (`:432-433`); frontend `P` (`lib/permissions.ts:194-195`) | Only by the order edge `DISPATCHED->INVOICED` |
| `SalesOrderStatus.INVOICED`, `INVOICED->CLOSED` | sales models and service | Nothing ever reaches them |
| `sales_order_lines.invoiced_quantity`, plus CHECK | `sales/models.py:365, 416-419` | Never written; the "Invoiced" column in `sales-order-detail-page.tsx:522-527` always shows 0 |
| `dispatches.invoice_document_id` (uploaded file) | `dispatch/models.py:297-302` | Schema and validation only, no screen |
| `CustomerAdjustmentType.CREDIT`, labelled "Credit note" | `finance/models.py:150-161`; `CUSTOMER_ADJUSTMENT_LABELS` (`service.py:218`) | API only |
| `purchase_returns.debit_note_number` (free text) | `procurement/models.py:1371-1372` | Nothing is posted to finance |
| `NotificationType.PAYMENT_DUE` | `notifications/models.py:125` | Never sent |
| `TransactionType.CUSTOMER_RETURN` (+1, into AVAILABLE, serial becomes RETURNED) | `inventory/models.py:118,148`; `service.py:373,1710` | No document posts it |
| Status badges `INVOICED`, `POSTED`, `PAID`, `PARTIALLY_PAID` | `components/shared.tsx:153-201` | n/a |

### 12. Job-work costs

**`JobWorkOrder`** money fields (`job_work/models.py:252-275`):
- `rate_per_unit` (UnitPrice) and `rate_basis` (PER_PIECE, PER_KG or LOT)
- `estimated_charges`, fixed at approval
- `total_charges`, built up by receipts
- `vendor_debit_amount`, the value of excess loss
- **There is no tax rate, no SAC code and no invoice link.**

**`JobWorkChallan.total_value`** (`:487-543`) is the declared value of goods sent out; no GST is charged on it.

**`JobWorkReceipt`** (`:593-741`) has `total_charges`, `vendor_debit_amount`, `vendor_challan_number` and `vendor_challan_date`. **`JobWorkReceiptLine`** (`:744-851`) has `charges`, `vendor_debit_amount` and `output_quantity`.

**Charge rules:**
- `estimate_charges(*, rate_per_unit, rate_basis, expected_output_quantity)` (`service.py:391-403`)
- `receipt_charge(*, rate_per_unit, rate_basis, output_quantity, lot_already_charged)` (`:406-424`): good output only; a LOT rate is charged once.

**What finance is supposed to read:** `job_work_charges_for_period(session, tenant, *, date_from, date_to, vendor_ids=None, limit=500) -> list[VendorChargeRow]` (`:1153`), plus `list_charges_page` (`:1179`).
- Route: `GET /job-work/charges` (`job_work/router.py:150-186`), needing `finance.view` or `job_work.view`.
- `VendorChargeRow` (`schemas.py:592-608`): `{vendor_id, gross_charges, vendor_debits, net_charges, receipt_count, order_count, output_quantity}`.
- **Nothing calls it**, neither in finance nor in the frontend.

**How a job worker's bill is entered today:**
- It is a generic `VendorInvoice` with no `purchase_order_id`.
- `evaluate_match` adds "The invoice is not linked to a purchase order…", so the bill can only be approved with `override_reason`. The demo "plating" bills work this way (`core/demo_data/finance.py:24-30`).
- It is posted to PURCHASE_EXPENSE.
- Nothing links the bill to the job-work order or receipt, so double billing is not prevented and the built-up charges are never reconciled.

The receipt stock movements (`_build_receipt_movements`, `service.py:1807-1920`) pass no `unit_cost`, so the processing charge is not added to the output item's cost.

### 13. Frontend finance and sales screens

**Routes:**
- `route-loaders.ts:63-66`: `/finance`, `/finance/vendor-invoices`, `/finance/payments`, `/finance/accounts`
- `router.tsx:321-333`:
  - `/finance` needs `P.FINANCE_VIEW`
  - `/finance/vendor-invoices` needs `P.VENDOR_INVOICE_VIEW`
  - `/finance/payments` needs `P.PAYMENT_VIEW`
  - `/finance/accounts` needs `P.LEDGER_VIEW`
- `navigation.ts:251-279`: the Finance menu group.

**`finance-page.tsx` tabs:**
- Outstanding (payables only)
- Ageing (Receivables/Payables switch; recharts loaded lazily)
- Ledger (`ledger.view`)
- Inventory valuation
- GST summary

**Other finance files:**
- `vendor-invoices-page.tsx`: the list (DataTable, `useListQuery`, filters kept in the URL) plus `VendorInvoiceMatchDialog` and `VendorInvoiceFormDialog`
- `payments-page.tsx`, `payment-dialogs.tsx`, `accounts-page.tsx`
- `finance-types.ts`: `financeKeys` and types taken from the generated contract via `SchemaOf<...>`. It also holds `priceInvoiceLine`, a copy of the server's vendor-invoice pricing so the form can show totals before saving.

**Sales:**
- `sales-order-detail-page.tsx` offers Edit, Add line, Approve pricing, Cancel and Confirm. There is **no Invoice and no Close action.**
- `sales-types.ts` includes `INVOICED` in `SALES_ORDER_STATUSES`.

**Customers:** `dealers-page.tsx` shows the dealer channel's credit position (`over_credit_limit`, blocked, state, "only mine").

**There are no screens** for customer adjustments, sales invoices, credit notes, job-work charges, or a per-document list of customer receivables.

### 14. Documentation

**`docs/assumptions.md`:**
- §1 credit limit; §2 segregation of duties; §5 match tolerance
- **§9 GST place of supply** (rounded HALF_UP per line; exports, SEZ and reverse charge are not covered)
- **§13 numbering** (`PREFIX/2026-27/00042`, reset each financial year)
- §27 ageing kept per currency; §28 no AR account gives an empty report
- **§32 "Customer charges are receivable journals, not invoices".** It says: "If Atcraft needs a real customer invoice … that is a new document with its own numbering and posting path, and the adjustment route becomes the correction path beside it".
- §38 currency of adjustments
- Open questions 3 (multiple currencies) and 7 (partial dispatch)

**`docs/database.md`:**
- §2 says finance owns `ledger_accounts`, `ledger_entries`, `vendor_invoices`, `vendor_invoice_lines`, `payments`, `payment_allocations`, with no customer invoice table.
- §7 shows the sales order machine including INVOICED.
- §8 lists the cross-module integration points, and has no finance row.

---

## Missing vs proposal

### Gap 3: sales invoice and PDF
1. **No document.** There are no `sales_invoices` or `sales_invoice_lines` tables, and no service, repository, routes, schemas or screens.
2. **No GST invoice numbering in use.** The `INV` series would also produce **17 characters** (`INV/2026-27/00042`). GST Rule 46 limits a tax invoice serial number to 16 characters, and the e-invoice schema has the same limit.
3. **No revenue posting.** Nothing posts Dr AR / Cr Sales / Cr Output GST from a document. The default chart has **no income system account.**
4. **No copy of the buyer and seller details** on a document: legal name, GSTIN, billing and shipping address text, and the seller's GSTIN and state.
5. **No due date** for customer receivables, so they cannot be "overdue against terms".
6. **No link from invoice to dispatch or order.** `invoiced_quantity` is never written, and orders never reach `INVOICED` or `CLOSED`.
7. **The GST output figure is not based on invoices.** It is worked out from dispatches, uses today's item HSN code, and ignores credit notes.
8. **No PDF and no rendering helpers:** no amount in words, no Indian digit grouping, no font for ₹, and no company bank details.
9. **No e-invoice fields** (IRN, acknowledgement number and date, signed QR code). Nothing covers exports, SEZ, reverse charge, or tax collected at source.
10. **Customer receipts cannot be matched against invoices.** Allocations only accept vendor invoices.

### Gap 4: credit side
1. **No GST credit note** with lines, a reference to the original invoice, reversal of output GST, and its own series. "CADJ" produces 18 characters and is not a GST series.
2. **No stock return posting.** `CUSTOMER_RETURN` is never posted.
3. **No sales-returns account.**
4. **The GST summary ignores credits.**
5. **No limit on what can be credited.** Nothing stops a credit for more than was invoiced.
6. **No screen for the existing manual adjustment either.**

### Gap 7: job-work invoice
1. **No link** between a vendor invoice and a job-work order, receipt or receipt line.
2. **No job-work match.** The rate and good output quantity are never compared with the bill.
3. **No tracking of billed against built-up charges**, so double billing is possible.
4. **No GST rate or SAC code** on job work.
5. **No job-work expense account.**
6. **Excess-loss debits are never posted to the vendor's account.** `vendor_debit_amount` is only recorded, never posted as a debit note.
7. **No caller** for `GET /job-work/charges`.

### Gap 10: dealer performance and payment follow-up
1. **No customer or dealer performance report.** Only `VENDOR_PERFORMANCE` exists (`reports/service.py:614`, `repository.py:716`).
2. **No per-invoice list of customer receivables and no overdue logic.** Receivable ageing uses the posting date.
3. **No follow-up record:** no notes, promise-to-pay date, next action or escalation.
4. **No scheduled check of overdue receivables.** `PAYMENT_DUE` is never sent, and the Celery beat schedule (`workers/celery_app.py:71-110`) has no receivable job.

---

## Patterns to copy

| New piece | Closest existing code | What to copy exactly |
|---|---|---|
| Invoice header and lines | `VendorInvoice` and `VendorInvoiceLine` (`finance/models.py:487-752`) | **Base classes:** header is `TenantEntity, OptimisticLockMixin`; lines are `TenantEntity` with ON DELETE CASCADE to the header. **Columns:** `code` String(64), `status` String(24) with CHECK `status IN ({sql_in_list(Enum)})`, money in `Money`, prices in `UnitPrice`, quantities in `Quantity`, rates in `Percentage`. **Constraints:** `UniqueConstraint("company_id","code", name="uq_<table>_company_id_code")`; header CHECK total = taxable + tax + round_off, `paid <= total`; line CHECKs for the tax split, line total and IGST exclusivity; CHECKs that approval, cancellation and posting evidence are complete; a partial index for open documents named after the status tuple. |
| GST line amounts | `sales.service.compute_line_amounts` (`:288`) | Use this function, not `finance.price_invoice_line`. See Traps. |
| Seller and buyer GST treatment | `sales.service.resolve_tax_treatment` (`:260`) and `OrderPartyRepository.seller_state_code` (`sales/repository.py:291`) | Copy `is_interstate` and `place_of_supply_state_code` from the sales order; do not recompute them. |
| Document number | `create_invoice` (`finance/service.py:1735`) | `code = await next_document_number(session, tenant.company_id, DocTypes.X, on=document_date)` inside the same `transaction()`. |
| State machine | `VENDOR_INVOICE_LIFECYCLE` (`:340-416`) and `_transition_invoice` (`:1948-1980`) | `StateMachine(entity="Sales invoice", initial=..., transitions=(Transition(src, dst, permission=Permission.X, label="...", is_irreversible=True), ...), terminal=frozenset({...}))`. One `_transition_*` function writes the status and calls `record_status_change(..., action=..., reason=...)`. |
| Posting a document | `approve_invoice`, `_post_invoice_journal` (`:2165-2305`), the demo customer charge (`core/demo_data/finance.py:1240-1310`) | Lock the header (`lock_for_update`); `accounts = await ensure_system_accounts(session, tenant)`; build a list of `JournalLine`; `await post_journal(session, tenant, lines, entry_date=doc.date, reference_type=REFERENCE_X, reference_id=doc.id, reference_code=doc.code, narration=..., currency=doc.currency)`; then `record_audit(action=AuditAction.APPROVE, ...)`. |
| Moving the customer balance | `post_customer_adjustment` (`:3189-3315`), `approve_payment` (`:2648-2664`) | Lock the customer first with `get_customer_for_order(session, tenant, customer_id, lock=True)`, then `apply_outstanding_delta(session, tenant, customer_id, OutstandingAdjustment(delta=..., reason=..., reference_type=REFERENCE_X, reference_id=doc.id, reference_code=doc.code))` in the same transaction. |
| Cancel with reversal | `cancel_payment` (`:2729-2812`) | `entries_for_reference(REFERENCE_X, doc.id)`; skip journals already reversed; `reverse_journal(session, tenant, journal_id, reason=...)`; undo the outstanding change; refuse if the document is already cancelled. |
| Updating order lines from another module | `dispatch_consignment` and `_advance_sales_order` (`dispatch/service.py:1427-1667`), `dispatch/repository.py:416-476` | Lock the header (`with_for_update`); `lock_lines` ordered by id; update quantities; recompute `line_status_for`; step through `SALES_ORDER_LIFECYCLE.assert_transition` one edge at a time, writing `record_status_change` each time. |
| Settling invoices from a payment | `allocate_payment`, `plan_allocation`, `_apply_allocation_plan` (`:2864-3131`) | Pure planning function; lock the payment, then `lock_many` the invoices ordered by id; two limits checked after locking; status follows the money. |
| Listing and detail projection | `invoice_summary` / `invoice_detail(invoice, lines)` (`:1422-1465`), `list_invoices` (`:1468`), `VendorInvoiceRepository` (`repository.py:896-990`) | Build the read model by hand rather than from relationships (see the `model_validate` trap). The repository declares `sortable_fields`, `searchable_fields`, a `filtered()` builder, and `list_page` using `sort.resolve(self.sortable_fields, default)`. |
| Schemas | `finance/schemas.py:49-58, 210-305` | `ConfigDict(extra="forbid")`; the `Money`, `PositiveMoney`, `TaxRate`, `Reason`, `Currency` types; never accept `company_id`, `status`, totals or tax. Rules that need several fields go in `@model_validator(mode="after")`. |
| Router | `finance/router.py:310-496` | Thin handlers: `dependencies=[require_permission(Permission.X)]`; return `ApiResponse[T].ok(data, "message")` or `PaginatedResponse[T](data=..., pagination=...)`; reasons for DELETE go in the query string (`ReasonQuery`); actions with a body are POST `/{id}/action` with `ReasonRequest`. |
| PDF | `render_packing_list_pdf`, the `generate_packing_list` task, `queue_packing_list`, the packing-list route | A pure render function taking a plain dictionary; a data-assembly function in the service; a Celery task using the job-row lifecycle; `Document(entity_type="sales_invoice", ...)`; a 202 `AcceptedJobResponse`. |
| Adding a report key | Migration `20260905_0100_report_snapshots_packing_list_key.py` | List the key values literally; drop the constraint, re-add it `NOT VALID`, then `VALIDATE`; the downgrade deletes the new keys' rows first. |
| Adding permissions | Migration `20260906_0902_grant_item_attribute_permissions.py` | `UPDATE roles SET permissions = array_append(...) WHERE is_system AND code = ANY(...)`. |
| Customer or dealer performance report | `ReportKey.VENDOR_PERFORMANCE`: definition `reports/service.py:614-623`, query `repository.py:716`, route `router.py:609-630` | `ReportDefinition(key, title, description, row_model, build=lambda repo, ctx: ..., requires_period, cacheable, derive)`. The reports screen picks up new reports automatically (`features/reports/reports-page.tsx`). |
| Overdue-receivable check | `dispatch.service.scan_delays` and `_raise_delay_alert` (`:1846-1960`), `workers/tasks/notifications.py:60`, beat entry `celery_app.py:86-91` | Read across tenants, then split by company; one nested transaction per company; `notify_role(..., notification_type=NotificationType.PAYMENT_DUE, group_key=f"...:{company_id}")`; a "notified at" timestamp to avoid repeat alerts; one audit row per company per run. |
| Frontend list page | `vendor-invoices-page.tsx` | `useListQuery({path, queryKey, defaultSortBy, staleTime: staleTimes.transactional, filters})`, `DataTable`, a `FILTER_KEYS` constant with a single-write Clear, `StatusBadge`, permission checks such as `can(P.X)`, and separate detail and form dialogs held in page state. |
| Frontend detail and commands | `vendor-invoice-match-dialog.tsx`, `payment-dialogs.tsx` | `ConfirmDialog` with a reason; `toast` and `success.posted(...)` from `lib/messages.ts`; show an action only when the server's state machine allows it. |
| Frontend PDF button | `PackingListAction` (`packing-order-dialog.tsx:913-1010`), `features/reports/report-jobs.ts` | Calls the job route, polls `/reports/jobs/{id}` until finished, then `api.download('/documents/{id}/download', name)`. Polling depends on the user having `P.REPORT_VIEW`. |
| Frontend types | `finance-types.ts` | `SchemaOf<'SalesInvoiceDetail'>`; add keys to `financeKeys` under `['finance', 'sales-invoices', ...]`. |
| Frontend tests | `vendor-invoices-page.test.tsx` | `renderScreen`, `ok`, `page`, `fail` from `@/test/harness`. |
| Backend tests | `tests/unit/test_finance_customer_adjustment.py`, `test_sales_tax.py`, `test_finance_gst_summation.py`, `test_packing_list_render.py`; `tests/integration/test_finance_service.py` (one schema per module, `tests/seed_support`); `tests/api/test_finance_customer_adjustments_http.py`, `test_report_jobs_for_documents.py`, `test_route_writes_persist.py` | Pure rules tested without a database; a real-Postgres test of journal balance and outstanding; an HTTP test of permissions and commit; an end-to-end 202 job test. |

---

## Integration points

**A sales invoice must call or update all of the following:**
1. `sales_order_lines.invoiced_quantity`. The database enforces ≤ ordered quantity; the service should also enforce ≤ dispatched quantity. Lock the lines as dispatch does.
2. `SALES_ORDER_LIFECYCLE`:
   - step `DISPATCHED -> INVOICED` once every line that is not cancelled is fully invoiced;
   - add a `close_order` service and route for `INVOICED -> CLOSED` (the edge exists, permission `sales_order.update`).
3. `dispatches` / `dispatch_lines`: link each invoice line to its dispatch line, and allow only one live invoice per sales dispatch. Optionally fill `dispatches.invoice_document_id` with the generated PDF.
4. `finance.ensure_system_accounts` must provide a new `SystemAccount.SALES_REVENUE`; then `post_journal` posts:
   - Dr ACCOUNTS_RECEIVABLE (party CUSTOMER) for the total
   - Cr SALES_REVENUE for the taxable value
   - Cr OUTPUT_TAX for the tax
   - Dr or Cr ROUND_OFF for any rounding
5. `customers.apply_outstanding_delta(+total)` in the same transaction. This also feeds the next credit check.
6. `GstRepository.output_rows_stmt` must read invoice lines minus credit-note lines. Keep the dispatch-based query only as a "dispatched but not invoiced" check.
7. `reports`: a new document-only `ReportKey`, plus a CHECK migration, for the PDF job. Optionally `product_profitability` could use invoiced revenue instead of order lines (`reports/repository.py:773-811`).
8. Receivable ageing (`_open_receivable_items`): to age by due date, outer-join AR debits where `reference_type='sales_invoice'` to `sales_invoices.due_date`, falling back to `entry_date`. Matching receipts to invoices requires `payment_allocations` to accept a sales invoice (nullable `sales_invoice_id` plus an exactly-one CHECK and partial unique indexes), or a separate table. The refusal at `finance/service.py:2926-2933` must then be lifted.

**A credit note must call or update:**
- The source invoice line (credited quantity and value, with a limit).
- `post_journal`:
  - Dr SALES_RETURNS (new system account) or SALES_REVENUE for the taxable value
  - Dr OUTPUT_TAX for the tax
  - Cr AR (party CUSTOMER) for the total
- `apply_outstanding_delta(-total)`.
- The GST summary, as a deduction from output tax.
- When goods physically come back, the customer-return document (owned by another area) posts `post_movements([MovementRequest(transaction_type=TransactionType.CUSTOMER_RETURN, ...)])`. The default target pool is AVAILABLE (`inventory/service.py:373`). Goods awaiting inspection need `QC_HOLD` or `DAMAGED` instead.

**A job-work invoice must call or update:**
- `job_work_orders`, `job_work_receipts` and `job_work_receipt_lines`: the match runs against `rate_per_unit`, `rate_basis`, `output_quantity` and `charges`, and a new "invoiced charges" amount must be tracked.
- `_post_invoice_journal`: debit a new `JOB_WORK_EXPENSE` account instead of `PURCHASE_EXPENSE`.
- Optionally post the vendor's excess-loss debit: Dr AP (vendor), Cr JOB_WORK_EXPENSE (or a recoveries account).
- `GET /job-work/charges` and `job_work_charges_for_period` become the source for proposing invoice lines.

**Things that cross module boundaries:**
- `finance.service` already imports `customers.service` and `reports.service`.
- `finance.repository` already imports sales, dispatch, procurement and inventory *models*.
- `finance.service` importing `sales.service` (for `SALES_ORDER_LIFECYCLE`, `line_status_for` and `compute_line_amounts`) does not create an import cycle; dispatch already does the same.

---

## Traps & constraints

**Rules specific to this area**

1. **There are two different GST split rules, and they disagree.**
   - Sales (`compute_line_amounts`) rounds each half separately, giving equal halves. This is pinned by `test_sales_tax.py::test_intra_state_halves_are_rounded_independently_and_stay_equal`, where 5% on 1234.55 gives 30.86 + 30.86 = 61.72.
   - Finance (`price_invoice_line`) rounds the tax first and makes SGST the remainder, giving 61.73 split as 30.87 + 30.86. This is pinned by `test_finance_gst_summation.py` and copied on the frontend in `priceInvoiceLine`.
   - A sales invoice priced with the finance function will not equal its own order. For a full-quantity invoice, copy the order line's stored split; for a partial one, recompute with `compute_line_amounts`.
   - A flat `discount_amount` on a partly invoiced line must be shared out pro rata, with the last invoice taking whatever is left so the totals reconcile.
2. **The invoice number is too long.** `DocTypes.SALES_INVOICE` produces `INV/2026-27/00042`, which is 17 characters; the GST limit is 16. Change `padding=4` or the prefix before the first invoice is issued. First check that `document_sequences` has no `sales_invoice` row; the format comes from code, not from the stored row. A `CREDIT_NOTE` series with prefix "CN" gives 16 characters. Record the decision in `docs/assumptions.md` §13.
3. **The company's financial-year start month is ignored.** `next_document_number(..., fy_start_month=4)` is never passed `companies.financial_year_start_month`, despite what assumption §13 says.
4. **New account codes can clash.** `ensure_system_accounts` inserts using fixed codes and does not check for existing ones. The demo data already creates non-system accounts **4000, 4100 and 4200**. A new `SALES_REVENUE` seeded as "4100" will fail with a unique-constraint error the first time an invoice is posted in a demo or hand-built chart. Choose codes that cannot clash, or adopt an existing account with that code.
5. **The vendor-invoice cancel path is a defect not to copy.** `APPROVED -> DISPUTED` (`service.py:379-384`) followed by `DISPUTED -> CANCELLED` (`:409-414`) lets `cancel_invoice` (`:2357-2389`) cancel an invoice whose journal has already been posted, **without reversing it**. The ledger keeps the payable and the input GST, while the invoice-based ageing and the GST summary drop the invoice. Nothing tests this case. A sales-invoice or credit-note cancel must reverse its journal, as `cancel_payment` does, and must undo the outstanding change. This is the "follow the row one step further" trap from CLAUDE.md.
6. **Copy the HSN code onto the invoice line.** The current GST output query groups by today's `Item.hsn_code`, so changing an item's code rewrites past returns. `VendorInvoiceLine.hsn_code` (`finance/models.py:679-683`) is the pattern; the finance `ItemLookupRepository.load` already returns `hsn_code` and `gst_rate`.
7. **Avoid double counting GST output.** Once invoices exist, the dispatch-based query and an invoice-based one must not both feed the summary. The `GstRepository` docstring assumption must be rewritten, along with the route description at `router.py:869-874` and the GST tab text.
8. **Customer exposure depends on the cached balance.** `outstanding_amount` must be moved by every posting and every reversal, in the same transaction; `OutstandingAdjustment.reference_code` is required. Never apply the change twice. The demo-data docstring explains why `apply_outstanding_delta` is not idempotent.
9. **Closed financial years.** `post_journal` refuses entries dated in a closed year, so reversals for old invoices must be dated today. `reverse_journal` already defaults to `today()`.
10. **Receipts cannot be matched to invoices yet.** `PaymentAllocation.vendor_invoice_id` is NOT NULL and part of a unique constraint. Making it polymorphic needs a migration, partial unique indexes, and an exactly-one CHECK. Remember that partial unique indexes are checked per statement, not at commit (CLAUDE.md).
11. **Segregation of duties for credit notes.** `ACCOUNTS` holds `sales_invoice.create` but **not** `payment.approve`. If credit notes and cancellations follow the customer-adjustment precedent (`payment.approve`), only ADMIN can post them. That is a business decision; record it.
12. **New permission strings do not reach most deployed roles.** `_seed_roles` refreshes only `is_system` roles (SUPER_ADMIN, ADMIN and the vendor roles). ACCOUNTS and SALES_EXECUTIVE are owned by the client and will not receive a new permission on existing deployments. You need a data migration (precedent `20260906_0902`) and a note that an administrator must grant it. Reusing `sales_invoice.*`, `vendor_invoice.*` and `payment.*` avoids this.
13. **Adding a new `ReportKey` needs a CHECK migration**, and the key must go into `DOCUMENT_ONLY_REPORT_KEYS`. Otherwise the import-time assertion in `reports/service.py:749-757` stops startup.
14. **Invoice PDFs must never expire.** Use DocumentCategory REPORT or DOCUMENT, never EXPORT or TEMP, because statutory records must be kept for 72 months.
15. **Rendering the PDF.** Built-in fonts cannot draw ₹. Register a TTF font or print "INR"/"Rs.". Amount in words and lakh grouping must be written in Python. Rendering must stay in Celery ("Long work" rule; `queue_packing_list` sets the pattern).
16. **When the invoice is raised.** The GST tax invoice is due at or before goods leave (CGST Act s.31). Raising one per dispatch fits this better than the order-level `DISPATCHED -> INVOICED` edge suggests. If invoices can be created while the dispatch is `READY_FOR_DISPATCH`, block `cancel_dispatch` while a live invoice exists. Record this as an ASSUMPTION.
17. **Currency.** Use the sales order's `currency`. Receivable netting is per (party, currency) (assumption §27). Never add amounts in different currencies together.

**General CLAUDE.md traps that apply here**

- **`model_validate(orm_row)` reads every declared field, including `lazy="raise"` relationships.** Build detail responses explicitly, as `invoice_detail(invoice, lines)` does, or use `set_committed_value`. Test with a list that contains a row you actually created.
- **`clause or default` raises.** Sort defaults must use `x if x is None else y`, as `VendorInvoiceRepository.list_page` (`repository.py:941-945`) does.
- **`if xs: stmt.where(col.in_(xs))` with no `else` inverts the filter.** See assumption §28 and the comment at `repository.py:600-602`.
- **Order audit and ledger reads by `(created_at, id)` or `(entry_date, id)`.**
- **Never call `session.rollback()` in a service.** After an expected failure inside `transaction()`, keep ids in plain variables.
- **Money is `Decimal` only**, and `to_money` is applied once at the point a figure is stored.
- **A screen button and its endpoint must name the same permission** (`test_frontend_permission_gates.py`).
- **A status missing from the badge map shows grey**, so every new status needs an entry in `STATUS_VARIANTS` (`test_status_badge_catalogue.py`). DRAFT, POSTED, PARTIALLY_PAID, PAID, CANCELLED and ISSUED already exist; CREDITED does not.
- **A sortable column the server does not allow empties the table.** Every column marked `enableSorting` must be in the repository's `sortable_fields`.
- **Do not offer a button on a status the state machine cannot leave.**
- **Do not hand-write response types.** Use `SchemaOf`, or register the type in `test_frontend_contract_types.py`, and avoid `response_model=None`.
- **Build test rows through the ORM** (`tests/seed_support`). No raw `INSERT`, and no `CREATE TABLE` for another module's table in a test.

**Meta-tests that will check new code**

| Test | What it requires of new code |
|---|---|
| `test_state_machine_entity_names.py` | The machine's entity name must read like a phrase: "Sales invoice", "Credit note". |
| `test_entity_names.py`, `test_repository_entity_label.py` | Set `entity_label` if the default derived from the class name reads badly. |
| `test_sales_lifecycle.py` | `validate()`: no unreachable states and no dead ends; the happy path; irreversible edges. Any new edge, such as `INVOICED -> DISPATCHED` for cancellation, must keep these passing. |
| `test_dispatch_partial_dispatch.py` | `sales_order_status_path` behaviour. |
| `test_role_presets_reach_roles.py` | Every permission resource belongs to a `PERMISSION_GROUPS` entry. |
| `test_frontend_permission_catalogue.py` | The frontend `P` list equals the backend permission enum. |
| `test_frontend_permission_gates.py` | Every mutating call on a screen is gated on the endpoint's permission. |
| `test_frontend_contract_types.py`, `test_frontend_constants.py` | No inline page sizes or debounce delays. |
| `test_status_badge_catalogue.py` | A badge colour for every status value. |
| `test_schema_identifier_length.py` | Generated `ck_<table>_<name>` must be ≤ 63 bytes. |
| `test_reports_catalogue.py` | `set(REPORT_CATALOGUE) == set(ReportKey) - DOCUMENT_ONLY_REPORT_KEYS`. |
| `test_e2e_sweep_routes.py` | A new static route such as `/finance/sales-invoices` must appear in `browser-sweep.mjs` STATIC_ROUTES, `a11y-sweep.mjs` ROUTES, `responsive-sweep.mjs` STATIC_ROUTES, and `permission-sweep.mjs` ROUTES with the right permission. |
| `test_postman_and_contract_targets.py` | Regenerate `openapi.json` and `postman_collection.json` (`make contract`); update `docs/api.md` "494 requests in 27 folders". |

The pre-push hooks also run mypy, vitest, `tsc -b` and the contract check.

---

## Files a change here will touch

**Backend: models and code**
- `apps/api/src/modules/finance/models.py`:
  - new `SalesInvoice`, `SalesInvoiceLine`, `CreditNote`, `CreditNoteLine` and their status enums and open-status tuples
  - `SystemAccount` gains `SALES_REVENUE`, `SALES_RETURNS`, `JOB_WORK_EXPENSE`
  - optionally `VendorInvoice.job_work_order_id`, `VendorInvoiceLine.job_work_receipt_line_id`, and polymorphic `PaymentAllocation`
- `apps/api/src/modules/finance/service.py`: `DEFAULT_CHART`, new state machines, create/post/cancel/list/get, PDF queueing, credit notes, a job-work branch in `evaluate_match`/`_match`, and the expense account in `_post_invoice_journal`.
- `apps/api/src/modules/finance/repository.py`: new repositories; `GstRepository.output_rows_stmt`; due-date ageing in `_open_receivable_items`.
- `apps/api/src/modules/finance/schemas.py`, `finance/router.py` (routes under `/finance/sales-invoices`, `/finance/credit-notes`).
- `apps/api/src/common/codes.py`: `SALES_INVOICE` padding or prefix; `CREDIT_NOTE`.
- `apps/api/src/modules/sales/service.py` and `router.py`: `close_order`; any new edge.
- `apps/api/src/modules/dispatch/service.py`: guard against cancelling a dispatch that has a live invoice; optionally a "raise invoice" helper.
- `apps/api/src/modules/job_work/models.py`, `service.py`, `schemas.py`: `tax_rate` and `sac_code` on the order; invoiced charges on receipts.
- `apps/api/src/modules/reports/models.py`, `service.py`, `repository.py`, `router.py`, `schemas.py`: the invoice PDF key; `DEALER_PERFORMANCE`.
- `apps/api/src/workers/tasks/reports.py`: `render_sales_invoice_pdf` and the task.
- `apps/api/src/workers/tasks/notifications.py` and `celery_app.py`: the overdue-receivable check.
- `apps/api/src/core/permissions.py`: only if new permission strings are added.
- `apps/api/src/core/demo_data/finance.py`: optionally replace the hand-posted customer charges with real invoices.

**Migrations**
- New file(s) in `apps/api/migrations/versions/` with `down_revision = "2f5e08c7ba61"` (the current head).
- Contents: tables; seeding no system accounts (that happens lazily); widening the `report_snapshots` CHECK; any permission grants.
- Each must document its locking impact, any data migration, and how to roll back.

**Tests**
- `apps/api/tests/unit/`:
  - new `test_sales_invoice_rules.py` (pricing and sharing out discounts, journal lines, the quantity limit)
  - new `test_credit_note_rules.py`
  - updates to `test_finance_gst_summation.py`, `test_sales_lifecycle.py`, `test_reports_catalogue.py`
  - a new invoice render test modelled on `test_packing_list_render.py`
- `apps/api/tests/integration/`: `test_finance_service.py` or a new `test_sales_invoice_service.py`; `test_dispatch_service.py`; `test_job_work_service.py`.
- `apps/api/tests/api/`: an HTTP permission and commit test; extend `test_report_jobs_for_documents.py`.

**Frontend**
- `apps/web/src/features/finance/`:
  - new `sales-invoices-page.tsx` (with test) and `sales-invoice-dialog.tsx`
  - credit-note dialogs
  - a customer-adjustment dialog (the screen still missing today)
  - `finance-types.ts`
- `features/sales/sales-order-detail-page.tsx` and `sales-types.ts`: Close and invoice links.
- `features/dispatch/dispatch-dialogs.tsx`: a "Raise invoice" action.
- `features/job-work/` for job-work charges; `features/customers/dealers-page.tsx` for a link to dealer performance.
- `app/route-loaders.ts`, `app/router.tsx`, `app/navigation.ts`.
- `components/shared.tsx` (badges), `lib/permissions.ts` (only for new permissions), `types/openapi.ts` (generated).

**Repository root and docs**
- `openapi.json`, `postman_collection.json`.
- `tests/e2e/scripts/{browser,a11y,responsive,permission}-sweep.mjs`.
- `docs/database.md` §2, §7 and §8.
- `docs/assumptions.md`: rewrite §32, update §9 and §13, and add new sections for when the invoice is raised, the invoice number format, account codes, and credit-note authority.
- `docs/api.md`: the request count and the `/finance` line.
- `docs/handbook/content.json`: the finance chapter's "has no screen" sentence and role descriptions.

---

## Recommendation

### Sales invoice, owned by finance at `/finance/sales-invoices`

- **Why finance owns it:** posting, numbering, payments, ageing and the GST summary all live there, and `vendor_invoices` is the matching document on the purchase side. The permission group "Sales" already lists `sales_invoice`.
- **Tables:** `sales_invoices` and `sales_invoice_lines`, one invoice per SALES dispatch.
  - **Header:** `dispatch_id`, `sales_order_id`, `customer_id`, `invoice_date` (defaults to `dispatch_date`), `due_date` = invoice date + `customers.credit_days`, `currency` (copied from the order), `place_of_supply_state_code` and `is_interstate` (copied from the order), the seller's state and GSTIN, copies of the buyer's details, `subtotal` (gross), `discount_amount`, `taxable_amount`, `cgst`, `sgst`, `igst`, `tax_amount`, `round_off_amount`, `total_amount`, `paid_amount`, `status`, posting and cancellation evidence, and an optional `pdf_document_id`.
  - **Lines:** `dispatch_line_id`, `sales_order_line_id`, `item_id`, `hsn_code` (copied), `quantity`, `unit_price`, `discount_amount`, `taxable_amount`, `tax_rate`, the tax split, `line_total`.
- **Statuses:** DRAFT → POSTED → PARTIALLY_PAID ⇄ PAID; also DRAFT → CANCELLED, and POSTED → CANCELLED only while `paid_amount == 0`, with the journal reversed. Use `Permission.SALES_INVOICE_CREATE` to create and post. For cancelling, use `payment.approve` (like adjustments) or add a new permission with a grant migration.
- **Numbering:** `next_document_number(session, tenant.company_id, DocTypes.SALES_INVOICE, on=invoice_date)`, after fixing the 16-character limit.
- **Pricing:** `sales.service.compute_line_amounts(quantity=invoiced_qty, unit_price=line.unit_price, tax_rate=line.tax_rate, is_interstate=order.is_interstate, discount_percentage=line.discount_percentage or None, discount_amount=<shared-out flat discount>)`.
- **Posting** (`post_sales_invoice`), in one `transaction()`:
  1. `get_customer_for_order(lock=True)`; lock the invoice; lock the order lines with the `lock_lines` pattern.
  2. `accounts = await ensure_system_accounts(...)` with `SALES_REVENUE` added to `SystemAccount` and `DEFAULT_CHART` (INCOME, using a code that cannot clash).
  3. `post_journal(session, tenant, [`
     - `JournalLine(AR, debit=total, party_type=CUSTOMER, party_id=customer_id)`
     - `JournalLine(SALES_REVENUE, credit=taxable)`
     - `JournalLine(OUTPUT_TAX, credit=tax)`
     - `ROUND_OFF` on whichever side applies

     `], entry_date=invoice_date, reference_type="sales_invoice", reference_id=invoice.id, reference_code=invoice.code, currency=invoice.currency)`
  4. `apply_outstanding_delta(+total, reference_type="sales_invoice", reference_code=invoice.code)`.
  5. `order_line.invoiced_quantity += qty`, refusing anything above `dispatched_quantity`.
  6. Step the order `DISPATCHED -> INVOICED` with `record_status_change` once every line that is not cancelled is fully invoiced.
  7. `record_audit(action=AuditAction.APPROVE, entity_type="sales_invoice", ...)`.
- **Cancel:** mirror `cancel_payment`. Call `reverse_journal`, apply `apply_outstanding_delta(-total)`, and reduce `invoiced_quantity`. If the order is already INVOICED, either add an `INVOICED -> DISPATCHED` edge or refuse and require a credit note; this is a decision to record.
- **PDF:**
  - `queue_sales_invoice_pdf` modelled on `queue_packing_list`;
  - add `ReportKey.SALES_INVOICE` to `DOCUMENT_ONLY_REPORT_KEYS`, with the CHECK migration;
  - task `reports.generate_sales_invoice` that uses `render_sales_invoice_pdf(data)` and `finance.service.sales_invoice_print_data(...)`;
  - `Document(category=REPORT, entity_type="sales_invoice")`;
  - route `GET /finance/sales-invoices/{id}/pdf` returning 202.
  - The PDF needs company bank details, which `Company` does not have, plus amount in words and a font that can draw ₹.
- **GST:** switch `GstRepository.output_rows_stmt` to read posted invoice lines (HSN code as stored on the line, by `invoice_date`), and add a separate figure for dispatched-but-not-invoiced goods.

### Credit note (the finance side of a customer return)

- **Tables:** `credit_notes` and `credit_note_lines`, with `DocTypes.CREDIT_NOTE` ("CN").
  - Header: `sales_invoice_id` (required), `customer_id`, `reason_type` (RETURN, PRICE, DISCOUNT, OTHER), an optional link to the future customer-return document.
  - Lines: `sales_invoice_line_id`, with the quantity or value limited to what was invoiced minus what has already been credited. The tax split is taken from the invoice line, which also fixes the original `is_interstate`.
- **Posting:** `post_journal(`
  - Dr `SALES_RETURNS` (new system account; INCOME type used as a deduction from income) for the taxable value
  - Dr `OUTPUT_TAX` for the tax
  - Cr AR (party CUSTOMER) for the total
  - plus any round-off

  `)`, then `apply_outstanding_delta(-total)`, and audit it.
- **Settlement:** if the invoice is later allocated, reduce its open amount.
- **Stock:** goods coming back are posted by the return document via `post_movements(TransactionType.CUSTOMER_RETURN, ...)` (into QC_HOLD or DAMAGED pending inspection). The credit note itself does not move stock.
- **GST summary:** deduct posted credit-note lines from output tax by credit-note date.
- **Existing route:** keep `POST /finance/customer-adjustments` for non-GST corrections such as interest or write-offs, and give it a screen. This follows assumption §32.

### Job-work invoice (extend vendor invoices rather than add a new table)

- **Schema:**
  - add `vendor_invoices.job_work_order_id` (nullable FK, RESTRICT) and `vendor_invoice_lines.job_work_receipt_line_id` (nullable FK, RESTRICT);
  - add a CHECK that a line names either a purchase-order line or a job-work receipt line, not both;
  - add `job_work_receipt_lines.invoiced_charges`, or a running total on the receipt.
- **Match:** add a job-work branch to `evaluate_match`.
  - Expected amount = `receipt_line.charges`.
  - Price is compared with `rate_per_unit` according to `rate_basis`.
  - Quantity is compared with `output_quantity` (good pieces only).
  - Billing beyond the charges built up is refused.
  - This removes the "no purchase order" override for genuine job-work bills.
- **Posting:** in `_post_invoice_journal`, debit `SystemAccount.JOB_WORK_EXPENSE` (new, EXPENSE) when `job_work_order_id` is set. Input GST and AP stay as they are; the SAC code (e.g. 9988) goes in `hsn_code`, and the rate comes from a new `job_work_orders.tax_rate` or is entered on the line.
- **Excess loss:** post `vendor_debit_amount` as Dr AP (vendor) / Cr JOB_WORK_EXPENSE, via a vendor debit adjustment written the same way as `post_customer_adjustment`. This also gives `purchase_returns.debit_note_number` a real posting to point at.
- **Interface:** use `job_work_charges_for_period` / `GET /job-work/charges` to suggest bill lines on the vendor-invoice form. Permissions stay `vendor_invoice.create` and `vendor_invoice.approve`.

### Gap 10, once invoices exist
- **Dealer performance report:** add `ReportKey.DEALER_PERFORMANCE`, with the CHECK migration, modelled on `VENDOR_PERFORMANCE`. Per dealer it would show invoiced value, on-time delivery from the dispatch delivery dates, outstanding, overdue, average days to pay, and credit notes.
- **Payment follow-up:**
  - a beat task `notifications.scan_overdue_receivables` modelled on `scan_delays`, sending `NotificationType.PAYMENT_DUE` with a per-company `group_key` and an "overdue notified at" column on `sales_invoices`;
  - optionally a `receivable_follow_ups` table (`TenantEntity`, audited) holding customer, invoice, contact, outcome, promised date and next action date.
