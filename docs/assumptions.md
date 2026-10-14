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

## 32. Customer charges are receivable journals, not invoices

**Assumed.** No customer-invoice document exists. Customer charges are posted as
receivable journals through finance (`post_customer_adjustment`,
`POST /finance/customer-adjustments`, gated on `payment.approve`), and the
cached `customers.outstanding_amount` is only ever moved alongside such a
journal - `OutstandingAdjustment.reference_type` / `reference_code` are
required for that reason. The former `POST /customers/{id}/outstanding` route,
which moved the cache with no ledger document, was removed.

**Why.** A cached balance that can change without a ledger row is a figure
nobody can reconcile: the ageing report reads the ledger, the credit check reads
the cache, and the two drift apart on the first hand edit. Requiring the
reference makes every movement of the cache traceable to the document that
caused it.

**Where.** `src/modules/finance/service.py` - `post_customer_adjustment`;
`src/modules/customers/schemas.py` - `OutstandingAdjustment`.

**Impact if wrong.** If Atcraft needs a real customer invoice - a statutory GST
series, e-invoicing - that is a new document with its own numbering and posting
path, and the adjustment route becomes the correction path beside it, not the
charge path.

---

## 33. Vendor portal scope

**Assumed.** A vendor principal's purchase-order reads and row locks are
confined at `PurchaseOrderRepository._scoped` to that vendor's own orders -
and, within those, to orders it has been sent and to its categories (§56).
`PATCH /purchase-orders/{id}` and `POST /purchase-orders/{id}/submit` are
buyer-only (`forbid_vendor_users`), even for the order's own vendor;
acknowledge and shipment recording stay vendor-reachable.

**Why.** Confining the query rather than the route means a vendor cannot reach
another vendor's order by any path, including the locks a status change takes.
Editing and submitting an order is the buyer's act: a vendor who could amend
quantities or prices on an order addressed to them would be writing their own
contract. Acknowledging it and recording the shipment are the two things the
portal exists for.

**Where.** `src/modules/procurement/repository.py` -
`PurchaseOrderRepository._scoped`; `src/modules/procurement/router.py` - the
`forbid_vendor_users` dependencies on the two routes.

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

**Where.** The `cost_centre_id` columns (migration `3746fadd0b86`);
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
ADMIN) gets both through migration `e1eed3775093`; a client-owned role on an
existing deployment does not, and is granted from Settings -> Roles.

**Why.** The centre is the analytic dimension of the ledger, so its master is
the accounts team's to keep; buyers and the shop floor name centres on their
documents and need to see them. Client-owned roles are never rewritten from a
preset - the `2f5e08c7ba61` precedent.

**Where.** `src/core/permissions.py`; migration `e1eed3775093`.

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
migration `159ffb5b9cb4` (the backfill).

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

## 54. The right that makes a vendor user an order's assignee is `purchase_order.update`

**Assumed.** A vendor user is eligible to be assigned a purchase order when they
are active, live, bound to the order's vendor and - after their roles, direct
grants and denials are folded exactly as the request path folds them - hold
`purchase_order.update`, the right the portal's acknowledge and shipment actions
use today. It is one named constant, `VENDOR_PO_RESPOND_PERMISSION`.

**Why.** An order assigned to somebody who cannot act on it is an order nobody at
the vendor answers. Folding the rights with the same function as
`core.deps._resolve_permissions` means "may act" cannot mean two things.

**Where.** `src/modules/procurement/service_rules.py` -
`VENDOR_PO_RESPOND_PERMISSION`, `is_eligible_assignee`;
`src/core/permissions.py` - `resolve_role_grants`.

**Impact if wrong.** A dedicated vendor-portal right replaces it by changing the
constant (planned for the vendor portal work package).

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

**Assumed.** At approval, the still-unposted vendor debit on the invoiced receipt lines posts as Dr AP against the vendor / Cr job-work expense under `job_work_recovery`, in the same transaction as the bill. The vendor's net payable is the invoice minus the recoveries. Billing beyond a receipt line's unbilled charges is refused outright - no override reason can approve it - and is re-checked under a row lock at approval, when the money actually moves.

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
