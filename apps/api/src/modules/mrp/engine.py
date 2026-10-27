"""The MRP calculation: demand in, shortages and suggestions out.

This implements the flow master rules section 14 fixes, and database.md section
6 restates, with the engine-depth step (E3a) built in:

    sales orders (+ manual demand)
      -> one bucket per item: the common-part merge
      -> low-level codes: the deepest level each item appears at
      -> level by level, products first:
           net against free stock, then against in-time supply
           explode only the net shortfall into the item's own structure
      -> shortage per item
      -> suggestions

Query strategy - read this before changing anything here
--------------------------------------------------------
Everything the calculation needs is loaded for a *set* of items. A statement
issued inside a per-item loop is a defect, not a slow path (master rules section
32, performance.md). One run costs, after the demand lines the caller supplies:

1. one statement for the planning facts and one for the effective structures of
   the items each level of the structure introduces - two statements per level
   of the deepest structure in scope, never one per part;
2. one statement for the stock positions of every discovered item;
3. one statement each for confirmed incoming purchase orders and in-progress
   production output, over the whole discovered set;
4. one statement per level for the shop lead time of the short, manufactured
   items at that level that have an effective routing: the routing module
   answers a whole level in one call;
5. one statement for the preferred-vendor terms of every item a suggestion will
   be raised for.

So the cost grows with the *depth* of the deepest structure in scope, never with
the number of parts, demand lines or branches. :class:`MrpDataSource` exists so
this is *testable*: the unit suite drives the engine through an in-memory source
that counts calls, which turns "no N+1" from a claim in a comment into a failing
test if anyone reintroduces a per-item fetch.

Level-by-level netting (E3a)
----------------------------
Each item is planned once, at its **low-level code**: the deepest level at which
it appears anywhere in the structures in scope. Processing strictly by that code
means every parent has already been netted and exploded by the time the child is
reached, so the child's gross requirement - direct demand plus dependent demand
from every branch that needs it - is complete before any supply decision is
taken against it. With 50 Rose Handles in stock, 100 lock kits raise parts for 50
handles, not 100; and an item that is both sold and used as a component is netted
against one summed requirement, however deep the structure above it.

The ordering rests on one property of a bill of materials: an item's structure
is the same wherever the item occurs, so an item reached at level N reaches its
children at level N+1. The deepest occurrence of the parent therefore bounds
each child strictly below it, a child's code is always greater than every
parent's, and a structure that cannot be levelled is refused
(:class:`MrpStructureTooDeepError`) rather than followed.

Two behaviours are the reason this module exists
------------------------------------------------
**Common parts merge across demands.** Two different sales orders that both need
``SCREW-M4`` produce *one* requirement for the summed quantity, whether the screw
was reached directly, through one BOM branch, or through five. The merge is keyed
on ``item_id`` and every contribution is retained in ``source_demands`` so the
number stays auditable. ``mrp_requirements`` is unique on ``(run_id, item_id)``,
so a regression here fails at the database rather than in a buyer's inbox.

**An item that is both sold and used as a component is netted once.** If 100 of
``X`` are sold, 100 more are consumed by a kit, and 60 are in stock, the shortage
is 140 - not 80. The stock pool is consumed by the direct demand first and only
the remainder is offered to the dependent demand, which falls out of computing
the netting against the *summed* gross requirement rather than against each
demand separately. :func:`net_after_stock` is where that single netting lives,
and it is unit-tested against exactly this case.

Supply is time-phased - a bucket per arrival date, not one lump
---------------------------------------------------------------
Inbound supply is read as *dated tranches* (:class:`SupplyArrival`) and each
requirement is netted only against the tranches landing on or before the date
that requirement is needed. A purchase order arriving in six weeks is not supply
for a line shipping on Thursday, and netting it against one is the difference
between a plan that orders nothing and a stockout on the shop floor.

The date a requirement is "needed" is its ``earliest_required_date`` - the
earliest of every demand merged into it, and for a component the parent's order
start: the parent's required date less the parent's lead time, taken level by
level, never once from the root. A merged row cannot be half early and half late,
so the earliest date governs the whole quantity: supply landing between the two
dates covers the later demand in reality and is not counted here, which
over-states the shortage rather than under-stating it. That is the direction this
module takes whenever it has to choose.

**Nothing is read while the loop that computes it is still mutating it.** The
date that governs an item's netting is final before the item is netted: direct
demand is merged before the pass starts and every dependent contribution comes
from a parent at a strictly lower level, processed in an earlier iteration. The
fixed point the previous implementation needed is gone rather than detected - the
ordering makes the mutation impossible, which is what CLAUDE.md's MRP trap asks
for.

The alternative - one lump per item for the whole horizon, which is what this
engine did before - reads identically in every report while silently erasing a
genuine shortage, so the split is made where the arithmetic happens
(:func:`split_supply`) and the part that was excluded is never dropped in
silence: it is summed into the per-feed late quantities on the requirement,
each dated by its own interval, counted in the run's stats and named in a
warning, so a planner can see the supply exists, when it lands, and that the
plan did not count on it. The requirement row carries all of it as typed
columns rather than leaving a client to read the figures out of warning prose.

One interval per feed, not one date per row. Purchase orders, production output
and job-work returns are independent schedules; a single date beside their
quantities belongs to at most one of them. ``first_late_supply_date`` still
exists - it is the earliest across all feeds - but answers "how soon could any
of this be relieved", not "when does that quantity land".

Lead times (E3a)
----------------
A manufactured item's lead time is its effective routing's shop time for the
quantity being made (``routing.service.manufacture_lead_times``), falling back
to ``items.lead_time_days`` where no routing exists; a purchased item's
suggestion keeps its preferred vendor's lead time. The lead time is applied per
level: a component is required when the parent's order starts, and the parent's
order starts its own lead time before the parent is needed - a ladder, not one
subtraction from the root. And because the order start is the last date an order
can still land in time, a suggestion whose order date has passed is dated from
``as_of`` instead (:func:`expected_availability`).

Expected availability (E3a)
---------------------------
A requirement carries the date its gross requirement is fully covered - by
stock from ``as_of``, by dated supply in arrival order, or by the order the plan
raises when neither is enough - and a suggestion carries ``max(as_of,
suggested_order_date) + lead_time``, the earliest date its order can physically
land. A parent's expected date is then pulled no earlier than its components'
dates plus its own lead time, so one late part moves the assembly it feeds
instead of disappearing into a lead-time roll-up.

Job-work supply
---------------
Expected good output from issued, unaccounted-for job-work input is netted at
the order's expected return date within the run's warehouse scope. A missing
feed produces an explicit warning and an upper-bound suggestion, just like
unavailable purchase-order or production feeds.

Rounding: every quantity is ``Decimal`` and is quantised with
:func:`~src.core.types.to_quantity` as it is produced, so a plan's printed lines
add up to its printed totals.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any, Protocol

from src.core.demand_provenance import (
    MAX_ROOT_DEMAND_REFS,
    DemandReference,
    RootDemandProvenance,
)
from src.core.errors import BusinessRuleError, ErrorCode
from src.core.logging import get_logger
from src.core.types import ZERO, to_quantity
from src.modules.bom.engine import apply_scrap, line_requirement
from src.modules.bom.models import DEFAULT_MAX_EXPLOSION_DEPTH
from src.modules.mrp.models import (
    MAX_DEMAND_LINES,
    MAX_SOURCE_DEMANDS_LISTED,
    MrpDemandSource,
    MrpSuggestionType,
)
from src.modules.products.models import ProductionType

log = get_logger(__name__)

#: Working precision for the order-multiple division. Wide enough that rounding
#: 12345.6789 up to a multiple of 0.0001 cannot lose a digit before the final
#: quantisation.
PLANNING_PRECISION = 34

#: Default priority for a demand line that does not carry one, matching the
#: sales module's own default so a manual line and an order line sort together.
DEFAULT_DEMAND_PRIORITY = 5


class MrpDemandTooLargeError(BusinessRuleError):
    """The run was handed more demand than a single plan should carry.

    Almost always an unbounded horizon over years of history rather than a
    genuine order book. Refusing is better than spending twenty minutes
    producing a plan nobody asked for.
    """

    def __init__(self, line_count: int, limit: int) -> None:
        super().__init__(
            f"This run covers {line_count} demand lines, which is beyond the "
            f"{limit}-line limit. Narrow the horizon or the warehouse scope.",
            details={"demand_line_count": line_count, "limit": limit},
        )


# -----------------------------------------------------------------------------
# Inputs
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemandLine:
    """One piece of independent demand the run must cover.

    ``quantity`` is what is *still* needed: for a sales-order line that is the
    ordered quantity less what has already shipped and less what is already
    reserved for it. Excluding the reserved part here is one half of the
    "reserved stock is not available" rule - the other half is that
    :attr:`StockPosition.available` excludes reservations too, so a reservation
    is neither counted as free supply nor demanded a second time.
    """

    item_id: uuid.UUID
    quantity: Decimal
    required_date: date
    source: MrpDemandSource
    source_id: uuid.UUID | None = None
    source_line_id: uuid.UUID | None = None
    source_code: str | None = None
    warehouse_id: uuid.UUID | None = None
    priority: int = DEFAULT_DEMAND_PRIORITY
    notes: str | None = None
    #: Stable identity of the snapshotted input, including manual demand.
    demand_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PlanningItem:
    """The item-master facts the planner reads.

    A narrow projection rather than the ``Item`` entity: ``items`` is a very wide
    table and a run touches every part in every structure, so loading nine
    capability flags, three prices and a valuation method to use eight columns is
    exactly the ``SELECT *`` the rules forbid.
    """

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    production_type: str
    lead_time_days: int
    min_order_quantity: Decimal
    safety_stock: Decimal
    can_purchase: bool
    can_manufacture: bool
    can_job_work: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class StockPosition:
    """Summed stock pools for one item over the warehouses the run is scoped to.

    ``available`` reproduces ``inventory_balances.available_quantity`` - the
    generated column defined as ``on_hand - reserved - qc_hold - damaged``
    (database.md section 4.3). It is computed here from the summed pools rather
    than read back so that "reserved stock is not available to a new demand" is a
    property of the planner that a unit test can assert, and so a requirement row
    can report *why* free stock is lower than on-hand. Summing the pools and
    subtracting is arithmetically identical to summing the generated column.
    """

    on_hand: Decimal = ZERO
    reserved: Decimal = ZERO
    qc_hold: Decimal = ZERO
    damaged: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        """Stock a new demand may actually be planned against."""
        free = self.on_hand - self.reserved - self.qc_hold - self.damaged
        return to_quantity(max(free, ZERO))


@dataclass(frozen=True, slots=True)
class SupplyArrival:
    """One dated tranche of supply that already exists for an item.

    A purchase-order line's undelivered balance, or a live job's unmade balance,
    together with the date it is promised for. The *date* is the whole point:
    supply is only supply for a demand it arrives in time for, and a planner that
    is handed one summed figure per item has no way to tell the difference
    (:func:`split_supply`).

    Quantities are magnitudes and are already floored at zero by the module that
    produced them - an over-received line contributes nothing rather than a
    negative that would cancel another line's genuine balance.
    """

    due_date: date
    quantity: Decimal


#: Dated inbound supply for a set of items. One entry per item, each a tuple of
#: tranches in date order. An item with nothing inbound is simply absent.
SupplySchedule = dict[uuid.UUID, tuple[SupplyArrival, ...]]


@dataclass(frozen=True, slots=True)
class SupplyTerms:
    """Ordering policy for one item.

    Resolved from the preferred vendor link where there is one, falling back to
    the item master. A vendor that ships in cartons of 50 cannot supply 51, and
    their lead time - not a generic one on the item - is what decides when the
    order has to be placed.
    """

    lead_time_days: int = 0
    min_order_quantity: Decimal = ZERO
    order_multiple: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class BomComponent:
    """One component line of one item's structure, one level deep.

    A deliberately small shape rather than the BOM module's own exploded line:
    the planner needs the multiplier and the scrap allowance, and depending on
    the rest would make this engine's unit tests hostage to changes in the other
    module's result type. ``quantity_per`` is stated per
    :attr:`BomStructure.output_quantity` units of the parent, exactly as the
    line stores it.
    """

    item_id: uuid.UUID
    quantity_per: Decimal
    scrap_percentage: Decimal


@dataclass(frozen=True, slots=True)
class BomStructure:
    """One item's effective structure, one level deep.

    What replaced the consolidated multi-level explosion line: the planner now
    descends level by level itself, so it needs the *edge* - this item's own
    lines and the structure's yield - not somebody else's flattened result.
    ``is_phantom`` is carried because a phantom is blown through rather than
    planned as an item, and ``is_default`` because a ``MAKE_OR_BUY`` item with
    only a non-default structure is bought rather than built
    (:func:`choose_suggestion_type`).
    """

    output_quantity: Decimal
    is_default: bool
    is_phantom: bool
    lines: tuple[BomComponent, ...]


# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """The merged, netted requirement for one item."""

    item_id: uuid.UUID
    item_code: str
    item_name: str
    level: int
    #: The deepest level this item appears at across the structures in scope -
    #: the order the planner actually planned in. :attr:`level` stays the
    #: *shallowest* (what a planner reads downward from); the two differ when a
    #: component sits under several branches at different depths, which is
    #: exactly the case the low-level code exists to net correctly.
    low_level_code: int
    direct_demand_quantity: Decimal
    dependent_demand_quantity: Decimal
    gross_requirement: Decimal
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    #: Inbound supply that arrives on or before :attr:`earliest_required_date`
    #: and was therefore netted off the requirement.
    incoming_quantity: Decimal
    in_production_quantity: Decimal
    job_work_quantity: Decimal
    net_requirement: Decimal
    shortage_quantity: Decimal
    earliest_required_date: date
    #: The date the gross requirement is fully covered: stock from ``as_of``,
    #: dated supply in arrival order, or the order the plan raises when neither
    #: is enough - and then no earlier than the item's own components plus its
    #: lead time. ``None`` only when the row is written before the plan's own
    #: order has been dated (the engine fills it in before the plan is
    #: returned), never on a persisted run.
    expected_available_date: date | None
    source_demand_count: int
    source_demands: tuple[dict[str, Any], ...]
    #: Inbound supply inside the run's horizon that arrives *after* the date
    #: this requirement needs it, so it was NOT netted. Reported rather than
    #: dropped: a plan that quietly ignores a known inbound order is its own
    #: trap, and a buyer looking at the shortage has to be able to see that the
    #: order exists and is simply too late.
    late_incoming_quantity: Decimal = ZERO
    late_in_production_quantity: Decimal = ZERO
    late_job_work_quantity: Decimal = ZERO
    #: The date each of those quantities starts landing on - one per feed, so a
    #: consumer can render "5000 arriving 2026-11-12" and "0.0001 arriving
    #: 2026-07-16" as two separate facts. ``None`` when that feed has no late
    #: supply at all: "nothing was excluded" and "something was excluded and
    #: lands today" are different answers and a sentinel date would merge them.
    #:
    #: ASSUMPTION: the *earliest* late tranche of each feed is the one reported,
    #: and a feed with no late supply reports ``None`` rather than any date.
    #: Section 14 does not discuss reporting the excluded supply at all. The
    #: earliest is the soonest that feed could relieve the shortage without a
    #: new order, which is the question a planner is actually asking; a later
    #: tranche is worse news and reporting it would over-state the delay. A
    #: feed's full schedule is not summarised into one date - the quantity
    #: beside it says how much, and the orders themselves say the rest. ``None``
    #: rather than the required date or a far-future sentinel because "this feed
    #: excluded nothing" and "this feed lands on that day" are different facts,
    #: and a sentinel makes a client's date arithmetic silently wrong instead of
    #: making the absence visible.
    first_late_incoming_date: date | None = None
    first_late_in_production_date: date | None = None
    #: When the LAST late tranche of each feed lands - the date by which the
    #: whole late quantity beside it is in. A late quantity is a SUM and its
    #: first date a MINIMUM, so the two together only describe a single-tranche
    #: feed; these close the interval so a consumer can state the fact fully.
    last_late_incoming_date: date | None = None
    last_late_in_production_date: date | None = None
    first_late_job_work_date: date | None = None
    last_late_job_work_date: date | None = None

    @property
    def first_late_supply_date(self) -> date | None:
        """The earliest late date across all supply feeds, or ``None``.

        The run-level "how soon could this be relieved at all" answer, kept
        because it is what the warning sorts on and what existing consumers
        already read. It is *not* the date for either quantity beside it - each
        of those carries its own - and it is derived here rather than stored so
        it cannot drift from the two dates it summarises.
        """
        dates = [
            when
            for when in (
                self.first_late_incoming_date,
                self.first_late_in_production_date,
                self.first_late_job_work_date,
            )
            if when is not None
        ]
        return min(dates) if dates else None


@dataclass(frozen=True, slots=True)
class Suggestion:
    """What to do about one item's shortage."""

    item_id: uuid.UUID
    item_code: str
    suggestion_type: MrpSuggestionType
    required_quantity: Decimal
    suggested_quantity: Decimal
    shortage_quantity: Decimal
    available_quantity: Decimal
    incoming_quantity: Decimal
    required_date: date
    suggested_order_date: date
    is_late: bool
    lead_time_days: int
    #: The same low-level code the requirement carries, so the work queue and
    #: the requirement table can be read side by side.
    low_level_code: int
    #: ``max(as_of, suggested_order_date) + lead_time_days``: the earliest date
    #: this order can physically land, which for a late suggestion is later than
    #: the date the requirement needed it (see :func:`expected_availability`).
    expected_available_date: date
    source_demands: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class MrpPlan:
    """Everything one run produced."""

    requirements: tuple[Requirement, ...]
    suggestions: tuple[Suggestion, ...]
    #: Counts and timings for ``mrp_runs.result_summary``.
    stats: dict[str, Any]
    #: Conditions a planner has to know about before trusting the numbers: a
    #: netting input that could not be read, a make item with no structure.
    #: Never swallowed - an unstated gap in the netting is an over-order.
    warnings: tuple[str, ...]

    def requirement_for(self, item_id: uuid.UUID) -> Requirement | None:
        return next((r for r in self.requirements if r.item_id == item_id), None)

    def suggestion_for(self, item_id: uuid.UUID) -> Suggestion | None:
        return next((s for s in self.suggestions if s.item_id == item_id), None)


