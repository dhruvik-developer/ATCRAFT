# WP A2: Sales invoice (GST tax invoice), its PDF, and receivables by invoice

Lane A (commercial), round 2. Assumption placeholders use the prefix `§A2-n`.

**What the proposal promised.** "Invoice PDF generation", plus Finance: "Sales ledger, Customer ledger, Outstanding report, GST report, Payment collection".

**What the audit found.** "A sales invoice document and its PDF" is missing: customer money is posted as a receivable journal, with no GST invoice series.

**Background reading.** `reports/sales-finance.md` in full, especially §1–§14, the Traps section, and "Recommendation / Sales invoice". `reports/anatomy.md` A13 covers the PDF pipeline. The `Company` bank fields and `invoice_terms` were added in round 1 (WP B1) and are on `main`.

## Decisions (binding)

### 1. Placement and naming

- The finance module owns the invoice: tables, models in `finance/models.py`, routes in `finance/router.py`, path prefix `/finance/sales-invoices`.
- Put the bulk of the service and repository code in **new files inside the finance package**, for example `finance/sales_invoice_service.py` and `finance/sales_invoice_repository.py`. This keeps merge conflicts down: other lanes also edit `finance/service.py`.
- If you do, register the new files with every meta-test that scans module files by name: state-machine names (`_EXTRA_MODULES`), repository entity labels, entity names. A new machine must not escape `test_state_machine_entity_names.py`.
- Name the schemas `SalesInvoiceSummary`, `SalesInvoiceDetail`, and so on. Check `openapi.json` for collisions (anatomy trap 9).

### 2. One invoice per SALES dispatch

- An invoice can be raised from a dispatch whose type is SALES and whose status is READY_FOR_DISPATCH or later (not CANCELLED).
- Each line comes from one dispatch line. The quantity is `dispatch_line.quantity` minus any already-returned quantity; WP A3 adds `returned_quantity` later, so read it defensively once it exists.
- Enforce at most one live invoice per dispatch with a partial unique index on `sales_invoices(company_id, dispatch_id) WHERE status <> 'CANCELLED'`.
- `cancel_dispatch` must refuse while a live invoice exists. Add a test for it.
- Invoice date defaults to the dispatch date and may be overridden, but never before the dispatch's sales-order date and never in a closed financial year.
- Record why: the GST tax invoice is due at or before removal of goods (CGST Act s.31). This is assumption `§A2-1`.

### 3. No drafts: preview, then issue

- `GET /finance/sales-invoices/preview?dispatch_id=` (sales_invoice.view) computes the full invoice and persists nothing.
- `POST /finance/sales-invoices` with `{dispatch_id, invoice_date?, notes?, transport fields?}` creates **and issues** the invoice in one transaction.
- The number is allocated at issue, so the series has no gaps (`§A2-2`). Initial status is `ISSUED`.
- Status set: `ISSUED`, `PARTIALLY_PAID`, `PAID`, `CANCELLED`.
- Machine `SALES_INVOICE_LIFECYCLE` (entity "Sales invoice"):
  - money edges: ISSUED→PARTIALLY_PAID, ISSUED→PAID, PARTIALLY_PAID→PAID, PARTIALLY_PAID→ISSUED, PAID→PARTIALLY_PAID, PAID→ISSUED. The last three cover cancelled or re-allocated receipts.
  - ISSUED→CANCELLED, which is irreversible.
- Status follows the money, through one helper, as `invoice_status_after_payment` does for vendor invoices.

### 4. Number

- Change `DocTypes.SALES_INVOICE` to prefix `"SI"`, padding 5. That gives `SI/2026-27/00042`, 16 characters, which is the GST Rule 46 limit.
- First assert that no `document_sequences` row exists for `sales_invoice`.
- Update the frontend fixture that shows `SINV/` if it matters.
- Record this in assumptions §13 and in `§A2-3`.

### 5. Cancellation

