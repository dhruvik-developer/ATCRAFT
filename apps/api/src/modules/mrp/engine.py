"""The MRP calculation: demand in, shortages and suggestions out.

This implements the flow master rules section 14 fixes, and database.md section
6 restates:

    sales orders (+ manual demand)
      -> net against finished-goods available stock
      -> explode the BOM for the shortfall
      -> MERGE common parts across every demand line and every BOM branch
      -> net against inventory available
      -> net against existing reservations
      -> net against confirmed incoming purchase orders *that arrive in time*
      -> net against in-progress production output *that arrives in time*
      -> shortage per item
      -> suggestions

Query strategy - read this before changing anything here
--------------------------------------------------------
Everything the calculation needs is loaded for a *set* of items. A statement
issued inside a per-item loop is a defect, not a slow path (master rules section
32, performance.md). One run costs:

1. one statement for the demand lines (the caller supplies them; the service
   reads them in a single join over open sales-order lines);
2. one statement for the planning facts of every demand item;
3. one statement for the stock positions of every demand item, grouped by item;
4. one statement for which of those items have an approved default BOM;
5. one *explosion per make-item with a shortfall* - never one per demand line.
   Two orders for the same lock kit are merged before anything is exploded, and
   each explosion is itself batched by level inside the BOM engine. An item that
   is directly demanded *and* a component of another directly demanded item is
   exploded again when its needed-by date is pulled earlier, because the first
   answer was taken against a date that turned out not to be final
   (:func:`_settle_demand`); nothing else re-explodes, and a settling pass that
   finds nothing stale costs no round trips at all;
6. one statement for the planning facts of the components the explosions
   discovered, and one for their stock positions - a second round, because the
   component set is not known until the explosions have run;
7. one statement for confirmed incoming purchase orders and one for in-progress
   production output, *per round* - the same two rounds as (2) and (3), because
   a parent's shortfall is netted against its own inbound supply before it is
   exploded and the component set does not exist until that has happened. Four
   statements for a run with components, two for a run without: a fixed number
   either way, never one per item. The settling in (5) re-reads nothing: the
   schedules are dated tranches, so re-phasing an item at a new date is
   arithmetic on data already in hand;
8. one statement for the preferred-vendor terms of every item a suggestion will
   be raised for.

So the cost is a fixed handful of statements plus one explosion per distinct
manufactured item that is actually short - and the explosion count is bounded by
the number of *products*, not by the number of order lines. :class:`MrpDataSource`
exists so this is *testable*: the unit suite drives the engine through an
in-memory source that counts calls, which turns "no N+1" from a claim in a
comment into a failing test if anyone reintroduces a per-item fetch.

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
--------------------------------------------------------------
Inbound supply is read as *dated tranches* (:class:`SupplyArrival`) and each
requirement is netted only against the tranches landing on or before the date
that requirement is needed. A purchase order arriving in six weeks is not supply
for a line shipping on Thursday, and netting it against one is the difference
between a plan that orders nothing and a stockout on the shop floor.

The date a requirement is "needed" is its ``earliest_required_date`` - the
earliest of every demand merged into it, and for a component the parent's date
less the parent's lead time. A merged row cannot be half early and half late, so
the earliest date governs the whole quantity: supply landing between the two
dates covers the later demand in reality and is not counted here, which
over-states the shortage rather than under-stating it. That is the direction
this module takes whenever it has to choose.

**That date is settled before any supply decision is taken against it.** It has
to be: exploding a parent pulls its components' dates earlier, and an item can
be both directly demanded *and* a component of something else, so the date one
stage reads can still be moving while another stage is deciding against it. When
that happened, an item was netted for its explosion at one date and for its own
shortage at another - the explosion saw an inbound order as in time, the
requirement saw it as late, and the components for the shortage that survived
were never raised. :func:`_settle_demand` runs the explosions to a fixed point
first, and hands the resulting phasing (:class:`SupplySplit`) to the netting
stage so both stages read one answer rather than computing two.

The alternative - one lump per item for the whole horizon, which is what this
engine did before - reads identically in every report while silently erasing a
genuine shortage, so the split is made where the arithmetic happens
(:func:`split_supply`) and the part that was excluded is never dropped in
silence: it is summed into ``late_incoming_quantity`` /
``late_in_production_quantity`` on the requirement, each dated by its own
``first_late_incoming_date`` / ``first_late_in_production_date``, counted in the
run's stats and named in a warning, so a planner can see that an inbound order
exists, when it lands, and that the plan did not count on it. The requirement
row carries all of it as typed columns rather than leaving a client to read the
figures out of the warning prose.

One date per feed, not one per row. The two feeds are independent schedules and
a single date beside two quantities belongs to at most one of them: a purchase
order for 5 000 landing in four months and a job for 0.0001 landing tomorrow
rendered as "5 000 arriving tomorrow", which is worse than the prose it
replaced because it looks structured. ``first_late_supply_date`` still exists -
it is the earliest across both feeds, derived from the two - but it answers "how
soon could any of this be relieved", not "when does that quantity land".

Two limitations, stated rather than hidden
------------------------------------------
**A live job's components are planned again.** A released production order for a
parent is counted as supply for that parent's own requirement, but is *not*
netted off the quantity the parent's structure is exploded for. Releasing a job
does not necessarily hold its parts - ``reserve_material`` is optional and a
release short of stock is reported rather than refused - so treating the job as
a reason not to raise component demand deletes the demand a live job depends on.
Where the job *did* reserve, the hold is already excluded from free stock, so
those components are planned a second time: an over-order bounded by what live
jobs are holding, taken deliberately because an unnecessary component order is
cancellable and a missing one stops the line. The reasoning is at the point of
decision in :func:`_settle_demand`.

**The two-stage flow over-states a component that is also sold.** The flow is
two-stage by construction: finished-goods demand is netted, the
shortfall is exploded, and the components are netted. An item that carries
*direct* demand and is *also* a component therefore has its own free stock and
its own inbound supply netted once against the combined requirement (which is
the correct answer for that item - see :func:`net_after_stock`), but its
dependent share was already carried through its parent's multi-level explosion
before that netting happened. So when such an item has enough stock to cover its
direct demand outright, the component demand raised beneath it can be overstated
by up to that item's free stock and in-time supply.

The error is bounded by the item's own free stock plus its in-time supply and is
always in the over-ordering direction, never under. Removing it needs
level-by-level low-level-code netting, which is a different algorithm from the
one master rules section 14 specifies; it is not silently approximated here.

Rounding: every quantity is ``Decimal`` and is quantised with
:func:`~src.core.types.to_quantity` as it is produced, so a plan's printed lines
add up to its printed totals.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any, Protocol

from src.core.errors import BusinessRuleError
from src.core.logging import get_logger
from src.core.types import ZERO, to_quantity
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
class ExplodedLine:
    """One consolidated component of one explosion.

    A deliberately small shape rather than the BOM module's ``ExplodedComponent``:
    the planner needs four fields, and depending on the rest would make this
    engine's unit tests hostage to changes in the other module's result type.
    """

    item_id: uuid.UUID
    item_code: str
    quantity: Decimal
    level: int


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
    net_requirement: Decimal
    shortage_quantity: Decimal
    earliest_required_date: date
    source_demand_count: int
    source_demands: tuple[dict[str, Any], ...]
    #: Inbound supply inside the run's horizon that arrives *after* the date
    #: this requirement needs it, so it was NOT netted. Reported rather than
    #: dropped: a plan that quietly ignores a known inbound order is its own
    #: trap, and a buyer looking at the shortage has to be able to see that the
    #: order exists and is simply too late.
    late_incoming_quantity: Decimal = ZERO
    late_in_production_quantity: Decimal = ZERO
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

    @property
    def first_late_supply_date(self) -> date | None:
        """The earliest of that supply across both feeds, or ``None``.

        The run-level "how soon could this be relieved at all" answer, kept
        because it is what the warning sorts on and what existing consumers
        already read. It is *not* the date for either quantity beside it - each
        of those carries its own - and it is derived here rather than stored so
        it cannot drift from the two dates it summarises.
        """
        dates = [
            when
            for when in (self.first_late_incoming_date, self.first_late_in_production_date)
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
    taken twice and hoped to agree. Both stages that care about supply - the
    explosion, which decides whether a parent still has to be built, and the
    netting, which decides what to order - read *this* object, so the date they
    phase at is the same date by construction and not by two callers remembering
    to pass the same argument.

    That is not a tidiness point. The defect this replaces read
    ``bucket.earliest_date`` for the explosion while the merge loop was still
    pulling that same date earlier, and read it again, later and earlier, for the
    requirement. One item was netted for its explosion at one date and for its
    own shortage at another: the explosion saw an inbound order as in time, the
    requirement saw it as late, and the components for the shortage that
    resulted were never raised. :func:`_settle_demand` now settles every date
    before a single split is taken, and this type is what makes the two stages
    provably read the same answer.
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
    #: When the LAST late tranche of each feed lands. The late quantity above is
    #: a SUM over that feed's late tranches and ``first_late_*`` is the MINIMUM
    #: over the same tranches, so the pair describes the whole feed only when it
    #: had one tranche. With several, the earliest date UNDERSTATES how late the
    #: bulk is - the dangerous direction, because it reads as nearly here. These
    #: close the interval: "the first of 5010 on 2026-07-30, all of it by
    #: 2026-12-15" is a fact; "5010 on 2026-07-30" is not.
    last_late_incoming: date | None = None
    last_late_in_production: date | None = None

    @property
    def late_total(self) -> Decimal:
        """Everything the phasing excluded, from either source."""
        return to_quantity(self.late_incoming + self.late_in_production)

    @property
    def first_late_arrival(self) -> date | None:
        """The earliest date, across both feeds, on which excluded supply lands.

        The soonest the shortage could be relieved without a new order, which is
        the run-level question. It says nothing about *how much* lands then -
        that is the per-feed pair above, and reading this date beside either
        quantity is the misreading the pairs exist to prevent.

        Derived rather than stored: a summary that can be set independently of
        the figures it summarises is a summary that can contradict them.
        """
        dates = [
            when
            for when in (self.first_late_incoming, self.first_late_in_production)
            if when is not None
        ]
        return min(dates) if dates else None


def supply_split(
    needed_by: date,
    *,
    incoming: Sequence[SupplyArrival] | None = None,
    in_production: Sequence[SupplyArrival] | None = None,
) -> SupplySplit:
    """Phase both supply feeds for one item at one date.

    The only function in this module that turns a schedule into a netting
    figure. Everything downstream reads the :class:`SupplySplit` it returns.
    """
    on_order, late_on_order = split_supply(incoming, needed_by)
    being_made, late_being_made = split_supply(in_production, needed_by)
    # Each feed's date is taken from that feed alone, so it describes the
    # quantity returned beside it and nothing else. The cross-feed earliest is
    # derived from these two by :attr:`SupplySplit.first_late_arrival`.
    return SupplySplit(
        needed_by=needed_by,
        incoming=on_order,
        late_incoming=late_on_order,
        in_production=being_made,
        late_in_production=late_being_made,
        first_late_incoming=earliest_late_arrival(incoming, needed_by),
        first_late_in_production=earliest_late_arrival(in_production, needed_by),
        last_late_incoming=latest_late_arrival(incoming, needed_by),
        last_late_in_production=latest_late_arrival(in_production, needed_by),
    )


def net_after_supply(
    net_requirement: Decimal, incoming: Decimal, in_production: Decimal
) -> Decimal:
    """What is still missing once confirmed inbound supply is counted.

    Incoming purchase orders and in-progress production are both stock that will
    exist without anybody doing anything further, so ordering against them again
    is a duplicate order.

    Both figures must already be time-phased by the caller: they are the *in
    time* half of :func:`split_supply`, the tranches arriving on or before the
    date this requirement is needed. Handing this function a whole horizon's
    supply is the defect it used to have - a purchase order landing six weeks
    after the line ships would net the shortage away to nothing.
    """
    remaining = net_requirement - incoming - in_production
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

    There is deliberately no single-item accessor on this protocol apart from
    :meth:`explode`, which is inherently per-structure. The engine physically
    cannot ask per item for stock, planning facts or supply, which is what keeps
    a run at a fixed number of statements rather than one per part.
    """

    async def planning_items(
        self, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, PlanningItem]: ...

    async def stock_positions(
        self, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, StockPosition]: ...

    async def bom_availability(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, bool]:
        """Items with an APPROVED structure effective on ``as_of``.

        Present in the mapping means "there is something to explode"; the value
        says whether that structure is the item's *default*, which is the signal
        :func:`choose_suggestion_type` reads for a ``MAKE_OR_BUY`` item.
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

    async def explode(
        self, item_id: uuid.UUID, quantity: Decimal, as_of: date
    ) -> Sequence[ExplodedLine]: ...


# -----------------------------------------------------------------------------
# The calculation
# -----------------------------------------------------------------------------


def _dependent_required_date(root_date: date, root_lead_time_days: int) -> date:
    """When a component must be available for its parent's build to start.

    ASSUMPTION: components are time-phased by the *root's* lead time only -
    the date the root's manufacture has to begin. Deeper levels are not offset
    again, because a consolidated explosion reports one quantity per part across
    every branch and no longer knows which intermediate assembly each
    contribution passed through; inventing an offset per level would be a number
    with no basis in the data. Offsetting by at least the root's lead time is the
    conservative direction - it pulls the requirement earlier, so the failure
    mode is ordering early rather than discovering the part is needed today.
    """
    return order_date_for(root_date, root_lead_time_days)


#: Ceiling on the date-settling passes below. The Bellman-Ford argument in
#: :func:`_settle_demand` needs at most one pass per explodable item plus one to
#: verify, which this only bites into for an order book where twenty-five
#: directly-demanded products are components of one another in a single chain -
#: not a bill of materials, a data problem. Hitting it is reported, never
#: silently accepted.
MAX_SETTLING_PASSES = 25


@dataclass(frozen=True, slots=True)
class _Explosion:
    """One parent's settled explosion decision, and the split that took it.

    ``split.needed_by`` is the date the decision was made at. Keeping it beside
    the result is what lets :func:`plan` *check* that the requirement stage is
    phasing the same item at the same date, instead of assuming it.
    """

    split: SupplySplit
    shortfall: Decimal
    component_date: date
    lines: tuple[ExplodedLine, ...]


def _independent_only(direct: dict[uuid.UUID, DemandBucket]) -> dict[uuid.UUID, DemandBucket]:
    """A fresh set of buckets carrying the independent demand and nothing else.

    Each settling pass rebuilds the dependent side from scratch on top of one of
    these, so re-deciding an explosion *replaces* its contribution rather than
    adding a second one. The alternative - mutating in place and trying to
    retract - is how a merged quantity ends up counted twice.

    ``direct`` is the untouched output of :func:`merge_demand`, so every field
    copied here is independent demand: the explosions have not run yet.
    """
    return {
        item_id: DemandBucket(
            item_id=item_id,
            direct=bucket.direct,
            earliest_date=bucket.earliest_date,
            level=bucket.level,
            sources=list(bucket.sources),
            source_count=bucket.source_count,
        )
        for item_id, bucket in direct.items()
    }


def _apply_explosion(
    buckets: dict[uuid.UUID, DemandBucket],
    *,
    parent_id: uuid.UUID,
    parent_code: str,
    explosion: _Explosion,
) -> None:
    """Merge one settled explosion's components into the buckets."""
    for line in explosion.lines:
        if line.quantity <= 0:
            continue
        child = buckets.setdefault(line.item_id, DemandBucket(item_id=line.item_id))
        child.dependent = to_quantity(child.dependent + line.quantity)
        child.note_date(explosion.component_date)
        child.note_level(line.level)
        child.add_source(
            {
                "source": "BOM",
                "via_item_id": str(parent_id),
                "via_item_code": parent_code,
                "quantity": str(to_quantity(line.quantity)),
                "required_date": explosion.component_date.isoformat(),
                "level": line.level,
            }
        )


