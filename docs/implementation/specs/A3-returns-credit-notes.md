# WP A3 — Customer goods return (sales return) + GST credit note (+ customer adjustment screen)
Lane A (commercial), round 3. Placeholder `§A3-n`.

Proposal: "Goods return management", QC type "Return QC", Return warehouse/stock, Finance "Customer ledger".
Audit: "A customer goods-return document. Purchase returns exist. A dealer return is not its own order."
Background: `reports/returns-inventory-qc.md` (ALL — follow its "Recommendation: the Sales Return document"),
`reports/sales-finance.md` (Gap 4 credit side, credit-note recommendation, Traps). WP A2's sales invoice is on main.

## Part 1 — Sales return document (new module `sales_returns`, prefix `/sales-returns`, tag "Sales Returns")
Follow the report's recommendation (tables, state machine, posting, QC hookup, serial disposition, validations) with
these decisions:
1. **Tables** exactly as recommended (`sales_returns`, `sales_return_lines`, `dispatch_lines.returned_quantity` with
   CHECKs), plus on the header: `source String(16)` (`ERP` | `DEALER_PORTAL`, CHECK, default ERP — the dealer portal in
   WP C3/C4 will create DRAFT returns with source DEALER_PORTAL) and `requested_by_user_id` (FK users SET NULL).
   Lines also get `credited_quantity` (Quantity, CHECK ≤ quantity) for credit-note tracking.
2. **Statuses** `DRAFT → RECEIVED → CLOSED`, `DRAFT → CANCELLED` (cancel = reject a request; needs a reason).
   Machine `SALES_RETURN_LIFECYCLE` (entity "Sales return"). Every status word has/gets a badge.
3. **Receive** posts `CUSTOMER_RETURN` movements exactly as the report's "Posting (receive)" says: into the chosen
   return warehouse (default: the company's active RETURN-type warehouse; serial lines default to the dispatch's
   warehouse because serials cannot transfer — §25), bin-less grain when QC applies, batch from the DISPATCH ledger rows,
   **unit cost = the original dispatch cost** (blended per line, or the serial's own row), `stock_state=QC_HOLD` when
   `requires_qc` (default true) else DAMAGED or RETURN as chosen per line; `reference_type="sales_return"`; then
   `dispatch_line.returned_quantity += qty` under row locks; enforce `items.can_return` (first enforcement of that flag).
4. **QC**: non-serial lines → `quality.create_in_process_inspection(..., reference_type="sales_return", vendor_id=None,
   batch_id=...)` (stage resolves to RETURN); verdict read back with `inspections_for_reference` (pull model).
   Serial lines → return-owned `POST /sales-returns/{id}/units {accepted:[serial_ids], damaged:[...], scrapped:[...]}`
   posting per-unit `change_stock_state` (QC_HOLD→AVAILABLE makes the unit IN_STOCK again; →DAMAGED stays RETURNED;
   →SCRAP). Close (`RECEIVED → CLOSED`) refused while any hold remains (inspections not decided / units undisposed).
   Also fix the latent defect the report found **only if you can prove it with a test**: GRN can post QC_HOLD at a bin
   while the QC decision releases at the bin-less grain — if reproducible, make the decision release at the grain the
   hold was posted at (carry `location_id` on the inspection), with a regression test; otherwise document it.
5. **Validations** per the report (dispatch is SALES and has left; lines belong to it; qty ≤ dispatched − returned under
   lock; serial subset and status DISPATCHED; non-serial lines carry no serials; warehouse active and in scope; blocked
   customers may still return goods — ASSUMPTION). Vendor/dealer principals cannot reach these staff routes (gate).
6. **Routes**: list (filters status, customer, dispatch, source, date range; search), create (201), get, PATCH (DRAFT
   only: lines/notes/warehouse), `/receive`, `/units`, `/close`, `/cancel` (reason), `/credit-note` (see Part 2).
   Permissions **new** `sales_return.view`, `sales_return.create` (receive/close/units/cancel also `sales_return.create`;
   keep it simple) — group "Sales"; presets `_SALES` view+create, `_DISPATCH` view+create, `_STORE` view+create,
   `_QC` view; grant migration.
7. Reports: decide whether fast/slow moving should net `CUSTOMER_RETURN` (report §6.11) — implement netting in
   `CONSUMPTION_TYPES` logic if straightforward and test it; else document.

## Part 2 — Credit note (finance)
1. Tables `credit_notes`, `credit_note_lines` (finance; models in `finance/models.py`, service/repository in new files
   like A2 did — register with the meta-tests). `DocTypes.CREDIT_NOTE` prefix `"CN"`, padding 5 → `CN/2026-27/00042`
   (16 chars). Like invoices: **preview, then issue** (numbered at issue, no drafts); statuses `ISSUED`, `CANCELLED`
   (machine "Credit note"; cancel within `CREDIT_NOTE_CANCEL_WINDOW_HOURS`=24 and only if nothing depends on it).
2. A credit note is always **against one sales invoice** (`sales_invoice_id` NOT NULL). Header: `reason_type`
   (`RETURN | PRICE_ADJUSTMENT | DISCOUNT | OTHER`, CHECK, reporting-only), `sales_return_id` NULL, `customer_id`,
   `note_date`, snapshots like the invoice (seller/buyer/place of supply/`is_interstate` copied **from the invoice**),
   totals + round-off (same rounding rule as invoices), `issued_at/by`, cancellation evidence, `pdf_document_id`.
   Lines: `sales_invoice_line_id` NOT NULL, `item_id`, `hsn_code` (from the invoice line), `quantity` (NULL for pure
   value adjustments), `taxable_amount`, `tax_rate`, split, `line_total`. **Limits** (under row locks): per invoice
   line, credited quantity ≤ invoiced quantity and credited taxable ≤ invoiced taxable (track `credited_quantity` and
   `credited_amount` on `sales_invoice_lines`); tax split always mirrors the invoice line's treatment.
3. **Posting** at issue: Dr `SALES_RETURNS` (new SystemAccount, INCOME contra-revenue; preferred code 4600 via A2's
   free-code picker) taxable, Dr OUTPUT_TAX tax, Cr AR total (party customer), round-off; `reference_type="credit_note"`;
   `apply_outstanding_delta(-total)`; invoice `credited_amount += total`; audit.
4. **Invoice settlement**: settlement = paid_amount + credited_amount. Status follows settlement: 0 → ISSUED;
   0 < s < total → PARTIALLY_PAID; s ≥ total → PAID if paid_amount > 0 else **CREDITED** (new invoice status; add edges
   to `SALES_INVOICE_LIFECYCLE` + badge + tests). Over-settlement (refund due) stays visible as a credit balance in the
   customer ledger (ASSUMPTION). A2's invoice-cancel rule "no credit notes" now becomes real.
5. **From a sales return**: `POST /sales-returns/{id}/credit-note` (permission `credit_note.create`) builds a credit note
   for the not-yet-credited returned quantities against the dispatch's live invoice lines (matched via
   `dispatch_line_id`), reason RETURN, links `sales_return_id`, updates `sales_return_lines.credited_quantity`; refused
   if the dispatch has no live invoice (then nothing was billed — say so: the uninvoiced dispatch's invoiceable quantity
   already excludes returned quantity per A2) and refused when nothing is left to credit. Manual credit notes from the
   invoice: `POST /finance/credit-notes` with lines (quantity for goods, value for price adjustments).
