# WP E1b: Sales order automation and trace

Lane D, round 4, step 2 (after E3a). Placeholder `§E1b-n`.

**Covers:** auto-plan on confirm, the fulfilment trail, and other-warehouse availability with transfer.

**Checklist IDs:**
- S01.1
- S03.13
- K02 (production plan for the shortfall)
- K10 (sales-order side; MRP TRANSFER is E3b)
- the planning part of K08 ("plans the balance 300")

Read each in `trace/trace.json`.

**Already on main:** E1a (top-up, partial dispatch) and E2 (owner port, cross-warehouse transfer from reallocation, "Raise transfer" seam).

## Decisions (binding)

### 1. Auto-plan on confirm (K02, K08, S01.1)

Company setting `AUTO_PLAN_ON_CONFIRM`: per-company row with a code default, **on**. Expose it in the same settings area E2/B4 created, or a "Sales settings" tab.

When a confirmed order has a shortfall (after reservation, and after E2's auto-reallocation proposals), do the following in the **same transaction**:

- **MAKE item, or MAKE_OR_BUY with a default approved BOM:** create a **DRAFT production order**.
  - `source_type=SALES_ORDER`; `source_id` = the order; add a `source_line_id` column if missing.
  - Quantity = shortfall net of open supply already pegged to that line (do not duplicate on re-confirm or edit).
- **BUY item:** create a **DRAFT purchase requisition line**, one requisition per order, with `source_demand_refs` (D1) pointing at the order line.
- **Item that needs job work:** create nothing. List it in the response as "needs job work".

Rules:
- Everything is idempotent per order line.
- An order quantity decrease or cancellation cancels the still-DRAFT planned documents.
- Notify the planner roles ("Production shortage" via B4's event).
- Record `§E1b-1`: DRAFT only, never auto-released, never a PO.

Add a **"Plan shortfall"** button on the allocation panel. It does the same on demand, and also works when the setting is off.

### 2. Scheduled MRP

The beat task `mrp.scheduled_run` is in E3b; leave it there. Do not duplicate it here.

### 3. Fulfilment trail (S03.13, S01.1)

- **Endpoint:** `GET /sales/{order_id}/fulfilment` (`sales_order.view`). One batched query per document family, through each module's read seam; no N+1. Return, per order line:
  - reservations (and allocated quantity)
  - MRP suggestions referencing the line
  - planned and actual production, assembly and job-work orders sourced from the order (status, quantities, dates)
  - requisitions and POs pegged to the line (via `source_demand_refs`)
  - QC inspections on those documents
  - packing orders and cartons
  - dispatches with the latest tracking
  - invoices and credit notes
  - returns
- **SO detail:** render it as a "Fulfilment" timeline panel, with links to every document.
- **Back-links:** production, assembly and job-work detail pages show a link back to the sales order.
- **Filters:** add `sales_order_id` URL filters to `/dispatch` and `/packing`.

### 4. Other warehouses (K10)

- `GET /sales/{id}/allocation-status` gains `available_elsewhere` per short line: free stock by other warehouse, from one grouped query.
- The panel shows it, with **"Raise transfer"** (E2's seam). The transfer carries a source reference to the order line. When it is received, E1a's top-up reserves the stock for the order.
- Optional: take a SOFT reservation at the source warehouse while the transfer is in flight (`§E1b-n`).

## Tests

**Integration:**
- K02 500/120: confirm creates a DRAFT production order for 380, pegged to the line; idempotent on re-confirm; cancelled when the order is cancelled
- a BUY item creates a requisition with refs
- the fulfilment trail returns each family
- the K10 transfer path ends with the order allocated after transfer receipt
- the settings switch

**Frontend:** fulfilment panel; Plan shortfall body; Raise transfer body.

## Docs

- `database.md` §8 (the confirm event now plans)
- assumptions
- handbook: the Sales order chapter, the scenarios K02/K08/K10 explained