# -----------------------------------------------------------------------------
# Pure arithmetic - unit tested without a database
# -----------------------------------------------------------------------------


def planning_available(position: StockPosition, safety_stock: Decimal, *, respect: bool) -> Decimal:
    """Free stock the run is allowed to plan against.

    ASSUMPTION: safety stock is *protected* by default - it is held back from
    the netting rather than consumed by it, so a plan never proposes running the
    buffer down to zero. Section 14 does not say either way, and this is the
    conservative direction: the failure mode is ordering slightly early, not
    discovering the buffer was planned away. ``respect`` makes it a per-run
    decision (``respect_safety_stock`` on the request) so a planner deliberately
    drawing the buffer down can say so.
    """
    free = position.available
    if respect and safety_stock > 0:
        free = free - safety_stock
    return to_quantity(max(free, ZERO))


def net_after_stock(gross_requirement: Decimal, available: Decimal) -> Decimal:
    """Gross requirement less free stock, floored at zero.

    This single subtraction is what stops an item that is *both sold directly and
    consumed by a BOM* from having its stock counted twice. The two demands are
    summed into ``gross_requirement`` first and the stock pool is netted against
    the sum, so 100 sold + 100 consumed against 60 in stock is a shortage of 140.
    Netting each demand against the same 60 separately would report 80 and
    under-order by 60 - the defect this function exists to prevent.
    """
    return to_quantity(max(gross_requirement - available, ZERO))


