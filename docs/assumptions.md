# Recorded assumptions

Master rules section 81: where a business rule was not fully specified, the
implementation does **not** invent one silently. It picks the conservative
option, marks the decision with an `ASSUMPTION:` comment at the point it is
made, and records it here.

Every entry below is a decision Atcraft should confirm or overturn. Changing one
means changing it in the named place — not adding a second interpretation
somewhere else.

---

## How to read this

| Field | Meaning |
|---|---|
| **Assumed** | what the code does today |
| **Why** | why this option rather than the alternative |
| **Where** | the single place to change it |
| **Impact if wrong** | what breaks or has to be reworked |

---

## 1. Credit limit semantics

**Assumed.** `credit_limit IS NULL` means unlimited credit. `credit_limit = 0`
means strictly no credit — a cash-only customer.

Raising line quantities on a confirmed order re-runs the credit check against
the full prospective order total — not the delta, because `outstanding_amount`
never includes open orders — under the locked customer row, and only when the
total grows. A reduction is never checked, so a customer now on hold can still
shrink its exposure.

**Why.** The two states are genuinely different and a single nullable number has
to carry both. Treating `0` as "unlimited" would be dangerous by default; a
customer set up with no limit yet would silently get infinite credit.

**Where.** `src/modules/customers/service.py` — `assert_within_credit_limit`;
the re-check on a confirmed order is `src/modules/sales/service.py` —
`_update_confirmed`.

**Impact if wrong.** Orders are blocked or allowed incorrectly at confirmation.
Low rework cost.

---

## 2. Segregation of duties on approval

**Assumed.** The approver of a document may not be its creator, unless they are
a superuser. Applies to BOM approval, purchase-order approval, requisition
approval and approval requests.

**Why.** It is the standard control, and the master rules treat approval as
evidence of a second person's judgement (section 42). A small team may find it
obstructive, which is why it is recorded rather than assumed permanent.

**Where.** The approval path of each module's service, and
`src/modules/audit/` for approval requests.

**Impact if wrong.** A small plant with one buyer cannot approve their own
purchase orders and needs a second account. Easy to relax; harder to add later
once people are used to its absence.

---

## 3. Over-receipt tolerance on purchase orders

**Assumed.** A goods receipt may exceed the ordered quantity by up to **5 %**.
Beyond that the receipt is refused and needs a purchase-order amendment.

**Why.** Hardware is supplied in batch quantities and an exact match is
unrealistic; unlimited over-receipt would let a vendor invoice for whatever they
shipped.

**Where.** `src/modules/procurement/models.py` — `OVER_RECEIPT_TOLERANCE`. The
same constant renders the `received_within_over_receipt_tolerance` CHECK on
`purchase_order_lines`, so the database and
`service_rules.assert_within_over_receipt_tolerance` cannot disagree.

**Impact if wrong.** Either receipts are refused too often, or over-supply is
absorbed without a decision. Should be confirmed against Atcraft's actual vendor
agreements, and is a candidate for a per-vendor or per-item setting.

---

## 4. Job-work loss tolerance

**Assumed.** Loss within the line's `expected_loss_percentage` is posted as
`SCRAP` automatically. Loss beyond it requires approval and flags the vendor for
a debit note.

**Why.** Polishing and PVD have real, expected material loss. Silently absorbing
loss beyond the agreed figure would hide a vendor problem.

**Where.** `src/modules/job_work/service.py` — the receipt path,
`assert_excess_loss_authorised`. Accepting loss beyond the allowance requires
`job_work.close`, the module's senior right: a `job_work.receive` holder who
sets `approve_excess_loss` is refused with `PERMISSION_DENIED`, and the receipt
records the `job_work.close` holder as `excess_loss_approved_by_id`.

**Impact if wrong.** Vendor recoveries are missed, or routine loss creates
unnecessary approvals.

---

## 5. Three-way match tolerance

**Assumed.** A vendor invoice may differ from the purchase-order price by up to
**2 %**, and from the accepted GRN quantity by **0 %**. Anything outside that
blocks approval unless a holder of `vendor_invoice.approve` overrides with a
recorded reason.

**Why.** Price rounding and freight allocation cause small legitimate price
variances. A quantity variance is not a rounding artefact — it means the invoice
disagrees with what was physically accepted.

**Where.** `src/modules/finance/service.py` — `match_invoice`.

**Impact if wrong.** Either invoices are blocked too often, or overbilling is
approved. Finance should confirm both figures.

---

## 6. MAKE_OR_BUY resolution in MRP

**Assumed.** An item with `production_type = MAKE_OR_BUY` is suggested for
**manufacture** when an approved default BOM exists, and for **purchase**
otherwise.

**Why.** The existence of an approved BOM is the strongest available signal that
the item is genuinely made in-house. The alternative — always purchase — would
make the BOM pointless.

**Where.** `src/modules/mrp/engine.py` — suggestion type selection.

**Impact if wrong.** MRP proposes the wrong action for dual-sourced items. The
planner sees and can change every suggestion before conversion, so the cost is
noise rather than a wrong transaction.

---

## 7. Negative stock

**Assumed.** Negative on-hand stock is **never** permitted. The database `CHECK`
constraint enforces it regardless of code path. A per-warehouse
`allow_negative_stock` flag exists but only relaxes the service's pre-flight
check, not the constraint.

**Why.** Master rules section 10 treats inventory correctness as
non-negotiable. Shop floors sometimes want to issue before the receipt is
posted; that is a process problem, and hiding it behind negative stock makes
valuation and MRP wrong at the same time.

**Where.** `src/modules/inventory/models.py` (constraint) and
`src/modules/warehouses/models.py` (the flag).

**Impact if wrong.** WIP stores that post receipts late will see refusals. The
right answer is likely a backdated receipt, not negative stock.

---

## 8. Price override authority

**Assumed.** A sales-order line price may be overridden only by a user holding
`price.manage`. An override **below item cost** additionally requires approval.

**Why.** Discounting is a commercial decision; selling below cost is a different
decision and should be visible.

**Where.** `src/modules/sales/service.py` — pricing resolution.

**Impact if wrong.** Sales staff may find the approval step obstructive on
genuine clearance pricing.

---

## 9. GST place-of-supply determination

**Assumed.** Intra-state (CGST + SGST) when the company's `state_code` equals
the shipping address `state_code`; otherwise inter-state (IGST). Rounding is
`ROUND_HALF_UP` at two decimal places, applied per line.

**Why.** It is the ordinary rule and the rounding convention Indian invoicing
expects. Exports, SEZ supplies and reverse-charge cases are **not** modelled.

**Where.** `src/modules/sales/service.py` — tax computation.

**Impact if wrong.** Incorrect tax on invoices — a statutory problem, not just a
data one. Must be confirmed by Atcraft's accountants before go-live, and
extended if Atcraft exports.

---


Issued sales invoice lines reuse `sales.compute_line_amounts`, preserving the
same independently rounded CGST/SGST halves as the sales order.

## 10. Reservation release policy

**Assumed.** Reservations are released automatically when their demand document
is cancelled. A scheduled job releases reservations whose demand has been
inactive past `expires_at`. Reallocation of a `HARD` reservation always requires
an approved `ReallocationRequest`; `SOFT` reservations may be taken by a
higher-priority demand without approval.

**Why.** Stale reservations make MRP over-order, which is expensive. The
hard/soft distinction is what the master rules provide for expressing "this one
is really promised".

**Where.** `src/modules/allocation/service.py` and the
`maintenance.release_stale_reservations` task.

**Impact if wrong.** Either stock stays locked to dead demand, or a genuine
commitment is taken without the right person knowing.

---

## 11. Allocation priority ranking

**Assumed.** Reallocation candidates are ranked by: worse priority number first,
then later required date, then `SOFT` before `HARD`, then not-yet-consumed only.

**Why.** The master rules require the priority to be "configurable and
business-driven" but do not state the order. This is a defensible default,
expressed as a policy object rather than hardcoded comparisons.

**Where.** `src/modules/allocation/service.py` — `ReallocationPolicy` and
`DEFAULT_POLICY`.

**Impact if wrong.** Suggestions are ordered unhelpfully. The engine only ever
*suggests*; a human approves, so the risk is wasted attention, not wrong stock.

---

## 12. BOM effectivity and phantom items

**Assumed.** Only `APPROVED` BOMs whose effectivity window contains the
as-of date are exploded. A `PHANTOM` BOM is exploded through — the phantom item
itself never appears as a requirement line, only its children.

**Why.** Standard MRP semantics. A phantom exists to structure the drawing, not
to be stocked.

**Where.** `src/modules/bom/engine.py`.

**Impact if wrong.** Requirements for a phantom assembly would be raised for an
item nobody stocks or buys.

---

## 13. Financial year and document numbering

**Assumed.** The financial year runs April to March. Statutory document series
(`SO`, `SI`, `PO`, `GRN`, …) reset each financial year and are formatted
`PREFIX/2026-27/00042`. Internal documents (stock transfer, adjustment,
physical count) do not reset.

**Why.** Indian statutory convention, and the company master already carries
`financial_year_start_month` so the assumption is configurable rather than
baked in.

**Where.** `src/common/codes.py` — `DocTypes` and `financial_year_key`.

**Impact if wrong.** Document numbering would have to be migrated, which is
disruptive once documents exist. Confirm before go-live.

---


The sales invoice prefix is SI with five digits (`SI/2026-27/00042`), meeting
the 16-character GST limit. Rollout refuses an existing sales_invoice sequence
until its previous numbering has been reviewed.

## 14. Refresh-token storage in the browser

**Assumed.** The access token lives in memory only. The refresh JWT is never in
a JSON body: the API sets it as an `HttpOnly`, `SameSite=Strict` cookie
(`atcraft_refresh`, path `/api/v1/auth`, `Secure` when deployed) and a readable
companion cookie `atcraft_csrf` carrying an HMAC of that JWT. `/auth/refresh`,
`/auth/logout` and `/auth/logout-all` require the CSRF cookie's value echoed in
`X-CSRF-Token`; both cookies rotate on every refresh. A reload restores the
session because the browser still holds the cookie, and `localStorage` holds
only a sign-out marker, never a credential.

**Why.** Script cannot read an `HttpOnly` cookie, so an XSS payload cannot
exfiltrate the refresh credential. The double-submit HMAC closes the hole a
cookie opens - a cross-site form post carries cookies but cannot set a header,
and a sibling host cannot forge an HMAC keyed on `JWT_SECRET`. The earlier
design, which persisted the refresh token in `localStorage`, was replaced when
this landed; `docs/security.md` no longer lists it as a gap.

**Where.** `src/modules/auth/cookies.py` (`set_refresh_cookies`,
`require_refresh_csrf`), `src/modules/auth/router.py`, and
`apps/web/src/lib/api-client.ts`.

**Impact if wrong.** A non-browser client that expected a `refresh_token`
field must keep a cookie jar and send `X-CSRF-Token` instead - there is no
body-based refresh. Serving the API on a different origin from the SPA would
need the cookie's `SameSite` policy and the CORS credential settings revisited
together.

---

## 15. Count ceiling on list totals

**Assumed.** Exact result counts are computed up to 10 000 rows. Past that the
API returns `total: null` and the UI shows a range without a total.

**Why.** Counting a large filtered set is a second scan of the same data, and
nobody reads "page 412 of 9 187".

**Where.** `src/core/pagination.py` — `COUNT_CEILING`.

**Impact if wrong.** Purely cosmetic. Raise it if users genuinely need exact
totals on large sets, and accept the cost.

---

## 16. Duplicate purchase order matching window

**Assumed.** Two purchase orders compete for the same demand when they share a
vendor, an item and a delivery warehouse *and* their required dates fall within
**7 days** of each other. Naming the same source demand (MRP suggestion, sales
order, requisition) overrides the date window entirely. A missing date on either
side counts as overlapping.

**Why.** A same-day-only rule misses the ordinary case - one shortage ordered
twice, three days apart - which is what master rules section 16 exists to catch.
A month-wide window would flag every routine repeat order for a consumable.
Treating a missing date as "no match" would silently hide the duplicate.

**Where.** `src/modules/procurement/models.py` —
`DUPLICATE_REQUIRED_DATE_WINDOW_DAYS`; the matcher is
`service_rules.classify_candidates`.

**Impact if wrong.** Either buyers are warned too often and learn to acknowledge
without reading, or genuine double-ordering slips through. Cheap to retune.

---

## 17. Quotation recommendation weighting

**Assumed.** The recommended vendor is scored 60% on effective unit price, 20%
on lead time and 20% on vendor rating. An unrated vendor scores 0.5 (neutral)
rather than zero. No recommendation is made when quotations are in different
currencies.

**Why.** Price is what the business buys on, but a part four weeks late stops a
line and a poor-quality vendor costs more than their discount. Scoring an
unrated vendor as zero would stop any new vendor ever winning a first order.
Comparing 100 EUR against 9,000 INR by magnitude would recommend confidently and
wrongly.

**Where.** `src/modules/procurement/service_rules.py` —
`DEFAULT_SCORE_WEIGHTS`, `UNRATED_VENDOR_SCORE`.

**Impact if wrong.** Advisory only: every recommendation states its reason, the
buyer's own decision and reason are what get recorded, and the per-item rows
support splitting an order across vendors.

---

## 18. Incoming QC, freight and rejected quantity at goods receipt

**Assumed.** Three related decisions on the receipt path:

1. **Incoming inspection is flagged per purchase-order line** (`requires_qc`),
   not per item. `items` carries no incoming-QC flag and belongs to another
   module, and the requirement is genuinely per-order - a first delivery from a
   new vendor is inspected where a repeat order of the same part is not.
2. **Rejected quantity is never posted to inventory.** Goods refused at the gate
   never entered the company's custody. Material accepted and later found
   defective goes back through a purchase return, which posts the offsetting
   `PURCHASE_RETURN` movement.
3. **Header freight is not apportioned into item cost at receipt.** Stock is
   valued at the discounted net unit price, excluding recoverable GST.
   Apportioning freight needs a basis (value, weight, volume) nobody has agreed,
   and a wrong basis misstates the cost of every item on the order.

**Where.** `src/modules/procurement/service.py` — `post_goods_receipt`;
`service_rules.receipt_unit_cost`.

**Impact if wrong.** (1) is a data-entry burden if most items always need
inspection - a per-item default would fix it. (2) would need a
receive-then-return flow if the business books rejected goods into a rejection
area first. (3) understates landed cost until finance applies freight.

---

## 19. Inbound shipment tracking lives on the purchase order

**Assumed.** Courier name, tracking reference and shipped-at for the *inbound*
leg are columns on `purchase_orders`, recorded by the vendor's dispatch manager
through the portal.

**Why.** The `dispatch` module owns `dispatches` and `dispatch_tracking_events`,
but those describe consignments this company sends. Nothing in the contract
gives the vendor's shipment *to* us a home, and the tracking board in master
rules section 23 cannot be built without one.

**Where.** `src/modules/procurement/models.py` — `PurchaseOrder.courier_name`,
`tracking_reference`, `shipped_at`; `service.record_shipment`.

**Impact if wrong.** If inbound consignments should be first-class `dispatches`
rows, these three columns become a migration and the tracking view reads from
there instead.

---

## 20. Serial-tracked items cannot be received through a goods receipt

**Assumed.** A purchase order line for a serial-tracked item is refused at
receipt with an explicit message directing the user to register the units
through inventory.

**Why.** The inventory engine requires a serial id on every movement of a
serial-tracked item, and the procurement contract gives a receipt line nowhere
to record the many serials arriving against one line. Posting stock whose units
cannot be identified afterwards defeats the tracking the flag exists for.

**Where.** `src/modules/procurement/service.py` — `SERIAL_RECEIPT_MESSAGE`.

**Impact if wrong.** Serial-tracked purchases need a serial capture table on the
receipt line before they can be received through this path. Related to open
question 6 below.

---

## 21. Supply netting is time-phased

**Assumed.** MRP nets a requirement only against supply arriving on or before
that requirement's own earliest required date. Supply promised *on* the required
date counts as in time; one day later does not. Supply excluded for being late is
never dropped silently — it is carried on the requirement as
`late_incoming_quantity` / `late_in_production_quantity`, counted in
`result_summary["late_supply_item_count"]`, and named in a run warning giving the
item, the quantity, the date it does arrive and the date it was needed.

**Why.** Without phasing, a purchase order arriving at the end of the horizon
cancelled a shortage due at the start of it: a line needing 100 bolts in two days
was netted against an order arriving in six weeks, and the run reported no
shortage and raised no suggestion. Under-ordering stops the shop floor; the
opposite failure is a purchase order a buyer can see and cancel.

**Where.** `src/modules/mrp/engine.py` — `supply_split()` / `SupplySplit`, which
is the single place the split is computed. Each item is netted exactly once, at
its low-level code, and only the net shortfall is exploded, so there is no
second decision that could phase the same item at a different date.

**Impact if wrong.** If Atcraft plans against a firmer promise date than the
system holds, the horizon is being cut too finely and suggestions will be raised
for stock that was going to arrive in time anyway.

---

## 22. Production output is netted as dated supply, components included

**Assumed.** A live production order's unmade balance is supply like a purchase
order: it is netted against the parent's gross requirement before the shortfall
is computed, and only the net shortfall is exploded. Components for output that
is already on the floor are therefore not planned a second time by MRP.

**Why.** E3a's binding rule is one netting stage for both halves: free stock,
then in-time supply (purchase orders and production), then explode the shortfall.
The old asymmetry - the parent's requirement netting against production while the
explosion did not - was defensible only while the two decisions were taken at
different dates. Under level-by-level planning they are one visit, and netting
production on one side but not the other would raise a second set of material for
work already released. Where a job reserved its components, those holds are
already excluded from free stock, so nothing is double-counted. A release that
could not secure its material reports the shortage through the production
module; the MRP plan does not over-order to compensate.

**Where.** `src/modules/mrp/engine.py` — `plan()`, the in-time supply split
before each explosion.

**Impact if wrong.** If a released job can no longer get its material, the
shortage surfaces at issue rather than in the plan. The conservative alternative
is the old bounded over-order, which is cancellable but hides the job's own
problem; switching back needs a reliable "this job's components are secured"
signal, which does not exist today.

---

## §E3-1. Every item is planned once, at its low-level code

**Assumed.** An item's low-level code is the deepest level at which it appears
across the structures reachable from the run's direct demand, capped at
`DEFAULT_MAX_EXPLOSION_DEPTH` (20). Items are netted level by level, shallowest
first, and each item is netted exactly once - at its low-level code. Gross
requirement is direct demand plus the dependent demand raised by parents already
planned; free stock and in-time supply are netted against it; only the net
shortfall is exploded one level down. A structure that would exceed the cap is
refused with `BOM_DEPTH_EXCEEDED` rather than silently truncated.

