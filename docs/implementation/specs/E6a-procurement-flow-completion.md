# WP E6a: Procurement flow completion

Lane B, round 6, step 1. Assumption placeholder prefix: `§E6-n`.

**Checklist IDs to close:**
- P07.M1 (requisition detail)
- P07.M3 and A04.2 (quotation → PO)
- P07.M5 (draft edit, close/short-close, duplicate re-check on PATCH)
- P07.M6 (GRN detail and cancel)
- P07.M9 (dispute/resolve/cancel UI)
- A04.13 and A04.14 (PO INVOICED and PAID)
- A04.5 (the email part of vendor notification, for vendors without a portal login: G5 note #6)

Read each ID in `trace/trace.json`.

**Already on main:** C0, C1a, D1, E4 (QC write-back) and B3a (cancel reversal fix).

## Decisions (binding)

### 1. PO billing status (A04.13, A04.14)

- Add `procurement.service.sync_order_billing_status(session, tenant, order_id)`. Finance is downstream of procurement, so finance may call it.
- Call it from vendor-invoice approve and cancel, and from payment allocate, de-allocate and cancel.
- Transitions:
  - GRN_COMPLETED → INVOICED once approved, non-cancelled invoices cover the accepted quantity, net of returns (see E6b).
  - INVOICED → PAID once every such invoice is PAID.
  - Reverse edges INVOICED → GRN_COMPLETED and PAID → INVOICED for cancellations and reversed allocations.
- Update the lifecycle tests deliberately.
- Add a backfill for existing orders through the idempotent maintenance reconciler pattern from E4.

### 2. Requisition detail (P07.M1)

- New page `/procurement/requisitions/:requisitionId` showing:
  - lines with quantity, ordered quantity, suggested vendor, required date and source;
  - linked POs;
  - history.
- Embed the line table in the approve/reject dialog, so approvers see what they approve.
- Add the page to the sweeps and route-loaders.

### 3. Quotation → PO (P07.M3, A04.2)

**Endpoint:** `POST /procurement/quotations/{id}/convert-to-order`, with `purchase_order.create` + `forbid_vendor_users`. It builds a DRAFT PO through the shared create body:
- vendor and currency come from the quotation;
- warehouse comes from the RFQ;
- lines carry `quotation_line_id`, price, discount and required date, plus `requisition_line_id` followed from rfq_line to requisition_line;
- source is VENDOR_QUOTATION;
- the duplicate check runs;
- `converted_purchase_order_id` is set.

**UI:** a "Raise purchase order" action on SELECTED quotations and in the decision dialog.

**Conversion preference:** C0's conversion prefers a SELECTED quotation's vendor and price for lines covered by an RFQ (`§E6-n`).

### 4. PO editing and closing (P07.M5)

- The PO form dialog gains an **edit mode** for DRAFT and PENDING_APPROVAL (PATCH, `P.PURCHASE_ORDER_UPDATE`). It also exposes the header fields the conversion dialog lacked: delivery address, terms, notes, expected delivery and priority.
- `update_purchase_order` re-runs the duplicate check under the same advisory lock, with the acknowledge rule.
- **Close / Short-close** command (`P.PURCHASE_ORDER_CANCEL`, matching the route) on PARTIALLY_RECEIVED, GRN_COMPLETED and PAID, each with a reason.

### 5. GRN detail and cancel (P07.M6)

- New page `/procurement/goods-receipts/:receiptId` showing lines, batch and bin, accepted and rejected quantities, QC status with inspection links, and any returns.
- "Cancel draft" row action gated on `P.GRN_CREATE`.

### 6. Vendor invoice actions (P07.M9)

- Add Dispute (reason), Resolve dispute and Cancel (reason) actions to the vendor-invoice row and the match dialog, gated on `P.VENDOR_INVOICE_APPROVE`.
- Show each action only on statuses the machine allows.
- After B3a, cancel reverses the journals. Verify that with a test through the UI flow.

### 7. Vendor email (A04.5)

**On Send:**
- If the vendor has **no active portal users**, or the company setting `EMAIL_PO_ON_SEND` is on (default on), queue an email to the vendor master's email or primary contact.
- The email carries the PO PDF attached (C1a's renderer) and a short body.
- This needs attachment support in `core/mail.py` `OutboundEmail` (MIME multipart; keep the confidentiality rules).

**Delivery:**
- Use the durable outbox pattern: an after-commit queue plus a recovery sweep, as C1b does.
- Audit each recipient.
- Show "Emailed to <address>" on the PO.

**Respect:**
- vendors without an email produce an explicit warning in the Send response;
- email off when `EMAIL_PROVIDER=disabled`.

### 8. Small carry-overs from round 1

- `purchase_requisitions.requested_by_id` is still accepted without a tenant check (T3 class). Validate it, or derive it from the principal, on create and update.
- Converted POs take the buyer's `branch_id` instead of the requisition's. Make conversion use the requisition's branch when set; this is the more faithful choice. Record it in `§E6-n`.

## Tests

**Integration:**
- billing sync on approve, cancel, allocate and reverse;
- quotation → PO links the source documents and the requisition `ordered_quantity`;
- duplicate re-check on PATCH;
- close and short-close;
- GRN cancel;
- dispute → cancel reverses;
- send emails the PDF (fake mail transport) for a vendor without portal users;
- backfill.

**Frontend:**
- requisition detail;
- edit dialog request body;
- quotation conversion action;
- GRN detail;
- invoice actions gating.

## Docs

- Handbook: the Procurement chapter.
- `database.md` §7 (PO machine).
- Assumptions.