- `POST /finance/sales-invoices/{id}/cancel {reason}` requires **new** permission `sales_invoice.cancel`.
- It is allowed only when all of these hold:
  - the status is ISSUED;
  - there are no payment allocations;
  - there are no credit notes against the invoice (A3 adds credit notes; until then, check whatever exists);
  - `now - issued_at <= INVOICE_CANCEL_WINDOW_HOURS` (24, a named constant mirroring the e-invoice cancellation window; `§A2-4`).
- Outside those conditions the answer is "issue a credit note". The credit note arrives in WP A3; until then, return a clear `BusinessRuleError` saying so.
- Cancelling does all of the following, in one transaction:
  - `reverse_journal`, dated today if the original period is closed;
  - `apply_outstanding_delta(-total)`;
  - `invoiced_quantity -= qty` on the sales-order lines;
  - if the order is INVOICED, take a new edge **INVOICED→DISPATCHED** (permission `sales_invoice.cancel`, label "Invoice cancelled"), added to `SALES_ORDER_LIFECYCLE` along with its tests;
  - audit.
- **Do not copy the vendor-invoice defect** (sales-finance trap 5): every cancel path must reverse its journal.

### 6. Money and tax

- Price each line with **`sales.service.compute_line_amounts`**, not the finance helper.
- Inputs:
  - the sales-order line's `unit_price`, `tax_rate` and discount;
  - `is_interstate` and `place_of_supply_state_code` from the order;
  - for a partial quantity, the flat `discount_amount` apportioned pro rata, with the **last** invoice of the line taking the remainder so the totals reconcile.
- Copy `hsn_code` onto the line from the item. Never read today's item master later.
- Header totals:
  - `subtotal` (gross), `discount_amount`, `taxable_amount`, `cgst_amount`, `sgst_amount`, `igst_amount`, `tax_amount`, `round_off_amount`, `total_amount`, `paid_amount`, `currency` (from the order);
  - round the total to the nearest rupee, HALF_UP (`ROUND_INVOICE_TOTAL_TO = Decimal("1")`, `§A2-5`);
  - CHECKs that mirror the vendor invoice: total = taxable + tax + round_off; paid ≤ total; the tax split is consistent; due ≥ invoice date.
- `due_date = invoice_date + customers.credit_days`.

### 7. Snapshots on the header

Copy these at issue time so a later master edit never rewrites an issued invoice:
- seller: legal name; GSTIN (the branch GSTIN when the order's branch has one, otherwise the company's); address; state code and name;
- buyer: legal name; GSTIN (the billing address GSTIN, otherwise the customer GSTIN); billing address text; shipping address text;
- place of supply, `is_interstate`;
- transport: transporter or courier name, vehicle, LR number and e-way bill number, from the dispatch where present.

Also store `issued_at` and `issued_by_id`, and the cancellation evidence. Header and lines get the usual CHECKs. Lines carry `dispatch_line_id` and `sales_order_line_id`.

### 8. Posting at issue (one transaction)

1. Lock, in this order: the customer (`get_customer_for_order(lock=True)`), the dispatch header, the sales-order lines (id order).
2. Re-check under the locks: the dispatched quantity and that no live invoice exists.
3. `ensure_system_accounts`, with new `SystemAccount.SALES_REVENUE` (INCOME).
4. Post the journal:

   | Account | Side | Amount | Party |
   |---|---|---|---|
   | AR | Dr | total | customer |
   | SALES_REVENUE | Cr | taxable | |
   | OUTPUT_TAX | Cr | tax | |
   | ROUND_OFF | Dr or Cr | round-off | |

   Use `reference_type="sales_invoice"`, `reference_id` and `reference_code`, and `entry_date` = the invoice date.
5. `apply_outstanding_delta(+total)`.
6. `invoiced_quantity += qty`, refusing to exceed `dispatched_quantity`.
7. Move the order DISPATCHED→INVOICED once every non-cancelled line is fully invoiced (`record_status_change`).
8. Audit.