6. **GST summary**: subtract issued credit-note lines by `note_date` (HSN from the line) — output tax net.
7. **PDF**: reuse A2's renderer/pipeline with title "CREDIT NOTE", "Against invoice <no> dated <date>", reason.
   `ReportKey.CREDIT_NOTE` (document-only, CHECK-swap listing ALL keys), task `reports.generate_credit_note`.
8. Permissions **new** `credit_note.view`, `credit_note.create`, `credit_note.cancel` (group "Finance"); presets
   `_ACCOUNTS` all three; `_SALES` view; grant migration.

## Part 3 — Customer adjustment screen
`POST /finance/customer-adjustments` (CHARGE/CREDIT, `payment.approve`) exists without a screen. Add a "Customer
adjustment" dialog on the customer ledger panel (finance) gated on `P.PAYMENT_APPROVE`, with account picker (non-AR,
active), amount, date, reason; invalidate ledger/ageing keys. Copy explains: "For GST credit for goods or price, issue a
credit note instead."

## Frontend
- New page `/sales-returns` (Sales nav; gated `P.SALES_RETURN_VIEW`): list; "New return" dialog (pick a SALES dispatch
  that has left — server-searched picker with a `left_only` filter added to dispatch listing — then lines with
  returnable qty, reason code, per-line pool, serial picker for serial lines); detail page/drawer with lines, QC status
  per line (from inspections), unit disposition dialog for serial lines, Receive/Close/Cancel/Credit-note actions shown
  only on states the machine allows.
- Dispatch page: "Raise return" action on SALES dispatches that have left (`P.SALES_RETURN_CREATE`).
- New page `/finance/credit-notes` (Finance nav; `P.CREDIT_NOTE_VIEW`): list, detail with PDF download, cancel;
  "New credit note" from an invoice (on the invoice detail: "Issue credit note" → choose lines + qty or value → preview →
  issue).
- Invoice detail shows credit notes and credited amount; CREDITED badge.
- Customer adjustment dialog (Part 3).
- Sweeps/handbook for both new pages; badges; messages.

## Demo data
One received-and-QC'd demo return (non-serial) with a credit note against its invoice; one DRAFT return; one manual
price-adjustment credit note. Idempotent by code/identity.

## Docs
database.md (§2 new module + finance tables; §7 machines; §8 events: return received → CUSTOMER_RETURN + QC; credit
note issued → journal), assumptions (`§A3-n`: dispatch-cost valuation, receiving warehouse/pool defaults, serial
disposition owned by the return, credit value from invoice lines, settlement/CREDITED, blocked customers may return,
dispatched_quantity never decremented — replacements need a new SO line), api.md, architecture.md (new module),
handbook (Sales returns, Credit notes, customer adjustment).

## Tests (minimum)
Per the report's "Tests to mirror" list (unit + integration incl. FIFO round-trip value neutrality, batch, serial,
QC stage RETURN, can_return refusal, list lists, tenant isolation, audit order) plus: credit-note limits per line
(qty and value), journal balanced, outstanding decreases, invoice settlement statuses incl. CREDITED, GST net of credit
notes, credit note from return credits only uncredited qty and only once, cancel window, PDF renders, permissions
(view/create/cancel), frontend page/dialog tests (bodies, gating, state-dependent actions).
