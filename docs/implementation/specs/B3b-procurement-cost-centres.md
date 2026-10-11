# WP B3b: Cost centres on requisitions, purchase orders and their invoices

Lane B (operations), round 3, step 2. Placeholder `§B3-n`, continuing from B3a's numbers.

This completes the cost-centre carriers deferred from WP B1. During round 1, lane C owned procurement, so these fields were held back.

**Already on main:**
- `cost_centres`
- the `require_cost_centre` / `active_cost_centre_ids` seam
- `departments.cost_centre_id`
- `vendor_invoice_lines.cost_centre_id`
- per-line expense splitting
- WP C0's requisition → PO conversion
- WP C1a's vendor portal

**Background:** `reports/masters-pricing.md` "Conclusion B" item 4.2, and `specs/B1-cost-centres.md` for the rules already in force.

## Decisions (binding)

### 1. Columns

Each is a nullable FK with RESTRICT, indexed, and validated for tenant and active on write.

- `purchase_requisitions.cost_centre_id`
- `purchase_order_lines.cost_centre_id`
- `purchase_orders.cost_centre_id`. This is **derived**: the single distinct line cost centre, otherwise NULL. Like `item_category_id`, it is never accepted from the client for the header.

### 2. Defaults

Precedence, recorded as `§B3-n`:
1. explicit value
2. source document
3. requester's department default
4. NULL

How each document applies it:
- **Requisition header:** explicit value, otherwise `departments.cost_centre_id` of the requesting user's department, otherwise NULL.
- **PO line:** explicit value, otherwise the requisition header's cost centre through `requisition_line_id`, otherwise NULL.
- **C0's conversion:** copies the requisition header cost centre onto each PO line. It must **not** split POs by cost centre; grouping stays (vendor, procurement category, warehouse).
- **MRP-created requisitions:** NULL, unless the planner's department has a default.
- **Vendor invoice line linked to a PO line:** defaults to that PO line's cost centre, otherwise the header cost centre from B1.
- **Purchase returns:** credit the same cost centre if a credit posting exists. Check this; if purchase returns post nothing to finance, document that.

### 3. Validation

- Everything goes through the organization seam, and a foreign id answers 404.
  - The seam lives in `src.modules.organization.service`.
  - Use `require_cost_centres(session, tenant, [(field, id), ...])` to batch-validate a document's lines. A foreign id answers 404; an inactive centre answers 422 with `details.fields` naming the field (`lines.N.cost_centre_id` for a line).
  - Use `require_cost_centre` for a single header value.
- Batch-validate line ids, one query per document.
- Leave B1's `post_journal` INCOME/EXPENSE rule untouched.
- **Add every new carrier to `COST_CENTRE_REFERENCE_PROBES`** in `organization/repository.py`: `purchase_requisitions`, `purchase_order_lines` and `purchase_orders`. Without this, a centre that a requisition names could be archived. `tests/unit/test_organization_cost_centres.py::TestReferenceProbes::test_every_table_that_carries_a_cost_centre_is_probed` fails until the probes are added.

### 4. API

- `RequisitionCreate`/`Update`, `PurchaseOrderCreate`/`Update` lines, the conversion overrides (optional `cost_centre_id` per line), and the PO and requisition reads all gain `cost_centre_id`.
- Add `cost_centre_id` filters on the requisition and PO lists.

### 5. Frontend

- Requisition form: a cost-centre select, defaulting to the user's department default shown as a hint.
- PO form: a per-line cost centre, with a "same for all lines" quick-set.
- PO detail and requisition detail display the cost centre.
- The conversion dialog shows each line's cost centre.
- The vendor invoice form defaults line cost centres from PO lines. The hook is `lineFromOrderLine` in `features/finance/vendor-invoice-form-dialog.tsx`: today it starts every copied line as "As the invoice".
- Reuse B1's components: `CostCentreSelect`, `CostCentreName` and `useCostCentreOptions`. Follow the same pattern:
  - render the picker and the label only with `cost_centre.view`;
  - fill the form from the saved record;
  - "none" is the `SELECT_NONE` sentinel, sent as `null`.
- Requisitions, purchase orders and their detail pages gain no other new right.

### 6. Demo data

Tag the demo requisitions and POs with production and maintenance cost centres, so the cost-centre statement shows purchase expense by centre after the demo invoices are approved.

## Tests

**Integration:**
- defaults precedence for requisitions
- PO lines inherit through conversion
- derived header
- foreign id refused
- a vendor invoice line defaults from the PO line, and approval splits expense per cost centre
- the cost-centre statement shows the purchase expense

**Frontend:** the requisition and PO form bodies include `cost_centre_id`.