Lead times: a MANUFACTURE suggestion uses the routing's lead time for the
suggested quantity (`routing.service.manufacture_lead_times`, batched per run),
falling back to the item master. A PURCHASE suggestion keeps the preferred
vendor-link lead time, falling back to the item master. A JOB_WORK suggestion
uses the item master. Each child's required date is offset from its parent's
required date by the parent's own lead time, at every level - not once from the
root.

**Why.** The previous engine decided the explosion against a parent's date while
that date was still being moved by other demand for the same parent, which
under-planned components for items demanded both directly and as components.
Netted-once-at-LLC removes the second date: the requirement, the explosion and
the suggestion are computed in one visit. It also makes stock held for a
sub-assembly reduce what its components are planned for, which is the physically
correct answer.

**Where.** `src/modules/mrp/engine.py` - `_discover_levels`, `plan()`;
`src/modules/routing/service.py` - `manufacture_lead_times`.

**Impact if wrong.** If a routing lead time is systematically longer than the
real shop calendar, manufacture suggestions are dated early; if the depth cap is
ever reached by a genuine product, the run refuses instead of planning a
truncated structure, which is visible and safe.

---

## §E3-2. Expected availability reports coverage, it does not promise it

**Assumed.** A requirement's `expected_available_date` is the first date at
which stock plus dated in-time supply covers the gross requirement. If they never
cover it, it is the date the plan's own suggestion can arrive; if coverage
completes later than that, the coverage date wins. The value is rolled up through
the structure: `max(own date, each child's date + own lead time)`, so a late
component slips the parent. A suggestion's date is
`max(as_of, suggested_order_date) + lead_time_days`, so a late suggestion shows
when the order can physically land rather than a date in the past.

**Why.** "Short 40" is actionable; "short 40, and the earliest this can be fixed
is 12 November" is what a planner promises a customer against. Showing the
suggestion's own required date on a late order would name a date that is already
impossible.

**Where.** `src/modules/mrp/engine.py` - `coverage_date`, `expected_availability`,
the post-netting roll-up in `plan()`; `src/modules/mrp/service.py` - the demand
stamping after a run completes; `GET /mrp/sales-orders/{id}/expected-availability`.

**Impact if wrong.** These dates are informational. They do not move
`required_date` or `promised_date`; "Use as promised date" on a sales order is an
explicit, audited PATCH. If the dates prove optimistic, planners will miss the
promises they copy, so the arithmetic has to stay tied to the same supply the
netting used.

---

## §E3b-1. Job-work output is dated supply, never twice-ordered

**Assumed.** MRP nets a third dated supply feed beside purchase orders and
production: the good output still expected from input already issued to a
job-work vendor. The quantity for a line is its frozen output ratio applied to
the input no receipt has accounted for yet -
`expected_output_quantity × max(issued − received, 0) ÷ quantity_to_issue` -
grouped by the order's expected return date. Input not yet issued is not supply:
the planner has not sent it out, so it cannot come back processed. Input already
accounted for by a receipt (consumed into output, returned unworked, rejected or
scrapped) is excluded, as is output already received. The feed is restricted to
the run's warehouse scope through the line's return warehouse and to the run's
horizon.

A job-work arrival after the requirement's own date is not netted. It is
reported in `late_job_work_quantity` with its opening and closing dates, exactly
like late purchase and production supply; `first_late_supply_date` is the
earliest of all three feeds, not a date for any one of them.

**Why.** An order whose material is already at a vendor is work in progress
owned by the company. Re-suggesting it on the next run raises a duplicate order
for output that is already coming back. Treating the unissued balance as supply
would do the opposite and suppress a genuine shortage before anything left the
building.

**Where.** `src/modules/job_work/repository.py` - `open_output_by_item`;
`src/modules/job_work/service.py` - `open_job_work_output`;
`src/modules/mrp/repository.py` - `job_work_supply`; `src/modules/mrp/engine.py` -
`supply_split`, `net_after_supply`, `plan()`; migration `20261026_0100`.

**Impact if wrong.** If vendors routinely return less than the frozen ratio, MRP
will under-order and the shortage will surface at receipt. If a vendor's date is
not maintained, the plan trusts the stale date; the job-work order remains the
single place to correct it. Splitting one input line across several batches does
not change the arithmetic because the unit of supply is the expected output item
line, not the lot.

---

## 23. Traceability gates arrivals for batch, and unit-changing movements for serial

**Assumed.** The two flags deliberately behave differently on the way out.

*Batch.* `is_batch_tracked` gates only movements that start stock **into** a
position. A batch-less departure is allowed.

*Serial.* `is_serial_tracked` gates arrivals **and** every movement that changes
whose hands hold the unit — an issue, a dispatch, a scrap, a job-work issue, a
purchase return, a job-work return. A movement that leaves the unit's status
alone (a QC hold or release, either leg of a transfer) moves a quantity rather
than a unit and is not gated, so it cannot trap a pool. The gate is
`SERIAL_MOVEMENT_TARGETS` — the same mapping that decides the unit's next status
— so the rule and the serial lifecycle read one source and cannot drift apart.

**Why.** The asymmetry follows from the balance grain, not from taste.
`batch_id` **is** part of `BalanceGrain`, so a batch-less issue can only ever
reach the batch-less position — precisely the stock that predates the flag — and
`on_hand_quantity` may not go negative, so it cannot invent a lot either. Leaving
it gated in both directions made stock immovable: the flag could be switched on
over existing stock, and then a movement naming no batch was refused while a
movement naming one keyed a different grain holding nothing.

`serial_id` is **not** part of the grain. A serial-less departure therefore draws
on the whole position, identified units included, choosing which by nobody's
decision — stock leaves while every `stock_serials` row still says `IN_STOCK`, and
the same unit can be issued twice. That is exactly the defect serial tracking
exists to prevent, and it is live on the ordinary path of every serial-tracked
item, not only after a flag flip.

Untraced legacy quantity is therefore refused until somebody identifies it. That
is **not** stranding: registering serials for what arrived (`create_serial`) and
issuing them is an ordinary operator action, and it is proved end to end in
`tests/integration/test_inventory_tracking_flags.py::TestTheSerialTwinOfIt`.

**Where.** `src/modules/inventory/service.py` — `validate_movement`, the
`gates_policy` and `serial_gates` rules. Pinned by
`tests/unit/test_inventory_movement_rules.py::...::test_the_batch_and_serial_rules_disagree_on_purpose`,
which fails if the two rules are ever merged into one gate — that merge is how
this defect was introduced.

**Impact if wrong.** If Atcraft wants an anonymous issue of serial-tracked stock
to be legal, serial traceability is not a guarantee the system can make, and the
serial lifecycle should be reconsidered as a whole rather than loosened here.

---

## 24. Tracking flags may only change while the item holds no stock

**Assumed.** `is_batch_tracked` and `is_serial_tracked` may change only while the
item holds nothing anywhere — no on-hand, reserved, allocated, in-transit,
QC-hold, damaged, job-work or scrap quantity in any position. Symmetric: turning
tracking *off* is refused on the same terms. The test is balance rows, not a sweep
of open documents.

**Why.** Refusing the flip outright would block a legitimate decision — "we are
starting to lot-track this part" is ordinary. Allowing it under live stock is what
created the immovable position. `inventory_balances` is the right test because it
is where an in-flight document has *already* put the stock, so the one class of
document whose completion cannot be refused — a dispatched transfer sitting in
`in_transit_quantity` — is always visible, while a document that has not moved
anything yet strands nothing, because its posting is an arrival and an arrival is
refusable.

**Where.** `src/modules/products/service.py` — `_assert_tracking_change_is_safe`,
using `ItemRepository.has_stock_balance`.

**Impact if wrong.** An operator wanting to start tracking a part with stock on
hand must first issue or adjust it out. If that is too strict for Atcraft, the
alternative is a supervised migration that assigns lots or units to existing
positions — not a relaxation of the guard.

> **Pre-existing rows.** This guard prevents *new* occurrences only. A database
> where a flag was flipped over live stock before the guard existed may still hold
> such positions. Nothing was migrated and no row was altered — what to do with
> that quantity is a business decision, since it could be a genuine lot never
> keyed, a counting error, or stock long since consumed off the books. Batch-NULL
> positions of batch-tracked items are reachable again under §23; serial ones need
> the units registering first.

---

## 25. Serial-tracked items cannot move on a stock transfer

**Assumed.** A stock transfer refuses a serial-tracked item outright, and requires
a batch-tracked line to name its batch. Both are refused at `create`, not at
dispatch.

**Why.** `stock_transfer_lines` carries a `batch_id` and no unit list, so there is
nowhere to name the units being shipped — the same "no contract on this path"
refusal that assembly and job work already make. Refusing at create rather than at
dispatch means a flag flipped mid-flight cannot strand a document that was already
accepted.

**Where.** `src/modules/inventory/service.py` —
`StockTransferService._assert_lines_are_traceable`.

**Impact if wrong.** Warehouse-to-warehouse movement of serialised stock needs a
unit list on the transfer line — a schema change and a UI change, not a rule
change.

---

## 26. In-transit arrivals are exempt from traceability, and the gap is recorded

**Assumed.** A movement carrying `completes_in_transit` is exempt from the batch
and serial requirements. The exemption is recorded rather than silent: the
movement's audit row gains a `traceability_gap` key and a warning is logged. No
serial row is invented for goods nobody identified.

**Why.** These goods left their source before the flag existed, so there is no lot
or unit to name, and `TRANSFER_FLOW` has no edge out of `IN_TRANSIT` except
`RECEIVED` — refusing the arrival creates no traceability at all, it only leaves a
lorry-load nowhere. The document that dispatched it is the trace and is on every
ledger row of both legs. Recording the gap means the untraced quantity can be
found deliberately rather than discovered during a recall.

**Where.** `src/modules/inventory/service.py` — `MovementEngine._traceability_gap`.

**Impact if wrong.** If Atcraft needs a hard guarantee that no untraced quantity
ever enters a tracked item's position, the transfer must be blocked at dispatch
instead — which strands the goods rather than the traceability.

---

## 27. Ageing and outstanding reports are per currency, never converted

**Assumed.** No amounts are summed across denominations. An ageing report covers
every currency with an open item; the `currency` argument chooses which block is
restated at the top level, not which blocks exist. `currencies[0]` always matches
`report.currency`, zeroed if nothing is outstanding in it. Detailed party rows are
returned for at most five denominations — the primary, then the largest by
outstanding — while every denomination still reports its totals. Receivable FIFO
netting is per `(party, currency)`: a receipt in one denomination never settles a
debt in another. Within a denomination, parties rank by outstanding descending
with the party code as tiebreaker.

Where no currency is asked for and nothing is open in the company's own, the
headline denomination is the one with the most open **documents**, then the most
parties, then the code — never the largest unconverted amount.

**Why.** There is no exchange-rate infrastructure (open question 3 below), so
there is no defensible rate at which to combine them. Ranking by unconverted
magnitude is the same mixing in a different place: it would put 1,000 USD below
50,000 INR. Document count is comparable across currencies; money is not.

**Where.** `src/modules/finance/service.py` — `primary_report_currency`,
`company_currency`, `normalise_currency`, `MAX_AGEING_ROW_CURRENCIES`;
`src/modules/finance/repository.py` — `_open_receivable_items`.

**Impact if wrong.** If Atcraft transacts in more than one currency and needs a
consolidated figure, this becomes a real FX feature — rate table, rate date policy,
revaluation — and none of it should be improvised here.

---


Receivable FIFO debits sourced by a sales invoice age from its due date;
legacy charges still age from their entry date. The not-due bucket therefore
includes issued invoices within their agreed customer credit period.

## 28. A company with no receivable control account has no receivables

**Assumed.** A company with no `ACCOUNTS_RECEIVABLE` control account gets an
**empty** customer ageing report — not an unrestricted one, and not an error.

**Why.** The query previously dropped its account predicate entirely when the
account set was empty, so "no accounts to filter by" silently became "no filter",
and an unprovisioned tenant saw every ledger entry. An empty set must mean an
empty result: the safe reading of "nothing matches" is nothing, never everything.

**Where.** `src/modules/finance/repository.py` —
`LedgerEntryRepository._open_receivable_items`.

**Impact if wrong.** A tenant mid-setup sees an empty report where they might
expect a warning. Raising instead would be defensible; returning everything is not.

---


Issuing a sales invoice provisions the AR account before posting. The empty
account set still returns an empty ageing result rather than all ledger entries.

## 29. MRP runs are rate limited per company

**Assumed.** `RateLimitPolicy.MRP_RUN` is enforced at five runs per five minutes,
keyed on the company alone, so all of a tenant's planners share one allowance. A
run refused further down — `MRP_RUN_IN_PROGRESS`, or demand over
`MAX_DEMAND_LINES` — still spends a token, because the token is taken before the
planning lock.

**Why.** An MRP run explodes every BOM for every demand line across the horizon
and is the most expensive operation in the product. Keying on the company matches
the cost model: what a run consumes is the cost of the company's own BOMs and
order book, whoever asks. Spending a token on a refused attempt is the precedent
auth already sets — a failed login counts — and it is what stops a client
discovering the refusal is free and retrying in a loop.

**Where.** `src/modules/mrp/service.py` — `start_run`. Values live in
`src/core/ratelimit.py:137`.

**Impact if wrong.** A large tenant with several planners may find five runs per
five minutes tight. The limit is one constant.

---

## 30. A serial's recorded position is where it was last put

**Assumed.** `StockSerial.warehouse_id` / `location_id` record where the unit was
last *put*, and are never cleared; `status` is what says whether it is still
there. Only a movement that relocates the unit rewrites them. A QC release
resolves a `RETURNED` unit to `IN_STOCK` only when the release lands the quantity
in the `AVAILABLE` pool; a release landing it elsewhere leaves the unit
`RETURNED`. A non-relocating movement must be raised at the warehouse the unit is
recorded at, compared by warehouse **and** bin, but only where the movement states
a bin.

**Why.** Clearing the position on issue or dispatch destroyed the last-known
location, which is the first thing anyone wants during a recall — and three other
modules read `warehouse_id` as a liveness check, so clearing it broke them.
Status already carries "is it still ours".

**Where.** `src/modules/inventory/service.py` — `MovementEngine._advance_serial`,
`serial_transition`, `assert_unit_is_where_the_movement_is`.

**Impact if wrong.** Reports that ask "where is this unit" answer with the last
place it was seen rather than `NULL` once it has shipped. Consumers must read
`status`, not the presence of a warehouse.

---

## 31. Inventory is valued FIFO for every item

**Assumed.** Stock is valued FIFO, for every item, by `inventory.service`
against `inventory_cost_layers` / `inventory_cost_consumptions`. There is no
per-item `valuation_method`; `inventory_balances.average_cost` is a derived
display figure over the open layers, never an independent moving average.

**Why.** The engine only ever costed FIFO. A per-item column defaulting to
`WEIGHTED_AVERAGE` described a choice nothing honoured and was dropped in
revision `7e8eb4bdd0ed` (`20260902_1147_drop_items_valuation_method.py`); see
`docs/database.md` section 3.

**Where.** `src/modules/inventory/service.py` - `consume_layers`,
`cost_movement`, `FifoCostPreview`; `src/modules/inventory/models.py` -
`InventoryCostLayer`, `InventoryCostLayerConsumption`.

**Impact if wrong.** If weighted average or standard cost is required for any
item class or statutory report, the column returns together with a costing
dispatch that reads it - never the column alone.

---

## 32. Customer charges are issued invoices; adjustments remain separate

**Assumed.** A priced SALES dispatch is billed through an issued GST sales
invoice with its own SI series and receivable journal. Manual customer
adjustments remain the non-GST correction path for interest, write-offs and
legacy balances. Historical customer_charge journals are retained.

**Why.** Customers need a statutory document and due date, while both invoice
and adjustment postings must reconcile to the cached outstanding balance.

**Where.** `finance/sales_invoice_service.py`, `finance/service.py` and the
customer outstanding-delta service.

**Impact if wrong.** E-invoicing, exports, SEZ and reverse-charge treatment need
additional explicit business rules; they must not be inferred from this series.

---

## 33. Vendor portal scope

**Assumed.** A vendor account reaches the ERP only through `/vendor-portal/**`,
its own `/notifications/**` and auth self-service: every other route answers 403
`SCOPE_DENIED` whatever its roles carry (the route allow-list in
`core.deps.get_current_principal`). Inside the portal, a vendor principal's
purchase-order reads and row locks are confined at
`PurchaseOrderRepository._scoped` to that vendor's own orders - and, within
those, to orders it has been sent and to its categories (§56). The vendor
accepts, declines, commits dates, marks ready, records consignments, files
documents and submits bills there; it cannot edit or submit an order. The
buyer-side acknowledge and shipment routes remain for a buyer recording what a
vendor phoned in, and refuse vendor accounts (`forbid_vendor_users`).

**Why.** Confining the query rather than the route means a vendor cannot reach
another vendor's order by any path, including the locks a status change takes;
the allow-list means a module that never learnt about vendors (dispatch,
finance, quality, documents, reports) cannot leak to one either. Editing and
submitting an order is the buyer's act: a vendor who could amend quantities or
prices on an order addressed to them would be writing their own contract.

**Where.** `src/core/deps.py` - `vendor_route_allowed`, `require_vendor_user`;
`src/modules/procurement/repository.py` - `PurchaseOrderRepository._scoped`;
`src/modules/vendor_portal/router.py`.

**Impact if wrong.** If a vendor is meant to propose changes to an order, that
is a counter-proposal the buyer accepts - a new flow - not write access to the
order. Open question 5 covers what a vendor may see.

---

## 34. Quotation requote

**Assumed.** Rejecting or lapsing a vendor's offer is not final while the RFQ
is open: a revised offer through `update_quotation` reopens the (RFQ, vendor)
row to `RECEIVED`. Only `SELECTED` is terminal.

**Why.** A buyer who rejects an offer as too dear and receives a better one from
the same vendor should record it against the same RFQ, where the comparison
lives, rather than raise a second RFQ or lose the history. One row per (RFQ,
vendor) keeps the recommendation and the audit trail on a single line.
Selection is final because it is what the purchase order was raised from.

**Where.** `src/modules/procurement/service.py` - `update_quotation`.

**Impact if wrong.** If a rejection is meant to close the door on a vendor for
that RFQ, the reopening becomes a refused transition and a requote needs a new
RFQ.

---

## 35. Purchase return stock pool

**Assumed.** A return line leaves the pool the inspection put the stock in.
When the caller names no pool, `DAMAGED` is used first when rejected stock
covers the whole line, then `AVAILABLE`; a line that would need both pools is
refused and must be split, naming the pool on each part. A line still on QC
hold cannot be returned.

**Why.** The canonical reason for a purchase return is goods that failed
incoming inspection, so rejected stock goes first. A quantity that would need
both pools is refused rather than split by the service, because the service
would then be guessing a split it would have to defend. Stock on QC hold has
not been decided on, and only an inspection can move it.

