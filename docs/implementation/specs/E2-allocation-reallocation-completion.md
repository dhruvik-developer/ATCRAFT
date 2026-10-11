# WP E2: Allocation and reallocation, end to end

Lane D, round 3, step 2 (after B3b). Placeholder `§E2-n`.

## Checklist IDs to close

- S01.2 and S03.8 (reallocation approval in the UI)
- K05
- P03.F7 and P03.F8
- P03.S4 (allocated quantity)
- P05.R5 (service/spare), P05.R6 (SOFT)
- P05.P6 (cross-warehouse donors and transfers on the allocation side)
- P05.S4 through P05.S9
- P05.D1 through P05.D7
- P02.I6 (`can_be_spare` and the spare issue)

## Background

- Read every listed ID in `trace/trace.json`.
- `trace/notes.md` G3 (all) and G10 #2–#3.
- `reports/returns-inventory-qc.md` §6 (allocation).
- `docs/assumptions.md` §10.
- On main: E1a's `allocate_order`, top-up sweep and dispatch rules; E4's reservation priority/date for assembly.

## Decisions (binding)

### 1. Reallocation approval UI (K05, S01.2, S03.8, P05.S6)

**Tab.** Add a "Reallocation requests" tab on `/inventory/reservations`:
- server-paginated, with a status filter (PENDING first);
- a detail view showing the donor and recipient demands with links, quantity, priority, required dates, and the ranked alternatives/impact;
- **Approve** and **Reject** buttons, each with a note, gated on `P.ALLOCATION_APPROVE`. Hide them for the requester and show the separate-approver refusal verbatim.

**Links.** Point notifications and `entityLink('reallocation_request')` at this tab with the request preselected. B4 (round 5) wires `open_approval` for these too.

### 2. Recipient linking and demand sync (P05.S7, P05.S8, P03.F7, K05)

**Recipient link.** Add `to_demand_line_id` to reallocation requests (schema + model + migration). The recipient reservation carries `demand_line_id`.

