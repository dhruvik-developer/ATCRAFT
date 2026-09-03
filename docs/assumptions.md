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

**Why.** The two states are genuinely different and a single nullable number has
to carry both. Treating `0` as "unlimited" would be dangerous by default; a
customer set up with no limit yet would silently get infinite credit.

**Where.** `src/modules/customers/service.py` — `assert_within_credit_limit`.

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

**Where.** `src/modules/job_work/service.py` — the receipt path.

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
(`SO`, `INV`, `PO`, `GRN`, …) reset each financial year and are formatted
`PREFIX/2026-27/00042`. Internal documents (stock transfer, adjustment,
physical count) do not reset.

**Why.** Indian statutory convention, and the company master already carries
`financial_year_start_month` so the assumption is configurable rather than
baked in.

**Where.** `src/common/codes.py` — `DocTypes` and `financial_year_key`.

**Impact if wrong.** Document numbering would have to be migrated, which is
disruptive once documents exist. Confirm before go-live.

---

## 14. Refresh-token storage in the browser

**Assumed.** The access token lives in memory only; the refresh token is
persisted in `localStorage` so a page reload does not sign the user out.

**Why.** It keeps the access token out of storage while preserving the session
across reloads. The stronger option — an httpOnly `SameSite=Strict` refresh
cookie — needs the API to set cookies and a CSRF strategy on the refresh
endpoint.

**Where.** `apps/web/src/lib/api-client.ts`, and `docs/security.md` records it
as a known gap.

**Impact if wrong.** An XSS payload could exfiltrate the refresh token. This is
the single most valuable hardening step before public internet exposure.

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
is the single place the split is computed. Both the explosion decision and the
requirement stage read the same object, so they cannot phase the same item at two
different dates.

**Impact if wrong.** If Atcraft plans against a firmer promise date than the
system holds, the horizon is being cut too finely and suggestions will be raised
for stock that was going to arrive in time anyway.

---

## 22. Component demand is not netted against in-progress production

**Assumed.** When a manufactured parent is exploded into component demand, the
exploded quantity is netted against the parent's free stock and against confirmed
incoming **purchase orders**, but **not** against in-progress production orders.
The parent's own requirement still nets against in-progress production.

**Why.** A purchase order for the parent arrives as a finished parent and consumes
no components. A live production order *is* a build that still needs its parts —
and releasing one does not guarantee they are held: `release_order` reserves
components only when `ReleaseRequest.reserve_material` asks it to, and
`_reserve_components` holds only what happened to be free, recording the rest as a
shortage while the job is released anyway. Netting against it therefore deletes
component demand for a job whose parts were never secured. Where a job did
reserve, the hold is already excluded from available stock, so nothing is
double-counted; the residual is a bounded over-order, which is the cancellable
direction.

**Where.** `src/modules/mrp/engine.py` — `_settle_demand`, at the explosion
netting.

**Impact if wrong.** Component suggestions are higher than strictly necessary
where jobs really are fully kitted. Reworking this needs a reliable "this job's
components are secured" signal, which does not exist today.

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

## Open questions for Atcraft

These could not be assumed responsibly and need a decision before the relevant
module is used in production:

1. **Inventory valuation method.** Weighted average is implemented. Is FIFO
   required for statutory reporting?
2. **Approval thresholds.** At what value does a purchase order, a stock
   adjustment or a price override require a second approval? Currently a single
   configurable threshold per document type.
3. **Multi-currency.** Everything is INR today. Are imports or exports priced in
   another currency?
4. **Batch and shelf life.** Which item groups genuinely require batch tracking,
   and is shelf-life expiry a blocking rule or a warning?
5. **Vendor portal scope.** Which vendor roles may see pricing? The current
   split shows a vendor their own purchase-order prices only.
6. **Serial number format.** Generated by the system, or supplied by the vendor
   or the customer?
7. **Partial dispatch.** May a sales-order line be dispatched in more than two
   instalments, and is there a minimum dispatch quantity?
