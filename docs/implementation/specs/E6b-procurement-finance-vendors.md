# WP E6b: Purchase returns with debit notes, QC rejection → return/replacement, lead time, transport and service vendors, RFQ dispatch, rating refresh

Lane B, round 6, step 2 (after E6a). Placeholder `§E6b-n`.

## Checklist IDs to close

- P07.M8, P13.M2, P13.M3 (purchase-return debit note; return-aware match)
- K09
- P07.M13 (lead time)
- P07.V5 (transport vendor)
- P07.V6 (service vendor)
- P07.M2 (RFQ sent to vendors)
- A10.F4 (rating refresh)
- P02.I8 (`can_return` on purchase returns)
- The input-tax part of P13.M9

Read each ID in `trace/trace.json`.

## Decisions (binding)

### 1. Vendor debit note on purchase return (P07.M8, P13.M2, P13.M3)

**Posting.** When a purchase return is posted, finance issues a **vendor debit note** through a finance seam:
- New document `vendor_debit_notes` + lines, with its own `DocTypes.DEBIT_NOTE` series (prefix "DN", 16 chars).
- Journal:
  - Dr ACCOUNTS_PAYABLE (party vendor)
  - Cr PURCHASE_EXPENSE (or the original invoice line's expense account / cost centre where known)
  - Cr INPUT_TAX for the returned taxable value plus GST
  - `reference_type="purchase_return"`
- Linked to the original vendor-invoice line where one exists.
- Allocatable against open vendor invoices: payments allocation gains a `debit_note_id` target, or the debit note **reduces the invoice's payable balance** like A3's credit-note settlement. Choose one, document it, and keep it consistent with A3.

**Cancel.** Cancelling the purchase return reverses everything.

**Match.** `evaluate_match` uses accepted − returned quantity.

**GST summary.** Input tax nets off the debit notes.

**PDF.** A debit note PDF built from A2's renderer.

### 2. QC rejection starts the return / replacement (K09)

**Draft return.** On a REJECTED (or partly rejected) incoming decision for a vendor's goods, the E4 owner handler for `goods_receipt` creates a **DRAFT purchase return** in the same transaction. It has:
- lines from the GRN line;
- `stock_state` DAMAGED;
- quantity = rejected;
- the reason from the inspection;
- a link to the inspection.

**Resolution type.** Add `purchase_returns.resolution_type` = `CREDIT_NOTE | REPLACEMENT`, set before posting:
- REPLACEMENT re-opens the PO line's pending quantity, so the replacement is expected and received against the same PO.
- CREDIT_NOTE issues the debit note from item 1.

**Clearing the flag.** `return_required` clears when the return is posted.

**Screens.** The quality and GRN screens show the open return status and a link.

### 3. Lead time (P07.M13)

- **Report `VENDOR_LEAD_TIME`.** Per vendor and item:
  - sample size;
  - master lead time and quoted lead time;
  - actual average, median and p90 days (GRN receipt date − PO `sent_at` date, else `po_date`);
  - on-time %.
- **Display.** Show the actual average on the vendor rating dialog and on item-vendor links.
- **Adopt action.** An audited "adopt measured lead time" action on links (`item.update`).

### 4. Transport vendor (P07.V5)

- `transporters.vendor_id`: nullable FK, tenant-validated. The vendor must hold `vendor_type` TRANSPORT. Add a vendor picker in the transporter dialog.
- Freight vendor-invoice lines may reference a dispatch id or an inbound shipment id. Add the columns and CHECK exactly-one-or-none with the PO/JW references.
- **Report `FREIGHT_RECONCILIATION`:** freight incurred (dispatch and shipment freight) vs billed, per transporter.
- **Demo data:** one TRANSPORT vendor linked to the demo courier.

### 5. Service vendor (P07.V6)

- **Item flag** `items.is_stock_item`, default true. SERVICE items default to false. Add it to the item form.
- **GRN.** Refuses non-stock lines with a clear message.
- **Service acceptance.** New document `service_acceptances` + lines: a PO line for a non-stock item is confirmed by quantity or amount, with no inventory movement and no QC. It is linked to the PO and drives PO receipt progress.
- **Vendor invoice match.** Matched against the acceptance. B3a's `invoice_type` gains `SERVICE`.
- **Rights.** New `service_acceptance.create`, with a grant migration.
- **UI.** A "Confirm service" action on the PO detail for non-stock lines.

### 6. RFQ to vendors (P07.M2)

- `RFQ` document-only PDF and render (C1a/A2 helpers).
- On send, email each named vendor with the PDF attached, using E6a's mail attachments and outbox.
- Audit each recipient.
- The RFQ detail shows "Sent to N vendors".

### 7. Vendor rating refresh (A10.F4)

- **Nightly beat task** `maintenance.recompute_vendor_ratings`: per company, batched over active vendors, calling `recompute_rating` as the system actor, with audit.
- **New weighted inputs** (weights are named constants, `§E6b-n`):
  - % of POs acknowledged within 24 h;
  - committed-date adherence.

### 8. `can_return` on purchase returns (P02.I8)

**Decision:** enforce the flag on purchase returns, **except** QC-rejected stock (DAMAGED pool from a failed inspection), which can always go back to the vendor. Record this as `§E6b-n`.

## Tests

Integration:
- debit note posting and reversal
- the return-aware match refuses an invoice for returned goods
- GST net of debit notes
- QC reject → DRAFT return; replacement re-opens the PO line and receives again
- lead time report
- freight reconciliation
- service acceptance → match → invoice
- GRN refuses a non-stock line
- RFQ emails (fake transport)
- the rating sweep

Plus PDFs and frontend bodies.

## Docs

- Handbook: Procurement, Finance and Quality chapters; scenario K09.
- `database.md`
- assumptions