def split_supply(
    arrivals: Sequence[SupplyArrival] | None, needed_by: date
) -> tuple[Decimal, Decimal]:
    """Divide one item's known supply at the date the demand needs it.

    Returns ``(in_time, late)``.

    ASSUMPTION: supply promised *on* ``needed_by`` counts. Section 14 says "net
    against confirmed incoming POs (by expected date)" without fixing the
    boundary, and a delivery due the day the line ships is supply for it -
    excluding it would raise a duplicate order for stock already arriving, and
    the two documents would sit side by side for the same shortage. One day
    later does not count, however close: a plan cannot ship what has not landed.

    This is the whole of the time dimension, and it is a pure function so the
    boundary is a unit test rather than a claim. Netting a requirement against
    supply that lands after it needs it erases a real shortage and reports
    success - the plan orders nothing and the floor runs out - which is strictly
    worse than the over-order that leaving late supply out can cause, because an
    unnecessary purchase order is visible and cancellable.
    """
    in_time = ZERO
    late = ZERO
    for arrival in arrivals or ():
        if arrival.quantity <= 0:
            continue
        if arrival.due_date <= needed_by:
            in_time += arrival.quantity
        else:
            late += arrival.quantity
    return to_quantity(in_time), to_quantity(late)


def _late_arrival_dates(arrivals: Sequence[SupplyArrival] | None, needed_by: date) -> list[date]:
    """The dates of exactly the tranches :func:`split_supply` counted as late.

    Same predicate as its ``late`` half, deliberately: a date drawn from a
    different rule than the quantity beside it would describe supply that
    quantity does not.
    """
    return [a.due_date for a in arrivals or () if a.quantity > 0 and a.due_date > needed_by]


def earliest_late_arrival(arrivals: Sequence[SupplyArrival] | None, needed_by: date) -> date | None:
    """When the FIRST excluded tranche of this feed lands. ``None`` if none was."""
    dates = _late_arrival_dates(arrivals, needed_by)
    return min(dates) if dates else None


def latest_late_arrival(arrivals: Sequence[SupplyArrival] | None, needed_by: date) -> date | None:
    """When the LAST excluded tranche of this feed lands - when all of it is in.

    A late quantity is a SUM over tranches while its earliest date is a MINIMUM
    over the same tranches, so the pair is only a whole fact when the feed had
    one tranche. With several, "5010 arriving 2026-07-30" is read as all 5010
    landing that day when 5000 of it lands in December - the earliest date
    understates the lateness, which is the dangerous direction: a planner reads
    it as nearly here.

    Reporting both bounds is what makes the quantity honest: the consumer can
    say "the first of 5010 lands 2026-07-30, all of it by 2026-12-15" and
    nothing is implied that is not true. One date and a sum cannot say that, and
    picking either bound alone misleads in one direction or the other.
    """
    dates = _late_arrival_dates(arrivals, needed_by)
    return max(dates) if dates else None


@dataclass(frozen=True, slots=True)
class SupplySplit:
    """One item's whole inbound supply, divided at the date the item is needed.

    THE time-phasing decision for one item, taken once and carried, rather than
    taken twice and hoped to agree. Every stage that cares about supply for an
    item - the netting that sizes its shortage and the explosion of that
    shortage into its components - reads *this* object, so the date they phase
    at is the same date by construction and not by two callers remembering to
    pass the same argument.

    That is not a tidiness point. An earlier shape read ``bucket.earliest_date``
    for the explosion while the merge loop was still pulling that same date
    earlier, and read it again, later and earlier, for the requirement. One item
    was netted for its explosion at one date and for its own shortage at
    another: the explosion saw an inbound order as in time, the requirement saw
    it as late, and the components for the shortage that resulted were never
    raised. Level-by-level planning removed the moving date - an item is netted
    only after every parent has been - and this type keeps the stages of one
    item reading the same answer, taken at that final date.
    """

    #: The date the split was taken at - the item's settled earliest required
    #: date. Carried so a consumer can prove which date produced these figures.
    needed_by: date
    #: Purchase-order balances arriving on or before :attr:`needed_by`.
    incoming: Decimal = ZERO
    #: ...and those inside the horizon that arrive after it. Never dropped.
    late_incoming: Decimal = ZERO
    #: Live production output promised on or before :attr:`needed_by`.
    in_production: Decimal = ZERO
    late_in_production: Decimal = ZERO
    job_work: Decimal = ZERO
    late_job_work: Decimal = ZERO
    #: The earliest date the excluded tranche of *this* feed lands, one per feed
    #: and beside the quantity it describes. ``None`` when that feed excluded
    #: nothing.
    #:
    #: One date across both feeds is the defect these two replace: a purchase
    #: order for 5 000 landing in November and a job for 0.0001 landing tomorrow
    #: produced "5000 arriving tomorrow" the moment a consumer read the single
    #: date beside the larger quantity. A quantity and a date belong to the same
    #: feed or they are not one fact.
    first_late_incoming: date | None = None
    first_late_in_production: date | None = None
    first_late_job_work: date | None = None
    #: When the LAST late tranche of each feed lands. The late quantity above is
    #: a SUM over that feed's late tranches and ``first_late_*`` is the MINIMUM
    #: over the same tranches, so the pair describes the whole feed only when it
    #: had one tranche. With several, the earliest date UNDERSTATES how late the
    #: bulk is - the dangerous direction, because it reads as nearly here. These
    #: close the interval: "the first of 5010 on 2026-07-30, all of it by
    #: 2026-12-15" is a fact; "5010 on 2026-07-30" is not.
    last_late_incoming: date | None = None
    last_late_in_production: date | None = None
    last_late_job_work: date | None = None

    @property
    def late_total(self) -> Decimal:
        """Everything the phasing excluded, from any source."""
        return to_quantity(self.late_incoming + self.late_in_production + self.late_job_work)

    @property
    def first_late_arrival(self) -> date | None:
        """The earliest date across all feeds on which excluded supply lands.

        The soonest the shortage could be relieved without a new order, which is
        the run-level question. It says nothing about *how much* lands then -
        that is the per-feed pair above, and reading this date beside either
        quantity is the misreading the pairs exist to prevent.

        Derived rather than stored: a summary that can be set independently of
        the figures it summarises is a summary that can contradict them.
        """
        dates = [
            when
            for when in (
                self.first_late_incoming,
                self.first_late_in_production,
                self.first_late_job_work,
            )
            if when is not None
        ]
        return min(dates) if dates else None


