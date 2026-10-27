# WP D1: Duplicate PO control with real actions, and a PO amendment flow

Lane B (operations), round 5, step 1. Assumption placeholder prefix: `§D1-n`.

**Proposal.** Add-on §03 "Duplicate Purchase Order Control":
- Rules: same vendor; same item / category; same requirement date; same sales order / MRP reference; same pending purchase request; same BOM demand; same warehouse requirement.
- Prompt: *"This purchase requirement already exists in PO-001. Merge, increase quantity, or create a separate PO with approval?"*
- Options: merge with existing PO; increase existing PO quantity; create new PO with approval; cancel duplicate request.

**Priority list.** "Should have: Duplicate PO prevention, **Merge PO option**".

**Previous baseline, superseded below.** Detection existed, but merge,
increase and cancel were advisory only. The implemented checkpoint now includes
the action API, approval confirmation, revision history and staff/vendor UI.

**Background.** `reports/procurement-vendor-portal.md` E6 (rule-by-rule table) and T9–T11. C0's conversion and C1a's vendor flow are on main. D4 (digital signature) comes after you in this lane; leave a seam for "content changed, so supersede the signature".

## Decisions (binding)

### 1. Complete the detection rules

**Same item / category.** Add a **category-level** signal: same vendor, same procurement category (C0's rule), overlapping date window, open PO, but a *different* item. Classify it `POSSIBLE`, with reason "same category".

**Same SO / MRP reference and same BOM demand.**
- Persist demand provenance on requisition lines: `purchase_requisition_lines.source_demand_refs` (JSONB array of `{demand_type, demand_id, demand_line_id}`, max 50).
- `create_requisition_from_mrp` must keep `request.source_demands` instead of dropping them.
- Carry the refs onto PO lines through `requisition_line_id`.
- Any overlap of demand refs with an open PO line is `EXACT` ("same sales order / BOM demand"). Index it with GIN.
- The JSONB shape is validated by a schema.

Keep the existing rules: same vendor and warehouse filters, ±7 days, same source.

### 2. Real actions: `POST /procurement/purchase-orders/duplicate-resolution`

Body: `{action, candidate_purchase_order_id?, candidate_line_id?, draft (the PO the user was creating, or requisition conversion context), reason?}`.

| `action` | Permission | Behaviour |
|---|---|---|
| `MERGE` | `purchase_order.update` | Adds the incoming lines to the candidate PO. A line for an item already on the PO increases that line; other items become new lines. Recompute totals. Link `requisition_line_id`s through `_link_source_documents`. Allowed directly when the candidate is DRAFT or PENDING_APPROVAL. When it is APPROVED, SENT or ACKNOWLEDGED with **nothing received**, it goes through the **amendment** flow (§3). Refused once any receipt exists: "PO-… has receipts; raise a new PO". |
| `INCREASE` | `purchase_order.update` | Same as MERGE but limited to the matched line (`candidate_line_id`): its quantity grows by the incoming quantity. Same status rules. |
| `CREATE_WITH_APPROVAL` | `purchase_order.create` | Creates the new PO flagged `duplicate_acknowledged` with the reason, **even when the creator lacks `po.approve`**. The acknowledgement is **confirmed at approval**: the approve endpoint requires `confirm_duplicate: true` plus a reason for any flagged PO, and stores `duplicate_confirmed_by_id` / `at`. This is the "with approval" the proposal means (`§D1-1`). Keep the old path (creator holds `po.approve`) working. |
| `CANCEL_REQUEST` | `purchase_requisition.create` | When the incoming demand came from requisition lines, those lines move to CANCELLED with the reason (line cancel through the requisition rules; the requisition status recomputes). For a manual PO draft, nothing is persisted: answer success "Duplicate request discarded". |

The whole action runs in one transaction, under the same `advisory_xact_lock("po-duplicate", vendor, warehouse)` as create. The duplicate check is re-run under the lock.

### 3. PO amendment flow (`§D1-2`)

- New PO columns: `amendment_number` (int, default 0) and `last_amended_at`.
- New machine edges, label "Amend", permission `purchase_order.update`: APPROVED → PENDING_APPROVAL, SENT → PENDING_APPROVAL, ACKNOWLEDGED → PENDING_APPROVAL. Each is allowed only when nothing has been received. Update `test_procurement_lifecycle.py` deliberately.
- `POST /procurement/purchase-orders/{id}/amend` takes `{reason, lines: [{line_id?, item_id?, quantity, unit_price?, ...}]}`. It is also used internally by MERGE and INCREASE. It does the following:
  - applies the changes;
  - increments `amendment_number`;
  - clears `acknowledged_at` and `vendor_committed_date`, because the vendor must re-accept the amended PO;
  - keeps `sent_at` history in audit;
  - moves the PO to PENDING_APPROVAL through the machine;
  - writes an audit diff of old and new lines;
  - notifies the assigned vendor user "PO-… amended (rev N)" (vendor link `/vendor/orders/{id}`) and the approvers;
  - calls a `on_purchase_order_content_changed(po)` seam (no-op today; D4 hooks signature supersession into it).
- After re-approval the buyer re-sends it as usual. MRP nets the PO only in nettable statuses, so the amended PO drops out of supply until re-approved. That is correct; document it.

### 4. Frontend

- **Duplicate dialog** (`purchase-order-duplicate-dialog.tsx` and the conversion dialog): each candidate card gets real buttons, "Merge into PO-…" and "Increase PO-… line qty" (when `candidate_line_id`). Below the cards: "Create separate PO (needs approval)" with a reason, and "Cancel this request" with a reason (shown when the demand came from a requisition).
  - The buttons must mirror the server's rules. For example, merge into a SENT PO is labelled "Amend PO-… (vendor re-accepts)".
  - Gate each button on its exact permission.
  - Each action shows a specific success message with the resulting PO code.
- **PO detail**:
  - an "Amend" action for APPROVED, SENT and ACKNOWLEDGED with no receipts (`P.PURCHASE_ORDER_UPDATE`); the dialog edits quantities, adds lines and takes a reason;
  - an amendment badge "Rev N";
  - an amendment history panel built from audit diffs.
- **Approve dialog**: for duplicate-flagged POs, show the duplicate context and require the confirm checkbox plus a reason.
- **Vendor app**: "Amended (rev N)" badge on the PO; the accept flow is unchanged.

### 5. Docs

- `database.md`: new columns and the §7 machine edges.
- `assumptions.md` (`§D1-n`): what merge does, the amendment rules, approval-time confirmation, the MRP effect.
- Update §16.
- `api.md`; handbook (duplicate PO control, amendments).

## Implementation checkpoint - 27 October 2026

The planning foundation is implemented: canonical sales/manual roots survive
multi-level explosion, independently of capped display contributions; purchase
conversion persists them in the existing requisition JSONB/GIN structure.
Incomplete or over-bound provenance refuses conversion atomically, while old
plans remain readable. Shared component quantities are labelled as aggregate
in the fulfilment trail. Revision `20261102_0100` indexes the MRP containment
lookup. No second `source_demand_refs` column or index is needed.

Real duplicate actions and amendments are now implemented through
`purchase_order_changes.py` and the staff/vendor screens. A physical PO line
can serve several PR lines through the durable tenant allocation mapping.
Revision `20261103_0100` backfills scalar links without changing counters and
refuses inconsistent history; `20261104_0100` gives composite foreign keys
explicit names within PostgreSQL's identifier bound. Replacement, amendment,
cancellation and rejection maintain allocations and derived counters.

Duplicate discovery batches item/vendor category facts, canonical PR roots and
active allocation links, retains physical quantity grain, reports current
versions/any-GRN flags and refuses overflow. Unknown dates widen the search.
Shared roots imply EXACT only for the same SKU. New demand triples resolve
real tenant-owned documents and validate their line ownership.

Changes require current versions, preserve surviving line IDs and agreed tax
rates, and serialize with receipt creation. Even draft/cancelled GRNs block
amendments. Reductions below linked quantity need explicit allocations. Both
the original creator and amendment author retain separation of duties, and
flagged duplicates require an approval-time checkbox and reason. Vendor
revisions remain status-only until approved and resent. The D4 content-change
seam remains a no-op while the signer decision is pending.

The normal seed now creates pooled demand and an issued revision with two
explicit retained allocations, plus a separate duplicate waiting for approval.
Existing markers are registered without advancing or rewriting them.

## Tests

**Unit**
- category POSSIBLE signal;
- demand-ref EXACT match;
- the lifecycle with amend edges;
- merge line arithmetic: same item increases, new item appends;
- button predicates mirror the server rules (frontend).

**Integration**
- MERGE into DRAFT and into PENDING_APPROVAL;
- MERGE into SENT amends, so status becomes PENDING_APPROVAL, acknowledgement is cleared and the vendor is notified;
- refused once receipts exist;
- INCREASE;
- CREATE_WITH_APPROVAL by a buyer without `po.approve`; approval requires the confirmation;
- CANCEL_REQUEST cancels the requisition lines;
- MRP-sourced requisition keeps `source_demand_refs`, and its duplicate is detected EXACT;
- the duplicate check re-runs under the lock.

**Frontend**
- the dialog actions' request bodies;
- the amend dialog body;
- the approve dialog confirmation.