async def _settle_demand(
    source: MrpDataSource,
    *,
    direct: dict[uuid.UUID, DemandBucket],
    items: dict[uuid.UUID, PlanningItem],
    positions: dict[uuid.UUID, StockPosition],
    structures: dict[uuid.UUID, bool],
    incoming: SupplySchedule,
    in_production: SupplySchedule,
    as_of: date,
    respect_safety_stock: bool,
) -> tuple[dict[uuid.UUID, DemandBucket], dict[uuid.UUID, SupplySplit], int, bool]:
    """Explode make-item shortfalls until every needed-by date has stopped moving.

    Returns ``(buckets, splits, explosion_count, settled)``. ``splits`` is the
    phasing every explosion was decided on, keyed by item, for the requirement
    stage to *reuse* rather than recompute.

    Why this is a loop and not a line of code
    -----------------------------------------
    An item's needed-by date has to be final before any supply decision is taken
    against it, and the thing that moves that date is the very explosion the
    decision gates. The previous shape read ``bucket.earliest_date`` for the
    explosion while the same loop was still calling ``note_date`` on it: an item
    that is *both* directly demanded *and* a component of something else was
    exploded at its own order date and then netted, later, at the earlier date
    its parent's lead time implied. The explosion counted an inbound order as in
    time; the requirement counted it as late; the components for the shortage
    that survived were never raised. That is a stockout the plan reports as a
    clean run.

    So the two stages are separated by a fixed point rather than by an ordering
    rule. There is no ordering rule that works: a parent may be needed earlier
    *or* later than a child that is also sold on its own, so neither ascending
    nor descending date order visits parents first in every case, and the graph
    that would give a topological order is only discovered *by* exploding.

    Each pass rebuilds the dependent demand from the explosions settled so far,
    then re-decides every explosion whose root date has moved since it was taken.
    Two properties make that converge, and make the answer the right one:

    * **Dates only ever move earlier.** A pull comes from ``parent_date -
      parent_lead_time``, and parent dates themselves only move earlier, so the
      iteration is monotone and cannot oscillate.
    * **An explosion never disappears.** An earlier root date can only *reduce*
      the supply that arrives in time (:func:`split_supply`), so the shortfall is
      non-decreasing as the date moves earlier. An item that explodes keeps
      exploding, and its quantity only grows.

    Only directly-demanded items are ever exploded - a component is reached
    through its parent's consolidated multi-level explosion, which already
    carries every level - so the chain of pulls runs through explodable demand
    items and cannot revisit one (a bill of materials is acyclic). That is the
    Bellman-Ford bound: ``len(candidates)`` passes relax every path, and one more
    verifies that nothing moved. A pass that finds nothing stale costs no calls
    at all, and an item is re-exploded only when its own date genuinely changed,
    so the common run - where no product is a part of another product - is still
    exactly one explosion per short make item.
    """
    buckets = _independent_only(direct)
    candidates = [
        item_id
        for item_id, item in ((i, items.get(i)) for i in direct)
        if item is not None and is_explodable(item, has_bom=item_id in structures)
    ]
    if not candidates:
        return buckets, {}, 0, True

    explosions: dict[uuid.UUID, _Explosion] = {}
    explosion_count = 0
    settled = False

    for _ in range(min(len(candidates) + 1, MAX_SETTLING_PASSES)):
        buckets = _independent_only(direct)
        # Insertion-ordered, so the provenance listing on a merged bucket reads
        # the same way on every run of the same plan.
        for parent_id, decided in explosions.items():
            _apply_explosion(
                buckets,
                parent_id=parent_id,
                parent_code=items[parent_id].code,
                explosion=decided,
            )

        stale = [
            item_id
            for item_id in candidates
            if (previous := explosions.get(item_id)) is None
            or previous.split.needed_by != (buckets[item_id].earliest_date or as_of)
        ]
        if not stale:
            settled = True
            break

        for item_id in stale:
            item = items[item_id]
            bucket = buckets[item_id]
            root_date = bucket.earliest_date or as_of
            split = supply_split(
                root_date,
                incoming=incoming.get(item_id),
                in_production=in_production.get(item_id),
            )
            free = planning_available(
                positions.get(item_id, StockPosition()),
                item.safety_stock,
                respect=respect_safety_stock,
            )
            # ASSUMPTION: the exploded quantity is netted against the parent's
            # free stock and against confirmed incoming *purchase orders*, and
            # NOT against in-progress production. The two are not the same kind
            # of supply and the asymmetry is deliberate.
            #
            # A purchase order for the parent brings the finished part in
            # without consuming a single component, so a kit already on order
            # genuinely does not need its handles and screws raised again -
            # exploding the pre-supply figure ordered parts for a build the plan
            # had already decided not to raise.
            #
            # A released production order is the opposite: it *is* a build, and
            # it still has to be given its parts. ``production.service.
            # release_order`` takes reservations only when the caller asks for
            # them (``ReleaseRequest.reserve_material``, which a line releasing
            # against staged material sets false), and even then
            # ``_reserve_components`` holds only what happened to be free and
            # reports the rest as a shortage - the job is released either way.
            # Issuing is a separate document later still. So a live job may hold
            # nothing at all, and netting its output off the *exploded* quantity
            # deletes the component demand that job depends on. Where it did
            # reserve, the hold is already excluded from
            # :attr:`StockPosition.available`, so those components are not
            # double-counted into stock either way; the residual error is an
            # over-order bounded by what live jobs are holding. An unnecessary
            # component order is visible and cancellable. A missing one stops the
            # line, so this module takes the visible failure.
            #
            # The parent's own requirement still nets against in-progress
            # production below: the parts it will make are real, and a plan that
            # ordered them again would be ordering the same kits twice.
            shortfall = net_after_supply(
                net_after_stock(bucket.direct, free),
                incoming=split.incoming,
                in_production=ZERO,
            )
            lines: tuple[ExplodedLine, ...] = ()
            if shortfall > 0:
                lines = tuple(await source.explode(item_id, shortfall, as_of))
                explosion_count += 1
            explosions[item_id] = _Explosion(
                split=split,
                shortfall=shortfall,
                component_date=_dependent_required_date(root_date, item.lead_time_days),
                lines=lines,
            )

    if not settled:
        # Believed unreachable. Rebuild anyway so the buckets carry every
        # explosion that was decided: a plan that is missing a whole level of
        # component demand is worse than one whose dates are a pass behind, and
        # :func:`plan` reports the dates that did not settle.
        buckets = _independent_only(direct)
        for parent_id, decided in explosions.items():
            _apply_explosion(
                buckets,
                parent_id=parent_id,
                parent_code=items[parent_id].code,
                explosion=decided,
            )

    splits = {item_id: decided.split for item_id, decided in explosions.items()}
    return buckets, splits, explosion_count, settled


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