**System-account codes.** `ensure_system_accounts` must not crash when a **non-system** account already holds a preferred code: the demo has 4000, 4100 and 4200, and clients may hold others. Implement "take the first free code from the preferred code upward" (preferred `4500` for SALES_REVENUE) and test it with a pre-existing account. WPs A3 (SALES_RETURNS) and B3 (JOB_WORK_EXPENSE) will reuse this mechanism.

### 9. Receipts allocated to invoices

- Make `PaymentAllocation` polymorphic:
  - nullable `sales_invoice_id`, and `vendor_invoice_id` becomes nullable;
  - an exactly-one CHECK;
  - partial unique indexes on `(payment_id, vendor_invoice_id)` and `(payment_id, sales_invoice_id)`.
  - Migrate carefully: existing rows all have a vendor invoice.
- `allocate_payment` accepts customer RECEIPT payments against the **same customer's** ISSUED or PARTIALLY_PAID invoices, in the same currency.
  - Lock order: payment first, then invoices in id order.
  - Both limits are re-checked after locking.
  - Invoice `paid_amount` and status follow the money.
- Payment cancel and de-allocation paths must roll invoices back the same way. Follow the row forward and test it.
- Remove the refusal "A customer receipt has no invoice to allocate against".
- An optional auto-allocation oldest-first helper may be offered as a UI button. It must call the same service.

### 10. Ageing by due date

- `_open_receivable_items` / `receivable_ageing_stmt` age each AR debit from `sales_invoices.due_date` when `reference_type='sales_invoice'`, falling back to `entry_date` for legacy journals. As a result `not_due` becomes meaningful.
- Keep the per-(party, currency) FIFO netting.
- Update assumptions §27 and §28 and the route description.

### 11. GST summary

Output tax comes from **issued, non-cancelled invoice lines**: HSN code from the line, grouped by `invoice_date`. Keep the old dispatch-based query only as a separate "dispatched, not yet invoiced" figure in the response, and rewrite the misleading texts in the router description and the GST tab. WP A3 will subtract credit notes.

### 12. Sales order close

Add `POST /sales/{order_id}/close` (`sales_order.update`) for INVOICED→CLOSED, and a "Close" action on the SO detail page shown only when INVOICED.

### 13. PDF

- Pipeline:
  - `ReportKey.SALES_INVOICE`, document-only, with a CHECK-swap migration listing **all** keys;
  - task `reports.generate_sales_invoice` on the `reports` queue;
  - pure `render_sales_invoice_pdf(data) -> bytes`;
  - data assembly `sales_invoice_print_data(...)`;
  - `Document(category=REPORT, entity_type="sales_invoice")`; set `dispatches.invoice_document_id` to the generated document. Statutory records must never expire.
- Route: `POST /finance/sales-invoices/{id}/pdf` returns 202 `AcceptedJobResponse` (sales_invoice.view). Also queue it automatically right after issue, after commit.
- Layout:
  - A4, "TAX INVOICE", plus "Original for Recipient".
  - Seller block: name, address, GSTIN, state with code. Invoice number and date. Buyer (bill-to) and ship-to blocks with GSTIN and state code. Place of supply.
  - Line table: Sr, description, HSN, quantity, UoM, rate, discount, taxable value, CGST / SGST or IGST with rate and amount, total.
  - **HSN-wise tax summary table.**
  - Totals with round-off, then **amount in words** in Indian numbering (Rupees … crore … lakh … thousand … hundred … and … paise only).
  - Bank details and terms from the company record, transport details, and the signature block "For <Company> / Authorised Signatory".
  - Write money as `INR` with Indian digit grouping (12,34,567.89). The built-in fonts cannot draw ₹.
- Add pure helpers for amount-in-words and grouping, with thorough unit tests (0, paise only, 1 crore, 99,99,99,999.99, …).
- **Dev Celery queue defect.** In base and dev compose the worker passes no `-Q`, so `reports.*` jobs (including the existing packing list) never run outside production. Fix both:
  - `docker-compose.yml` and `docker-compose.dev.yml`: `celery-worker` gets `-Q default,mrp,reports`;
  - the Makefile and `make.ps1` `dev-worker` targets too;
  - check that `scripts/hooks/check_compose.py` / `test_compose*` still pass.