def supply_split(
    needed_by: date,
    *,
    incoming: Sequence[SupplyArrival] | None = None,
    in_production: Sequence[SupplyArrival] | None = None,
    job_work: Sequence[SupplyArrival] | None = None,
) -> SupplySplit:
    """Phase each supply feed for one item at one date.

    The only function in this module that turns schedules into netting
    figures. Everything downstream reads the :class:`SupplySplit` it returns.
    """
    on_order, late_on_order = split_supply(incoming, needed_by)
    being_made, late_being_made = split_supply(in_production, needed_by)
    at_vendor, late_at_vendor = split_supply(job_work, needed_by)
    # Each feed's dates describe only its own quantity. The cross-feed earliest
    # is derived by :attr:`SupplySplit.first_late_arrival`.
    return SupplySplit(
        needed_by=needed_by,
        incoming=on_order,
        late_incoming=late_on_order,
        in_production=being_made,
        late_in_production=late_being_made,
        job_work=at_vendor,
        late_job_work=late_at_vendor,
        first_late_incoming=earliest_late_arrival(incoming, needed_by),
        first_late_in_production=earliest_late_arrival(in_production, needed_by),
        last_late_incoming=latest_late_arrival(incoming, needed_by),
        last_late_in_production=latest_late_arrival(in_production, needed_by),
        first_late_job_work=earliest_late_arrival(job_work, needed_by),
        last_late_job_work=latest_late_arrival(job_work, needed_by),
    )


def net_after_supply(
    net_requirement: Decimal,
    incoming: Decimal,
    in_production: Decimal,
    job_work: Decimal = ZERO,
) -> Decimal:
    """What is still missing once confirmed inbound supply is counted.

    Purchase orders, in-progress production and issued job-work output are
    supply that will exist without anybody doing anything further, so ordering
    against them again is a duplicate order.

    Both figures must already be time-phased by the caller: they are the *in
    time* half of :func:`split_supply`, the tranches arriving on or before the
    date this requirement is needed. Handing this function a whole horizon's
    supply is the defect it used to have - a purchase order landing six weeks
    after the line ships would net the shortage away to nothing.
    """
    remaining = net_requirement - incoming - in_production - job_work
    return to_quantity(max(remaining, ZERO))


def apply_order_policy(
    shortage: Decimal, *, min_order_quantity: Decimal, order_multiple: Decimal
) -> Decimal:
    """Turn a shortage into a quantity that can actually be ordered.

    The minimum is applied *before* the multiple, never the other way round: a
    supplier with a 50-piece minimum who ships in packs of 30 must be sent an
    order for 60, and rounding to the multiple first would leave 50 - a number
    they cannot fulfil. The result therefore never goes below the shortage, which
    ``mrp_suggestions`` also enforces as a check constraint.
    """
    if shortage <= 0:
        raise ValueError("order policy is only meaningful for a positive shortage")
    if min_order_quantity < 0 or order_multiple < 0:
        raise ValueError("order policy quantities cannot be negative")

    quantity = max(shortage, min_order_quantity)

    if order_multiple > 0:
        with localcontext() as ctx:
            ctx.prec = PLANNING_PRECISION
            packs = (quantity / order_multiple).to_integral_value(rounding=ROUND_CEILING)
            quantity = packs * order_multiple

    return to_quantity(quantity)


def order_date_for(required_date: date, lead_time_days: int) -> date:
    """When the order has to be placed to arrive in time.

    May land in the past. That is the answer, not an error: it is how the plan
    says a purchase needed raising last week, and the caller flags it with
    :func:`is_late_order`.
    """
    if lead_time_days < 0:
        raise ValueError("lead time cannot be negative")
    return required_date - timedelta(days=lead_time_days)


def is_late_order(suggested_order_date: date, as_of: date) -> bool:
    """True when the order should already have been placed."""
    return suggested_order_date < as_of


def choose_suggestion_type(item: PlanningItem, *, has_default_bom: bool) -> MrpSuggestionType:
    """Decide what to do about a shortage, from the item master.

    ``production_type`` is the item master's own statement of how the part is
    normally obtained, so it decides - never ``item_group``, which master rules
    section 1 fixes as a reporting classification with no business authority.

    * ``MAKE``        -> MANUFACTURE
    * ``BUY``         -> PURCHASE
    * ``JOB_WORK``    -> JOB_WORK
    * ``MAKE_OR_BUY`` -> MANUFACTURE when an approved default BOM exists,
      otherwise PURCHASE.

    The MAKE_OR_BUY rule is documented in ``docs/assumptions.md`` section 6: an
    approved structure is the strongest available evidence that the part is
    genuinely made in-house, and defaulting such items to purchase would make
    their BOM pointless. The planner sees and can change every suggestion before
    converting it, so a wrong guess costs noise rather than a wrong transaction.

    ASSUMPTION: a ``MAKE`` item with no approved BOM is still suggested for
    manufacture. The item master says it is made here; silently reclassifying it
    as a purchase would hide a missing BOM behind a purchase order. The run
    reports it as a warning instead so the gap gets fixed.
    """
    production_type = ProductionType(item.production_type)

    if production_type is ProductionType.MAKE:
        return MrpSuggestionType.MANUFACTURE
    if production_type is ProductionType.BUY:
        return MrpSuggestionType.PURCHASE
    if production_type is ProductionType.JOB_WORK:
        return MrpSuggestionType.JOB_WORK
    return MrpSuggestionType.MANUFACTURE if has_default_bom else MrpSuggestionType.PURCHASE


def is_explodable(item: PlanningItem, *, has_bom: bool) -> bool:
    """Whether a shortfall of this item should be exploded into components.

    Only items the company actually manufactures, and only when there is an
    approved structure effective today to explode. A purchased part with a BOM on
    file (kept for costing, or because it used to be made here) must not have
    that structure turned into material demand.
    """
    if not has_bom:
        return False
    production_type = ProductionType(item.production_type)
    if production_type is ProductionType.BUY:
        return False
    return item.can_manufacture or production_type in (
        ProductionType.MAKE,
        ProductionType.MAKE_OR_BUY,
    )


# -----------------------------------------------------------------------------
# Demand merge
# -----------------------------------------------------------------------------


@dataclass
class DemandBucket:
    """Mutable consolidation bucket for one item.

    The merge target. Everything about one item - direct demand, dependent demand
    from every branch, the earliest date anybody needs it, and the provenance of
    each contribution - accumulates here, which is what makes "one requirement
    per item" structural rather than a step somebody has to remember.
    """

    item_id: uuid.UUID
    direct: Decimal = ZERO
    dependent: Decimal = ZERO
    earliest_date: date | None = None
    level: int | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    source_count: int = 0
    root_refs: dict[DemandReference, None] = field(default_factory=dict, repr=False)
    root_refs_complete: bool = True

    def add_root_ref(self, ref: DemandReference | None) -> None:
        """Keep identities independently of the capped contribution listing."""
        if ref is None:
            self.root_refs_complete = False
        elif ref not in self.root_refs:
            if len(self.root_refs) < MAX_ROOT_DEMAND_REFS:
                self.root_refs[ref] = None
            else:
                self.root_refs_complete = False

    def inherit_root_refs(self, parent: DemandBucket) -> None:
        """Carry causal roots, without inventing per-demand quantity allocation."""
        for ref in parent.root_refs:
            self.add_root_ref(ref)
        self.root_refs_complete = self.root_refs_complete and parent.root_refs_complete

    def listed_sources(self) -> tuple[dict[str, Any], ...]:
        """Serialize typed aggregate metadata on the first existing display fact."""
        sources = [dict(source) for source in self.sources]
        if sources:
            provenance = RootDemandProvenance(
                root_demand_refs=list(self.root_refs),
                root_demand_refs_complete=self.root_refs_complete,
            )
            sources[0].update(provenance.model_dump(mode="json"))
        return tuple(sources)

    def add_source(self, entry: dict[str, Any]) -> None:
        """Record one contribution, keeping the listing bounded.

        The count is always exact; only the listing is capped, so a truncated
        list can never be mistaken for the complete provenance.
        """
        self.source_count += 1
        if len(self.sources) < MAX_SOURCE_DEMANDS_LISTED:
            self.sources.append(entry)

    def note_date(self, when: date) -> None:
        if self.earliest_date is None or when < self.earliest_date:
            self.earliest_date = when

    def note_level(self, level: int) -> None:
        """Keep the shallowest level the item was reached at.

        Direct demand is level 0 and always wins: an item a customer orders is
        one a planner recognises as a product, however deep it also sits in
        somebody else's structure.
        """
        if self.level is None or level < self.level:
            self.level = level


