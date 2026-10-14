# WP B3a: Job-work invoice, and a fix for the vendor-invoice cancel reversal defect

Lane B (operations), round 3, step 1. Placeholder prefix `§B3-n`.

**Proposal.** Phase 10, Job Work Management: "Job work invoice", "Job work vendor ledger". Finance: "Job work cost".
**Audit.** "A job-work invoice of its own. Job-work cost is tracked; the vendor's bill is the vendor invoice."
**Background.** Read `reports/sales-finance.md` §6, §7, §12, Traps 5 and 10–12, and "Job-work invoice (extend vendor invoices rather than add a new table)". Follow it with the decisions below.

Already on main:
- WP B1 cost centres, including `job_work_orders.cost_centre_id`, `vendor_invoice_lines.cost_centre_id`, and per-line cost-centre expense splitting in `_post_invoice_journal`.
- WP A2's free-code picker in `ensure_system_accounts`.
- WP C1a's vendor portal, whose invoice submission requires a PO. A job-work invoice is staff-entered in this package; see decision 8.

## Decisions (binding)

### 1. A typed vendor invoice

A job-work invoice is a vendor invoice of a new type. It shares payments, allocations, approval, disputes, the payables ledger and ageing.

- Add `vendor_invoices.invoice_type String(16)`: `PURCHASE | JOB_WORK`, NOT NULL, server default `'PURCHASE'`, with a CHECK.
- Add `vendor_invoices.job_work_order_id`: FK RESTRICT, NULL. A CHECK requires it exactly when `invoice_type = 'JOB_WORK'`.
- Add `vendor_invoice_lines.job_work_receipt_line_id`: FK RESTRICT, NULL.
- A CHECK on lines says a line names at most one of `purchase_order_line_id` and `job_work_receipt_line_id`.
- On a JOB_WORK invoice, every goods line must name a receipt line. Free service or freight lines are allowed only if a named constant permits them; the default is to refuse them (`§B3-1`).

### 2. Billing tracking

- Add `job_work_receipt_lines.invoiced_amount` (Money, default 0) with `CHECK invoiced_amount <= charges`.
- Add `job_work_receipt_lines.debit_posted_amount` (Money, default 0) for excess-loss recoveries.
- Both are written **only** when an invoice is approved or cancelled, under row locks taken in id order.

### 3. Job-work order tax facts

