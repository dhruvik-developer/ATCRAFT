# WP E1a: Sales-order fulfilment fixes (dispatch linkage, partial dispatch, allocation top-up)

Lane A, round 2, step 1. It runs **before** A2: A2's invoice lines rely on dispatch lines carrying `sales_order_line_id`. Assumption placeholder prefix: `§E1-n`.

**Checklist IDs to close:**
- K01: direct finished-goods sale
- K08: partial dispatch
- The SO-line part of P12.M6
- The "allocation top-up" part of K02, so the order ships once the balance is made

**Background:** read these IDs in `trace/trace.json`, `trace/notes.md` (G7 #1 and G10 #1–#2), and `reports/returns-inventory-qc.md` §4 (dispatch internals).

## Decisions (binding)

### 1. Dispatch lines must name their sales-order line (critical defect)

**Server side:**
- When a SALES dispatch names `sales_order_id`, **every** line must carry a `sales_order_line_id` belonging to that order.
- Each line's item must equal the order line's item.
- Each line's quantity must not exceed what is dispatchable for that line: `min(live reservation remaining, ordered − dispatched − already on other open dispatches)`. Use the exact rule from section 2.
- Otherwise refuse with a `ValidationError` that names the line.
- Lines for items not on the order are refused.
- A SALES dispatch **without** `sales_order_id` keeps today's free-stock behaviour (direct sale).

**Frontend (`CreateDispatchDialog`):**
- When a sales order is picked, load `GET /sales/{id}/allocation-status` and pre-fill one line per open order line, carrying `sales_order_line_id`, item, and quantity = dispatchable.
- The user may lower a quantity or remove a line, but never add foreign items.
- Fix the dialog copy that promised reservation consumption without delivering it.
- Add a **"Dispatch"** action on the SO detail page (`P.DISPATCH_CREATE`) that opens the pre-filled dialog.
- Add a vitest asserting the POST body carries `sales_order_line_id`.

**Regression test:** an API test with a UI-shaped payload proves the reservation is consumed, `dispatched_quantity` moves, and the order status advances correctly. This is the test that was missing.

### 2. Partial dispatch of a partly reserved order (K08)

- Add the SO machine edge **CONFIRMED → PARTIALLY_DISPATCHED** (permission `dispatch.create`, label "Dispatch").
- `assert_order_dispatchable` already derives from the machine. Update the unit test that pins the gate (`tests/unit/test_dispatch_lifecycle.py`) **deliberately**.
- A line of a CONFIRMED order may dispatch only up to its **live reservation** remaining. Shipping unreserved stock against a sales-order line is not allowed (`§E1-1`, conservative: the reservation is the promise).
- Lines with zero reservation are simply not dispatchable yet.

**Required test (the proposal's own case):** order 500, 200 reserved; dispatch 200; the order is PARTIALLY_DISPATCHED; the balance of 300 is still open and is reserved once stock arrives (section 3).

### 3. Allocation top-up

**Service function.** Add `sales.service.allocate_order(session, tenant, order_id, *, reason)`:
- Lock the order header, then its lines in id order.
- Take per-item advisory locks, exactly as `confirm_order` does.
- Reserve newly free stock at the order warehouse for each short line, by line order.
- Update `reservation_id` and `allocated_quantity` the same way confirm does (reuse the confirm code path; refactor rather than duplicate).
- Move CONFIRMED → ALLOCATED when every non-cancelled line is fully covered.
- Write an audit row.

**Endpoint and button.** `POST /sales/{order_id}/allocate` (`sales_order.update`) with button **"Allocate available stock"** on the SO allocation panel, shown when the order is CONFIRMED or PARTIALLY_DISPATCHED with a shortfall.

**Automatic top-up.** Celery beat task `maintenance.allocate_short_orders`:
- Runs every `SO_TOPUP_INTERVAL_SECONDS` (900; a setting, also in `.env.example`).
- For each company, take confirmed short orders in priority order, then by required date, then by order date. Bound the work with `MAX_TOPUP_ORDERS` (200).
- Call `allocate_order` per order, each in its own transaction, with failures isolated.
- Write an audit row only when something changed.

This makes production/GRN/transfer arrivals flow to waiting orders without coupling those modules to sales (`§E1-2`).

**Inverse fix.** When `_update_confirmed` raises a line's quantity past what is reserved, move ALLOCATED → CONFIRMED (new edge, permission `sales_order.update`). An ALLOCATED order must never be under-reserved.

### 4. Reservations already released

If a line's `reservation_id` points at a RELEASED, EXPIRED or CONSUMED reservation, every path in this package must treat it as "no live reservation":
- top-up re-reserves it;
- dispatch refuses with a clear message.

Never call `allocation.release` on a closed reservation. Make the sales release paths idempotent for closed reservations (P03.F7, sales side). Allocation-side sync for manual release and reallocation is WP E2; leave a seam.

## Frontend

- **SO detail:** Dispatch action, the Allocate button, and a dispatch list with links. A2 adds invoices.
- **Allocation panel:** show "Reserved / Dispatchable now / Short".
- Copy goes through `lib/messages.ts`. Add tests.

## Docs

- `database.md` §7: the SO machine edges.
- `assumptions.md` (`§E1-n`): the dispatch cap, top-up cadence, and inverse edge.
- Handbook: Sales order and Dispatch chapters.
- `api.md`.

## Tests

**Unit:** lifecycle, including the new edges; the dispatchable-quantity rule.

**Integration:**
- K01 through UI-shaped payloads;
- K08 500/200;
- top-up after a GRN;
- the sweep is bounded and isolates failures;
- the inverse edge on a quantity increase;
- a closed reservation is treated as none.

**Frontend:** dispatch dialog body; SO detail actions.