def merge_demand(demands: Sequence[DemandLine]) -> dict[uuid.UUID, DemandBucket]:
    """Consolidate independent demand by item.

    This is the first half of the common-part merge: two sales orders for the
    same product become one bucket before anything is exploded, so the product's
    BOM is exploded once for the combined shortfall rather than once per order.
    """
    if len(demands) > MAX_DEMAND_LINES:
        raise MrpDemandTooLargeError(len(demands), MAX_DEMAND_LINES)

    buckets: dict[uuid.UUID, DemandBucket] = {}
    for line in demands:
        if line.quantity <= 0:
            continue
        bucket = buckets.setdefault(line.item_id, DemandBucket(item_id=line.item_id))
        bucket.direct = to_quantity(bucket.direct + line.quantity)
        bucket.note_date(line.required_date)
        bucket.note_level(0)
        if line.source is MrpDemandSource.SALES_ORDER and line.source_id is not None:
            root_ref = DemandReference(
                demand_type="SALES_ORDER",
                demand_id=line.source_id,
                demand_line_id=line.source_line_id,
            )
        elif line.source is MrpDemandSource.MANUAL and line.demand_id is not None:
            root_ref = DemandReference(demand_type="MRP_DEMAND", demand_id=line.demand_id)
        else:
            root_ref = None
        bucket.add_root_ref(root_ref)
        bucket.add_source(
            {
                "source": str(line.source),
                "source_id": str(line.source_id) if line.source_id else None,
                "source_line_id": str(line.source_line_id) if line.source_line_id else None,
                "code": line.source_code,
                "quantity": str(to_quantity(line.quantity)),
                "required_date": line.required_date.isoformat(),
            }
        )
    return buckets


# -----------------------------------------------------------------------------
# Data source - the seam that makes the batching testable
# -----------------------------------------------------------------------------


class MrpDataSource(Protocol):
    """Everything the planner reads, always for a *set* of items.

    There is deliberately no single-item accessor: every acquisition is batched
    by construction, and the engine physically cannot ask per item for stock,
    planning facts, structures or supply. The engine's own processing loop is
    batched by *level* rather than by item (module docstring), which is why even
    the structure reader takes a set: one statement answers a whole level.
    """

    async def planning_items(
        self, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, PlanningItem]: ...

    async def stock_positions(
        self, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, StockPosition]: ...

    async def bom_structures(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, BomStructure]:
        """Each item's APPROVED structure effective on ``as_of``, one level deep.

        Absent means "nothing to explode": no approved structure is effective
        for that item on that date. A structure is returned even for an item the
        engine may decide not to explode - the production facts and the phantom
        flag are what :func:`is_explodable` decides on, and both live beside the
        structure so the two are read from one answer.
        """
        ...

    async def manufacture_lead_times(
        self, quantities: Mapping[uuid.UUID, Decimal], as_of: date
    ) -> dict[uuid.UUID, int]:
        """Shop days to make each quantity, from the item's effective routing.

        The batched form of the routing module's own
        ``manufacture_lead_time_days``: one call answers every short,
        manufactured item at one level. Items without an effective routing are
        simply absent, and the engine falls back to ``items.lead_time_days``.
        """
        ...

    async def supply_terms(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SupplyTerms]: ...

    async def incoming_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        """Confirmed purchase-order balances due on or before ``upto``, *by date*.

        One tranche per promised arrival date, not one lump per item. ``upto``
        still bounds what is read - an order landing beyond the run's horizon is
        not this plan's business - but the engine decides per requirement which
        of the tranches inside that horizon arrive in time to count
        (:func:`split_supply`). Returning a single summed figure is what made
        time-phasing impossible and let supply arriving weeks late erase a
        shortage.

        Returns the schedule and whether the netting could be performed at all.
        A ``False`` second element means the procurement module is not deployed:
        the plan then reports that it did not net against incoming orders rather
        than pretending there are none.
        """
        ...

    async def in_production_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        """Output still expected from production orders in progress, by due date."""
        ...

    async def job_work_supply(
        self, item_ids: Sequence[uuid.UUID], upto: date
    ) -> tuple[SupplySchedule, bool]:
        """Issued output still expected back from job-work vendors, by due date."""
        ...


# -----------------------------------------------------------------------------
# The calculation
# -----------------------------------------------------------------------------


#: How deep a structure the planner will follow before refusing. A low-level
#: code is only bounded when the structure is acyclic - a cycle assigns one
#: deeper level per pass and never stops - so the ceiling is the same one the
#: BOM engine puts on its own traversal (:data:`DEFAULT_MAX_EXPLOSION_DEPTH`):
#: a structure the BOM module refuses to explode is one the planner must refuse
#: to level, rather than a second opinion about the same data.
MAX_STRUCTURE_LEVELS = DEFAULT_MAX_EXPLOSION_DEPTH


class MrpStructureTooDeepError(BusinessRuleError):
    """A structure deeper than any real bill of materials - almost always a cycle.

    Level-by-level planning needs every level to be finite: a child's low-level
    code must be strictly greater than every parent's, and only an acyclic
    structure has that property. Following a cycle is not a slow answer, it is
    no answer, so the run refuses and names the items at the depth that was
    reached. The error code is the BOM module's ``BOM_DEPTH_EXCEEDED`` because
    the fix is the same one: the structure in the data, not the planner.
    """

    code = ErrorCode.BOM_DEPTH_EXCEEDED

    def __init__(self, item_codes: Sequence[str], max_depth: int) -> None:
        unique = sorted(set(item_codes))
        listed = ", ".join(unique[:10])
        suffix = f" (and {len(unique) - 10} more)" if len(unique) > 10 else ""
        super().__init__(
            f"Following the bills of materials reached {max_depth} levels, which "
            "is deeper than any real structure and almost always means a cycle "
            f"between: {listed}{suffix}. Fix the structure and re-run.",
            details={"max_structure_levels": max_depth, "item_codes": unique[:10]},
        )


def _component_required_date(parent_needed_by: date, parent_lead_time_days: int) -> date:
    """When a component must be available: the parent's order start.

    The parent's build starts :attr:`~PlanningItem.lead_time_days` before it is
    needed, and that is the date its components have to be there. One
    subtraction per level, applied as the pass descends, is what makes the
    offset a ladder rather than a single subtraction from the root (E3a).
    """
    return order_date_for(parent_needed_by, parent_lead_time_days)


def component_requirement(
    parent_quantity: Decimal, component: BomComponent, output_quantity: Decimal
) -> Decimal:
    """What one structure line needs when ``parent_quantity`` of the parent is made.

    The same arithmetic the BOM engine applies to a line - quantity per yield,
    then the line's scrap allowance. Both functions are imported from the BOM
    engine rather than re-implemented, so the planner's explosion and the BOM
    module's explosion cannot drift apart.
    """
    return apply_scrap(
        line_requirement(parent_quantity, component.quantity_per, output_quantity),
        component.scrap_percentage,
    )


def coverage_date(
    gross_requirement: Decimal,
    available: Decimal,
    *,
    supply: Sequence[SupplyArrival],
    as_of: date,
) -> date | None:
    """When stock plus the dated supply on hand fully covers the requirement.

    Stock counts from ``as_of``; each tranche counts from its own date, in date
    order, so the answer is the day the running total crosses the requirement.
    ``None`` when the known supply never covers it - the requirement is then
    waiting on the order the plan raises, and the caller fills the date from
    that suggestion. Supply is taken whole, late tranches included: a purchase
    order arriving after the needed-by date does not stop being a shortage, but
    it does fix the day the material physically arrives.
    """
    if gross_requirement <= 0:
        return as_of
    remaining = gross_requirement - available
    if remaining <= 0:
        return as_of
    for arrival in sorted(supply, key=lambda tranche: tranche.due_date):
        if arrival.quantity <= 0:
            continue
        remaining -= arrival.quantity
        if remaining <= 0:
            return arrival.due_date
    return None