### 14. Permissions

- Reuse `sales_invoice.view` and `sales_invoice.create`.
- New: `sales_invoice.cancel`. Grant migration, and add it to `_ACCOUNTS`.
- The routes:

  | Route | Permission |
  |---|---|
  | list / detail / preview / pdf | view |
  | issue | create |
  | cancel | cancel |
  | allocation of receipts | the existing `payment.record` rule |

### 15. Frontend

- **New page `/finance/sales-invoices`**, in the Finance nav, gated on `P.SALES_INVOICE_VIEW`:
  - list with filters for status, customer, date range, `overdue_only`, and search on number or customer;
  - columns: number, date, customer, total, paid, balance, due date, overdue days, status;
  - a detail drawer or page showing seller and buyer, lines, tax summary, payments allocated, PDF download (job polling via `useReportJob`, then `api.download`), and Cancel when allowed;
  - the new route goes in all four sweeps plus the handbook.
- **Dispatch page:** a "Raise invoice" action on SALES dispatches with no live invoice (`can(P.SALES_INVOICE_CREATE)`), opening a preview dialog with an optional invoice date, then Confirm.
- **SO detail:** an Invoices panel listing that order's invoices, the Close action, and the "Invoiced" column, which is now real.
- **Payments page:** a customer receipt can be allocated to invoices; reuse or extend the allocation dialog.
- **Ageing tab:** not-due is now shown meaningfully.
- **GST tab:** new copy, plus the "dispatched not invoiced" figure.
- Add badges for any new statuses, and the messages in `lib/messages.ts`.

### 16. Demo data

- Fill the demo company's bank details and `invoice_terms` (B1 added the columns but left them empty), so the demo invoice PDF prints a complete footer. Use obviously fictional values (a made-up bank name, account number and a syntactically valid IFSC), set only where currently null.
- Issue real invoices for the demo SALES dispatches that are dispatched or delivered, through the service, idempotent by dispatch.
- Allocate one demo receipt to an invoice.
- Leave the old hand-posted `customer_charge` journals in place as history, and say so in the demo docstring.

### 17. Docs

- `database.md`: §2 finance tables; §7 sales-invoice machine plus the new SO edge; §8 rows for "Invoice issued" and "Invoice cancelled".
- `assumptions.md`: rewrite §32; update §9, §13, §27 and §28; add `§A2-n` for each decision above.
- `api.md`; the handbook (the Finance chapter gains Sales invoices and Dispatch gains Raise invoice; remove the "has no screen" claims that are now false).
- `architecture.md` if module notes change.

## Tests (minimum)

**Unit**
- amount in words;
- Indian digit grouping;
- discount apportionment, with the last share taking the remainder;
- journal lines balance, including round-off on either side;
- status after payment;
- the `SALES_INVOICE_LIFECYCLE` `validate()` check, and `SALES_ORDER_LIFECYCLE` with the new edge;
- the PDF renders: bytes start with `%PDF` and the build has no exception, as in `test_packing_list_render.py`;
- the free-code picker for system accounts.

**Integration**
- Issuing posts a balanced journal and raises outstanding, `invoiced_qty` and the SO to INVOICED.
- One live invoice per dispatch.
- Cancelling within the window reverses everything, and the SO goes back to DISPATCHED.
- Cancel is refused after an allocation and outside the window.
- Dispatch cancel is refused while an invoice is live.
- Receipt allocation moves `paid_amount` and status; payment cancel rolls them back.
- Ageing uses the due date.
- The GST summary is built from invoices.
- A partial-dispatch invoice reconciles discounts over two dispatches.
- Listing returns the invoice you created.
- Cross-tenant ids are refused.

**API**
- Permissions: the view, create and cancel routes each answer 403 without their right.
- The PDF job answers 202, and the job completes in the existing job test harness (`test_report_jobs_for_documents.py`).

**Frontend**
- The list page, including gating.
- The raise-invoice dialog body.
- Cancel shown only within the conditions.
- Receipt allocation body.