**Where.** `src/modules/procurement/service_rules.py` - `resolve_return_pool`.

**Impact if wrong.** If returns are meant to draw from a single pool regardless
of inspection outcome, the resolution collapses to one branch; the split
refusal stays, because the ledger still needs to know which pool each quantity
left.

**Financial posting.** Posting the stock return also raises the vendor debit in
the same transaction: debit AP for the vendor, credit purchase expense, and
credit input tax using the tax snapshot on the return. The purchase-return code
is the ledger reference; the optional debit-note number remains the external
reference entered by the buyer. Finance may manually apply the posted debit to
approved invoices for the same vendor and currency. Allocations are limited by
both the remaining return credit and each invoice's remaining payable; they
reduce the invoice's outstanding amount and do not post a second journal.
Procurement does not currently carry cost centres on its order lines, so the
expense credit is unallocated until that carrier is implemented. A debit-note
PDF is available only for a posted return with a non-zero debit journal; it is
generated as a background report, audited, and retained without expiry. If no
external debit-note number was entered, the PDF uses the purchase-return code
as its displayed reference.

---

## 36. Receiving warehouse when an MRP run is scoped to several warehouses

**Assumed.** A run scoped to several warehouses pools their stock, so a
suggestion has no warehouse of its own. The production order raised from it is
received into the item's default warehouse when that lies inside the run's
scope, otherwise into the first warehouse the run named. A run scoped to one
warehouse receives there regardless of the item default; a run scoped to none
defers to the item default, and an item with no default is refused.

**Why.** The choice has to be deterministic and inside the scope the planner
asked for. The order boxes were ticked in on a form is neither a business rule
nor stable.

**Where.** `src/modules/production/service.py` - `mrp_receiving_warehouse`,
called by `create_order_from_mrp`.

**Impact if wrong.** Output is booked to a store the planner did not intend.
The order's `warehouse_id` is editable while it is a draft, so the correction
is one edit, but the intended store's reports under-count until it is made.

---

## 37. Only a super administrator may set a super administrator's password

**Assumed.** A caller who does not hold super-administrator authority cannot
set the password of an account that does - neither through
`POST /users/{id}/reset-password` nor by sending a `password` on
`PATCH /users/{id}`. Super-administrator authority is held by the
`is_superuser` flag or by the `SUPER_ADMIN` role, and a caller holding it by
either route is allowed. The refusal is `PERMISSION_DENIED` (403), naming
`user.reset_password` as the right involved and carrying the reason.

**Why.** `user.reset_password` is an ordinary administrative right. Without
this rule anyone holding it could take over a super administrator's account by
resetting its password, and with it acquire every permission in the system - a
privilege escalation through the one door the authority guards do not
otherwise cover. `REQUIRE_AUTHORITY_TO_REVOKE` refuses far weaker tampering for
the same reason.

**Where.** `src/modules/users/service.py` - `PROTECT_SUPER_ADMIN_PASSWORDS`,
applied by `assert_may_set_password_for` on both the reset-password and the
update paths of `UserService`.

**Impact if wrong.** If a delegated administrator is meant to reset a super
administrator's password - a break-glass rota, say - the constant turns the rule
off in one place. The audit row still names who set the password.

---

## 38. A customer adjustment is posted in the company's own currency

**Assumed.** A debit or credit note posted through
`POST /finance/customer-adjustments` is denominated in the company's currency.
`post_customer_adjustment` leaves `currency` unset on `post_journal`, so the
entries take the same default an invoice and a receipt take when none is
named; the request schema offers no currency field.

**Why.** Receivable ageing and outstanding balances net per (party, currency) -
section 27. A note in a currency nobody chose could never be settled by the
receipt that follows it, and would sit in the ageing report as a debt in its
own denomination indefinitely.

**Where.** `src/modules/finance/service.py` - `post_customer_adjustment`;
`src/modules/finance/schemas.py` - `CustomerAdjustmentCreate`.

**Impact if wrong.** If a customer's account is kept in another currency (open
question 3), the note needs a `currency` field validated against that account's
denomination, and the outstanding delta has to be applied to that
denomination's balance rather than the company-currency cache.

---

## 39. Brand and manufacturer codes are normalised, never reused, and a value in use cannot be retired

**Assumed.** A brand or manufacturer code passes through `normalise_code` - upper
case, internal whitespace collapsed, no alphabet check - the same canonical form
every other products master uses, rather than the organisation masters' stricter
`validate_code`. The code is unique per company *including retired rows*, is fixed
once created (an update that carries one is refused), and is never reissued after
a retirement. Deactivating and retiring (archiving, a soft delete) are refused on
the same terms, with `REFERENCED_RECORD_IN_USE`: a brand while any live item
carries it; a manufacturer while any live item, live item-vendor link or live
brand names it. "Live" means not soft-deleted, so an *inactive* item still holds
its brand. A brand or maker must be active to be newly assigned, but an item
keeps the one it has: an unchanged reference is not re-checked when the item is
saved again. Retirement is terminal - there is no restore endpoint.

**Why.** One canonical form across the item masters means "atcraft pro" and
"ATCRAFT  PRO" cannot become two brands, and the codes of the brand and
manufacturer sit beside item, category and attribute codes that already follow it.
A retired code still appears on the audit trail and in the history of the items
that carried it, so reissuing it would make that history ambiguous. Refusing to
retire a value that is still in use is the item-attribute rule: otherwise an item
would show a brand its own edit form can no longer offer, and saving that form
would silently drop it.

**Where.** `src/modules/products/schemas.py` - `BrandCreate`, `BrandUpdate`,
`ManufacturerCreate`, `ManufacturerUpdate`; `src/modules/products/repository.py` -
`_code_taken`, `BrandRepository.is_used`, `ManufacturerRepository.usage`;
`src/modules/products/service.py` - `BRAND_LIFECYCLE`, `MANUFACTURER_LIFECYCLE`,
`update_brand`, `delete_brand`, `update_manufacturer`, `delete_manufacturer`,
`update_item`.

**Impact if wrong.** If codes must follow the organisation alphabet, swap
`normalise_code` for `validate_code` in the four schemas (existing codes would
need checking first). If a retired brand must be restorable, add an
`ARCHIVED -> INACTIVE` edge and a restore route, as customers have. If retiring a
brand should instead clear it from its items, that is a bulk update that rewrites
item history and needs its own audit and confirmation design.

---

## 40. A brand has at most one owner, a part number is free text, and nothing branches on either master

**Assumed.** A brand names at most one manufacturer as its owner
(`brands.manufacturer_id`), recorded for reference only. A manufacturer's part
number is free text up to 64 characters - trimmed, blank stored as NULL, not
unique and never parsed - on the item and, separately, on each item-vendor link,
because different sources may supply different makers' parts for the same item.
A manufacturer is not a vendor. A new manufacturer is placed in India unless
someone says otherwise. No price, discount, permission, posting, approval or
stock rule reads a brand or a manufacturer; screens and reports may filter and
group by them.

**Why.** The proposal asks for brand and manufacturer *masters* - classification
and traceability - and the client must be free to edit the lists, which is only
safe if nothing computes with them. A brand with two owners is a licensing
arrangement rather than a master-data fact. Every maker numbers its parts its own
way, so any structure imposed on a part number would be wrong for some maker. The
country default is the one the vendor and customer address columns already
carry.

**Where.** `src/modules/products/models.py` - `Brand.manufacturer_id`,
`Item.manufacturer_part_number`, `ItemVendorLink.manufacturer_part_number`,
`DEFAULT_MANUFACTURER_COUNTRY`; `docs/database.md` section 3, "Brands and
manufacturers".

**Impact if wrong.** If a brand may have several owners, `brands.manufacturer_id`
becomes a link table. If part numbers must be unique per maker, that is a partial
unique index on `(company_id, manufacturer_id, manufacturer_part_number)` and a
data clean-up first. If a dealer discount or a price should depend on the brand,
that is a pricing rule that must be applied in every price resolver
(`products.resolve_price` and `sales.resolve_unit_price`), not a branch added
here.

---

## 41. A cost centre is optional everywhere, and NULL means unallocated

**Assumed.** Every carrier of a cost centre - departments, work centres,
production and job-work orders, stock adjustments, vendor invoices and their
lines, ledger entries - holds it as a nullable column, and NULL means
"unallocated". There is no default "general" centre a NULL silently stands for,
and nothing existing was backfilled when the column arrived.

**Why.** Nothing in the data says which centre an old document belonged to, so
any backfill would put fiction on a cost-centre statement. And a mandatory
centre would stop a company that has not set up its centres yet from raising a
single order - the same day-one reasoning that has the finance module provision
its control accounts on first use (`DEFAULT_CHART`).

**Where.** The `cost_centre_id` columns (now part of baseline migration
`20261021_0100`);
`src/modules/finance/service.py` - `_post_invoice_journal`, where a line with no
centre on an invoice with none posts the unallocated leg.

**Impact if wrong.** If allocation must be complete, make the column NOT NULL on
the documents that need it (a backfill decision per document first) and refuse
approval of an invoice with an unallocated line. The statement already shows
exactly what is allocated, so "unallocated" spend is the posted expense minus
the statement's total.

---

## 42. A document's cost centre is explicit, then its source document's, then none

**Assumed.** Precedence is: the value named on the document, then the source
document's, then NULL. A vendor invoice line resolves line, then invoice header,
then none - at approval, so editing the header moves every line that names no
centre of its own. Production orders and job-work orders take only an explicit
value: an order raised from an MRP suggestion or a sales order carries none,
and a job-work order does not inherit the centre of the production order it may
cover (its `source_id` is a bare polymorphic id this module never reads).
Material issues carry no centre; they reach one through `production_order_id`.
A stock adjustment's movements reach its centre through the movement's
`reference_type` / `reference_id`.

**Why.** None of the source documents this release can read carries a centre,
so inheriting would mean inventing one. Resolving the invoice line at approval
rather than copying the header onto each line at entry is what makes a
header-only edit behave the way a person expects.

**Where.** `src/modules/finance/service.py` - `_post_invoice_journal`;
`src/modules/production/service.py` - `create_order`;
`src/modules/job_work/service.py` - `create_order`.