def expected_availability(suggested_order_date: date, lead_time_days: int, *, as_of: date) -> date:
    """The earliest date a suggested order can physically land.

    ``max(as_of, suggested_order_date) + lead_time_days``. A late suggestion's
    order date is in the past, and an order that should have been placed last
    month can no longer land then; the date shown is the one achievable if it is
    placed today. The maximum rather than a clamp because both halves are
    answers: an order still in time lands when the requirement needed it, an
    order already late lands ``as_of + lead_time``.
    """
    return max(suggested_order_date, as_of) + timedelta(days=lead_time_days)


@dataclass(slots=True)
class _NettedItem:
    """One item mid-pass: everything its level decided, carried to its explosion.

    The pass computes the netting for a whole level before anything is exploded,
    because the routing module answers a level's lead times in one call and the
    call needs the level's quantities - which only exist once the level is
    netted. This type is that hand-off, deliberately not a tuple of thirteen
    fields nobody can read.
    """

    item: PlanningItem
    bucket: DemandBucket
    position: StockPosition
    free: Decimal
    needed_by: date
    gross: Decimal
    net: Decimal
    split: SupplySplit
    shortage: Decimal
    structure: BomStructure | None
    suggestion_type: MrpSuggestionType
    explodable: bool


async def _discover_levels(
    source: MrpDataSource,
    *,
    direct: Mapping[uuid.UUID, DemandBucket],
    as_of: date,
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, BomStructure], dict[uuid.UUID, PlanningItem]]:
    """Low-level codes, single-level structures and the planning facts, by level.

    One iteration per level of the deepest structure in scope. Each iteration
    reads the planning facts and the effective structures of whatever the
    previous level introduced - one statement each, however many items that is -
    then relaxes the children's levels: a child of an item at level N is at
    least N+1, and an item is re-visited when a deeper route to it is found
    (``SCREW-M4`` under a kit and under a sub-assembly is planned at its deeper
    code, so the sub-assembly has already been netted when the screw is).

    Termination: a finite acyclic structure has a finite deepest path, and each
    iteration either levels an item deeper or empties the frontier, so
    ``MAX_STRUCTURE_LEVELS`` catches only a cycle - a data defect, never a slow
    structure - which is refused rather than truncated.

    Traversal follows exactly what the pass will explode: a structure is only
    descended into when the item is explodable or the structure is a phantom, so
    a purchased item's unused BOM never drags its parts into the plan's demand.
    Items the item master cannot answer for stop the branch and are reported by
    the caller.
    """
    levels: dict[uuid.UUID, int] = dict.fromkeys(direct, 0)
    structures: dict[uuid.UUID, BomStructure] = {}
    #: Items whose structure has been asked for, including the ones that have
    #: none. Without the second set an item without a BOM would be asked again
    #: every time a deeper route re-queues it.
    asked: set[uuid.UUID] = set()
    items: dict[uuid.UUID, PlanningItem] = {}
    frontier = sorted(direct, key=str)
    level = 0

    while frontier:
        wanted = [item_id for item_id in frontier if item_id not in items]
        if wanted:
            items.update(await source.planning_items(wanted))
        missing_structures = [item_id for item_id in frontier if item_id not in asked]
        if missing_structures:
            structures.update(await source.bom_structures(missing_structures, as_of))
            asked.update(missing_structures)

        if level >= MAX_STRUCTURE_LEVELS:
            raise MrpStructureTooDeepError(
                [items[item_id].code for item_id in frontier if item_id in items],
                MAX_STRUCTURE_LEVELS,
            )

        next_frontier: list[uuid.UUID] = []
        candidate = level + 1
        for item_id in frontier:
            info = items.get(item_id)
            structure = structures.get(item_id)
            if info is None or structure is None:
                continue
            if not (structure.is_phantom or is_explodable(info, has_bom=True)):
                continue
            for line in structure.lines:
                if levels.get(line.item_id, -1) < candidate:
                    levels[line.item_id] = candidate
                    next_frontier.append(line.item_id)
        frontier = sorted(set(next_frontier), key=str)
        level += 1

    return levels, structures, items


#: How many items a "supply arrives too late" warning names before it summarises.
MAX_LATE_SUPPLY_LISTED = 10


def _late_tranches(split: SupplySplit) -> list[str]:
    """One phrase per feed with anything late: 5000.0000 on order arriving X.

    A feed is listed when it has a *date*, not when its quantity is positive: a
    tranche below the 4dp quantum rounds to zero and is still a real, dated
    arrival, and printing "0.0000 on order arriving ..." is the honest form of
    that. The date and the quantity in one phrase come from the same feed of the
    same split, which is the whole point - the previous text paired one date
    with the sum of both feeds, so a purchase order for 5 000 landing in
    November read as arriving on the day a 0.0001 job did.
    """
    feeds = (
        (split.late_incoming, split.first_late_incoming, "on order"),
        (split.late_in_production, split.first_late_in_production, "in production"),
        (split.late_job_work, split.first_late_job_work, "at job-work vendor"),
    )
    return [
        f"{quantity} {label} arriving {arrives.isoformat()}"
        for quantity, arrives, label in feeds
        if arrives is not None
    ]


def _late_supply_warning(entries: Sequence[tuple[str, SupplySplit]]) -> str:
    """Name the inbound supply the time phasing refused to count.

    A planner who can see the purchase order in the system and a shortage in the
    plan needs to be told *why* the two do not cancel, or the plan looks broken
    and the next person "fixes" it by widening the netting again. Bounded like
    every other listing here: the count is exact, only the listing is capped.

    Built from the same :class:`SupplySplit` the requirement row is, so the
    prose and the columns cannot state different figures - and each feed is
    named separately, exactly as the columns carry it.
    """
    listed = ", ".join(
        f"{code} ({'; '.join(_late_tranches(split))}, needed {split.needed_by.isoformat()})"
        for code, split in entries[:MAX_LATE_SUPPLY_LISTED]
    )
    suffix = (
        f" (and {len(entries) - MAX_LATE_SUPPLY_LISTED} more)"
        if len(entries) > MAX_LATE_SUPPLY_LISTED
        else ""
    )
    return (
        f"{len(entries)} short item(s) have inbound supply inside the horizon "
        f"that arrives AFTER it is needed, so it was not netted and the shortage "
        f"stands: {listed}{suffix}"
    )


def _missing_item_warning(item_ids: Iterable[uuid.UUID]) -> str:
    ids = sorted(str(i) for i in item_ids)
    listed = ", ".join(ids[:10])
    suffix = f" (and {len(ids) - 10} more)" if len(ids) > 10 else ""
    return (
        f"{len(ids)} item(s) referenced by demand or a bill of materials are not "
        f"readable in this company and were skipped: {listed}{suffix}"
    )