**Owner port.** Add an allocation → owner port, the same registry pattern as E4's quality owners. Call it whenever a reservation is created by reallocation, released (manual, expiry, reallocation donor) or reduced. The owning module then updates its pointers:
- **sales:** `reservation_id`, `allocated_quantity`, line status, and the order transitions (CONFIRMED ↔ ALLOCATED via E1a's edges);
- **production:** order-level reservation pointers;
- **assembly** and **job work:** their reservation pointers.

Use one handler per demand type.

**Idempotency.** Every document release path must be idempotent for closed reservations; E1a did sales, now do job work, assembly and production.

**Required end-to-end test:** a production reservation is reallocated to an urgent SO, then the SO dispatches.

### 3. Request from shortfalls (P05.S5)

Add "Request reallocation" on:
- the SO allocation panel's short lines (prefilled with item, warehouse, shortfall, priority, required date and `demand_line_id`);
- production component shortages.

This works even when the urgent demand holds **no** reservation.

### 4. Priorities (P05.D1, P05.D3, P05.D4, P05.D5, P05.S3)

- **One scale.** Map everything to 1–9 (1 = most urgent):
  - production, assembly, job-work and manual reservations stop defaulting to 100, and use 5 unless set;
  - write a migration that rescales existing rows ≥ 10 to 5;
  - document it in `§E2-n`.
- **Urgency.** `sales_orders.is_urgent` maps to priority 1 at reservation time.
- **Customer priority.** Add `customers.priority` (1–9, default 5) as the default SO priority. The reallocation score gains a customer-priority weight.
- **Propagation.** SO priority and required-date edits after confirmation update the order's open reservations.
- **Work order status as a driver.** Donors whose production work order is IN_PROGRESS, or whose material has been issued, are refused as donors (penalised to excluded) (`§E2-n`).

### 5. Allocated quantity (P03.S4, P05.S4, P05.D6)

Add an allocation `commit(reservation_id, quantity, reference)` primitive that moves part of a reservation into `allocated_quantity`, on both the balance and the reservation row, preserving the CHECKs. Call it when:
- assembly components are issued;
- packing seals a carton for an SO line;
- production material is kitted (if a kitting step exists; otherwise skip it).

`consume` draws allocated before reserved. Committed quantity is never a reallocation donor. The `/inventory` "Allocated" column then shows real values.

### 6. SOFT reservations and policy (P05.R6, P05.D7, §10)

**Policy settings.** Add a sparse per-company table `allocation_policy` with code defaults, plus an admin tab on `/admin/alerts` or its own tab. Settings:

| Setting | Default |
|---|---|
| `auto_approve_soft_donors` | true (matches §10) |
| `approval_threshold_quantity` | none |
| `approval_threshold_value` | none |
| `protection_window_days` | 2 (donors needed within N days are protected) |
| weights: priority, required date, customer priority | — |
| `auto_propose` | true |

Routes: `GET/PUT /allocation/policy` (`settings.view` / `settings.manage`).

**SOFT behaviour.**
- A request whose donor is SOFT executes immediately when `auto_approve_soft_donors` is on; it is still audited and still notifies.
- SOFT reservations are created by DRAFT production orders (planned demand). Hard reservations happen at release.
- Decide per module and document it in `§E2-n`.

### 7. Auto-reallocation proposals (P05.D2)

- **On demand:** on SO confirm or update with a shortfall, and on is_urgent or priority raise, run `suggest_reallocation` for each short line and create **PENDING** requests automatically, deduplicated per line.
- **Periodic:** a Celery beat sweep, `maintenance.propose_reallocations`, does the same for short open demand by priority, with bounds.
- Both respect the policy and are idempotent.

### 8. Cross-warehouse (P05.P6)

- Reallocation donors may come from other warehouses. An execution across warehouses also raises a DRAFT stock transfer from the donor warehouse to the demand warehouse, linked to the request.
- The SO allocation panel shows "Available in other warehouses" from one grouped query.
- A **"Raise transfer"** action pre-fills `POST /inventory/transfers` with the source reference.
- MRP TRANSFER suggestions are E3b. Keep a seam here.

### 9. Spare / service issue (P05.R5, P02.I6)

**Document:** `spare_requests` plus lines.
- Header: customer (optional) or internal; service ticket reference; required date; priority.
- Lines: `can_be_spare` items only; enforce the flag in `allocation.reserve` for `DemandType.SPARE`.
- Status flow: DRAFT → RESERVED (reserve SPARE) → ISSUED (consume via allocation, posting `MATERIAL_ISSUE`, or DISPATCH when shipped to a customer) / CANCELLED (release).
- New rights `spare_request.view` and `spare_request.manage`, with a grant migration.

**Screens:** a new page `/inventory/spares`, added to the sweeps and the handbook.

### 10. Notify affected departments (P05.S9)

- When a reallocation is executed, notify the donor demand's owners (DemandType → roles/rights, plus the customer's assigned sales user for SO donors).
- Notify the requester of the approval or rejection.
- Remove the B4 overlap: B4 notifies approvers when a request is raised. This package notifies about the decision and the donor impact.

## Tests

**Unit:**
- policy scoring, including customer priority, protection window and work-order exclusion;
- the SOFT auto-approval rule;
- the priority mapping.

**Integration:**
- reallocate production → urgent SO → dispatch, end to end;
- manual release updates the SO line and status;
- expiry does the same;
- idempotent closed releases;
- commit/consume order;
- cross-warehouse execution raises a transfer;
- the auto-proposal sweep deduplicates;
- spare request lifecycle with the `can_be_spare` refusal;
- the priority rescale migration.

**Frontend:** the requests tab (gating and bodies), request-from-shortfall, the spares page, the policy tab.

## Docs

- `database.md` §4.5 contract: new commit primitive, owner port, `to_demand_line_id`.
- `assumptions.md`: rewrite §10 to match the code exactly.
- Handbook: Reservations, Spares, Policy.