**Impact if wrong.** WP B3 adds requisitions and purchase orders as carriers and
extends the chain (requester's department default, PO line). A job-work order
that must follow its production order needs a read of the production order at
entry - job work and production are siblings, so through a port, not an import.

---

## 43. Retiring a cost centre never touches posted history

**Assumed.** Two levels. *Deactivating* stops a centre being offered for new
allocations and is refused only while something would keep handing it out: an
active child, a department naming it as its default, or a work centre charged to
it. *Archiving* is refused while any row at all names it - a live child, a
department, a work centre, or any production order, job-work order, stock
adjustment, vendor invoice or invoice line *whatever that document's status* -
except posted ledger entries. A document keeps its reference after
deactivation, and re-saving it is not refused; a *new* allocation to an
inactive centre is. A ledger reversal copies the centre of the leg it undoes and
is exempt from the "active" check.

**Why.** Retiring a centre must not make the journals posted to it
irreversible, and must not strand a draft that names it - each is the next
operation on a row that already exists. Probing every document regardless of
status keeps the guard simple and conservative: deactivation, not archiving, is
how a centre that history still names stops being used.

**Where.** `src/modules/organization/service.py` - `CostCentreService.
_assert_may_deactivate` and `delete_cost_centre`;
`src/modules/organization/repository.py` - `COST_CENTRE_REFERENCE_PROBES`;
`src/modules/finance/service.py` - `_assert_cost_centres_postable`.

**Impact if wrong.** If a centre named only on closed documents should be
archivable, give each `ReferenceProbe` a status predicate - the shape is already
per table. A vendor invoice approved after its centre was deactivated is refused
at approval until it is re-tagged (it is still editable then); if that should
pass, exempt approval postings from the active check the way reversals are.

---

## 44. Only income and expense ledger legs carry a cost centre

**Assumed.** `post_journal` refuses a cost centre on any leg whose account is
not INCOME or EXPENSE, naming the leg. The invoice journal tags only its
purchase-expense legs: input tax and the payable are balance-sheet legs and are
never tagged, and the round-off leg (an expense account) is left unallocated
because a rupee of rounding belongs to no process.

**Why.** A cost centre answers "where was this earned or spent". A payable, a
bank balance or a tax credit has no answer to that, and tagging one would put
balance-sheet movement on a cost-centre statement.

**Where.** `src/modules/finance/models.py` - `COST_CENTRE_ACCOUNT_TYPES`;
`src/modules/finance/service.py` - `assert_cost_centre_legs`.

**Impact if wrong.** If capital spend should be tracked by centre (an asset leg
on a capex project), widen `COST_CENTRE_ACCOUNT_TYPES` - the statement filters
on the same constant, so the two cannot disagree.

---

## 45. The cost-centre tree is as deep as the department tree, and active only under active

**Assumed.** `MAX_COST_CENTRE_DEPTH` equals `MAX_DEPARTMENT_DEPTH` (six levels),
and the depth checked is the deepest descendant's, so moving a centre cannot
push its children past the limit. An active centre may not sit under an
inactive one: creating, reparenting or activating into that state is refused.

**Why.** A plant > production > process > cell breakdown is four levels, and
the department tree it usually shadows has the same bound - defined as equal so
the two cannot drift apart. An active child under an inactive parent would keep
offering a branch the business switched off.

**Where.** `src/modules/organization/service.py` - `MAX_COST_CENTRE_DEPTH`,
`assert_parent_allowed(subtree_height=...)`, `_assert_parent_active`.

**Impact if wrong.** Raise the constant; it is not a database constraint. The
department rule does not yet count descendants on a reparent - a known gap in
that older code, left unchanged here.

---

## 46. The cost-centre statement reads posted history, with one sign

**Assumed.** The statement (`COST_CENTRE_EXPENSE`) sums `ledger_entries` per
(cost centre, account) over a required period, for INCOME and EXPENSE accounts.
`net_amount` is debit minus credit on every row - positive is cost, negative is
income. A retired centre's postings still appear. There is no roll-up of
children into parents, and `search` matches the centre's code or name. It is
never cached.

**Why.** The ledger is the one place every route money takes - invoices, direct
journals, reversals - is recorded, so a statement read from it agrees with the
accounts by construction. One sign lets a reader add the column down a centre
and get its net cost.

**Where.** `src/modules/reports/repository.py` - `cost_centre_expense`;
`src/modules/reports/service.py` - `REPORT_CATALOGUE[COST_CENTRE_EXPENSE]`.

**Impact if wrong.** A roll-up needs the subtree walk (`ancestor_ids` already
exists) folded into the query; a per-class sign is a change to `net_amount` and
to its description, not to the grouping.

---

## 47. The company's bank details are validated for shape and audited masked

**Assumed.** The bank details printed on invoices are optional; the IFSC is
checked for shape only (`^[A-Z]{4}0[A-Z0-9]{6}$`, upper-cased, spaces removed);
the account number loses its spaces and is otherwise free text up to 34
characters. Anyone who may read the company profile reads the full number - it
is printed on every invoice the company sends - while the audit trail records
it masked to its last four characters, with an explicit flag when it changed.

**Why.** Only the RBI's register can say a branch exists. Redirecting where
customers pay is how invoice-redirection fraud starts, so the change must be
provable without the audit log becoming a second copy of the number - the rule
the vendor master already applies. Two numbers ending alike mask alike, which
is why the change is also flagged.

**Where.** `src/common/banking.py` (shared with the vendor master);
`src/modules/organization/schemas.py` - `CompanyUpdate`;
`src/modules/organization/service.py` - `_company_snapshot`.

**Impact if wrong.** If the full number must be hidden from some readers, mask
it in `CompanyRead` for principals without `company.manage`, as the vendor read
model does with `bank_account_number_masked`.

---

## 48. Who may see and manage cost centres

**Assumed.** `cost_centre.view` and `cost_centre.manage` are new permissions in
the Organisation group. The ACCOUNTS preset holds both; the purchase and
production presets and PLANNER hold view. A deployed system role (SUPER_ADMIN,
ADMIN) gets both through the historical data migration `e1eed3775093`; a
client-owned role on an existing deployment does not, and is granted from
Settings -> Roles. The baseline carries the schema, not that data step: existing
databases are stamped after verification, and fresh databases get the current
preset when they are seeded.

**Why.** The centre is the analytic dimension of the ledger, so its master is
the accounts team's to keep; buyers and the shop floor name centres on their
documents and need to see them. Client-owned roles are never rewritten from a
preset - the `2f5e08c7ba61` precedent.

**Where.** `src/core/permissions.py`; the historical system-role grant is
`e1eed3775093`, preserved at tag `pre-migration-squash-20261019`. Existing
databases are stamped, so it is not replayed; fresh databases get the current
preset when seeded.

**Impact if wrong.** Move the strings between presets; existing roles change
only through Settings -> Roles.

---

## 49. Only someone who may see the cost centres names one on a document

**Assumed.** The cost-centre picker on a production order, job-work order,
stock adjustment, vendor invoice (header and line), department and work centre
is shown only to an account holding `cost_centre.view`. Without it a new
document is raised unallocated, and an edit sends back exactly the centre the
record already carries - never the null that would clear it. A vendor invoice
line's picker starts on "As the invoice", which is the null that makes the line
follow the header at approval.

**Why.** `GET /organization/cost-centres` enforces `cost_centre.view`, so a
picker shown without it could only ever be empty, and an empty picker on an edit
reads as "unallocated" and saves that way. Hiding the control while carrying
the value through keeps an edit by, say, a storekeeper from quietly releasing a
work centre's cost pool or a bill's allocation.

**Where.** `apps/web/src/features/admin/cost-centre-select.tsx` and each form
that renders it; the payload builders in `organisation-dialogs.tsx`,
`routing-dialogs.tsx`, `production-dialogs.tsx`,
`job-work-order-form-dialog.tsx`, `movement-dialogs.tsx` and
`vendor-invoice-form-dialog.tsx`.

**Impact if wrong.** If every write-off must carry a centre, grant
`cost_centre.view` to the STORE_KEEPER role (or the client's equivalent) - the
storekeeper preset does not hold it today, so adjustments raised there are
unallocated. Making a centre mandatory on a document is a backend rule, not a
picker setting (§41).

---

## 50. A line is bought under the nearest category its vendor is qualified for

**Assumed.** When a requisition is split into purchase orders, each line's
*procurement category* is the nearest ancestor-or-self of the item's category
that appears in the resolved vendor's `vendor_categories`; if none does, it is
the item's own category, and an uncategorised item has none. One order is raised
per (vendor, procurement category, warehouse). A purchase order's
`item_category_id` is always derived, never accepted from a client: the group's
category on conversion, and on a buyer's own order the one category every line
resolves to against the order's vendor, or NULL when the lines differ (a line
with no category counts as a category of its own). Existing orders were
backfilled with the item's own category where every line shares one.

**Why.** That is the granularity at which the vendor is engaged, and it is the
same set a vendor manager's `allowed_category_ids` must be drawn from - so the
order a manager is scoped to and the order a manager is assigned can never be
cut at two different levels of the category tree. Matching is by subtree: a
manager scoped to Handles sees and is assigned Lever Handles orders.

**Where.** `src/modules/procurement/service_rules.py` -
`resolve_procurement_category`, `single_order_category`, `scope_covers_category`;
`src/modules/procurement/service.py` - `_order_category`, `_plan_conversion`;
the schema is part of baseline migration `20261021_0100`. The historical data
backfill (`159ffb5b9cb4`) is preserved at tag `pre-migration-squash-20261019`
and is not replayed, because existing databases are stamped and fresh databases
have no rows to backfill.

**Impact if wrong.** If Atcraft wants one order per item category regardless of
how the vendor is qualified, `resolve_procurement_category` returns the item's
own category and nothing else changes; vendor managers would then need scopes at
that finer level.

---

## 51. Vendor resolution order, and an unresolved line refuses the conversion

**Assumed.** A converted line's vendor is the first of: the buyer's per-line
override, the requisition line's `suggested_vendor_id`, the item's preferred
`item_vendor_links` row, and the preferred `vendor_categories` row of the item's
category or its nearest ancestor that has one. The vendor must also pass
`can_receive_purchase_orders`. A line with no vendor, an archived vendor, a vendor
not approved for orders, no price (§52), or an item retired, deactivated or no
longer flagged purchasable since the requisition was approved is *unresolved*,
and if any line is unresolved the whole conversion is refused - nothing is
written - with every unresolved line and every reason listed. The preview reports
the same lines without refusing, so each can be fixed or left out on screen. A
vendor a requisition line suggests must be a live vendor of the tenant when the
line is typed; the foreign key alone would accept another company's.

**Why.** The person closest to the decision wins: a buyer overriding at
conversion time knows something the master data does not, and a requester who
named a vendor did so on purpose. Refusing the whole conversion rather than
raising the resolvable part keeps a requisition from being half-ordered by
accident - the buyer can exclude a line deliberately, which leaves it open.

**Where.** `src/modules/procurement/service_rules.py` - `resolve_line_vendor`,
`preferred_category_vendor`; `src/modules/procurement/service.py` -
`_plan_conversion`, `_unorderable_item_reasons`, `convert_requisitions_to_orders`,
`_assert_suggested_vendors`.

**Impact if wrong.** If the resolvable lines should be raised and the rest left
open, `convert_requisitions_to_orders` stops refusing on `plan.unresolved` and the
result reports them instead; the requisition then stays partly ordered.

---

## 52. A converted line is priced from the override, the estimate, or the last price paid

**Assumed.** A converted line's unit price is the first of: the buyer's override,
the requisition line's `estimated_unit_price`, and
`item_vendor_links.last_purchase_price` for that item and the resolved vendor.
Never the item's `standard_cost`. A zero is a price. No price is an unresolved
line. Converted orders are raised in the company's currency
(`DEFAULT_CURRENCY`).

**Why.** `standard_cost` is what the company values the item at, not a price any
vendor offered; a purchase order printed at it would put an internal costing
figure on a document the vendor reads. A requisition carries no currency, so its
estimate is in the company's own; a last purchase price recorded in another
currency is not converted (open question 3).

**Where.** `src/modules/procurement/service_rules.py` - `resolve_line_price`;
`src/modules/procurement/service.py` - `_conversion_order_payload`.

**Impact if wrong.** If a price list or the last quoted price should rank above
the requester's estimate, it is one more source in `resolve_line_price`. Imports
need the currency question answered first.

---

## 53. Converted purchase orders are raised as DRAFT

**Assumed.** A conversion raises its purchase orders in DRAFT, with
`source_type = PURCHASE_REQUISITION` (and `source_id` the requisition when all of
an order's lines come from one). Each goes through submit, approve and send like
an order a buyer typed. The requisition moves APPROVED to CONVERTED, and only
`POST /purchase-requisitions/{id}/close` (right `purchase_requisition.approve`,
the right the state machine declares) takes it on to CLOSED.

**Why.** Conversion is a buyer's convenience, not an approval. An order that
skipped approval because a requisition was approved would let one sign-off commit
money twice, at a price nobody has looked at.

**Where.** `src/modules/procurement/service.py` - `convert_requisitions_to_orders`,
`_create_purchase_order`; `src/modules/procurement/router.py` - `close_requisition`.

**Impact if wrong.** Auto-submitting the converted orders is one transition per
order at the end of the conversion; auto-approving them would need a value
threshold (open question 2).

---

## 54. The right that makes a vendor user an order's assignee is `vendor_portal.respond`

**Assumed.** A vendor user is eligible to be assigned a purchase order when they
are active, live, bound to the order's vendor and - after their roles, direct
grants and denials are folded exactly as the request path folds them - hold
`vendor_portal.respond`, the right the portal's accept, decline and date
actions require (WP C1a; it was `purchase_order.update` until the portal had
rights of its own). It is one named constant, `VENDOR_PO_RESPOND_PERMISSION`.

**Why.** An order assigned to somebody who cannot act on it is an order nobody at
the vendor answers. Folding the rights with the same function as
`core.deps._resolve_permissions` means "may act" cannot mean two things.

**Where.** `src/modules/procurement/service_rules.py` -
`VENDOR_PO_RESPOND_PERMISSION`, `is_eligible_assignee`;
`src/core/permissions.py` - `resolve_role_grants`.

**Impact if wrong.** Another right replaces it by changing the constant.

---

## 55. An ambiguous assignee leaves the order unassigned

**Assumed.** The vendor user an order goes to is: the one eligible user scoped to
the order's category or an ancestor of it; if several are, nobody (AMBIGUOUS); if
none is, the one unrestricted eligible user (empty `allowed_category_ids`); if
there are several unrestricted users, nobody; otherwise nobody. An order with no
category matches unrestricted users only. The choice is made at conversion and,
when an order reaches Send with no assignee, at Send; a named assignee is
validated on create, update and Send (tenant, active, same vendor, the right, and
category or unrestricted). An edit that moves an order out of its assignee's
categories clears the assignee rather than refusing the edit. Likewise, an
assignee already on the order who can no longer take it when it is sent -
deactivated, re-scoped or stripped of the right since it was raised - is replaced
by the rule's choice (possibly nobody) rather than refusing the send, unless the
buyer names somebody, who is validated as usual.

**Why.** Guessing between two equally scoped managers would put the order in one
person's queue for a reason nobody chose. The buyer picks at Send, where the
choice is visible and the picker (`GET /purchase-orders/{id}/vendor-assignees`)
offers exactly the users Send accepts. An APPROVED order cannot be edited, so
refusing a stale assignee at Send would leave it with no way to the vendor.

**Where.** `src/modules/procurement/service_rules.py` - `choose_vendor_assignee`,
`may_take_order`; `src/modules/procurement/service.py` -
`_assert_vendor_assignee`, `_send_assignee`, `list_vendor_assignees`.

**Impact if wrong.** A tie-break (least open orders, a named primary manager) is
a change to `choose_vendor_assignee` alone.

---

## 56. A vendor sees a purchase order only once it has been sent

**Assumed.** A vendor-portal principal sees its own vendor's orders in SENT and
every later status, and a CANCELLED order only if it had been sent first
(`sent_at` set). DRAFT, PENDING_APPROVAL, APPROVED and REJECTED are "not found".
A vendor user with a non-empty `allowed_category_ids` additionally sees only
orders whose `item_category_id` is one of those categories or beneath one, plus
any order assigned to them by name. An empty list is unrestricted. The scope sits
in `PurchaseOrderRepository._scoped`, so every read and lock inherits it.

**Why.** Before it is sent an order is the buyer's working document, with prices
nobody has approved; showing it to the vendor discloses a negotiating position.
The by-name exception keeps an order visible to the manager it was assigned to
even if their scope is narrowed afterwards.

**Where.** `src/modules/procurement/models.py` - `VENDOR_VISIBLE_PO_STATUSES`;
`src/modules/procurement/repository.py` - `PurchaseOrderRepository._scoped`,
`category_within`.

**Impact if wrong.** If vendors should see approved orders before they are sent,
APPROVED joins the tuple. Section 33 and open question 5 cover what a vendor may
do to the orders it sees.

---

## 57. An overdue requirement is asked for on the order date

**Assumed.** A converted line asks for delivery on its requisition need date (the
line's, else the requisition's), but never before the order date; the order's
expected delivery date is the buyer's, else the earliest date among its lines.
The order's priority is the buyer's, else the most urgent among its
requisitions.

**Why.** A requirement already overdue is wanted as soon as possible, and an
order cannot promise a delivery before it was raised - the database refuses an
expected date before the order date.

**Where.** `src/modules/procurement/service_rules.py` - `conversion_delivery_date`;
`src/modules/procurement/service.py` - `_group_expected_date`, `_group_priority`.

**Impact if wrong.** If the order date plus the vendor's lead time is the better
floor, `conversion_delivery_date` takes the vendor's lead time as a third input.

---

## §E8-1. Putaway posts immediately inside one warehouse

**Assumed.** Saving a bin move posts paired TRANSFER_OUT and TRANSFER_IN
movements, carrying the exact FIFO layers in one transaction. It creates no
in-transit stock. NULL means unbinned stock, and source and destination must
differ. A posted move is corrected by another move. Warehouse transfers retain
their existing lifecycle.

**Why.** The engine already accepts a same-warehouse relocation as a transfer
pair without a transit pool, so a new BIN_MOVE type would duplicate that path.

**Where.** `src/modules/inventory/service.py` - `BinMoveService.create`.

**Impact if wrong.** A dedicated BIN_MOVE_OUT/IN pair belongs in the transaction
vocabulary, with the sign table and every test that enumerates types.

## §E8-2. Scrap keeps its historical cost in a separate FIFO queue

**Assumed.** Scrap is outside on-hand but remains company-owned until disposed
of. It keeps the receipt costs consumed when it was scrapped, independently of
later receipts. Legacy scrap is reconstructed at its recorded blended scrap
cost, falling back to standard cost only where history is uncovered; exact
historical FIFO order cannot be recovered where it was never stored. The
migration verifies that the reconstructed queue covers every scrap position. A
disposal consumes the oldest scrap costs and does not change on-hand valuation.

**Why.** Scrap was written off the on-hand books when it was scrapped. Repricing
it from a later receipt would invent a cost the ledger never charged.

**Where.** `src/modules/inventory/service.py` - scrap layer draw and
`ScrapDisposalService`; the schema is part of baseline migration
`20261021_0100`. The historical data backfill
(`20261012_1703_backfill_scrap_cost_layers`) is preserved at tag
`pre-migration-squash-20261019` and is not replayed.

**Impact if wrong.** If scrap should be valued at current standard cost, the
SCRAP queue and the backfill both go.

## §E8-3. Internal issues use the existing stock-adjustment approval value

**Assumed.** The named INTERNAL_ISSUE_APPROVAL_VALUE is 25,000 in company
currency, measured against FIFO cost rather than sale price or net variance. A
larger issue needs submission and a separate approver, with the existing
superuser exception. Posting rechecks the actual cost and rolls back entirely
if approval is needed. A low-value draft posts directly.

**Why.** Stores issues are the same class of uncontrolled stock leaving the
warehouse as a write-off, so they share that threshold until the business names
another.

**Where.** `src/modules/inventory/service.py` - `INTERNAL_ISSUE_APPROVAL_VALUE`,
`InternalIssueService`.

**Impact if wrong.** Change the constant. Confirm the amount before production
use.

## §E8-4. Disposal proceeds describe the disposal

**Assumed.** WRITE_OFF realises zero; money received belongs to SALE or RECYCLE.
Buyer or reference is optional. These documents record stock and disposal
evidence; receivable, GST invoice and general-ledger automation are not inferred
from an optional buyer string. Disposal proceeds are visible with the document,
while FIFO unit and total costs require the existing cost-view rights.

**Why.** A disposal is an inventory fact. Turning an optional buyer string into
an invoice would post money nobody asked to recognise.

**Where.** `src/modules/inventory/schemas.py` - `ScrapDisposalCreate`;
`src/modules/inventory/service.py` - `ScrapDisposalService`.

**Impact if wrong.** A sale of scrap that must raise a receivable needs a finance
document of its own, fed by the disposal rather than inferred from it.

## §E8-5. The scrap capability controls entry, not clearing existing scrap

**Assumed.** Every resolved target SCRAP requires can_scrap, including
inspection and vendor losses. An existing scrap position can still be disposed
of after the capability is disabled. Damaged stock is the QC alternative when
material cannot be scrapped. SCRAP_DISPOSAL leaves the scrap pool; it does not
transfer scrap back into useful stock. Serial-tracked operation documents wait
for E8b's identity support.

**Why.** Turning the capability off must stop new scrap. It must not trap stock
that is already scrap, because nothing else can address that pool.

**Where.** `src/modules/inventory/service.py` - `validate_movement`; quality and
job-work paths that resolve a SCRAP target.

**Impact if wrong.** If disposal should also be refused, the disposal check
reads can_scrap. If scrap may return to available stock, that is a different
movement, not a disposal.

## §E8-6. An item default bin belongs to its default warehouse

**Assumed.** The optional default bin must be live, active and tenant-owned in
the default warehouse. Pickers prefill it only for that warehouse. Clearing it
is respected. A live item's default bin cannot be deactivated or retired,
including by retiring its warehouse; reassign or clear the default first.
Unchanged saved assignments do not become new assignments during an unrelated
item edit.

**Why.** A default bin in another warehouse would prefill a position the receipt
cannot use. Retiring a bin that items still name would leave every picker
pointing at a location that cannot take stock.

**Where.** `src/modules/products/service.py` - default-bin validation;
`apps/web/src/features/inventory/location-picker.tsx`.

**Impact if wrong.** If the default bin may sit in any warehouse, drop the
same-warehouse check and prefill whenever the picker is in that bin's warehouse.

## §E8-7. Incoming inspection receives at the unbinned position

**Assumed.** The current inspection record has no bin, so its decision releases,
damages or scraps warehouse-level stock. A GRN line requiring QC therefore
cannot receive into a bin; otherwise the decision could never find its held
quantity. The receipt dialog explains this. Accepted material is put away using
a bin move.

**Why.** A QC decision addresses the position the receipt wrote. A binned hold
with an unbinned decision would leave the held quantity where nothing can
release it.

**Where.** `apps/web/src/features/procurement/grn-receive-dialog.tsx`; goods
receipt posting when `requires_qc` is set.

**Impact if wrong.** If inspection records gain locations later, the receipt and
the decision must carry the same position together.

---

## §E4-1. Stranded QC owners are repaired by a sweep, not by a migration

**Assumed.** Purchase orders, receipt lines, assembly orders and job-work lots
that a quality decision stranded before the owner callback existed are brought
up to date by the idempotent maintenance sweep
`maintenance.reconcile_quality_owners` - run once when beat starts and then
daily - acting as the system actor with full audit rows, rather than by a data
migration.

**Why.** Pure SQL in a migration cannot write the audit rows and status-change
history a repair owes; the sweep replays each decided inspection through the
same owner handlers a live decision calls, in its own transaction per
inspection, so one refusal is logged and retried next sweep instead of undoing
every other repair.

**Where.** `src/modules/quality/owners.py` - `reconcile_owners`;
`src/modules/quality/service.py` - `reconcile_quality_owners`;
`src/workers/tasks/maintenance.py` - `reconcile_quality_owners`;
`src/workers/celery_app.py` - beat schedule and the start-up hook.

**Impact if wrong.** If repairs must be one-shot rather than eventual, the
sweep still converges on the first beat start after deploy; removing it needs a
migration that writes audit rows by hand.

---

## §E4-2. A receipt line passes only when all of it was accepted

**Assumed.** A goods receipt line goes PASSED only when everything the
inspection covered was accepted, and FAILED when any of it was refused
(rejected or scrapped) - however small the refused share. While anything is
still held for rework or a further decision the line stays PENDING, and so does
the purchase order.

**Why.** A line that owes the vendor a return must not read as a clean pass on
the receipt; the refused share is what the return and the debit are worked
from.

**Where.** `src/modules/procurement/service_rules.py` -
`receipt_line_qc_status`; `src/modules/procurement/service.py` -
`record_incoming_inspection_outcome`.

**Impact if wrong.** If a tolerance applies (a 2% fallout still passes), the
rule takes a threshold per item group instead of zero.

---

## §E4-3. The owner-side verdict never moves stock, and never contradicts the inspection

**Assumed.** `POST /assembly/{id}/qc-result` and `POST /job-work/{id}/qc-result`
serve only holds no quality inspection covers. When the hold names an
inspection, that inspection is the one release path: while it is open the call
is refused and pointed at it, and once it is decided the order completes from
the inspection's own figures - which the request must repeat - with no second
movement. Quality already moved the stock when it decided.

**Why.** Two release paths for one hold was the defect: whichever ran second
either failed for want of held stock or released another lot held at the same
grain, and the document stayed stranded either way.

**Where.** `src/modules/assembly/service.py` - `record_qc_result`,
`_settle_from_inspection`; `src/modules/job_work/service.py` -
`record_qc_result`, `_settle_from_inspections`.

**Impact if wrong.** If an owner must be able to overrule a decided inspection,
that is a new inspection decision (rework/hold), not a second verdict on the
old one.

---

## §E4-4. An assembly order is built from an ASSEMBLY or a PRODUCTION structure

**Assumed.** The order's recipe is an approved ASSEMBLY or PRODUCTION bill of
materials - a lock set whose recipe engineering filed as a production BOM is
still put together on the assembly bench. A PHANTOM is never built, and an
ALTERNATE is reached only by naming it as a production structure's
alternative, so neither is offered; omitting the BOM builds from the item's
default approved structure of those two types.

**Why.** Refusing a production-typed recipe would push the planner into a
production order for what is assembly work, while building a phantom would
explode stock that was never meant to be stocked.

**Where.** `src/modules/assembly/service.py` - `ASSEMBLABLE_BOM_TYPES`,
`_resolve_bom`.

**Impact if wrong.** If alternates must be choosable directly, the select takes
them with the default-resolution falling back to the assembly/production
default.

---

## §E4-5. A component top-up states new totals and may only raise them

**Assumed.** `issue_components` from RELEASED is the first issue; while the
order is COMPONENTS_ISSUED or IN_PROGRESS it is a top-up that states each
line's new running total - like the first issue does, never a delta - bounded
by the reserved requirement. Totals may only go up, and a top-up that would
change nothing is refused rather than recorded as an empty hand-over.

**Why.** Parts already on the bench are not handed back through this path, and
an empty hand-over would write audit history for nothing moved; assigning (not
adding) keeps a retry that repeats the bench state idempotent.

**Where.** `src/modules/assembly/service.py` - `plan_issue`, `issue_components`.

**Impact if wrong.** If the bench must return parts to the store, that is a
separate return movement, not a negative issue.

---

## §E4-6. A sub-assembly starts as semi-finished, an assembly as a finished good

**Assumed.** The SUB_ASSEMBLY item group seeds the semi-finished capability
defaults (built in-house, consumed by a higher assembly or a production
order) and ASSEMBLY seeds the finished-good defaults (made on the bench and
sold). Like every group they only seed defaults at creation; no rule branches
on the value.

**Why.** A sub-assembly is exactly what a semi-finished item is, and an
assembly (a lock set) is exactly a finished good that happens to be built on
the bench rather than on the production floor.

**Where.** `src/modules/products/service.py` - `default_capabilities`;
`src/modules/products/models.py` - `ItemGroup`.

**Impact if wrong.** If either group needs different flags, the mapping is one
branch; existing items keep the flags they were created with.

---

## §E4-7. A component reservation is wanted by the planned start, as a UTC date

**Assumed.** Every component reservation taken at release carries the order's
priority and its planned start as the required date, read as a UTC business
date; an order with no planned start reserves with no date, as it always did.

**Why.** Reallocation weighs an assembly's claim on a part against sales and
production demand, and without the order's urgency and date on the reservation
an urgent lock-set build is indistinguishable from a speculative one.

**Where.** `src/modules/assembly/service.py` - `reservation_required_date`,
`release_order`.

**Impact if wrong.** If dates must follow the planner's timezone rather than
UTC, the conversion moves to the site timezone with the same fallback.

---

## §E4-8. A packing order from an assembly packs its output, for its customer

**Assumed.** `POST /packing/from-assembly` packs the assembled item from the
warehouse the output was received into, for the assembled quantity unless a
smaller one is named (never more). Customer and sales order come from the
assembly's source when it was raised for a sales order - a named customer must
then be that customer, and a named sales order that order - otherwise the
customer must be named. The sales-order *line* is not guessed.

**Why.** Guessing the line would attach the packing to the wrong promise when
an order has several; the packer names it on the packing order itself, where
the choice is visible.

**Where.** `src/modules/packing/service.py` - `create_order_from_assembly`.

**Impact if wrong.** If one assembly always covers exactly one sales-order
line, the handover can carry it once the source records the line.

---

## §E4-9. A job-work order waits on inspection only while a lot is held

**Assumed.** A fully received order moves to QC_PENDING only when at least one
returned lot is still on QC hold; lots inspected and released while the order
was still partly at the vendor need nothing more. An order sent nowhere held -
or held with nothing left to decide - stays RECEIVED and is closed like one
that never needed inspecting, because no decision would ever release a hold
that does not exist.

**Why.** QC_PENDING has exactly one way out - the inspection verdict - so
entering it with nothing held strands the order with real stock no verdict can
release.

**Where.** `src/modules/job_work/service.py` - `receive`.

**Impact if wrong.** If every QC order must show a verdict even when nothing
was held, the direct verdict endpoint records a zero hold instead of refusing.

---

## §E4-10. The board calls an order late once its plan has slipped

**Assumed.** An open order is late once its planned end has passed while it
still owes finished stock - QC_PENDING included, because held output is not
yet usable - and its start is overdue once the planned start has passed with
the bench not begun. An order with no plan is never late. Days late are whole
days past the planned end.

**Why.** The board is read at a glance on the floor; a held order shown as on
plan would hide the exact backlog quality is sitting on.

**Where.** `src/modules/assembly/service.py` - `lateness`; `src/modules/assembly/models.py` - `NOT_STARTED_STATUSES`.

**Impact if wrong.** If held output counts as delivered for lateness, QC_PENDING
leaves the late set while keeping its badge.

---

## §B2-1. A shift in future use cannot be re-timed or retired

**Assumed.** Future PLANNED assignments must be cancelled before changing times, breaks, deactivating or retiring a shift. Name and sort order changes do not change a window.

**Why.** A plan must keep resolving to the window somebody assigned.

**Where.** `routing.service.ShiftService._assert_off_the_roster`.

**Impact if wrong.** Cancel the affected plan, then retire and replace the shift.

---

## §B2-2. One operator per machine and one window per operator

**Assumed.** Overlapping PLANNED windows are refused for the same operator across all cells and for the same non-null machine. Cell-level assignments permit multi-machine tending.

**Why.** Double-booking cannot be a usable staffing plan.

**Where.** `routing.models.ShiftAssignment; routing.service.ShiftRosterService.plan`.

**Impact if wrong.** Helpers need an explicit capacity or staffing model before the machine exclusion can change.

---

## §B2-3. Legacy shift text keeps its history

**Assumed.** Existing shift text retains NULL shift_id. New inputs select a governed shift and snapshot its immutable code; clearing clears both.

**Why.** Legacy codes carry no start or end times. Creating them would invent a rule.

**Where.** `production.models; routing.service.require_shift`.

**Impact if wrong.** An administrator-approved audited linking action can connect legacy codes to an established master later.

---

## §B2-4. The roster is advisory

**Assumed.** Bookings and work-order assignments outside the selected operator/shift/cell/date roster warn and remain accepted.

**Why.** A booking is an actual fact; refusal would hide production that happened.

**Where.** `production.service.ROSTER_MISMATCH_WARNING; production dialogs`.

**Impact if wrong.** Make enforcement a separately approved policy rather than altering historical bookings.

---

## §B2-5. The roster date is local and windows are snapshots

**Assumed.** roster_date is the company-local date the shift starts. Times use companies.timezone; UTC start/end snapshots never move after a timezone change. Half-open windows admit back-to-back shifts.

**Why.** A night shift belongs to its starting day and a changed timezone cannot rewrite the plan.

**Where.** `routing.service.shift_window; routing.models.ShiftAssignment`.

**Impact if wrong.** Timezone and daylight-saving policy changes need an explicit reviewed rescheduling operation.

---

## §B2-6. Shift codes are immutable

**Assumed.** Codes are normalised, immutable and never reused after retirement.

**Why.** Production rows preserve the code that identified their shift.

**Where.** `routing.schemas.ShiftUpdate; uq_shifts_company_id_code`.

**Impact if wrong.** Introduce separately audited aliases if the business needs code changes.

---

## §B2-7. Rostered capacity sits beside calendar capacity

**Assumed.** Machine utilisation adds net PLANNED window minutes and busy/rostered percent. Existing calendar-days times working-hours capacity remains unchanged.

**Why.** A new plan cannot silently restate existing capacity figures.

**Where.** `reports.repository.machine_utilisation; reports.service._derive_machine_utilisation`.

**Impact if wrong.** A holiday-aware calendar needs its own business policy and report denominator.

---

## §B2-8. Existing planning and costing keep their scheduling conventions

**Assumed.** Routing run/setup minutes are chargeable; queue/move minutes are elapsed only. Work-centre working hours divide lead time, defaulting to eight. Work orders remain scheduled from the planned date in UTC, back-to-back, and calendar capacity includes every day.

**Why.** This package adds staffing plans without rescheduling existing jobs or changing costing.

**Where.** `routing.service.total_routing_minutes/resolve_working_hours_per_day; production.service.plan_work_orders; reports.service.available_minutes`.

**Impact if wrong.** Holiday, minimum-rest and shift-aware job scheduling need a separately agreed calendar policy.

---

## §B3-1. A job-work invoice is a typed vendor invoice, billed line by line

**Assumed.** A job-work bill shares payments, allocations, approval, disputes, the payables ledger and ageing with a purchase bill, and matches a job-work order and its receipt lines instead of a purchase order and a goods receipt. Every goods line names a receipt line; a free service or freight line is refused unless `JOB_WORK_ALLOW_FREE_LINES` is turned on, because an unbilled extra is how the same charges get billed twice.

**Why.** A separate invoice table would reimplement approval, payment and ageing for a document that differs only in what it is matched against.

**Where.** `finance.models.VendorInvoiceType`, `VendorInvoice.invoice_type/job_work_order_id`, `VendorInvoiceLine.job_work_receipt_line_id`, `finance.schemas` validators, `finance.service._match_job_work`.

**Impact if wrong.** Freight or service extras on a job-work bill need their own matching rule before the constant is enabled.

---

## §B3-2. The excess-loss recovery posts beside the bill, and over-billing is a hard block

**Assumed.** At approval, the still-unposted vendor debit on the invoiced receipt lines posts as Dr AP against the vendor / Cr job-work expense under `job_work_recovery`, in the same transaction as the bill. The vendor's net payable is the invoice minus the recoveries. Every what-is-owed read is net of them - the invoice summary's outstanding figure, the outstanding list, the payables ageing, the payment-allocation bound and the payment-driven status - because the recovery already reduced the payable in the ledger, and a gross figure would guide the next payment to overpay the vendor by exactly the recovery. Billing beyond a receipt line's unbilled charges is refused outright - no override reason can approve it - and is re-checked under a row lock at approval, when the money actually moves.

**Why.** The debit is money the vendor owes back for company material lost beyond allowance; netting it at payment time without posting it would leave the payable overstated and the recovery untraceable. An overridable over-bill would bill the same charges twice with a sentence attached.

**Where.** `finance.service._post_job_work_billing`, `evaluate_match` job-work branch, `approve_invoice` over-billing guard.

**Impact if wrong.** If recoveries should settle through a separate debit-note document, the recovery journal becomes that document's posting; the tracking columns stay.

---

## §B3-3. The GST rate and SAC default, and the client confirms them

**Assumed.** A new job-work order takes GST 18% and SAC 9988 (manufacturing services on others' goods) when the payload omits them. Rates vary by vendor and ruling; 18% is the ordinary rate for this SAC, not a determination.

**Why.** An order with no rate cannot produce an invoice line with one, and refusing to raise the order until somebody looks the rate up blocks real despatch-morning work.

**Where.** `job_work.models.JOB_WORK_DEFAULT_GST_RATE/JOB_WORK_DEFAULT_SAC`, `job_work.service.create_order`, `job_work.schemas` tax fields.

**Impact if wrong.** Confirm the rate per vendor; orders already raised keep the rate they were raised with until amended.

---

## §B3-4. The job-work expense account takes the next free code above 5150

**Assumed.** `JOB_WORK_EXPENSE` is provisioned at chart code 5150, inside the 5xxx expense range beside purchases; when that code belongs to a hand-built account the provisioning walks upward to the next free one. Postings resolve the system code, never the chart code, so the walk changes nothing they read.

**Why.** Fixed-code provisioning fails on any chart that already holds the preferred code, and the failure arrives as a unique-constraint error at the first approval.

**Where.** `finance.service.DEFAULT_CHART`, `pick_free_chart_code`, `ensure_system_accounts`.

**Impact if wrong.** A chart numbering plan that reserves the 515x range needs its own preferred code per company.

---

## §B3-5. Cancelling a posted bill reverses its journals

**Assumed.** Any cancel of an invoice with posted journals reverses each one - the bill and, on a job-work invoice, the recovery beside it - and rolls the job-work tracking back, in the same transaction. Cancelling before approval moves nothing and rolls nothing back.

**Why.** Cancelling without reversing leaves the payable and the input GST on the ledger while the invoice vanishes from ageing and the GST summary, which is the defect this package fixes.

**Where.** `finance.service.cancel_invoice`, `VENDOR_INVOICE_LIFECYCLE` APPROVED to CANCELLED edge.

**Impact if wrong.** A bill cancelled in error needs re-raising; the reversed journals are the trail.

---

## §B3-6. Vendor-submitted job-work bills are a follow-up, not this package

**Assumed.** Vendor-portal invoice submission stays purchase-order-only. A vendor-submitted job-work bill - matched to an order the vendor can see through the portal scope - is recorded here as a follow-up.

**Why.** The portal scope and submission flow were built around purchase orders; extending them to job-work orders needs its own scoping review.

**Where.** This section only; no code.

**Impact if wrong.** Staff continue to key job-work bills from the job-work screen until the portal flow is extended.

---

## §E1-1. A reservation caps order-linked dispatch

**Assumed.** Only a live reservation can back an order-linked dispatch. Other open consignments reduce the order-line balance before a new plan is accepted.

**Why.** The reservation is the stock promise; free stock must not bypass it.

**Where.** `sales.dispatchable_quantity; dispatch validation and departure.`

**Impact if wrong.** Allowing unreserved shipment would need a different allocation contract.

---

## §E1-2. Stock arrivals top up short orders on a bounded schedule

**Assumed.** The default top-up cadence is 900 seconds, at most 200 short orders per company, ranked by priority, required date, order date and id. Each order commits separately.

**Why.** Arrivals flow to waiting demand without coupling GRN, production or transfers to sales.

**Where.** `SO_TOPUP_INTERVAL_SECONDS; maintenance.allocate_short_orders.`

**Impact if wrong.** A shorter cadence or explicit event-driven wake-up can be added without changing reservations.

---

## §E1-3. Quantity growth restores the shortage state

**Assumed.** An ALLOCATED order returns to CONFIRMED when quantity changes leave any live shortfall.

**Why.** Allocated must mean fully covered; closed reservations count as zero.

**Where.** `sales._update_confirmed and SALES_ORDER_LIFECYCLE.`

**Impact if wrong.** Any different meaning for ALLOCATED would require retuning dispatch and planning gates.

---

## §A2-1. An invoice may precede removal

**Assumed.** A SALES dispatch may issue a tax invoice once ready, or afterwards. Invoice date defaults to dispatch date and cannot precede the SO date or enter a closed year.

**Why.** CGST Act section 31 places the tax invoice at or before removal.

**Where.** `sales_invoice_service.preview_sales_invoice.`

**Impact if wrong.** Different statutory timing requires explicit workflow changes.

---

## §A2-2. Issue and number allocation are atomic

**Assumed.** There is no draft invoice; preview persists nothing, and the number is allocated inside the issuing transaction. Cancelled numbers remain on cancelled invoices.

**Why.** Failed issues must not consume numbers and issued history must not be renumbered.

**Where.** `issue_sales_invoice and next_document_number.`

**Impact if wrong.** A separate approval stage would need a reserved-number policy.

---

## §A2-3. Sales numbering fits the GST limit

**Assumed.** The SI prefix and five-digit pad produce a 16-character financial-year number. Existing sales_invoice sequences stop migration for review.

**Why.** GST Rule 46 constrains the serial number length.

**Where.** `DocTypes.SALES_INVOICE and invoice schema migration.`

**Impact if wrong.** Any previous series needs a deliberate migration instead of silently changing its prefix.

---

## §A2-4. Unpaid cancellation is time bounded

**Assumed.** Only ISSUED invoices with no allocations or credit notes can cancel within 24 hours of issued_at. Closed sales orders require a credit note.

**Why.** The window mirrors e-invoice cancellation and prevents reversing settled or closed business.

**Where.** `INVOICE_CANCEL_WINDOW_HOURS and cancel_sales_invoice.`

**Impact if wrong.** Different filing rules require a configuration or credit-note workflow.

---

## §A2-5. Invoice totals round to rupees

**Assumed.** The total rounds HALF_UP to Decimal(1), with an explicit ledger round-off leg. A rounded total must be positive; zero-value supplies need a separate free-of-charge document. Flat discounts share pro rata, and the final invoice takes the remainder.

**Why.** Printed totals, journals and partial invoices must reconcile exactly.

**Where.** `ROUND_INVOICE_TOTAL_TO, apportion_discount and sales_invoice_journal_lines.`

**Impact if wrong.** Different rounding precision needs coordinated statutory and ledger changes.

---

## §A2-6. Statutory invoice records never expire

**Assumed.** Invoice job snapshots and REPORT documents have no expiry; the header retains immutable bank and terms snapshots too.

**Why.** An issued statutory document must remain reproducible after masters change.

**Where.** `queue_sales_invoice_pdf, sales_invoice_print_data and reports.generate_sales_invoice.`

**Impact if wrong.** Any archive policy must preserve document retrieval and evidence.

---

## §A2-7. An invoice requires agreed source pricing

**Assumed.** An unlinked free-stock SALES dispatch cannot issue this invoice until linked to a priced sales order. Its existing stock movement behavior remains available.

**Why.** The issue payload has no pricing inputs, so current item prices cannot safely replace an agreed order price.

**Where.** `preview_sales_invoice source linkage guard.`

**Impact if wrong.** A true cash/direct-sale invoice needs an explicit agreed-price entry flow.

---

## §A2-8. A pre-removal invoice temporarily counts staged goods

**Assumed.** For READY_FOR_DISPATCH, invoiced quantity is capped by dispatched quantity plus this dispatch staged quantity, itself rechecked against the live reservation. Order INVOICED still requires actual departure.

**Why.** The specification permits pre-removal invoicing but the departure counter has not moved yet.

**Where.** `preview_sales_invoice and dispatch._advance_sales_order.`

**Impact if wrong.** If invoices must be post-departure only, remove READY eligibility and this staged cap.

---

## §A3-1. Legacy no-QC serial returns were accepted

**Assumed.** Existing RECEIVED or CLOSED serial-return lines with
`requires_qc = false` were already treated as accepted by the former workflow.
The serial-disposition migration backfills those serials into
`accepted_serial_ids`; draft returns are not backfilled.

**Why.** The former receive path posted those serials directly into usable stock
and could issue the corresponding credit immediately. Reclassifying that
history as pending QC would contradict both the ledger and existing credits.

**Where.** Baseline migration `20261021_0100` (historically
`20261018_0100_sales_return_unit_dispositions`; its data backfill is preserved
at tag `pre-migration-squash-20261019`).

**Impact if wrong.** Review historical no-QC serial return documents before
deploying the migration; incorrect backfill would require correcting the
disposition sets and associated stock state.

---

## §C1-1. The vendor's committed delivery date is kept apart from the buyer's planning date

**Assumed.** When a vendor accepts an order it may commit to a delivery date, and
may later move it (`POST /vendor-portal/purchase-orders/{id}/delivery-date`).
Both write `purchase_orders.vendor_committed_date`. Neither touches
`expected_delivery_date`, the buyer's planning date. A buyer adopts the vendor's
date explicitly with `POST /procurement/purchase-orders/{id}/accept-vendor-date`,
which is audited and a no-op when the two already agree. The committed date may
not precede the order date.

**Why.** MRP nets supply on `expected_delivery_date` and the delay flag measures
lateness against it. A vendor who could rewrite it could make its own late
order look on time and move the company's plan without anyone deciding to
(trap T8 in the procurement report).

**Where.** `src/modules/vendor_portal/service.py` - `accept`, `change_date`;
`src/modules/procurement/inbound_service.py` - `accept_vendor_date`.

**Impact if wrong.** If the vendor's word should drive planning directly, `accept`
and `change_date` also write `expected_delivery_date` and the adopt route goes.

---

## §C1-2. A consignment is recorded only on an accepted order with goods still due

**Assumed.** A numbered consignment (`purchase_order_shipments`) can be recorded or
corrected only while the order is ACKNOWLEDGED or PARTIALLY_RECEIVED - by the
vendor through the portal or by a buyer on its behalf, with the same body and
rules. Consignments are numbered 1..n per order under the order's row lock. A
dispatch may not be dated before the order date or in the future, and a
delivery may not precede its dispatch or lie in the future (five minutes of
clock skew allowed). Once an order is CANCELLED or CLOSED its consignments can
no longer change.

**Why.** Goods dispatched against an order nobody has accepted arrive against a
refusal-in-waiting. A dispatch or delivery dated tomorrow is not a fact, and the
tracking status would report goods on the road that have not left.

**Where.** `src/modules/procurement/inbound_service.py` - `SHIPPING_PO_STATUSES`,
`SETTLED_PO_STATUSES`, `DELIVERY_CLOCK_SKEW`, `record_inbound_shipment`,
`mark_delivered`.

**Impact if wrong.** If dispatch may be recorded on a SENT order (the vendor
ships without accepting), SENT joins `SHIPPING_PO_STATUSES`.

---

## §C1-3. The order's documents route is the channel shared with the vendor

**Assumed.** A file carries a `kind` (INVOICE, DISPATCH_PHOTO, LR_COPY,
DELIVERY_CHALLAN, OTHER) through a link table, `purchase_order_documents`; the
generic `documents` table is unchanged. A vendor sees and downloads only the
files filed through the order's documents route (by itself or by a buyer) and
the generated PO PDF. A file a buyer filed against the order from the generic
Documents screen stays internal. An INVOICE upload needs `vendor_portal.invoice`,
every other kind `vendor_portal.ship`. A vendor's bill names one of its own
orders, bills only items on that order, may name only its own receipts and
receipt lines on that order, carries no cost centre (staff tag centres while
reviewing) and is submitted for review at once. One uploaded invoice file
attaches to one bill.

The generated PDF is identified by its completed PURCHASE_ORDER job's document
reference, rather than the generic REPORT category. Action responses also use an
explicit vendor field list, so a requisition source link stays internal.

**Why.** A link table is the least invasive way to type a file: nothing that
reads `documents` changes. Internal attachments - a quote comparison, an
approval memo - are not the supplier's to read. A vendor cannot list cost
centres and must not name one.

**Where.** `src/modules/procurement/inbound_repository.py` -
`InboundDocumentRepository.visibility`; `src/modules/vendor_portal/service.py` -
`create_invoice`, `_assert_bill_references`, `download`, `upload_vendor_document`.

**Impact if wrong.** A file meant to stay internal but uploaded through the
order's documents route is visible to the vendor; such a file belongs on the
generic Documents screen. If a vendor may bill freight or charges against an
item not on the order, `_assert_bill_references` relaxes its item check.

---

## §C1-4. The "QC rejected" tile looks back thirty days and counts receipt lines

**Assumed.** The vendor dashboard's "QC rejected" counts the vendor's posted
goods-receipt lines with a rejected quantity whose receipt is dated within the
last `VENDOR_QC_LOOKBACK_DAYS` (30) days, on orders the account can see.
"Payment pending" counts the vendor's bills in an open status with money still
owed. Every tile is a count of documents or lines, not a sum of quantities.

**Why.** Recent enough to be actionable, long enough to cover a monthly review.
Purchase-order statuses past RECEIVED are never written by the application
(trap T7), so the tiles are derived from receipts and bills instead.

**Where.** `src/modules/vendor_portal/repository.py` - `VENDOR_QC_LOOKBACK_DAYS`,
`VendorPortalRepository.dashboard`.

**Impact if wrong.** A different window is the constant; a quantity-weighted
tile is a different aggregate in the same statement.

---

## §C1-5. The tracking status describes the latest consignment

**Assumed.** One ordered rule derives the proposal's tracking status. In order:
QC_PENDING (a receipt with an open inspection, when the order is fully received
or the receipt came after the latest dispatch); GRN_COMPLETED (fully received or
short-closed, nothing open); RECEIVED (a receipt raised at or after the latest
consignment's dispatch, or any receipt when nothing was dispatched); DELIVERED
(the latest consignment is marked delivered); DISPATCHED (it left today, UTC);
IN_TRANSIT (it left earlier); READY_FOR_DISPATCH (the vendor's ready flag);
otherwise NOT_DISPATCHED. "Delivery expected date" is the latest consignment's
own expectation, else the vendor's commitment, else the planning date. "Late"
is one predicate: receivable status, goods still due, planning date passed.

**Why.** The buyer's board, the vendor's order list and the vendor dashboard read
the same rule and cannot disagree. A receipt for an earlier consignment must not
hide the next one on the road.

**Where.** `src/modules/procurement/inbound_repository.py` -
`inbound_tracking_status`, `po_is_late`, `InboundRepository.projection`.

**Impact if wrong.** A goods receipt does not name the consignment it received,
so "after the latest dispatch" is decided by time; a receipt keyed in late for
an earlier consignment counts for the later one. Linking receipts to
consignments would make the rule exact.

---

## §C1-6. The buyer's inbound board shows open work by default

**Assumed.** `GET /procurement/inbound-tracking` lists orders still in motion: SENT,
VENDOR_DECLINED (the buyer has to decide), ACKNOWLEDGED, PARTIALLY_RECEIVED, and
RECEIVED, QC_PENDING or QC_COMPLETED orders whose tracking status is not yet
GRN_COMPLETED. Completed, closed and cancelled orders appear with
`include_completed=true`. The vendor's list always shows its whole history; its
tabs narrow it.

**Why.** A work queue that fills with history stops being read.

**Where.** `src/modules/procurement/inbound_repository.py` -
`BOARD_OPEN_PO_STATUSES`, `InboundRepository.filtered_projection`.

**Impact if wrong.** Change `BOARD_OPEN_PO_STATUSES`.

---

## §C1-7. Who hears about a vendor action or a consignment change

**Assumed.** A vendor's accept, decline, date change and ready flag, and every
consignment change - recorded, corrected, delivered, LR copy or dispatch photo
uploaded, whoever did it - notify the order's creator, every internal user
holding `purchase_order.approve` and the ADMIN and PURCHASE_MANAGER roles.
Vendor accounts never receive these, and the person who made the change is not
told about it. Everything about one consignment collapses onto one unread row
per recipient (`po-shipment:{id}`), carrying carrier, tracking, dispatch and
expected dates, boxes, weight and the latest event.

**Why.** The proposal's "admin is notified immediately", without a row per edit;
the creator and approvers own the order.

**Where.** `src/modules/procurement/inbound_service.py` - `BUYER_ALERT_ROLE_CODES`,
`buyer_recipients`, `notify_buyer`, `shipment_message`.

**Impact if wrong.** The recipient list is `buyer_recipients`; a per-company
setting would replace the role constant.

---

## §C1-8. The vendor's order detail embeds the first page of each list

**Assumed.** `GET /vendor-portal/purchase-orders/{id}` embeds the first 50
consignments, files and receipts, each with its paging metadata; the dedicated
list routes page the rest. The timeline lists status changes from SENT onwards
- drafting and internal approval are the buyer's process. Money totals are SQL
aggregates.

**Why.** One request paints a phone screen; nothing returned is unbounded.

**Where.** `src/modules/vendor_portal/service.py` - `DETAIL_EMBED_LIMIT`;
`src/modules/vendor_portal/repository.py` - `TIMELINE_STATUSES`.

**Impact if wrong.** Change the constants.

---

## §C1-9. An order's header courier fields mirror its latest consignment

**Assumed.** The single-shipment fields on the order header (`courier_name`,
`tracking_reference`, `shipped_at`) stay readable and are kept equal to the
latest numbered consignment. An order with no numbered consignment is tracked
from the header fields alone; historical header data is not turned into
consignment rows.

**Why.** Screens and reports built on the header keep working, and no audited
consignment record is invented after the fact.

**Where.** `src/modules/procurement/inbound_service.py` - `record_inbound_shipment`;
`src/modules/procurement/inbound_repository.py` - `InboundRepository.projection`.

**Impact if wrong.** A backfill migration could create consignment 1 from each
header that carries shipment details.

---

## §C1-10. A declined order stays open for the buyer's decision

**Assumed.** SENT -> VENDOR_DECLINED records the vendor's reason. The buyer then
cancels the order (VENDOR_DECLINED -> CANCELLED, which gives the quantity back to
the requisition) or sends it again (VENDOR_DECLINED -> SENT, which re-checks that
the vendor can still take orders and clears the old reason; the audit keeps it).
Until then the requisition quantity stays committed, the order is not expected
supply (it is outside `OPEN_PO_STATUSES`, so MRP and the duplicate check ignore
it and no partial index changes), and it blocks retiring the vendor.

**Why.** A decline is a question to the buyer, not an answer: re-sourcing,
re-negotiating and cancelling are all reasonable, and none should happen behind
the buyer's back. Counting a refused order as supply would leave the plan short.

**Where.** `src/modules/procurement/service.py` - `transition_purchase_order`;
`src/modules/procurement/service_rules.py` - `PURCHASE_ORDER_LIFECYCLE`;
`src/modules/vendors/repository.py` - `OPEN_PURCHASE_ORDER_STATUSES`.

**Impact if wrong.** If a decline should release the requisition at once, the
VENDOR_DECLINED transition calls `_release_source_lines` and a re-send re-links.

---

## §C1-11. A vendor-facing notification reaches only users who could act on it

**Assumed.** An order sent to a vendor notifies its assignee; when it is
unassigned, every user of that vendor who holds `vendor_portal.respond` and can
see the order - unrestricted, scoped to its category or an ancestor of it, or
assigned to it. "Material received" (`vendor_portal.access`) and a rejection at
incoming inspection (`vendor_portal.quality`) follow the same visibility rule. A
bill approved or disputed and a payment approved or allocated notify the
vendor's users holding `vendor_portal.invoice`; a dispute carries its reason, an
approval does not carry the approver's note. Vendor-app links are `/vendor/...`
routes (WP C2a).

**Why.** A notification whose link the portal answers "not found" for is noise,
and one that reaches a different vendor is a leak. The approver's note is the
buyer's own record.

**Where.** `src/modules/procurement/service.py` - `vendor_recipients_for_order`,
`notify_vendor_about_order`, `notify_vendor_about_receipt`;
`src/modules/finance/service.py` - `_tell_vendor_about_invoice`,
`_tell_vendor_about_payment`; `src/modules/notifications/repository.py` -
`user_ids_for_vendor`.

**Impact if wrong.** The rights per event are named at each call.

---

## §C1-12. Registering a browser is the push opt-in

**Assumed.** New per-type preferences default to push enabled; a user with no
registered, unexpired device never gets the PUSH channel. An update that omits
`push_enabled` preserves a stored opt-out.

**Why.** The browser's own permission prompt is the consent; a second switch per
type would be a setting nobody finds.

**Where.** `src/modules/notifications/service.py` - `resolve_channels`, `notify`;
`src/modules/notifications/repository.py` - `upsert_preferences`.

**Impact if wrong.** Default `push_enabled` to false and the preferences screen
becomes the opt-in.

---

## §C1-13. The VAPID key derives from the credential root unless one is supplied

**Assumed.** Without `WEB_PUSH_VAPID_PRIVATE_KEY` the VAPID signing key is derived
deterministically from `CREDENTIAL_ENCRYPTION_KEY` with the HMAC purpose
`web-push-vapid`. Rotating the root key changes the VAPID key, and browsers
re-subscribe.

**Why.** No new deployment secret is needed to switch push on.

**Where.** `src/core/webpush.py`; `src/core/config.py`.

**Impact if wrong.** Supply an explicit key; it survives root rotation.

---

## §C1-14. A collapsed repeat never re-sends to a device

**Assumed.** An unread grouped notification that repeats updates its wording and
count but keeps its email and push delivery markers, so it is not sent to a
device again. Once read, the next occurrence is a new row.

**Why.** A consignment that is edited five times should buzz a phone once.

**Where.** `src/modules/notifications/repository.py` - `upsert_many`.

**Impact if wrong.** Clear the markers on collapse to re-send every update.

---

## §C3-1. A dealer account is bound to exactly one dealer-channel customer

**Assumed.** `users.customer_id` (FK `customers`, CASCADE, indexed
`ix_users_customer`) binds an account to the dealer portal; the CHECK
`portal_binding_exclusive` refuses a vendor binding and a dealer binding on one
account. Dealer roles (`roles.is_dealer_role`: DEALER_OWNER, DEALER_BUYER,
DEALER_ACCOUNTS) may be held only by dealer-bound accounts, and dealer-bound
accounts may hold only dealer roles - enforced in both directions by
`assert_portal_role_consistency`, mirroring the vendor rule. A binding must
name a live customer of type DEALER or DISTRIBUTOR. `dealer_route_allowed`
keeps dealers on `/dealer-portal/**`, their own `/notifications/**` and auth
self-service; every `/dealer-portal` route also demands a dealer principal
(`require_dealer_user`), so staff get 403 `SCOPE_DENIED`. Repository scopes
(`UserRepository`, `SalesOrderRepository`, `DispatchRepository`) filter dealer
principals to their own customer as defence in depth, and a foreign order,
consignment or statement answers 404, never 403.

**Why.** One account must never see two principals' data, and a dealer user who
also held an internal role would see the tenant's costing and every other
dealer's book through the internal screens, which the dealer filter does not
cover.

**Where.** `src/modules/users/models.py` - `customer_id`, `is_dealer_role`;
`src/modules/users/service.py` - `assert_portal_role_consistency`;
`src/core/deps.py` - `dealer_route_allowed`, `require_dealer_user`;
`src/modules/users/repository.py`, `src/modules/sales/repository.py`,
`src/modules/dispatch/repository.py` - `_scoped`.

**Impact if wrong.** If a dealer account must span several customers, the
binding becomes a list and every scope, filter and checkout changes with it.

---

## §C3-2. Availability is a band from live free stock, never a quantity

**Assumed.** `availability_band` folds live `available_quantity` summed across
sellable warehouses (FINISHED_GOODS, PACKING) into IN_STOCK (>=
`AVAILABILITY_IN_STOCK_AT` = 10), LIMITED (> 0) or MADE_TO_ORDER (0). It is
read on every catalogue call and never cached; exact quantities are never
returned. A DISTRIBUTOR channel with no price-list row has no item-master
fallback column, so the item is `price: null`, `price_on_request: true` and not
orderable - rather than quietly invoiced at the dealer rate.

**Why.** Exact stock figures are commercially sensitive and go stale; a cached
band would promise what a reservation has since taken. The distributor channel
genuinely has no master price, and reusing the dealer one would invoice the
wrong party at the wrong rate.

**Where.** `src/modules/dealer_portal/models.py` - thresholds and warehouse
types; `src/modules/dealer_portal/pricing.py` - `availability_band`;
`src/modules/dealer_portal/service.py` - `_channel_price`.

**Impact if wrong.** If the business wants exact figures for trusted dealers,
the band becomes a permission-gated quantity; if more warehouse types become
promisable, `SELLABLE_WAREHOUSE_TYPES` grows.

---

## §C3-3. Schemes are percentage-only, best single offer, explicit wins

**Assumed.** `dealer_offers` carries one percentage rule (0 < x <= 100) with a
scope (ALL, CATEGORY with subtree matching, BRAND, ITEM), an optional channel
audience, an optional negotiated `customer_id`, a minimum quantity, a validity
window and a sort order. The best single offer per line wins - highest
percentage, then scope specificity, then `sort_order`, then id - and offers
never stack. One pure function (`select_best_offer`) serves the catalogue, the
cart, checkout and ERP sales pricing: a sales-order line with no explicit
discount takes the best scheme as its default `discount_percentage` and stores
`offer_id` (a bare UUID, no FK, so the sales table never depends on the portal
schema); an explicit percentage or flat discount always wins and stores no
scheme. With no offers configured, ERP pricing is byte-for-byte unchanged. A
header-only re-price keeps a carried scheme citation when the discount is
unchanged.

**Why.** One matcher means the catalogue, the cart and the order can never
disagree about what a dealer pays. An explicit discount is a commercial
decision somebody signed; a scheme is a default. A foreign key from sales to
the portal schema would make the order table uncreatable without it.

**Where.** `src/modules/dealer_portal/pricing.py` - `select_best_offer`,
`load_pricing_offers`; `src/modules/sales/service.py` - `price_line`,
`_build_pricing_context`, `_apply_priced_line`; `src/modules/sales/models.py`
- `offer_id`.

**Impact if wrong.** Stacking, flat-amount schemes or tiered slabs change the
matcher and the offer table together; the ERP default follows automatically.

---

## §C3-4. One cart per dealer user

**Assumed.** `dealer_carts` has one row per dealer user (`uq` on company and
user) carrying that user's dealer; lines carry item, variant and quantity only
- no stored price. A duplicate grain is refused (change the quantity
instead); a cart holds at most `MAX_CART_LINES` = 50 grains. The wishlist is
one row per user, item and variant.

**Why.** The cart is a working draft, not a bulk import: 50 lines always fit
the checkout payload (sales allows 200), and a price stored on the line would
go stale the moment the price list moved.

**Where.** `src/modules/dealer_portal/models.py`; `src/modules/dealer_portal/service.py`
- cart and wishlist methods.

**Impact if wrong.** Raise `MAX_CART_LINES`, or key carts per dealer instead of
per user - and decide what two users' conflicting drafts merge into.

---

## §C3-5. Dealer checkout fulfils from the default warehouse

**Assumed.** The checkout names no warehouse: it resolves
`warehouses.is_default` through a new `WarehouseRepository.default_warehouse`
getter, and refuses when none is flagged. The order still reserves against
that warehouse on confirmation like any other order.

**Why.** A dealer cannot be asked to pick a warehouse, but a reservation has
to name the position it is taken against - an order with no warehouse could be
confirmed but never allocated.

**Where.** `src/modules/warehouses/repository.py` - `default_warehouse`;
`src/modules/dealer_portal/service.py` - `checkout`.

**Impact if wrong.** If fulfilment must follow the shipping address or the
item, the checkout resolves the warehouse per line instead.

---

## §C3-6. Portal orders stay DRAFT for sales review, and cancel rather than delete

**Assumed.** Checkout builds `SalesOrderCreate` with no unit price and no
discount; the server prices, discounts and taxes every line, stamps
`source = DEALER_PORTAL` (server-only, CHECKed, default ERP, exposed in
summary/detail and filterable), clears the cart, writes a `dealer_checkout`
audit row and raises "New order request from \<dealer\>" (`APPROVAL_PENDING`,
linking the SO page) for the assigned salesperson, else `sales_order.approve`
holders - all in one outermost transaction. The order stays DRAFT: sales staff
confirm it, which runs the credit check and reservation. The dealer cancels
its own DRAFT with a reason; past DRAFT the portal refuses. `delete_draft_order`
refuses portal-sourced drafts - a delete would silently drop an order the desk
may already be reviewing.

**Why.** A dealer order is a request, not a commitment: stock and credit move
only when sales confirms. Cancelling keeps the trail the dealer and the desk
both read; deleting would fork it.

**Where.** `src/modules/sales/models.py` - `source`;
`src/modules/sales/service.py` - `create_order(source=...)`,
`delete_draft_order`; `src/modules/dealer_portal/service.py` - `checkout`,
`cancel_order`.

**Impact if wrong.** If trusted dealers may self-confirm, checkout confirms
orders whose credit check passes - and the notification becomes informational.

---

## §C3-7. Dealer tracking drops the driver and the freight

**Assumed.** The tracking projection carries courier or transporter name,
tracking id, LR number, dispatched/expected/actual dates and the event history
(status, time, location, remark) - but never `driver_mobile` and never freight
fields. The feed is a bounded newest-first read (`TRACKING_EVENT_LIMIT` =
200), not a cursor: a consignment's history is short and the dealer always
sees the whole of it.

**Why.** The driver's phone number is personal data and the freight cost is an
internal commercial; neither is the dealer's to have. A cursor would page a
feed nobody pages.

**Where.** `src/modules/dealer_portal/service.py` - `dispatch_tracking`;
`src/modules/dealer_portal/schemas.py` - `DealerTracking`.

**Impact if wrong.** If dealers must call the driver, expose the number behind
a separate right rather than widening this projection.

---

## §C3-8. Item detail carries the shape D5 will fill

**Assumed.** The detail returns variants with their own channel price, the
current channel slabs, the applicable schemes, HSN, GST rate, description,
`can_return` and up to `CATALOGUE_MAX_IMAGES` = 8 image ids. Specifications
and accessories return in their shapes but empty: their masters belong to WP
D5, which does not exist yet, and this package invents no tables another
package owns.

**Why.** The screen contract is stable while the masters land; inventing
specification tables here would collide with D5's the day it arrives
(CLAUDE.md: never hand-write another module's table).

**Where.** `src/modules/dealer_portal/service.py` - `catalog_item`.

**Impact if wrong.** When D5 lands, the two empty lists are filled from its
batch helper with no contract break.

---

## §C3-9. Catalogue paging limits: price sort refused, offer filter coarse

**Assumed.** `GET /catalog/items` sorts by `code` or `name` in SQL; `sort_by =
price` is refused with 422 naming the allowed fields, because a channel price
is a slab ranking plus a fallback and cannot be ordered in the database.
`on_offer_only` prefilters in SQL on schemes that *could* apply and matches
exactly per row in Python (CATEGORY scopes need the subtree), so a page can
come back short and the total is the coarse one. A cart holds at most 50 lines
(§C3-4), well under the sales-order bound.

**Why.** Refusing an unorderable sort beats returning a wrong order; resolving
every CATEGORY scope in SQL would join the category tree into the page query
for a filter most calls do not use.

**Where.** `src/modules/dealer_portal/repository.py` - `page_items`;
`src/modules/dealer_portal/service.py` - `catalog_items`.

**Impact if wrong.** If price sorting becomes a requirement, it needs a
materialised channel-price column with its own invalidation - which is a cache
on inventory-adjacent data and needs a decision, not a query tweak.

---

## §C3-10. Overdue invoices wait for sales invoicing

**Assumed.** `GET /outstanding` returns the receivables ageing for the dealer
(overdue = total less not-due, oldest due date, open document count) with an
empty `overdue_invoices` list: sales invoices are not in this release, so
there is no invoice document to name, and the FIFO-netted ageing is the
overdue position.

**Why.** A placeholder row inventing invoice numbers would be mistaken for a
receivable that exists; the ageing is the honest position until invoicing
lands.

**Where.** `src/modules/dealer_portal/service.py` - `outstanding`;
`src/modules/dealer_portal/schemas.py` - `DealerOutstanding`.

**Impact if wrong.** When sales invoices land, the list names them per the
finance overdue shape.

---

## §ADDITIONAL11-1. Recovery email changes require credential authority

**Assumed:** Administrator-managed login/recovery email changes require the same
authority as setting the account's password, including protected-administrator
checks. Administrators exercising that authority remain able to maintain accounts;
ordinary profile editors can submit an unchanged email but cannot replace it.

**Why:** The public reset flow uses that address as proof of account control.
Allowing a profile editor to replace it bypasses the password-reset authority.

**Where:** `users.service.UserService.update_user`, reset-grant/session repository
operations and the administration email input.

**Impact if wrong:** A self-service verified email-change flow can be added as a
separate authenticated workflow. Generic profile PATCH must retain the credential
authority boundary until that flow exists.

## §ADDITIONAL11-2. Customer credit operates only in company currency

**Assumed:** Customer orders, invoices and receipts must use the company's denomination
while outstanding/credit is a single company-currency balance. Omitted order and
receipt currencies resolve to company currency. Vendor foreign-currency documents
and denomination-separated historical reporting remain supported.

**Why:** A nominal USD receipt cannot subtract an equal number of INR from a
company-currency credit balance. No frozen conversion-rate or per-currency credit
balance implementation exists yet.

**Where:** Sales create/update/confirm, sales invoice preview/issue/cancel and
customer receipt create/update/approve. Unposted legacy foreign receipts may be
cancelled; reversal of posted foreign receipts or invoices is refused before any
journal/cache mutation pending reconciliation.

**Impact if wrong:** Implement audited conversion or per-currency customer credit
before lifting the restriction. Do not change existing financial data silently.

## §ADDITIONAL11-3. Payment methods represent actual money movements

**Assumed:** Cash, bank, cheque and UPI record money received or paid. The legacy
`ADJUSTMENT` label does not identify a supported noncash journal operation and
cannot create, update or approve a new money movement. Existing drafts may be
cancelled or corrected to an actual payment method; historical bank postings can
still be reversed through cancellation.

**Why:** Treating a noncash adjustment as a bank transfer invents cash movement.
An explicit journal adjustment workflow must identify its proper counterpart.

**Where:** Payment request validation, service posting boundaries and the payment
form's method options. Historical display and method filters retain the label.

**Impact if wrong:** Add a defined journal adjustment workflow before enabling
noncash adjustments. Never reinterpret existing postings silently.

## §ADDITIONAL11-4. Warehouse assignments cover stock documents and posting

**Assumed:** An empty assignment and a platform superuser are unrestricted within
the company. Transfers require both warehouses; adjustments and counts require
their single warehouse. User operations retain their assignment at posting.
Trusted background contexts retain their existing empty assignment except finance
valuation exports, which capture and restore that user's verified scope.

**Why:** Permission to act on inventory does not grant access to every warehouse.
Documents created before a restriction must not bypass it when stock moves.

**Where:** Shared warehouse policy, document repositories, procurement/production
destination checks and every grain touched by the movement engine. Company
isolation applies to restricted, unrestricted and system contexts alike.

**Impact if wrong:** Define a separate authority for cross-warehouse operations
before changing the both-endpoint transfer rule. No database schema change is
needed for this policy.

## §ADDITIONAL11-5. Selected cartons travel with their complete contents

**Assumed:** A selected sealed carton ships whole. A mixed-item carton can appear
on distinct dispatch lines for its actual item, batch and sales-order demand.
Each carton/content grain appears once and matches its quantity and serials.
Loose goods may use lines without cartons.

**Why:** A carton reference alone cannot prove which goods physically leave.
Omitting one of a mixed carton's contents understates the physical shipment.

**Where:** Dispatch create/update and the locked pre-shipment validation of
legacy drafts. Unlinked packing demand matches only unlinked dispatch demand.

**Impact if wrong:** Repack or split a carton before shipping part of its contents;
do not label a partial or different item shipment with the original sealed box.

## §ADDITIONAL11-6. Valuation file access follows its captured warehouse scope

**Assumed:** A queued finance valuation captures warehouse assignments and filters on the
server. Its worker uses those stored values. A legacy queued job without scope
evidence captures its active internal creator's assignments; invalid evidence or
an unavailable creator refuses generation. A restricted reader must cover every
captured warehouse, including through general report/document permissions.
Whole-company and untracked valuation files cannot be narrowed after generation.

**Why:** An asynchronous export must not broaden the inline report's access, and
a saved file must not expose warehouses after a user's access is narrowed.

**Where:** Finance export queue, valuation worker, report-job reads/downloads and
the shared document predicate registry. Scope snapshots outlive job expiry while
their workbook remains. Tagged or recognizable legacy valuation files without
scope evidence are hidden from restricted readers.

**Impact if wrong:** Regenerate the file under the reader's current scope. Empty
assignments and platform superusers remain unrestricted within the company; no
repair migration or silent rewriting of historical workbooks is required.

## §E5-1. Production output is held when the item or the routing asks for it

**Assumed:** A production order holds its good output for QC when the item
carries `requires_production_qc` or any routing operation gates its completion
on QC. An explicit order-level choice wins over the item flag, but an explicit
false is refused while a routing operation gates - the operation's gate would
strand the job with output the routing demands inspected and no inspection
anyone will raise.

**Why:** The routing gate is structural (a step that may not report complete
until inspected) while the item flag is policy (this part is always checked).
Overriding policy per order is planning; overriding structure per order is a
job that can never finish.

**Where:** `production._resolve_requires_qc` at order creation and on every
draft edit touching the hold or the routing; `items.requires_production_qc`.

**Impact if wrong:** Allow explicit false against a gated routing and such jobs
complete their operations uninspected; the gate stays as the backstop and names
the missing inspection.

## §E5-2. Completion waits for the hold; operations wait for acceptance

**Assumed:** A production order cannot COMPLETE while any booking's output is
still flagged held - the refusal names the open inspections. A work order whose
routing operation gates on QC cannot COMPLETE until an inspection on that
operation is ACCEPTED *and settled* (nothing left in rework or hold): a partial
decision can carry an ACCEPTED verdict while quantity is still parked. Only
operations that produced output are gated; a dry run booked nothing into stock.

**Why:** Completing around held stock closes the job around a verdict that is
still outstanding, and completing an operation on a partial verdict releases
the job around stock nobody has released.

**Where:** `production.complete_order`, `production.transition_work_order`,
`production.record_quality_verdict` (clears the flag only on a settled verdict).

**Impact if wrong:** If partial acceptance should release the operation, gate on
the verdict alone; held quantity then completes with the job and is re-decided
afterwards.

## §E5-3. Final QC reserves instead of holding, and rejects are replaced

**Assumed:** Starting a packing order raises one FINAL inspection per line whose
item requires final QC, over the quantity to pack. The goods are reserved for
the sales order, not held: completion and hand-over are refused until each
line's inspection is ACCEPTED. A REJECTED verdict moves the stock to DAMAGED
through the ordinary decision path, and the line is unblocked by repacking
replacement goods and re-inspecting it - never by force. Starting is
fail-closed: a QC line starts no bench without its inspection.

**Why:** Packing never owns the stock it boxes, so there is no pool to hold
it in; the gate is the control, and the re-inspection is the way out that keeps
the gate honest.

**Where:** `packing.start_packing`, `packing.complete_packing`,
`packing.hand_over_to_dispatch`, `packing.reinspect_line`.

**Impact if wrong:** If rejected lines should be editable instead of
re-inspected, add a line-replacement path that voids the old inspection's link;
the gate reads only the current link.

## §E5-4. Inspection attachments are entity documents, not link rows

**Assumed:** Files about an inspection - including inspections raised without a
template - are `documents` rows filed with `entity_type='qc_inspection'`, not
`qc_inspection_documents` link rows. Readings keep their own
`photo_document_id`; anything else about the lot attaches at the inspection
level.

**Why:** The documents module already files, lists, downloads and expires
against any snake_case noun, so a link table would duplicate exactly that
machinery for one noun.

**Where:** Quality detail screen; `DocumentFilters(entity_type, entity_id)`.

**Impact if wrong:** If per-inspection retention or approval is ever needed,
introduce the link table then; existing rows migrate by their entity pair.

## §E5-5. Every verdict is kept, numbered per inspection

**Assumed:** Each call to `decide_inspection` appends one
`qc_inspection_decisions` row carrying the cumulative disposition it wrote,
numbered 1, 2, 3... per inspection under the inspection's row lock. Re-deciding
overwrites the five live quantities; the cycles survive here.

**Why:** Without the sequence, two concurrent verdicts could claim one history
position, and without the rows a reworked lot's earlier decisions would survive
only as audit diffs.

**Where:** `quality.decide_inspection`, `qc_inspection_decisions`.

**Impact if wrong:** If decisions must be immutable rather than re-decidable,
refuse re-decision outright; the history table then records exactly one row per
inspection.

## §E5-6. Completing rework moves nothing

**Assumed:** Finishing a rework cycle records the outcome and re-opens the
inspection (REWORK to IN_PROGRESS, verdict back to PENDING) so re-decision is
owed. Even a SCRAPPED outcome leaves the quantity in QC hold for the inspector
to scrap on re-decision, because the decision is the one release path. A
RETURNED outcome against a named vendor flags the inspection for a vendor
return the way a rejection does.

**Why:** A second movement path out of the hold would be the two-release-paths
defect again: the completion would release stock the decision also releases.

**Where:** `quality.complete_rework`.

**Impact if wrong:** If the bench should write scrap off directly, post the
movement in the completion and narrow the re-decision to the remainder; the
owner callback and the gates must then both understand a partially-completed
cycle.

## §E5-7. A decision moves stock from the pool the lot sits in

**Assumed:** The decision reads the position's QC-hold quantity first. What is
still on hold leaves the hold pool; the rest is already where an ACCEPTED
verdict would put it (no movement), or leaves the available pool for DAMAGED
and SCRAP. This is what lets FINAL inspections - which never hold anything -
and hold-free manual inspections be decided at all.

**Why:** Sourcing unconditionally from QC hold made every unheld inspection
undecidable: the movement failed for want of held stock that was never
supposed to be there.

**Where:** `quality._post_decision_movements`.

**Impact if wrong:** If unheld lots must not be rejectable to DAMAGED either,
refuse REJECTED/SCRAP dispositions on inspections that hold nothing.

## §E5-8. Packing raises FINAL inspections per line at start

**Assumed:** See §E5-3. The inspection covers the line's quantity to pack at
the order's warehouse, named against the line, so a replacement line
re-inspection supersedes exactly the verdict it replaces.

**Why:** Per-line naming is what makes the re-inspection unambiguous when one
line of an order fails and the rest pass.

**Where:** `packing._raise_line_inspections`, `packing_order_lines.qc_inspection_id`.

**Impact if wrong:** If final QC should gate dispatch rather than packing,
move the raise to the hand-over; lines packed before the change carry no
inspection and pass the gate vacuously.

## §E10-1. Variants share the parent's stock and bill of materials

**Assumed:** A variant is a finish/size/colour identity on the *same* item. It
shares the parent's stock balances, bill of materials, planning figures and all
nine capability flags, and differs only in its code and name, in the price and
cost deltas, in its barcode and in whether it is active. A finish that needs
its own stock position, its own bill of materials or its own planning figures
is a separate item, not a variant.

**Why:** Stock is keyed by item everywhere - balances, ledger, reservations,
price resolution - so a variant-level quantity would be a second stock master
to reconcile. The delta model also keeps price revisions cheap: changing the
parent's price moves every variant without touching their rows.

**Where:** `products.service.create_variant` / `update_variant` /
`resolve_price`, `sales.service.price_line`, the item detail Variants tab and
the sales order line editor's variant picker. Sales lines carry an optional
`item_variant_id`; stock and BOM references remain on `item_id`.

**Impact if wrong:** If a finish genuinely needs its own on-hand figure, model
it as its own item; promoting variants to stocked entities later is a schema
change (balances keyed by variant) plus a migration of every document line that
names one.

## §E10-2. Batch and serial tracking change only while the item holds nothing

**Assumed:** `is_batch_tracked` and `is_serial_tracked` may change in either
direction only while the item holds no stock anywhere - no on-hand, nothing
reserved, nothing in transit, nothing out at a job-work vendor, nothing on QC
hold, damaged or scrapped. The check reads the balance rows, which is where an
in-flight document has already put the stock, rather than sweeping open
documents. A change attempted over a balance is refused with the item's code.

**Why:** `batch_id` is part of the balance grain, so switching batch tracking
on over existing stock strands that quantity at the batch-NULL position no
receipt can ever add to; switching serial tracking on claims a per-unit
history for units that were never identified. Switching either off splits a
live item across two grain shapes.

**Where:** `products.service._assert_tracking_change_is_safe`, called from
`update_item`; the item form's Stock identification checkboxes.

**Impact if wrong:** If the business needs to start lot-tracking a stocked
part, the supported path is to clear the balance first (issue, transfer or
write it off) and then flip the flag; a same-day migration of live balances
into identified lots needs a dedicated, audited routine.

## §E10-3. Line discounts above the threshold need the override right and an approval

**Assumed:** `SALES_DISCOUNT_APPROVAL_PERCENT` (deployment configuration,
default 10%) is the line-discount percentage above which an *explicit*
discount - a percentage, or a flat amount restated against the line gross -
needs `sales_order.discount_override`. Without the right the save is refused
outright; with it the order is flagged `requires_approval` and goes through
the existing `approve_order` path before it can be confirmed. A line whose
*net* unit price after the discount sits below the item's standard cost is
flagged too. Offer-derived discounts are pre-authorised: they skip the
threshold but still face the below-cost check. A price list that simply sits
under cost, with no override and no discount, is not flagged - that is a
commercial decision already made on the price-list screen.

**Why:** The old checks read the price before the discount, so a discount
could take a line under cost unnoticed, and any 0-100% discount was ungated.
The threshold is a number the business owns, so it lives in configuration
rather than in code.

**Where:** `sales.service.price_line`, `core.config`, the sales order line
editor's discount hint and the order detail approval banner.

**Impact if wrong:** If the desk needs a different number, set the deployment
configuration - no code change. If scheme discounts should also face the
threshold, remove the offer exemption in `price_line`; dealers ordering above
it will then queue for approval like everyone else.

## §E10-4. Relinking a removed supplier restores the existing source

**Assumed:** An item/vendor pair retains one link identity, including after
removal. Linking that supplier again restores the row as active with the newly
entered source details and commercial terms. Recorded last purchase price,
purchase date and currency survive unless explicitly supplied again. A live
duplicate is still refused, and restoring a preferred source demotes the current
preferred source first. Reference validation and tenant scope apply on relink.

**Why:** The pair is unique even after soft deletion. Restoring it makes removal
reversible while retaining purchase history and the audit trail.

**Where:** `products.service.link_vendor`; the item detail Vendors tab.

## §E7-1. Production close records variance without revaluing stock

**Assumed.** The approved BOM roll-up is snapshotted per unit when a production
order is released. Its component roll-up is the material standard; direct
outsourced BOM operations are the subcontract standard; the remaining
operation standard is internal operation cost. Outsourced routing work orders
are retained for execution and traceability but do not add internal labour,
machine or process cost. Actual subcontract cost is the sum of accrued
job-work receipt charges for job-work orders sourced by the production order;
unreceived estimates and vendor-debit recoveries are excluded.

At closure, actual unit cost is total actual cost divided by good output.
Material, operation and subcontract variances compare each actual category
with its release-time standard multiplied by good output. The total variance
is actual total less standard unit cost times good output; any currency
rounding remainder is assigned to operation variance so the three displayed
categories reconcile exactly. Older orders without a release snapshot retain
NULL standard and variance values rather than receiving a guessed backfill.

**No finished-stock layer revaluation is made at closure.** Production receipts
were valued when posted and FIFO layers may already have been consumed by
downstream work; changing those layers retrospectively would rewrite the cost
of later issues. The inventory engine has no production cost-adjustment
mechanism that safely redistributes such a change. The order records actual
cost and variance only. No GL posting is made: inventory accounts are not
defined in this system.

**Where:** `production.release_order`, `production.close_order`,
`job_work.service.production_order_charges`, and the E7a production costing
screen.

## §E7-2. Production-cost report period

**Assumed.** The report's period is the UTC date of `production_orders.updated_at`
when the order transitions to CLOSED. That timestamp is assigned by the model's
Python-side `onupdate`; closed orders cannot be edited, so it remains the close
time. The report includes no open or historically estimated order.

**Why.** The schema has no dedicated `closed_at` column, and using
`actual_end_date` would report when floor work ended rather than when the order
was formally closed. A new timestamp is not added solely for this report.

**Where:** `reports.repository.production_cost`; the query is supported by
`ix_production_orders_company_status_updated_at`.

**Impact if wrong.** A close entered after midnight falls in the day it was
closed, not the day the work physically ended. If the business wants the latter,
the report period should use `actual_end_date` and name the report accordingly.

## §E7-3. WIP includes accrued time for running operations

**Assumed.** WIP value is the net cost of posted material issues and returns,
plus operation cost to date. A running, clocked operation accrues its work-centre
hourly rate from `actual_start_at` through the time of the query. Its setup and
process charges are included once it has started. Outsourced operations are not
included. This is a live estimate; final closed-order costing continues to use
completed clockings. The endpoint requires both production-order view and
production-cost view permissions because it exposes monetary cost.

**Why.** Excluding a currently running operation would report no operation cost
for work that is actively consuming capacity. The final-cost calculation remains
unchanged because its actual end time is the authoritative completed duration.

**Where.** `src/modules/production/repository.py` - `ProductionOrderRepository._wip_stmt`.

**Impact if wrong.** If the business wants WIP to include only completed
operations, remove the in-progress elapsed-time branch; the final-cost model is
independent of this estimate.

## §E7-4. A machine double-booking warns instead of refusing

**Assumed.** Two open operations naming the same machine with overlapping
planned windows are reported in the response message, naming every holder
(operation code, job code, window) - on assignment and at release - rather
than refused. Only PENDING and IN_PROGRESS operations count: a finished run's
planned window is a stale plan, not a hold on the machine. Setting
`BLOCK_MACHINE_OVERLAP` to True refuses instead, before anything is written.
A machine in MAINTENANCE or BREAKDOWN warns through the routing module's
`WORKABLE_MACHINE_STATUSES`; a RETIRED or inactive machine is still refused.

**Why.** The floor re-sequences work constantly - an hour's breakdown pushes
everything behind it - and refusing an assignment over a plan that is already
stale would strand the traveller. The warning names what to move, which is the
useful half of a refusal.

**Where.** `src/modules/production/service.py` - `BLOCK_MACHINE_OVERLAP`,
`_apply_overlap_rule`, `_check_release_machines`; `WorkOrderRepository.machine_conflicts`.

**Impact if wrong.** If the business wants hard single-booking, set
`BLOCK_MACHINE_OVERLAP` to True (a named constant, not a code change). If
finished runs should also block, extend the overlap query beyond open
statuses.

## §E7-5. Operations are planned inside rostered shift windows

**Assumed.** Release schedules each operation to start inside its work
centre's rostered shift windows (first window it fits in, earliest first; an
operation longer than any window overruns from the earliest window still
ahead). A centre with no roster rows plans round the clock, and setting
`PLAN_WITHIN_SHIFTS` to False restores the old 24/7 back-to-back schedule
everywhere. Operations remain strictly sequential - no overlap of a long run
with the next setup - matching the routing lead-time convention.

**Why.** A 24/7 schedule promises Friday-night output from a plant working one
eight-hour shift. Planning inside shifts keeps the promise honest where a
roster exists, while refusing to schedule at all where none does would be
worse.

**Where.** `src/modules/production/service.py` - `PLAN_WITHIN_SHIFTS`,
`plan_work_orders`, `_roster_windows`; `ShiftAssignmentRepository.windows_for_centres`.

**Impact if wrong.** If the business plans 24/7 despite the roster, set
`PLAN_WITHIN_SHIFTS` to False. If operations should overlap shifts or split
across them, the placement rule in `_place_in_windows` is the line to change.

## §E7-6. Capacity buckets whole planned windows by company day

**Assumed.** `GET /production/capacity` slices each open operation's full
planned window (setup plus run minutes) across the company-local dates it
touches, and compares the day's total against the rostered minutes for that
centre on that date when roster rows exist, else `working_hours_per_day`.
Days the plan exceeds capacity are flagged. The default window is four weeks
from today; at most 62 days are answered in one request. Operations without a
planned window are invisible to capacity.

**Why.** Capacity is a per-day statement, and a multi-day operation occupies
each day it runs through. Rostered minutes are used where planned because
that is what the day actually holds; the standard day is the fallback, not a
second opinion - the same two figures the machine-utilisation report carries.

**Where.** `src/modules/production/service.py` - `production_capacity`,
`MAX_CAPACITY_DAYS`; `WorkOrderRepository.open_windows`;
`ShiftAssignmentRepository.rostered_minutes_by_centre`.

**Impact if wrong.** If capacity should count only remaining (pro-rated)
minutes rather than whole planned windows, change `open_windows` to net off
completed quantity as the work-centre load board does. If the window cap is
wrong for planning horizons, change `MAX_CAPACITY_DAYS`.

## §E7-7. A BOM without operations inherits its default routing at approval

**Assumed.** Creating or approving a BOM with no operations snapshots the
item's effective default approved routing into `bom_operations` - sequence,
name, routing link, work centre, setup/run minutes, the centre's current rate,
the step's process charge as the fixed cost, and the outsourced flag.
Explicitly authored operations are never overwritten, and a missing or
stepless routing leaves the BOM operation-free rather than inventing steps.
The snapshot is costed by the same roll-up as authored operations.

**Why.** Every BOM built from the UI used to cost material only, because the
dialog posts no operations and nothing else populated them. The routing the
item would be released against is the only defensible default, and freezing
it at approval keeps later rate revisions from rewriting an approved cost.

**Where.** `src/modules/bom/service.py` - `_snapshot_default_routing`,
called from `create_bom` and `approve_bom`.

**Impact if wrong.** If snapshots should follow routing revisions instead of
freezing, the roll-up would have to read live routing data - which contradicts
the snapshot-at-commitment rule the rest of the module follows.

## §E7b-1. Rejected and scrap quantities are shown, not outstanding

**Assumed.** `JOB_WORK_PENDING` carries `rejected_quantity` and
`scrap_quantity` per order beside issued, received and pending. Rejected
output is accounted for - it sits in damaged stock, with the vendor for
rework, or as scrap - so it is never part of the pending figure the vendor is
chased for.

**Why.** The gap audit (K07) found rejected quantity captured but never shown
afterwards, which reads as material unaccounted for. Stating it on the same
row as the pending balance keeps the two from being confused.

**Where.** `src/modules/reports/repository.py` - `job_work_pending`;
`JobWorkPendingRow`.

**Impact if wrong.** If rejected output should age like outstanding stock
until a rework order consumes it, the pending definition would have to add the
`REWORK_AT_VENDOR` share back in.

## §E7b-2. Receipt output is capitalised at input cost plus the receipt charge

**Assumed.** A job-work receipt books good output at (frozen challan issue
value of the input it consumed + the receipt charge it earned) per good unit.
The input value is the quantity-weighted challan `unit_value` - the ledger's
FIFO draw captured when the material left - read before posting, so the price
is exact rather than a second read of layers the posting consumes. Process
loss within the allowance is expensed as scrap rather than absorbed into the
good units; the later invoice price variance is expensed too; the sub-paisa
rounding remainder is recorded on the receipt audit, not posted anywhere.

Good output and rejected output returned to DAMAGED share the frozen material
value proportionally; only good output earns processing charges. For
`SCRAP_AT_VENDOR`, the rejected input share rounds once to input quantity scale
(four places), moves from JOB_WORK to SCRAP, and good output carries the
remaining input value. Rejected scrap stays distinct from process-loss scrap:
it triggers neither automatic vendor recovery nor excess-loss approval. The
receipt audit records good/rejected cost residuals and the rejected input share
scrapped. The cost report calculates the same allocation per receipt before
summing lots.

**Why.** The contract never stated a costing policy, so output used to arrive
at the output item's standard cost and neither the material nor the processing
charge reached stock value. Capitalising only the input the good output
consumed is the conservative half of full absorption costing: normal loss
keeps its existing P&L treatment and the change is bounded to the output
movement's unit cost.

**Where.** `src/modules/job_work/service.py` - `capitalised_output_cost`,
`issue_unit_basis`, `receive`, `JOB_WORK_COST` report.

**Impact if wrong.** If normal process loss should be absorbed into the good
units, add the within-allowance scrap value to `input_value`. If invoice
price variance should revalue the output instead, revaluation belongs on the
invoice approval, which is a second posting against stock the receipt has
already Passed on.

## §E7b-3. The vendor ledger closes on input consumed, not output received

**Assumed.** Every vendor-ledger row closes as opening + issued - consumed,
where consumed is the input drawn out of the job-work pool. Good output is
shown in output units beside it, because input and output are different items
whenever the process changes what the thing is and the two cannot be netted.
`charges_billed` is what approved job-work invoices have billed of the
period's receipt lines - a billed-to-date figure for those lines, not a
period cash figure.

**Why.** Netting output units against input units would restate the yield on
every row. The pool identity is the one the reconciliation already proves per
order; the ledger states it per vendor and period.

**Where.** `src/modules/job_work/service.py` - `vendor_ledger`.

**Impact if wrong.** If finance needs billed-per-period rather than
billed-of-period-lines, invoice approvals must stamp a period on the
`invoiced_amount` movement - the line only carries the cumulative figure.

## §E7b-4. No vendor-portal job work and no automatic rejected-output debit

**Assumed.** Vendor-portal and vendor-app job work (spec section 6, A01.M8)
is not built here: it is C2 territory with its own scope and security
model, and this package closes the staff-side completion flow only. Likewise
there is no automatic vendor debit for rejected output and no setting for
one: the only debit posted is the excess-loss value beyond the agreed
allowance, through the receipt path that already carries approvals.

**Why.** A half-scoped portal (endpoints without the vendor-scope review C1a
requires) would be a security hole dressed as a feature, and an automatic
charge-back for rejected output invents a commercial policy the contract does
not state.

**Where.** Nowhere - deliberately. `JobWorkSourceType.REWORK` with
`source_id` = the receipt is the traceability link. "Create rework draft"
prefills a draft only for physically returned rejects. Creating the draft moves
no stock, and the form states that ordinary approval/issuing uses AVAILABLE
stock. Issuing DAMAGED stock for a new rework order needs a separate reservation
policy and is not implemented by this action. Scrapped and vendor-held rejects
cannot seed this draft action.

**Impact if wrong.** If the client wants rejected-output charge-backs, add a
vendor debit through B3a's recovery mechanism behind a setting that defaults
to off, as the spec anticipates.

## §E7b-5. Job-work batches retain one same-item lot per order line

**Assumed.** A batch-tracked item may go for job work only when the processed
output is the same item. The lot is selected on the draft order, so approval
reserves that exact stock grain; every challan, receipt and movement for the
line uses the selected batch. A second batch uses a separate order line.
Tracked transformations remain blocked until the input-to-output lineage is
specified. Serial-tracked job work remains blocked because receipt, scrap and
rework do not yet account for each unit's identity.

**Why.** A batch is the identity of a lot, not of a particular warehouse
position, so the lot can return to a different warehouse without losing its
identity. A different SKU cannot inherit that identity without traceable
lineage. Likewise, allowing serials without specifying which units were
returned, scrapped or retained for rework would produce an incomplete ledger.

**Where:** `job_work_order_lines.is_batch_tracked` snapshots the item setting;
`batch_id` is selected on order entry and passed to the allocation reservation,
challan/receipt lines and inventory movements. See migration `20261025_0100`.

## §E1b-1. Shortfall planning creates drafts, never commitments

**Assumed.** Automatic planning raises only DRAFT documents: a production order
for a made line and one purchase requisition per order for bought lines. It
never releases a production order, approves or converts a requisition, or sends
anything to a supplier. A planner reviews every draft and takes the commitment
decision. Job-work items are reported as "needs job work" and raise nothing,
because obtaining them is a human make/buy choice.

**Why.** Stock reservation is reversible and customer-facing; releasing work or
committing money to a supplier is not. The setting removes the clerical steps
between confirmation and a reviewable plan without pretending the plan is a
commitment.

**Where:** `mrp.shortfall`; `sales.service.confirm_order` through the deferred
`ShortfallPlanningPort`; `/admin/settings` and `GET/PUT /sales/settings`.

## §E1b-2. The automation switch is nullable, and NULL means on

**Assumed.** `sales_settings.auto_plan_on_confirm` is nullable. A company with
no row, or a row whose value is NULL, uses
`DEFAULT_AUTO_PLAN_ON_CONFIRM = True`. An explicit `false` is stored and reads
as "off". `PUT /sales/settings` with an omitted field leaves the stored value
alone; explicit JSON `null` restores the code default. The settings screen
distinguishes the two states so an auditor can tell "off" from "never decided".

**Why.** A non-nullable boolean default would make the code default
indistinguishable from a stored choice, and a migration that backfilled `true`
would silently override a later change to the default.

**Where:** `sales.models.SalesSettings`; `sales.service.get_sales_settings` /
`update_sales_settings`; `apps/web/src/features/admin/settings-page.tsx`.

## §E1b-3. Drafts never net against themselves

**Assumed.** Replanning nets committed supply - production orders past DRAFT and
requisition lines past DRAFT - against the line's shortfall, then resizes or
cancels the drafts the planner already owns. Its own DRAFT documents are not
counted as supply for the purpose of deciding the target quantity, so a re-run
does not progressively shrink the plan to zero. A top-up that newly reserves
stock triggers the same reconciliation immediately.

**Why.** Counting a draft as supply makes idempotency impossible: the second run
sees the first draft as cover and raises nothing, while the first run's target
was calculated before that draft existed. Excluding drafts gives the same target
on every run and lets the document be resized in place.

**Where:** `mrp.shortfall.plan_sales_order_shortfall`; the planning hooks in
`sales.service` after confirm, line/date edits, cancel and top-up.

## §E1b-4. A capability mismatch warns and withdraws the stale draft

**Assumed.** If the planner chooses MANUFACTURE but the item is not flagged
manufacturable, chooses PURCHASE but it is not purchasable, or chooses JOB_WORK
but it is not job-workable, it raises nothing, appends an actionable warning
naming the line and item, and cancels any DRAFT production order it previously
raised for that line. Confirmation still succeeds.

**Why.** Confirmation is the customer-facing transaction; a gap in item master
data is the planner's problem to fix, not a reason to refuse an order the
business has already accepted. The warning makes the gap visible, and
withdrawing the planner's own stale draft keeps the plan honest.

**Where:** `mrp.shortfall` capability branches and `_withdraw_production_drafts`.

## §E1b-5. A transfer suggestion is advisory

**Assumed.** `GET /sales/{id}/allocation-status` reports free stock in other
warehouses, largest first, but does not reserve it. "Raise transfer" pre-fills a
normal stock transfer with the source warehouse, the order line and
`min(shortfall, free)`; the transfer then needs approval, dispatch and receipt
like any other. Receipt does not itself reserve the goods - E1a's top-up does,
either immediately when the receipt covers a waiting order or on the scheduled
sweep.

**Why.** A soft reservation at the source warehouse would freeze stock for a
transfer that may never be approved, and the allocation engine already owns the
priority order for scarce stock. Keeping the suggestion advisory preserves one
reservation authority.

**Where:** `sales.repository.available_elsewhere`;
`sales.service.OrderAllocationService`; `inventory.StockTransferService`;
`apps/web/src/features/inventory/movement-dialogs.tsx` (`TransferSeed`).

## Open questions for Atcraft

These could not be assumed responsibly and need a decision before the relevant
module is used in production:

1. **Inventory valuation method.** FIFO is implemented for every item
   (section 31). Is weighted average or standard cost required for any item
   class or for statutory reporting? If so, `items.valuation_method` returns
   together with the costing dispatch that honours it.
2. **Approval thresholds.** At what value does a purchase order, a stock
   adjustment or a price override require a second approval? The
   stock-adjustment threshold is a code constant
   (`ADJUSTMENT_APPROVAL_VALUE_THRESHOLD`, 25 000, in
   `src/modules/inventory/service.py`) pending the business decision; there is
   no setting for it, and the OpenAPI description of `POST /inventory/adjustments`
   says so.
3. **Multi-currency.** Everything is INR today. Are imports or exports priced in
   another currency?
4. **Batch and shelf life.** Which item groups genuinely require batch tracking,
   and is shelf-life expiry a blocking rule or a warning?
5. **Vendor portal scope.** Which vendor roles may see pricing? The current
   split shows a vendor their own purchase-order prices only; what a vendor may
   do to those orders is section 33.
6. **Serial number format.** Generated by the system, or supplied by the vendor
   or the customer?
7. **Partial dispatch.** May a sales-order line be dispatched in more than two
   instalments, and is there a minimum dispatch quantity?

8. **Staffing policy.** Do operators need employee records without logins? Are helpers on one machine allowed? What holidays, weekly offs and minimum rest apply? Is a weekly publish/lock step required?

## Warehouse assignment policy for A02/A03

An empty warehouse assignment denotes unrestricted access within the company;
platform superusers use that same exemption. Company isolation always applies.
Transfers require assignment to both warehouses, matching their existing create
rule. Adjustments/counts require their single warehouse. Procurement/production
destinations and every grain touched by a stock movement are checked with this
same policy, including documents raised before the restriction. Trusted system
contexts use an empty assignment; user-driven operations retain the caller's
assignment. This clarifies existing assignment semantics without changing the
database schema.