async def plan(
    source: MrpDataSource,
    *,
    demands: Sequence[DemandLine],
    as_of: date,
    horizon_end: date,
    respect_safety_stock: bool = True,
) -> MrpPlan:
    """Run the section 14 flow over ``demands`` and return the resulting plan.

    ``source`` is the only way this function reaches data, and every one of its
    methods takes a set of ids - see the module docstring for the statement
    budget. The structure is discovered level by level before anything is
    netted, then each item is netted and exploded once, at its own low-level
    code - so every supply decision is taken against a needed-by date that is
    already final, and the fixed point the previous shape needed does not exist.

    ``horizon_end`` bounds the inbound supply that is *read*: an order arriving
    after the horizon is not this plan's business. Inside that horizon the
    supply is dated, and each requirement is netted only against the tranches
    landing on or before the date that requirement is needed - a purchase order
    due in six weeks does not cover a line shipping in two days. Whatever the
    time phasing excludes is reported, never dropped in silence.
    """
    warnings: list[str] = []

    buckets = merge_demand(demands)
    if not buckets:
        return MrpPlan(
            requirements=(),
            suggestions=(),
            stats={
                "demand_line_count": len(demands),
                "demand_item_count": 0,
                "requirement_count": 0,
                "shortage_item_count": 0,
                "suggestion_count": 0,
                "explosion_count": 0,
                "low_level_code_max": 0,
            },
            warnings=("No open demand fell inside the horizon, so nothing was planned.",),
        )

    demand_item_ids = list(buckets)

    # --- level the structure before planning it ------------------------------
    # Only reachable items are levelled, so an unrelated cycle elsewhere in the
    # data cannot stop this run. Every item's level is final before the pass
    # below starts, which is what makes the processing order an answer rather
    # than a fixed point to settle.
    levels, structures, items = await _discover_levels(source, direct=buckets, as_of=as_of)

    unknown = [item_id for item_id in levels if item_id not in items]
    if unknown:
        warnings.append(_missing_item_warning(unknown))
        for item_id in unknown:
            buckets.pop(item_id, None)
            levels.pop(item_id, None)

    # Supply is read for every discovered item in one statement per source: the
    # structure is known in full before the first requirement is netted, so
    # there is no second round and no per-level supply read.
    known = list(levels)
    positions = await source.stock_positions(known)
    incoming, incoming_ok = await source.incoming_supply(known, horizon_end)
    in_production, production_ok = await source.in_production_supply(known, horizon_end)
    job_work, job_work_ok = await source.job_work_supply(known, horizon_end)

    inactive = [i for i in demand_item_ids if i in items and not items[i].is_active]
    if inactive:
        codes = sorted(items[i].code for i in inactive)
        warnings.append(
            "Demand exists for de-activated item(s), which were still planned: "
            + ", ".join(codes[:10])
            + (f" (and {len(codes) - 10} more)" if len(codes) > 10 else "")
        )

    for item_id, bucket in buckets.items():
        info = items.get(item_id)
        if info is None or bucket.direct <= 0:
            continue
        if (
            ProductionType(info.production_type)
            in (ProductionType.MAKE, ProductionType.MAKE_OR_BUY)
            and item_id not in structures
        ):
            warnings.append(
                f"{info.code} is planned as manufactured but has no approved default "
                "bill of materials, so no component demand was raised for it."
            )

    if not incoming_ok:
        warnings.append(
            "Confirmed incoming purchase orders could not be read, so the plan did "
            "NOT net against them. Treat purchased-item suggestions as an upper bound."
        )
    if not production_ok:
        warnings.append(
            "In-progress production output could not be read, so the plan did NOT "
            "net against it. Treat manufactured-item suggestions as an upper bound."
        )
    if not job_work_ok:
        warnings.append(
            "Open job-work output could not be read, so the plan did NOT net against "
            "it. Treat job-work suggestions as an upper bound."
        )

    # --- level by level: net once, explode the shortfall one level down ------
    requirements: dict[uuid.UUID, Requirement] = {}
    shortages: list[tuple[Requirement, PlanningItem]] = []
    #: The lead time each item's requirement and suggestion were dated with,
    #: keyed by item: the same figure that offsets its components, so a
    #: suggestion and the demand it raised can never be dated two ways.
    lead_times: dict[uuid.UUID, int] = {}
    #: ``(code, split)`` per short item whose inbound supply was excluded for
    #: being late. Drives the warning below. The whole split is carried rather
    #: than a flattened tuple so the prose is written from the same object the
    #: row is: a quantity and a date extracted separately can be paired
    #: wrongly, which is the defect this shape removes.
    late_supply: list[tuple[str, SupplySplit]] = []
    #: item -> the children its shortfall was exploded into, for the expected
    #: availability roll-up after the suggestions are dated.
    exploded_children: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
    explosion_count = 0

    for level in range(max(levels.values(), default=0) + 1):
        level_ids = [
            item_id
            for item_id, code in levels.items()
            if code == level and item_id in buckets and item_id in items
        ]
        if not level_ids:
            continue
        level_ids.sort(key=lambda item_id: items[item_id].code)
        netted: list[_NettedItem] = []

        for item_id in level_ids:
            item = items[item_id]
            bucket = buckets[item_id]
            position = positions.get(item_id, StockPosition())
            free = planning_available(position, item.safety_stock, respect=respect_safety_stock)
            needed_by = bucket.earliest_date or as_of

            gross = to_quantity(bucket.direct + bucket.dependent)
            net = net_after_stock(gross, free)
            # THE time phasing. Only tranches promised on or before the date this
            # requirement is needed are supply for it; the rest are reported.
            # The date is final - every parent was processed at a lower level -
            # so the split taken here is the split the explosion below and the
            # requirement row both describe.
            split = supply_split(
                needed_by,
                incoming=incoming.get(item_id),
                in_production=in_production.get(item_id),
                job_work=job_work.get(item_id),
            )
            shortage = net_after_supply(net, split.incoming, split.in_production, split.job_work)
            structure = structures.get(item_id)
            suggestion_type = choose_suggestion_type(
                item, has_default_bom=bool(structure and structure.is_default)
            )
            # A phantom is a grouping on a drawing, not a stocked or built part:
            # the BOM engine blows through one, and the planner follows its
            # shortfall through the same way.
            explodable = structure is not None and (
                structure.is_phantom or is_explodable(item, has_bom=True)
            )
            netted.append(
                _NettedItem(
                    item=item,
                    bucket=bucket,
                    position=position,
                    free=free,
                    needed_by=needed_by,
                    gross=gross,
                    net=net,
                    split=split,
                    shortage=shortage,
                    structure=structure,
                    suggestion_type=suggestion_type,
                    explodable=explodable,
                )
            )

        # The routing module answers the whole level in one call. Only items
        # that will be manufactured need it: their shop time is what offsets
        # their components and dates their own suggestion. The quantity is the
        # one the suggestion will order - the shortage raised to the item's
        # minimum - so the lead time belongs to the batch, not to the deficit.
        make_quantities = {
            entry.item.id: apply_order_policy(
                entry.shortage,
                min_order_quantity=entry.item.min_order_quantity,
                order_multiple=ZERO,
            )
            for entry in netted
            if entry.shortage > 0 and entry.suggestion_type is MrpSuggestionType.MANUFACTURE
        }
        routing_leads = (
            await source.manufacture_lead_times(make_quantities, as_of) if make_quantities else {}
        )

        for entry in netted:
            item = entry.item
            # A manufacture suggestion is paced by its routing's shop time. A
            # vendor's lead time only governs when the item is being bought, and
            # is applied where the suggestion is built - a purchased item is
            # never exploded, so nothing here is offset by it. Everything else
            # falls back to the item master's own lead time.
            if entry.suggestion_type is MrpSuggestionType.MANUFACTURE:
                lead_time = routing_leads.get(item.id, item.lead_time_days)
            else:
                lead_time = item.lead_time_days
            lead_times[item.id] = lead_time

            requirement = Requirement(
                item_id=item.id,
                item_code=item.code,
                item_name=item.name,
                level=entry.bucket.level or 0,
                low_level_code=level,
                direct_demand_quantity=to_quantity(entry.bucket.direct),
                dependent_demand_quantity=to_quantity(entry.bucket.dependent),
                gross_requirement=entry.gross,
                on_hand_quantity=to_quantity(entry.position.on_hand),
                reserved_quantity=to_quantity(entry.position.reserved),
                available_quantity=entry.free,
                incoming_quantity=entry.split.incoming,
                in_production_quantity=entry.split.in_production,
                job_work_quantity=entry.split.job_work,
                net_requirement=entry.net,
                shortage_quantity=entry.shortage,
                earliest_required_date=entry.needed_by,
                expected_available_date=coverage_date(
                    entry.gross,
                    entry.free,
                    supply=(
                        *incoming.get(item.id, ()),
                        *in_production.get(item.id, ()),
                        *job_work.get(item.id, ()),
                    ),
                    as_of=as_of,
                ),
                source_demand_count=entry.bucket.source_count,
                source_demands=entry.bucket.listed_sources(),
                late_incoming_quantity=entry.split.late_incoming,
                late_in_production_quantity=entry.split.late_in_production,
                late_job_work_quantity=entry.split.late_job_work,
                first_late_incoming_date=entry.split.first_late_incoming,
                last_late_incoming_date=entry.split.last_late_incoming,
                last_late_in_production_date=entry.split.last_late_in_production,
                first_late_in_production_date=entry.split.first_late_in_production,
                first_late_job_work_date=entry.split.first_late_job_work,
                last_late_job_work_date=entry.split.last_late_job_work,
            )
            # A phantom raises demand for its children but has no row and no
            # suggestion of its own: nothing is ever stocked, bought or built
            # under its name. A phantom that is *directly* demanded keeps the
            # row - somebody asked for it by name, and the shortage is real even
            # though the part is a grouping.
            if entry.structure is None or not entry.structure.is_phantom or entry.bucket.direct > 0:
                requirements[item.id] = requirement
                if entry.shortage > 0:
                    shortages.append((requirement, item))

            # ASSUMPTION: the caveat is raised only where it would have made a
            # difference. An item covered by its own stock with a late order
            # behind it needs no warning - the plan is right, and saying so
            # anyway is noise that trains a planner to skip the warnings that
            # matter. The figure is still on the requirement row for anyone who
            # wants it.
            if (
                entry.shortage > 0
                and entry.split.first_late_arrival is not None
                and entry.split.late_total > 0
            ):
                late_supply.append((item.code, entry.split))

            if entry.shortage > 0 and entry.explodable and entry.structure is not None:
                component_date = _component_required_date(entry.needed_by, lead_time)
                children: list[uuid.UUID] = []
                for component in entry.structure.lines:
                    quantity = component_requirement(
                        entry.shortage, component, entry.structure.output_quantity
                    )
                    if quantity <= 0:
                        continue
                    child = buckets.setdefault(
                        component.item_id, DemandBucket(item_id=component.item_id)
                    )
                    child.dependent = to_quantity(child.dependent + quantity)
                    child.note_date(component_date)
                    child.note_level(level + 1)
                    # ASSUMPTION: aggregate netting does not choose which root
                    # stock covered. Keep every causal root rather than
                    # assigning component quantities to sales lines.
                    child.inherit_root_refs(entry.bucket)
                    child.add_source(
                        {
                            "source": "BOM",
                            "via_item_id": str(item.id),
                            "via_item_code": item.code,
                            "quantity": str(quantity),
                            "required_date": component_date.isoformat(),
                            "level": level + 1,
                        }
                    )
                    children.append(component.item_id)
                if children:
                    exploded_children[item.id] = tuple(children)
                    explosion_count += 1

    # --- suggestions ---------------------------------------------------------
    # Terms are loaded once for the short items only: an item that is covered
    # never needs its supplier's pack size.
    terms = await source.supply_terms([r.item_id for r, _ in shortages]) if shortages else {}

    suggestions: list[Suggestion] = []
    planned_availability: dict[uuid.UUID, date] = {}
    for requirement, item in shortages:
        structure = structures.get(requirement.item_id)
        suggestion_type = choose_suggestion_type(
            item, has_default_bom=bool(structure and structure.is_default)
        )
        policy = terms.get(requirement.item_id, SupplyTerms())

        # A vendor's lead time only governs when the item is being bought from
        # them; a manufacture suggestion is paced by the routing's shop time
        # (already computed, and the same figure its components were offset
        # by); a job-work suggestion by the processor's stated turnaround.
        if suggestion_type is MrpSuggestionType.PURCHASE and policy.lead_time_days > 0:
            lead_time = policy.lead_time_days
        else:
            lead_time = lead_times.get(requirement.item_id, item.lead_time_days)
        minimum = policy.min_order_quantity or item.min_order_quantity
        quantity = apply_order_policy(
            requirement.shortage_quantity,
            min_order_quantity=minimum,
            order_multiple=policy.order_multiple,
        )
        order_date = order_date_for(requirement.earliest_required_date, lead_time)
        availability = expected_availability(order_date, lead_time, as_of=as_of)
        planned_availability[requirement.item_id] = availability

        suggestions.append(
            Suggestion(
                item_id=requirement.item_id,
                item_code=requirement.item_code,
                suggestion_type=suggestion_type,
                required_quantity=requirement.shortage_quantity,
                suggested_quantity=quantity,
                shortage_quantity=requirement.shortage_quantity,
                available_quantity=requirement.available_quantity,
                incoming_quantity=requirement.incoming_quantity,
                required_date=requirement.earliest_required_date,
                suggested_order_date=order_date,
                is_late=is_late_order(order_date, as_of),
                lead_time_days=lead_time,
                low_level_code=levels[requirement.item_id],
                expected_available_date=availability,
                source_demands=requirement.source_demands,
            )
        )

    # A requirement covered only by the order this plan raises takes that
    # order's expected arrival; one that supply already on its way covers takes
    # the earlier of the two.
    for item_id in list(requirements):
        planned = planned_availability.get(item_id)
        if planned is None:
            continue
        covered = requirements[item_id].expected_available_date
        requirements[item_id] = replace(
            requirements[item_id],
            expected_available_date=planned if covered is None else min(covered, planned),
        )

    # A parent cannot be available before the parts it is made from. Descending
    # by low-level code finalises every child before its parent reads it, so
    # the dates roll up in one pass with no fixed point - the order is the
    # proof, since a child's code is strictly greater than every parent's.
    #
    # Phantoms have no requirement row but still sit between a parent and its
    # real components, so the roll-up runs over the rolled dates of every
    # exploded item, row or not, and only the rows are updated at the end.
    rolled: dict[uuid.UUID, date] = {}
    for item_id, requirement in requirements.items():
        if requirement.expected_available_date is not None:
            rolled[item_id] = requirement.expected_available_date
    for item_id in sorted(exploded_children, key=lambda parent: levels[parent], reverse=True):
        child_dates = [
            when for child in exploded_children[item_id] if (when := rolled.get(child)) is not None
        ]
        if not child_dates:
            continue
        slipped = max(child_dates) + timedelta(days=lead_times.get(item_id, 0))
        own = rolled.get(item_id)
        rolled[item_id] = slipped if own is None else max(own, slipped)

    for item_id, when in rolled.items():
        existing = requirements.get(item_id)
        if existing is not None and existing.expected_available_date != when:
            requirements[item_id] = replace(existing, expected_available_date=when)

    if late_supply:
        # By code, which is unique per run - one entry per item - so two runs of
        # the same plan produce the same warning text. Keyed rather than sorted
        # on the tuple: a ``SupplySplit`` is not ordered and never should be.
        warnings.append(_late_supply_warning(sorted(late_supply, key=lambda entry: entry[0])))

    # Level first, then code: components read under the products that need
    # them, and the order is stable so two runs of the same plan diff cleanly.
    # "Level" is the shallowest, which is what a planner reads downward from;
    # the low-level code beside it is the level the planner actually planned in.
    ordered_requirements = tuple(
        sorted(requirements.values(), key=lambda r: (r.level, r.item_code))
    )
    # Suggestions are a work queue: the latest thing to act on comes first.
    ordered_suggestions = tuple(
        sorted(suggestions, key=lambda s: (s.suggested_order_date, s.item_code))
    )

    stats = {
        "demand_line_count": len(demands),
        "demand_item_count": len(demand_item_ids),
        "requirement_count": len(ordered_requirements),
        "shortage_item_count": len(shortages),
        "suggestion_count": len(ordered_suggestions),
        "explosion_count": explosion_count,
        # The deepest level planned: the number of passes the level-by-level
        # netting took, and a direct read on the depth of the structure in scope.
        "low_level_code_max": max(levels.values(), default=0),
        "late_suggestion_count": sum(1 for s in ordered_suggestions if s.is_late),
        # Short items whose inbound supply arrives after it is needed and was
        # therefore not netted. An int, like every other stat: ``result_summary``
        # is JSONB and a Decimal does not serialise into it - the quantities are
        # in the warning text and on the requirement rows.
        "late_supply_item_count": len(late_supply),
        "netted_against_incoming": incoming_ok,
        "netted_against_production": production_ok,
        "respected_safety_stock": respect_safety_stock,
        "suggestions_by_type": {
            str(kind): sum(1 for s in ordered_suggestions if s.suggestion_type is kind)
            for kind in MrpSuggestionType
            if any(s.suggestion_type is kind for s in ordered_suggestions)
        },
    }

    log.info(
        "mrp.plan_computed",
        demand_lines=len(demands),
        requirements=len(ordered_requirements),
        shortages=len(shortages),
        explosions=explosion_count,
    )

    return MrpPlan(
        requirements=ordered_requirements,
        suggestions=ordered_suggestions,
        stats=stats,
        warnings=tuple(warnings),
    )


__all__ = [
    "DEFAULT_DEMAND_PRIORITY",
    "MAX_LATE_SUPPLY_LISTED",
    "MAX_STRUCTURE_LEVELS",
    "BomComponent",
    "BomStructure",
    "DemandBucket",
    "DemandLine",
    "MrpDataSource",
    "MrpDemandTooLargeError",
    "MrpPlan",
    "MrpStructureTooDeepError",
    "PlanningItem",
    "Requirement",
    "StockPosition",
    "Suggestion",
    "SupplyArrival",
    "SupplySchedule",
    "SupplySplit",
    "SupplyTerms",
    "apply_order_policy",
    "choose_suggestion_type",
    "component_requirement",
    "coverage_date",
    "earliest_late_arrival",
    "expected_availability",
    "is_explodable",
    "is_late_order",
    "latest_late_arrival",
    "merge_demand",
    "net_after_stock",
    "net_after_supply",
    "order_date_for",
    "plan",
    "planning_available",
    "split_supply",
    "supply_split",
]