def _unsettled_dates_warning(codes: Sequence[str]) -> str:
    """The needed-by dates did not stop moving inside the pass budget.

    Believed unreachable - :func:`_settle_demand` has a Bellman-Ford bound on the
    number of passes it needs - so this exists to make the one failure that
    would matter loud rather than invisible. If it ever fires, the explosion for
    the named items was decided at a *later* date than the requirement was
    netted at, which is the direction that under-raises component demand.
    """
    listed = ", ".join(sorted(codes)[:10])
    suffix = f" (and {len(codes) - 10} more)" if len(codes) > 10 else ""
    return (
        f"{len(codes)} item(s) are both demanded directly and used as components in a "
        "structure deep enough that their required dates did not settle within the "
        "planning budget, so their component demand may be understated: "
        f"{listed}{suffix}. Re-run with a narrower scope and raise this with support."
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
    budget.

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
            },
            warnings=("No open demand fell inside the horizon, so nothing was planned.",),
        )

    # --- round one: the demand items themselves ------------------------------
    demand_item_ids = list(buckets)
    items = await source.planning_items(demand_item_ids)
    positions = await source.stock_positions(demand_item_ids)
    structures = await source.bom_availability(demand_item_ids, as_of)
    # Supply is read in this round rather than after the explosions because a
    # parent has to be netted against its own inbound orders *before* its
    # structure is exploded - otherwise a kit already covered by a purchase order
    # still raises demand for every part in it. One statement per source per
    # round, never one per item.
    incoming, incoming_ok = await source.incoming_supply(demand_item_ids, horizon_end)
    in_production, production_ok = await source.in_production_supply(demand_item_ids, horizon_end)

    inactive = [i for i, info in items.items() if not info.is_active]
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

    # --- explode the shortfall, merging components as they arrive ------------
    # Every needed-by date settles *before* any supply decision is taken against
    # it, and the phasing each explosion used comes back in ``splits`` for the
    # netting stage below to reuse. See :func:`_settle_demand`.
    buckets, splits, explosion_count, settled = await _settle_demand(
        source,
        direct=buckets,
        items=items,
        positions=positions,
        structures=structures,
        incoming=incoming,
        in_production=in_production,
        as_of=as_of,
        respect_safety_stock=respect_safety_stock,
    )

    # --- round two: whatever the explosions discovered -----------------------
    # Two rounds rather than one because the component set does not exist until
    # the explosions have run. Both rounds are one statement each, over a set.
    new_item_ids = [i for i in buckets if i not in items]
    if new_item_ids:
        items.update(await source.planning_items(new_item_ids))
        positions.update(await source.stock_positions(new_item_ids))
        structures.update(await source.bom_availability(new_item_ids, as_of))
        component_incoming, component_incoming_ok = await source.incoming_supply(
            new_item_ids, horizon_end
        )
        component_production, component_production_ok = await source.in_production_supply(
            new_item_ids, horizon_end
        )
        incoming.update(component_incoming)
        in_production.update(component_production)
        # A stage that failed in either round is a stage the plan cannot claim to
        # have run: the caveat has to cover the whole item set, not the half that
        # happened to answer.
        incoming_ok = incoming_ok and component_incoming_ok
        production_ok = production_ok and component_production_ok

    unknown = [i for i in buckets if i not in items]
    if unknown:
        warnings.append(_missing_item_warning(unknown))
        for item_id in unknown:
            buckets.pop(item_id, None)

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

    # --- net, in the fixed order, and record every input ---------------------
    requirements: list[Requirement] = []
    shortages: list[tuple[Requirement, PlanningItem]] = []
    #: ``(code, split)`` per item whose inbound supply was excluded for being
    #: late. Drives the warning below. The whole split is carried rather than a
    #: flattened tuple so the prose is written from the same object the row is:
    #: a quantity and a date extracted separately can be paired wrongly, which
    #: is the defect this shape removes.
    late_supply: list[tuple[str, SupplySplit]] = []
    #: Items whose explosion was decided at a different date from the one their
    #: requirement is netted at. Empty by construction - see below.
    unsettled: list[str] = []

    for item_id, bucket in buckets.items():
        item = items[item_id]
        position = positions.get(item_id, StockPosition())
        free = planning_available(position, item.safety_stock, respect=respect_safety_stock)
        needed_by = bucket.earliest_date or as_of

        gross = to_quantity(bucket.direct + bucket.dependent)
        net = net_after_stock(gross, free)
        # THE time phasing. Only tranches promised on or before the date this
        # requirement is needed are supply for it; the rest are reported.
        #
        # For an item that was exploded this is not computed a second time: it
        # is the *same* split the explosion was decided on, handed back by
        # :func:`_settle_demand`. Two computations of one split are two answers
        # waiting to differ, which is exactly the defect this replaces. The date
        # is checked rather than assumed, and a mismatch - which settling makes
        # unreachable - is reported instead of quietly recomputed away.
        arrivals = incoming.get(item_id, ())
        jobs = in_production.get(item_id, ())
        split = splits.get(item_id)
        if split is not None and split.needed_by != needed_by:
            unsettled.append(item.code)
            split = None
        if split is None:
            split = supply_split(needed_by, incoming=arrivals, in_production=jobs)
        on_order = split.incoming
        late_on_order = split.late_incoming
        being_made = split.in_production
        late_being_made = split.late_in_production
        shortage = net_after_supply(net, on_order, being_made)

        # ASSUMPTION: the caveat is raised only where it would have made a
        # difference. An item covered by its own stock with a late order behind
        # it needs no warning - the plan is right, and saying so anyway is the
        # noise that trains a planner to skip the warnings that matter. The
        # figure is still on the requirement row for anyone who wants it.
        if (
            shortage > 0
            and split.first_late_arrival is not None
            and late_on_order + late_being_made > 0
        ):
            late_supply.append((item.code, split))

        requirement = Requirement(
            item_id=item_id,
            item_code=item.code,
            item_name=item.name,
            level=bucket.level or 0,
            direct_demand_quantity=to_quantity(bucket.direct),
            dependent_demand_quantity=to_quantity(bucket.dependent),
            gross_requirement=gross,
            on_hand_quantity=to_quantity(position.on_hand),
            reserved_quantity=to_quantity(position.reserved),
            available_quantity=free,
            incoming_quantity=on_order,
            in_production_quantity=being_made,
            net_requirement=net,
            shortage_quantity=shortage,
            earliest_required_date=needed_by,
            source_demand_count=bucket.source_count,
            source_demands=tuple(bucket.sources),
            late_incoming_quantity=late_on_order,
            late_in_production_quantity=late_being_made,
            first_late_incoming_date=split.first_late_incoming,
            last_late_incoming_date=split.last_late_incoming,
            last_late_in_production_date=split.last_late_in_production,
            first_late_in_production_date=split.first_late_in_production,
        )
        requirements.append(requirement)
        if shortage > 0:
            shortages.append((requirement, item))

    # --- suggestions ---------------------------------------------------------
    # Terms are loaded once for the short items only: an item that is covered
    # never needs its supplier's pack size.
    terms = await source.supply_terms([r.item_id for r, _ in shortages]) if shortages else {}

    suggestions: list[Suggestion] = []
    for requirement, item in shortages:
        suggestion_type = choose_suggestion_type(
            item, has_default_bom=structures.get(requirement.item_id, False)
        )
        policy = terms.get(requirement.item_id, SupplyTerms())

        # A vendor's lead time only governs when the item is being bought from
        # them; a manufacture or job-work suggestion is paced by the item's own
        # lead time, which is how long the plant or the processor takes.
        lead_time = (
            policy.lead_time_days
            if suggestion_type is MrpSuggestionType.PURCHASE and policy.lead_time_days > 0
            else item.lead_time_days
        )
        minimum = policy.min_order_quantity or item.min_order_quantity
        quantity = apply_order_policy(
            requirement.shortage_quantity,
            min_order_quantity=minimum,
            order_multiple=policy.order_multiple,
        )
        order_date = order_date_for(requirement.earliest_required_date, lead_time)

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
                source_demands=requirement.source_demands,
            )
        )

    if unsettled:
        warnings.append(_unsettled_dates_warning(unsettled))

    if late_supply:
        # By code, which is unique per run - one entry per item - so two runs of
        # the same plan produce the same warning text. Keyed rather than sorted
        # on the tuple: a ``SupplySplit`` is not ordered and never should be.
        warnings.append(_late_supply_warning(sorted(late_supply, key=lambda entry: entry[0])))

    # Level first, then code: components read under the products that need them,
    # and the order is stable so two runs of the same plan diff cleanly.
    ordered_requirements = tuple(sorted(requirements, key=lambda r: (r.level, r.item_code)))
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
        "late_suggestion_count": sum(1 for s in ordered_suggestions if s.is_late),
        # Short items whose inbound supply arrives after it is needed and was
        # therefore not netted. An int, like every other stat: ``result_summary``
        # is JSONB and a Decimal does not serialise into it - the quantities are
        # in the warning text and on the requirement rows.
        "late_supply_item_count": len(late_supply),
        # True when every needed-by date stopped moving before a supply decision
        # was taken against it, which is the property the explosion and the
        # netting agreeing rests on.
        "demand_dates_settled": settled and not unsettled,
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
    "MAX_SETTLING_PASSES",
    "DemandBucket",
    "DemandLine",
    "ExplodedLine",
    "MrpDataSource",
    "MrpDemandTooLargeError",
    "MrpPlan",
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
    "earliest_late_arrival",
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