Add to `job_work_orders`:
- `tax_rate` (Percentage NULL; the default comes from the named constant `JOB_WORK_DEFAULT_GST_RATE`, documented as an ASSUMPTION the client must confirm, because rates vary);
- `sac_code` String(16) NULL (default "9988", the SAC for manufacturing services on others' goods).

The job-work order form gains both fields.

### 4. Match: a job-work branch in `evaluate_match`, kept pure

- **Expected line amount** follows `rate_basis`. For PER_PIECE and PER_KG it is `receipt_line.charges − invoiced_amount` (the unbilled part). For LOT the charge applies once per order: reuse `receipt_charge` semantics.
- **Price** is compared with `rate_per_unit` using the PO price tolerance constant.
- **Quantity** is compared with the receipt line's good `output_quantity`.
- **Billing beyond the unbilled charges is refused.** That is a hard block, not a variance.
- An invoice that matches has no "not linked to a purchase order" issue, so no override is needed.

### 5. Approval posting (`_post_invoice_journal` branch)

- Debit the new `SystemAccount.JOB_WORK_EXPENSE` (EXPENSE; preferred code 5150, via the free-code picker) instead of PURCHASE_EXPENSE.
- Split the debit per line cost centre. A line's cost centre defaults to the job-work order's `cost_centre_id`.
- Input GST and AP are unchanged.
- In the same transaction, add `invoiced_amount` to each receipt line.

### 6. Excess-loss recovery, posted at the same approval

For the receipt lines being invoiced, take `vendor_debit_amount − debit_posted_amount`. When it is greater than 0, post a second journal:

| Account | Side | Party |
|---|---|---|
| AP | Dr | the vendor |
| JOB_WORK_EXPENSE | Cr | none (same cost centre) |

Use `reference_type="job_work_recovery"`. Then add to `debit_posted_amount`.

The vendor's net payable is the invoice minus the recoveries (`§B3-2`). Cancelling or reversing the invoice reverses both journals and rolls back both amounts. Follow the row forward and test it.

### 7. Fix the cancel defect (sales-finance trap 5)

Today the path APPROVED → DISPUTED → CANCELLED cancels an invoice whose journal is posted without reversing it.

- Fix `cancel_invoice` so that any cancel of an invoice with posted journals calls `reverse_journal` for each one (invoice and recovery), and undoes the job-work tracking.
- Add regression tests for both PURCHASE and JOB_WORK invoices.
- Also check that disputes leave the PO and receipt state coherent.

### 8. Endpoints and service

- `GET /job-work/orders/{order_id}/billable-lines` (`vendor_invoice.create` or `job_work.view`; use `require_any_permission` written literally). It returns unbilled receipt lines: receipt code and date, output quantity, charges, invoiced amount, unbilled amount, vendor debit pending, rate, rate basis, tax rate, SAC.
- `POST /finance/vendor-invoices` accepts `invoice_type=JOB_WORK` with `job_work_order_id` and lines carrying `job_work_receipt_line_id`. Validation:
  - the order belongs to the tenant and to the same vendor, and is not DRAFT or CANCELLED;
  - each receipt line belongs to that order, and is not over-billed (re-checked under lock at approval).
- Matching, submission, approval and payment use the existing routes and permissions (`vendor_invoice.*`). There are **no new permissions**.
- Vendor portal: vendor-submitted invoices stay PO-only in this package. Record vendor-submitted job-work bills as a follow-up in assumptions; do not build it.

### 9. Frontend

- **Job-work page.** Add a new "Invoices" tab listing vendor invoices with `invoice_type=JOB_WORK` (add the `invoice_type` filter to the vendor-invoice list endpoint), showing the job-work order code.
- **"Record job-work invoice" dialog.** Steps: pick the vendor, then pick an issued job-work order of that vendor, then show billable lines with checkboxes and amounts (prefilled from unbilled charges, editable). Then enter the vendor invoice number, date and tax, see the live totals, and submit, which creates and submits the invoice.
- **Vendor invoices page.** Add a Type column and filter. Show the job-work match results in the match dialog, including the JW rows.
- **Job-work order form and detail.** Add `tax_rate` and `sac_code`. The detail shows billed against unbilled charges.

### 10. Demo data

Replace the demo "plating" bills that today need an override with proper JOB_WORK invoices linked to their receipts, idempotent by vendor and invoice number. Keep one receipt unbilled so the dialog has something to show.

### 11. Docs

- `database.md`: §2 columns, §8 "job-work invoice approved" event.
- `assumptions.md`: `§B3-n`, covering the typed invoice, the recovery timing, the SAC and rate defaults, and the vendor-portal follow-up.
- `api.md`.
- The handbook's Job work and Vendor invoices chapters.

## Tests

**Unit**
- `evaluate_match` job-work branch: within tolerance; over price; over-billing refused; quantity against good output; LOT charged once.
- Journal lines: expense split by cost centre; the recovery journal.

**Integration**
- Create, submit and approve a JOB_WORK invoice. It posts JOB_WORK_EXPENSE, moves `invoiced_amount`, and posts the recovery and its amount.
- A second invoice cannot over-bill.
- Cancelling an approved invoice reverses both journals and rolls back the amounts.
- The defect regression: dispute then cancel reverses the journal.
- A foreign or other-vendor order is refused.
- `billable-lines` lists the unbilled lines.
- The list filter `invoice_type` works.

**Frontend**
- The invoices tab, and the dialog request body.
- The vendor invoices page Type column and filter.
