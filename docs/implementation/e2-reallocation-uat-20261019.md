# E2 reallocation — restricted-role UAT

Date: 2026-10-19
Scope: reallocation requests raised from a sales-order shortage and from an MRP
shortage, the approval queue, decision notifications and the restricted-role
matrix.

This is the restricted-role UAT record for the E2 remainder. Automated coverage
pins the permission boundary; the manual steps below are what a person with each
role should see and be able to do in the running UI.

## Rights

| Right                   | What it allows                                                    |
| ----------------------- | ----------------------------------------------------------------- |
| `allocation.reallocate` | raise a request; read the reservations screen                     |
| `allocation.view`       | list/read reservations and reallocation requests, including the queue |
| `allocation.approve`    | approve (execute) or reject a request                             |

The approval queue is shown only when the user holds both view and approve. A
user with `allocation.reallocate` but not `allocation.approve` can raise a
request and sees its outcome, but has no decision control.

## Automated evidence

| Test | Boundary it pins |
| ---- | ---------------- |
| `test_a_requester_with_reallocate_can_raise_a_request` | raise succeeds with `allocation.reallocate`; donor chosen by the engine |
| `test_raising_a_request_needs_the_reallocate_right` | missing `allocation.reallocate` → 403 |
| `test_the_approval_queue_needs_the_allocation_view_right` | missing `allocation.view` → 403 |
| `test_approving_needs_the_approve_right` | missing `allocation.approve` → 403 |
| `test_the_requester_cannot_approve_their_own_request` | separation of duties → 422 |
| `test_a_separate_approver_executes_and_both_people_are_notified` | execution + requester `APPROVAL_GRANTED` + donor owner `ALLOCATION_CHANGED` |
| `test_the_requester_is_notified_when_the_request_is_rejected` | requester `APPROVAL_REJECTED` with the decision note |

Run them with:

```bash
cd apps/api
.venv/Scripts/python.exe -m pytest tests/api/test_allocation_reallocation_http.py tests/integration/test_allocation_service.py -q
```

The frontend refactor is covered by
`apps/web/src/features/inventory/reservations-page.test.tsx`; the shared dialog
is `apps/web/src/features/inventory/reallocation-request-dialog.tsx`.

## Manual UAT steps

Roles: **Sales executive** (`allocation.reallocate`), **Planner**
(`allocation.view` + `allocation.approve`), **Warehouse staff** (no allocation
rights), **Donor owner** (creator of the reservation that gives stock up).

1. **Raise from a sales-order shortage.**
   Open a confirmed order with a short line → Allocation panel →
   **Request reallocation**. The quantity is pre-filled with the line's
   shortfall, the warehouse is the order's warehouse, and the reason is
   mandatory. Submit. Expect: a PENDING request, the donor and alternatives
   shown, no stock moved.
2. **Raise from an MRP shortage.**
   Open a completed run → Shortages and late supply → a directly demanded row
   with a sales order behind it → **Request reallocation**. A run scoped to one
   warehouse is pre-filled; a run across several asks for the warehouse.
   Expect: same outcome as step 1. A BOM-component row offers no action, because
   it has no demand document to reallocate towards (E3a adds level provenance).
3. **Restricted role cannot raise.**
   Sign in as Warehouse staff → Reservations opens read-only where the role can
   view it, and no reallocation action is offered. Posting
   `POST /allocation/reallocation-requests` directly answers 403.
4. **Requester cannot decide.**
   As Sales executive, open the queue (if the role holds view): the request is
   listed but there is no approve/reject control. Posting approve directly
   answers 403 without `allocation.approve`, and 422 with it when the caller is
   the requester ("someone other than the person who requested it").
5. **Planner approves.**
   As Planner, open Reservations → Reallocation approval inbox. Approve with a
   note. Expect: status EXECUTED, the recipient reservation extended or created,
   the donor line's allocation reduced, and an audit entry for both sides.
6. **Requester notification.**
   As the requester, the bell shows **Reallocation approved** (or **rejected**)
   with the decision note. The donor owner separately sees **ALLOCATION_CHANGED**
   for the reservation that lost stock. An approval by a superuser acting on
   their own request does not notify them about their own action.
7. **Reject.**
   As Planner, reject a PENDING request with a reason. Expect: donor stock
   untouched, requester notified with the reason, no `ALLOCATION_CHANGED` row for
   the donor.

## Known limits

- The notification's action link opens `/inventory/reservations`; preselecting
  the request is part of the B4 deep-link work.
- Requests from MRP component rows wait for E3a, which carries root-demand
  provenance through the low-level netting. Directly demanded rows are covered
  today.
- Package-level E2 remainders (allocation policy settings, priority scale,
  cross-warehouse execution/transfers, automatic proposals, spare requests) are
  tracked separately in
  [`proposal-status-20261016.md`](proposal-status-20261016.md).
