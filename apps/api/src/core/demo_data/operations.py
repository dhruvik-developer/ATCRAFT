"""Operations demonstration data: making the goods, and getting them out of the door.

The second half of the working day. The masters stage made the *things*, the
engineering stage described *how they are made*, procurement bought the metal;
this stage runs the shop floor and the despatch bay:

* **production orders** at every point of the lifecycle - one still being typed,
  one approved, one released and holding material, one issued, two under way on
  the floor, two finished - so the production list, the WIP board and the
  material-issue register all open with rows on them;
* **work orders** left at different operation statuses across four work centres,
  because a WIP board where every cell reads the same teaches nobody what it is
  for;
* an **assembly order** taken all the way through, because the rose plate is the
  one structure engineering built as an ``ASSEMBLY`` bill of material;
* **job work** out at the plater and back again - the module whose screen had no
  create path at all until this file existed;
* **quality inspections** at three verdicts plus one left in the queue;
* **sales orders** at four different stages of commitment, the reservations they
  took, an **MRP run** over the demand they created, and the **packing** and
  **despatch** that follow the two that were fully covered.

Why this stage runs the factory before it takes the orders
----------------------------------------------------------
The brief for this file lists sales first, and the code deliberately does not.
A sales order reserves stock at the moment it is confirmed and never afterwards,
so "one fully allocated order" is only reachable if the finished goods already
exist when ``confirm_order`` runs. Nothing else makes a lever handle set: it is a
MAKE item with a production structure, so the handle line has to run first. The
same argument chains down - a handle set consumes machined levers and polished
rose plates, and those are made here too, by a production order and an assembly
order respectively. So the order of work is supply first, demand second, and the
sales stage is the *last* thing that touches finished-goods stock before packing.

Everything degrades. Each document is attempted on its own and a failure is
recorded in ``ctx.skipped`` with the reason, because the interesting failures
here are shortages: a job released against stock procurement did not buy is a job
with a shortage on it, which is a perfectly good thing for an evaluator to look
at, and it must not cost the eight documents that come after it.

Idempotence
-----------
None of these documents has a code this file chooses - every one is numbered from
its own document series. So each carries its demonstration tag as the first token
of ``notes`` (``DM-PO-COMPLETED - Antique brass handle sets ...``) and a re-run
finds it by that tag instead of raising a second one. Quality inspections use
``reference_code``, which is the field made for exactly this, and the MRP run is
found by the note it was started with.

Where a document exists but has not been driven far enough - a run interrupted
between "created" and "released" - the lifecycle is resumed from wherever it
actually is rather than skipped, so a half-finished run heals itself on the next
pass instead of leaving a draft nobody notices.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.core.clock import utcnow
from src.core.demo_data import DemoContext, find_by_code
from src.core.errors import AppError, BusinessRuleError, NotFoundError, ValidationError
from src.modules.allocation.models import DemandType, ReservationStatus, StockReservation
from src.modules.assembly import service as assembly_service
from src.modules.assembly.models import AssemblyOrder, AssemblyOrderComponent, AssemblyOrderStatus
from src.modules.assembly.schemas import AssemblyOrderCreate, IssueComponentsRequest
from src.modules.assembly.schemas import CompleteRequest as AssemblyCompleteRequest
from src.modules.customers.models import Customer
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.models import (
    Dispatch,
    DispatchStatus,
    DispatchType,
    FreightPaidBy,
    TrackingEventSource,
)
from src.modules.dispatch.schemas import (
    DeliveryConfirmation,
    DispatchCreate,
    DispatchLineIn,
    DispatchRequest,
    InTransitRequest,
)
from src.modules.inventory import service as inventory_service
from src.modules.inventory.models import InventoryBalance, StockSerial, TransactionType
from src.modules.inventory.schemas import StockSerialCreate
from src.modules.job_work import service as job_work_service
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderStatus,
    JobWorkProcess,
    RateBasis,
    RejectionAction,
)
from src.modules.job_work.schemas import (
    IssueLineIn,
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.mrp import service as mrp_service
from src.modules.mrp.models import MrpRun
from src.modules.mrp.schemas import ManualDemandIn, MrpRunRequest
from src.modules.organization.models import Company
from src.modules.organization.schemas import CompanyUpdate
from src.modules.organization.service import CompanyService
from src.modules.packing import service as packing_service
from src.modules.packing.models import Carton, CartonType, PackingOrder, PackingOrderStatus
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    PackingOrderCreate,
    PackingOrderLineIn,
    SealCartonRequest,
)
from src.modules.packing.schemas import CompleteRequest as PackingCompleteRequest
from src.modules.production import service as production_service
from src.modules.production.models import (
    MaterialIssue,
    MaterialIssueType,
    ProductionEntry,
    ProductionOrder,
    ProductionOrderComponent,
    ProductionOrderStatus,
    WorkOrder,
    WorkOrderStatus,
)
from src.modules.production.schemas import (
    ApproveRequest,
    MaterialIssueCreate,
    MaterialIssueLineIn,
    MaterialIssuePostRequest,
    ProductionEntryCreate,
    ProductionOrderCreate,
    ReleaseRequest,
    StartRequest,
    WorkOrderTransitionRequest,
)
from src.modules.production.schemas import CompleteRequest as ProductionCompleteRequest
from src.modules.products.models import Item
from src.modules.quality import service as quality_service
from src.modules.quality.models import (
    OPEN_STATUSES,
    InspectionStatus,
    ParameterType,
    QcInspection,
    QcStage,
    QcTemplateParameter,
)
from src.modules.quality.schemas import (
    DecideRequest,
    InspectionCreate,
    MeasurementIn,
    RecordResultsRequest,
)
from src.modules.routing.models import Shift
from src.modules.sales import service as sales_service
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
from src.modules.vendors.models import Transporter, Vendor
from src.modules.warehouses.models import Warehouse

STAGE = "operations"

ZERO = Decimal("0")

#: The plater's lorry, and the two carrier tracking numbers. Fixed strings rather
#: than generated ones: a re-run must produce the same paperwork, and a tracking
#: id that changes every run is one nobody can test a carrier lookup against.
PLATER_VEHICLE = "GJ 03 CT 4417"
DISPATCH_TRACKING_ID = "VRL-2200418877"
DISPATCH_LR_NUMBER = "LR/RJT/44831"
DELIVERED_TRACKING_ID = "GATI-9004112763"
DELIVERED_LR_NUMBER = "LR/RJT/44802"


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------
# One anchor, and no document reads the clock twice. Two rows that belong to the
# same event have to carry the same date, and a run that crosses midnight between
# two calls to "today" stops being idempotent in the least visible way possible.
#
# Functions rather than module constants, for the reason engineering.py gives: a
# constant is evaluated at import time, which is a different moment from the run,
# and the whole point of routing time through src.core.clock is that it can be
# frozen.


def _anchor() -> date:
    return utcnow().date()


def _days_before(days: int) -> date:
    return _anchor() - timedelta(days=days)


def _days_after(days: int) -> date:
    return _anchor() + timedelta(days=days)


def _at(day: date, hour: int) -> datetime:
    """A timestamp on a business day, in UTC.

    The assembly and dispatch modules schedule in ``datetime`` where the rest of
    this file works in dates. Fixing the hour here keeps the two consistent and
    keeps every timestamp timezone-aware, which is what ``src.core.clock`` exists
    to enforce.
    """
    return datetime.combine(day, time(hour=hour), tzinfo=utcnow().tzinfo)


# -----------------------------------------------------------------------------
# Registering, counting and finding
# -----------------------------------------------------------------------------


def _seen(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID, *, created: bool) -> uuid.UUID:
    """Register a row and count it, whether this run made it or found it.

    Later stages resolve by code and cannot tell the difference, so both cases
    have to reach ``ctx.remember``.
    """
    ctx.note(STAGE, created=1 if created else 0, existing=0 if created else 1)
    return ctx.remember(kind, code, row_id)


def _skip(ctx: DemoContext, reason: str) -> None:
    ctx.skipped.append((STAGE, reason))


def _tagged(tag: str, sentence: str) -> str:
    """The note a document carries: its demonstration tag, then plain English.

    The tag leads so the idempotence lookup is a prefix match, and so anybody
    reading the document on screen can see at a glance that it is demonstration
    data rather than something a colleague raised this morning.
    """
    return f"{tag} - {sentence}"


async def _find_tagged(ctx: DemoContext, model: type[Any], tag: str) -> Any | None:
    """Has this document already been raised?

    Ordered by ``(created_at, id)`` rather than ``created_at`` alone: every row
    written in one transaction carries the same transaction-start timestamp, so
    the UUIDv7 primary key is the only tiebreaker that recovers true insertion
    order (CLAUDE.md). Without it a duplicate would resolve arbitrarily between
    runs, which is the one thing an idempotence key may not do.
    """
    stmt = (
        select(model)
        .where(model.company_id == ctx.company_id, model.notes.like(f"{tag} -%"))
        .order_by(model.created_at, model.id)
    )
    return (await ctx.session.execute(stmt)).scalars().first()


# -----------------------------------------------------------------------------
# Resolving the master data this stage builds documents out of
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ItemRef:
    """The three facts this stage needs about an item, taken as plain values.

    Deliberately not the ORM row. A caught failure inside ``transaction(...)``
    rolls back a savepoint, and a rolled-back savepoint expires every object in
    the identity map - so an ``Item`` cached before the failure would try to
    refresh itself on the next attribute read, which in async code is a
    ``MissingGreenlet`` rather than a query. Snapshots cannot go stale that way,
    and nothing here needs them to be live.
    """

    id: uuid.UUID
    code: str
    base_uom_id: uuid.UUID


class _Lookup:
    """Master rows by demonstration code, from the register or from the database.

    The register comes first because it is free. The database fallback is what
    makes ``--only operations`` work against an already-populated development
    database, where nothing earlier in this run has registered anything.

    Every method returns ``None`` rather than raising. A missing master row costs
    the documents that needed it and nothing else, and the caller says so in
    ``ctx.skipped`` in words that name the thing that was missing.
    """

    def __init__(self, ctx: DemoContext) -> None:
        self.ctx = ctx
        self._items: dict[str, _ItemRef | None] = {}

    async def item(self, *parts: str) -> _ItemRef | None:
        code = self.ctx.code(*parts)
        if code not in self._items:
            found = await find_by_code(self.ctx.session, Item, self.ctx.company_id, code)
            self._items[code] = (
                None
                if found is None
                else _ItemRef(id=found.id, code=found.code, base_uom_id=found.base_uom_id)
            )
        return self._items[code]

    async def _by_code(self, kind: str, model: type[Any], code: str) -> uuid.UUID | None:
        known = self.ctx.maybe_id(kind, code)
        if known is not None:
            return known
        found = await find_by_code(self.ctx.session, model, self.ctx.company_id, code)
        return found.id if found is not None else None

    async def warehouse(self, code: str) -> uuid.UUID | None:
        return await self._by_code("warehouse", Warehouse, code)

    async def customer(self, *parts: str) -> uuid.UUID | None:
        return await self._by_code("customer", Customer, self.ctx.code(*parts))

    async def vendor(self, *parts: str) -> uuid.UUID | None:
        return await self._by_code("vendor", Vendor, self.ctx.code(*parts))

    async def transporter(self, *parts: str) -> uuid.UUID | None:
        return await self._by_code("transporter", Transporter, self.ctx.code(*parts))


async def _shift_id(ctx: DemoContext, code: str) -> uuid.UUID | None:
    """The shift a demonstration booking names, by code.

    Bookings take a shift from the master now, never typed text. The engineering
    stage creates the master; if this stage runs on a database without it, the
    booking is made without a shift rather than not at all.
    """
    known = ctx.maybe_id("shift", code)
    if known is not None:
        return known
    found = await find_by_code(ctx.session, Shift, ctx.company_id, code)
    if found is None:
        ctx.skipped.append((STAGE, f"shift {code} not found; bookings were made without it"))
        return None
    return ctx.remember("shift", code, found.id)


# -----------------------------------------------------------------------------
# The one prerequisite this stage has to fix for itself
# -----------------------------------------------------------------------------


async def _ensure_seller_state(ctx: DemoContext) -> None:
    """Give the selling company a GST state code if it has none.

    ``sales.service._assert_seller_state_known`` refuses to raise any taxed order
    from a company whose state is unknown - correctly, because intra-state and
    inter-state supply cannot be told apart without it, and every item in this
    demonstration carries GST. The baseline seed does not set one and the masters
    stage does not either, so every sales order in this file would fail at
    validation.

    Set through ``CompanyService`` so the change is audited like any other, and
    only when the field is empty: an administrator who has already stated where
    the company is registered must not have it overwritten by a demo tool.
    Gujarat, code 24, to agree with the Rajkot warehouses the masters stage
    creates.
    """
    company = (
        (await ctx.session.execute(select(Company).where(Company.id == ctx.company_id)))
        .scalars()
        .first()
    )
    if company is None or company.state_code:
        return

    await CompanyService(ctx.session, ctx.tenant).update_company(
        CompanyUpdate(city="Rajkot", state="Gujarat", state_code="24")
    )
    ctx.note(STAGE, created=1)


# -----------------------------------------------------------------------------
# Reading stock, and staging it where a document can draw on it
# -----------------------------------------------------------------------------


async def _free_at(ctx: DemoContext, item_id: uuid.UUID, warehouse_id: uuid.UUID) -> Decimal:
    """What is promisable at the plain position of one item in one store.

    The plain position - no bin, no batch - is the one a reservation taken
    without either of them holds, and the one an issue posted without either of
    them draws from. Asking about the warehouse as a whole would count stock in a
    bin that a batch-less movement can never reach.
    """
    balance = await inventory_service.get_balance(ctx.session, ctx.tenant, item_id, warehouse_id)
    return Decimal(balance.available_quantity) if balance is not None else ZERO


async def _sources_of(
    ctx: DemoContext, item_id: uuid.UUID, *, exclude: uuid.UUID
) -> list[tuple[uuid.UUID, Decimal]]:
    """Where else this item is held, most stock first, then by id.

    One statement rather than a call per warehouse. The second sort key matters:
    two stores holding the same quantity must be drawn on in the same order on
    every run, or the demonstration ledger differs between databases that hold
    identical stock.
    """
    stmt = select(InventoryBalance.warehouse_id, InventoryBalance.available_quantity).where(
        InventoryBalance.company_id == ctx.company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id != exclude,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None),
        InventoryBalance.available_quantity > 0,
    )
    rows = (await ctx.session.execute(stmt)).all()
    return sorted(
        ((row[0], Decimal(row[1])) for row in rows),
        key=lambda pair: (-pair[1], str(pair[0])),
    )


async def _stage_to(
    ctx: DemoContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    required: Decimal,
    reference_code: str,
    reason: str,
) -> Decimal:
    """Bring a component to the one store an assembly order draws everything from.

    Production releases each component from its own default store, so it needs
    none of this. An assembly order does not: it names a single warehouse for the
    whole kit, and refuses the release outright if any line is short. The rose
    plate's structure calls for stainless bar (raw material store) and fixing
    screws (component store), so without a kitting move no single store could
    ever satisfy it.

    That move is a real one - the trolley that takes a kit to the bench - and it
    goes through ``inventory.service.transfer_stock``, which posts both legs in
    one batch and carries the cost layers across. Returns what is free at the
    destination afterwards, so the caller can decide whether the job is viable.
    """
    have = await _free_at(ctx, item_id, warehouse_id)
    short = required - have
    if short <= ZERO:
        return have

    for source_id, available in await _sources_of(ctx, item_id, exclude=warehouse_id):
        take = min(short, available)
        if take <= ZERO:
            continue
        await inventory_service.transfer_stock(
            ctx.session,
            ctx.tenant,
            item_id=item_id,
            from_warehouse_id=source_id,
            to_warehouse_id=warehouse_id,
            quantity=take,
            reference_code=reference_code,
            reason=reason,
        )
        ctx.note(STAGE, created=1)
        short -= take
        if short <= ZERO:
            break

    return await _free_at(ctx, item_id, warehouse_id)


# =============================================================================
# Production
# =============================================================================
# Eight jobs. Six of them exist to put one order in each status the production
# list can filter on; the other two exist because the finished handle set is
# built from parts that have to be made first, and a demonstration database in
# which the sub-assemblies were conjured into stock would teach an evaluator the
# wrong thing about how this system works.
#
# Quantities are the batch sizes this trade actually runs: levers off the turning
# centre in hundreds, handle sets off the assembly bench in tens. They are also
# chosen so that the sub-assembly supply covers everything downstream of it with
# a little to spare - see the arithmetic on LEVER_SUPPLY below.


@dataclass(frozen=True, slots=True)
class _Booking:
    """One shift's work booked against one operation of a job.

    ``sequence`` is the routing operation it was booked at. A booking at any
    operation but the last posts no stock - the goods are still on the floor -
    which is exactly why the in-process jobs book at the early operations and the
    finished ones book their output at completion.
    """

    sequence: int
    produced: str
    shift: str
    downtime_minutes: str = "0"
    note: str = ""


@dataclass(frozen=True, slots=True)
class _JobSpec:
    """One production order and how far down the lifecycle it is driven."""

    tag: tuple[str, ...]
    item: tuple[str, ...]
    quantity: str
    #: Where the output is received. Also the fallback store for any component
    #: whose item names no default of its own.
    warehouse: str
    start_offset: int
    end_offset: int
    target: ProductionOrderStatus
    sentence: str
    priority: int = 100
    #: Booked by ``complete_order`` itself, which is the only path that posts a
    #: PRODUCTION_RECEIPT for the whole job in one movement.
    produced: str = "0"
    rejected: str = "0"
    rejection_reason: str | None = None
    bookings: tuple[_Booking, ...] = ()
    #: Operations to finish and to leave running, by routing sequence.
    completed_operations: tuple[int, ...] = ()
    started_operations: tuple[int, ...] = ()
    #: An item code suffix and a quantity to hand back to the store unused.
    material_return: tuple[tuple[str, ...], str, str] | None = None


#: The status ladder, in the order the state machine walks it. Used to work out
#: how far an order that already exists still has to be driven, so an interrupted
#: run resumes rather than leaving a draft behind.
_JOB_PATH: tuple[ProductionOrderStatus, ...] = (
    ProductionOrderStatus.DRAFT,
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
    ProductionOrderStatus.COMPLETED,
)


#: The machined lever, made before anything that consumes it.
#:
#: 500 levers cover every downstream claim with room to spare: the four handle
#: jobs below take 2 each for 115 sets (230), and the two job work orders send
#: 160 out to the plater. The remainder is what the in-process quality inspection
#: puts on hold and what is left on the shelf afterwards.
LEVER_SUPPLY: tuple[_JobSpec, ...] = (
    _JobSpec(
        tag=("PO", "LEVER-DONE"),
        item=("SF", "LEVER-BRASS"),
        quantity="500",
        warehouse="WH-ASM",
        start_offset=-45,
        end_offset=-38,
        target=ProductionOrderStatus.COMPLETED,
        produced="500",
        priority=80,
        completed_operations=(10, 20, 30),
        sentence=(
            "Batch of 500 machined levers for the antique brass range. Turned, "
            "drilled and lacquered; the whole batch went to the assembly store."
        ),
    ),
)


#: The lifecycle spread, in the order they are raised.
#:
#: The finished job goes first so that it gets the sub-assemblies it needs before
#: anything else reserves them, then the two in-process jobs, then the ones that
#: are only holding stock. That is also the order a planner would work in - the
#: job that ships this week is kitted before the job that ships next month.
HANDLE_JOBS: tuple[_JobSpec, ...] = (
    _JobSpec(
        tag=("PO", "COMPLETED"),
        item=("FG", "LEVER-AB"),
        quantity="32",
        warehouse="WH-FG",
        start_offset=-21,
        end_offset=-16,
        target=ProductionOrderStatus.COMPLETED,
        # Two sets failed the function test on the bench. Rejected output is
        # counted and never received: it was never good stock, so there is
        # nothing to post, and the yield is visible on the order.
        produced="30",
        rejected="2",
        rejection_reason="Lever return sluggish under spring load on two sets",
        priority=70,
        completed_operations=(10, 20),
        sentence=(
            "Stock build of antique brass handle sets, finished and received into "
            "finished goods. Everything sold and shipped below comes out of it."
        ),
    ),
    _JobSpec(
        tag=("PO", "WIP-HANDLE"),
        item=("FG", "LEVER-AB"),
        quantity="25",
        warehouse="WH-FG",
        start_offset=-6,
        end_offset=1,
        target=ProductionOrderStatus.IN_PROCESS,
        priority=60,
        # Booked at the assembly operation, which is not the last one on this
        # routing, so nothing is received: 18 sets are built and waiting to be
        # packed. That is what makes the WIP board show work in hand rather than
        # stock that has quietly appeared in the warehouse.
        bookings=(
            _Booking(
                sequence=10,
                produced="18",
                shift="A",
                downtime_minutes="25",
                note="Spring feeder jammed twice; 25 minutes lost resetting it.",
            ),
        ),
        started_operations=(10,),
        # Screws are drawn by the box and counted back at the end of the shift,
        # which is the ordinary source of a material return on this line.
        material_return=(("BO", "SCREW-CSK"), "18", "Unused fixing screws counted back in"),
        sentence=(
            "Antique brass handle sets on the assembly bench. 18 of 25 built and "
            "waiting for the packing operation."
        ),
    ),
    _JobSpec(
        tag=("PO", "WIP-LEVER"),
        item=("SF", "LEVER-BRASS"),
        quantity="150",
        warehouse="WH-ASM",
        start_offset=-4,
        end_offset=3,
        target=ProductionOrderStatus.IN_PROCESS,
        priority=65,
        # Turning is finished, drilling is half done, polishing has not started.
        # Three operations at three different statuses across two work centres is
        # what the WIP board is for.
        bookings=(
            _Booking(sequence=10, produced="150", shift="A", note="Full batch off the lathe."),
            _Booking(sequence=20, produced="90", shift="B", note="Tapping in progress."),
        ),
        completed_operations=(10,),
        started_operations=(20,),
        sentence=(
            "Replenishment batch of levers. Turned in full, tapping half done, "
            "polishing still queued behind the buffing line."
        ),
    ),
    _JobSpec(
        tag=("PO", "ISSUED"),
        item=("FG", "LEVER-AB"),
        quantity="20",
        warehouse="WH-FG",
        start_offset=-2,
        end_offset=4,
        target=ProductionOrderStatus.MATERIAL_ISSUED,
        priority=90,
        sentence=(
            "Kitted and standing at the assembly bench, waiting for the line to "
            "come free. Material is out of the store and on the job."
        ),
    ),
    _JobSpec(
        tag=("PO", "RELEASED"),
        item=("FG", "LEVER-AB"),
        quantity="40",
        warehouse="WH-FG",
        start_offset=3,
        end_offset=10,
        target=ProductionOrderStatus.RELEASED,
        priority=100,
        sentence=(
            "Released for next week. The structure is frozen onto the job and the "
            "components are held, but nothing has been picked yet."
        ),
    ),
    _JobSpec(
        tag=("PO", "APPROVED"),
        item=("FG", "LEVER-AB"),
        quantity="60",
        warehouse="WH-FG",
        start_offset=12,
        end_offset=19,
        target=ProductionOrderStatus.APPROVED,
        priority=110,
        sentence=(
            "Approved against the month's plan. Nothing is reserved until it is "
            "released, so it is a commitment to build and not yet to stock."
        ),
    ),
    _JobSpec(
        tag=("PO", "DRAFT"),
        item=("FG", "LEVER-AB"),
        quantity="75",
        warehouse="WH-FG",
        start_offset=25,
        end_offset=32,
        target=ProductionOrderStatus.DRAFT,
        priority=120,
        sentence=(
            "Provisional build against the distributor's forecast. Still being "
            "sized, so it is a draft and holds nothing."
        ),
    ),
)


async def _components_of(ctx: DemoContext, order_id: uuid.UUID) -> list[ProductionOrderComponent]:
    stmt = (
        select(ProductionOrderComponent)
        .where(
            ProductionOrderComponent.company_id == ctx.company_id,
            ProductionOrderComponent.production_order_id == order_id,
        )
        .order_by(ProductionOrderComponent.line_number)
    )
    return list((await ctx.session.execute(stmt)).scalars())


async def _held_by_line(ctx: DemoContext, order_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
    """What each component of one job is still holding, by component id.

    One statement for the whole job. Read from ``allocation``'s own table because
    that is where the promise lives - and it is the promise, not the requirement,
    that decides what a storekeeper can actually pick.
    """
    stmt = select(StockReservation.demand_line_id, StockReservation.remaining_quantity).where(
        StockReservation.company_id == ctx.company_id,
        StockReservation.demand_type == DemandType.PRODUCTION_ORDER,
        StockReservation.demand_id == order_id,
        StockReservation.status.in_(
            (ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CONSUMED)
        ),
    )
    held: dict[uuid.UUID, Decimal] = {}
    for line_id, remaining in (await ctx.session.execute(stmt)).all():
        if line_id is None:
            continue
        held[line_id] = held.get(line_id, ZERO) + Decimal(remaining)
    return held


async def _kit_lines(ctx: DemoContext, order_id: uuid.UUID) -> list[MaterialIssueLineIn]:
    """The picking list: exactly what this job is holding, and nothing else.

    Issuing the *reserved* quantity rather than the *required* one is the one
    decision in this function, and it is what makes the issue postable against
    any state procurement left the stores in. A line drawn beyond the reservation
    posts a direct movement, which needs free stock at the position; the reserved
    quantity is by construction already there.

    It also disposes of the batch-tracked components for free. Release reserves
    at the plain position, a batch-tracked item holds nothing there, so it never
    acquires a reservation and never reaches this list - which is correct, since
    an issue line for such an item would have to name a lot that this file has no
    business choosing.
    """
    components = await _components_of(ctx, order_id)
    held = await _held_by_line(ctx, order_id)

    lines: list[MaterialIssueLineIn] = []
    for component in components:
        if component.is_backflushed:
            # Backflushed parts are issued automatically at completion. Picking
            # them here would consume them twice.
            continue
        outstanding = production_service.outstanding_requirement(component)
        quantity = min(held.get(component.id, ZERO), outstanding)
        if quantity <= ZERO:
            continue
        lines.append(
            MaterialIssueLineIn(
                line_number=len(lines) + 1,
                component_id=component.id,
                quantity=quantity,
                warehouse_id=component.warehouse_id,
                notes=f"Picked against {component.item_code}",
            )
        )
    return lines


async def _issue_kit(ctx: DemoContext, order: ProductionOrder, spec: _JobSpec) -> bool:
    """Draft the kit, post it, and let the posting advance the job.

    ``allow_partial_issue`` is set on every one of these because a demonstration
    database is exactly the case it exists for: procurement may not have bought
    everything, and a job that cannot be issued at all is a job that can never
    reach the floor. It only bites when a mandatory component is short, and when
    it does the shortfall is written to the audit entry rather than hidden.
    """
    tag = ctx.code(*spec.tag, "KIT")
    # The order's own status is the usual guard - a job that has been issued is
    # no longer RELEASED - but the issue is a document in its own right and gets
    # its own key, so a run that posted the issue and then failed cannot pick a
    # second kit off the shelf on the way through again.
    if await _find_tagged(ctx, MaterialIssue, tag) is not None:
        return True

    lines = await _kit_lines(ctx, order.id)
    if not lines:
        _skip(
            ctx,
            f"{order.code} could not be kitted: none of its components had stock to hold, "
            "so the job stays RELEASED",
        )
        return False

    issue, _ = await production_service.create_material_issue(
        ctx.session,
        ctx.tenant,
        order.id,
        MaterialIssueCreate(
            # The day the job started, or today for one that starts later: a
            # store issue cannot be dated into the future.
            issue_date=_anchor() + timedelta(days=min(spec.start_offset, 0)),
            reason="Kit picked for the assembly bench",
            notes=_tagged(tag, "Store issue against the job."),
            lines=lines,
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    ctx.note(STAGE, created=1)

    await production_service.post_material_issue(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        issue.id,
        MaterialIssuePostRequest(
            allow_partial_issue=True,
            override_reason="Demonstration build: the line runs on what the store actually has",
        ),
    )
    return True


async def _return_material(ctx: DemoContext, order: ProductionOrder, spec: _JobSpec) -> None:
    """Hand a component back to the store, through the return document.

    A return is not an edit of the issue. The issue is history the moment it
    posts; putting stock back on the shelf is its own document posting its own
    ``MATERIAL_RETURN`` movement, which is why this goes through the same create
    and post pair with the other issue type.
    """
    if spec.material_return is None:
        return
    item_parts, quantity, reason = spec.material_return
    wanted_code = ctx.code(*item_parts)

    component = next(
        (row for row in await _components_of(ctx, order.id) if row.item_code == wanted_code),
        None,
    )
    if component is None:
        _skip(ctx, f"{order.code} has no {wanted_code} line to return material from")
        return

    # Never more than the job is actually holding: a return beyond the net issue
    # would be inventing stock, and the service refuses it.
    returnable = min(Decimal(quantity), production_service.net_issued(component))
    if returnable <= ZERO:
        _skip(ctx, f"{order.code} issued no {wanted_code}, so there is nothing to return")
        return

    tag = ctx.code(*spec.tag, "RETURN")
    if await _find_tagged(ctx, MaterialIssue, tag) is not None:
        # The job stays IN_PROCESS for as long as this demonstration wants it
        # there, so the status is no guard at all here: without this key every
        # re-run would put the same screws back on the shelf again.
        return

    issue, _ = await production_service.create_material_issue(
        ctx.session,
        ctx.tenant,
        order.id,
        MaterialIssueCreate(
            reason=reason,
            notes=_tagged(tag, "Unused material handed back to the component store."),
            lines=[
                MaterialIssueLineIn(
                    line_number=1,
                    component_id=component.id,
                    quantity=returnable,
                    warehouse_id=component.warehouse_id,
                )
            ],
        ),
        issue_type=MaterialIssueType.RETURN,
    )
    await production_service.post_material_issue(
        ctx.session, ctx.tenant, ctx.principal, issue.id, MaterialIssuePostRequest()
    )
    ctx.note(STAGE, created=1)


async def _work_orders_of(ctx: DemoContext, order_id: uuid.UUID) -> dict[int, WorkOrder]:
    stmt = (
        select(WorkOrder)
        .where(WorkOrder.company_id == ctx.company_id, WorkOrder.production_order_id == order_id)
        .order_by(WorkOrder.sequence)
    )
    return {row.sequence: row for row in (await ctx.session.execute(stmt)).scalars()}


async def _run_the_floor(ctx: DemoContext, order: ProductionOrder, spec: _JobSpec) -> None:
    """Book the shifts and leave the traveller at the statuses the spec asks for.

    Bookings come first because the first booking against an operation starts it
    on its own - the service does that, not this file - so an operation that is
    only "started" needs nothing further, and one that is finished is transitioned
    afterwards. Doing it the other way round would try to book against a
    completed operation.
    """
    work_orders = await _work_orders_of(ctx, order.id)

    # A job that is meant to *stay* IN_PROCESS is walked through this function on
    # every run, and a booking has no natural key of its own - so the presence of
    # any booking at all is the guard. Without it a re-run books the same shift's
    # output a second time and the order's produced quantity drifts upward.
    already_booked = (
        await ctx.session.execute(
            select(ProductionEntry.id)
            .where(
                ProductionEntry.company_id == ctx.company_id,
                ProductionEntry.production_order_id == order.id,
            )
            .limit(1)
        )
    ).first() is not None
    bookings: tuple[_Booking, ...] = () if already_booked else spec.bookings

    for booking in bookings:
        work_order = work_orders.get(booking.sequence)
        if work_order is None:
            _skip(
                ctx,
                f"{order.code} has no operation {booking.sequence} to book against; "
                "its routing is shorter than this demonstration expects",
            )
            continue
        await production_service.create_production_entry(
            ctx.session,
            ctx.tenant,
            order.id,
            ProductionEntryCreate(
                work_order_id=work_order.id,
                entry_date=_anchor() + timedelta(days=spec.start_offset + 1),
                shift_id=await _shift_id(ctx, booking.shift),
                produced_quantity=Decimal(booking.produced),
                downtime_minutes=Decimal(booking.downtime_minutes),
                notes=booking.note or None,
            ),
        )
        ctx.note(STAGE, created=1)

    for sequence in spec.completed_operations:
        work_order = work_orders.get(sequence)
        if work_order is None or WorkOrderStatus(work_order.status) in (
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.CANCELLED,
        ):
            continue
        # An operation nobody booked against is still PENDING, and
        # ``WORK_ORDER_FLOW`` has no PENDING -> COMPLETED edge - the traveller
        # must be started before it can be finished. The booking loop above
        # starts the operations it books, so this only bites an operation the
        # spec finishes without recording output against it, which is exactly
        # the case for the operations a job passed through before the shift this
        # demonstration books. Walking it through IN_PROGRESS is the honest fix:
        # the machine is right, and a job whose first operation was never
        # started is not a state the factory can be in either.
        if WorkOrderStatus(work_order.status) is WorkOrderStatus.PENDING:
            await production_service.transition_work_order(
                ctx.session,
                ctx.tenant,
                work_order.id,
                WorkOrderTransitionRequest(
                    status=WorkOrderStatus.IN_PROGRESS,
                    reason=f"Operation {work_order.operation_code} started",
                ),
            )
            ctx.note(STAGE, created=1)
        await production_service.transition_work_order(
            ctx.session,
            ctx.tenant,
            work_order.id,
            WorkOrderTransitionRequest(
                status=WorkOrderStatus.COMPLETED,
                reason=f"Operation {work_order.operation_code} finished",
            ),
        )
        ctx.note(STAGE, created=1)

    for sequence in spec.started_operations:
        work_order = work_orders.get(sequence)
        if work_order is None or WorkOrderStatus(work_order.status) is not WorkOrderStatus.PENDING:
            # Already running: the booking above started it, which is the usual
            # case and not something to correct.
            continue
        await production_service.transition_work_order(
            ctx.session,
            ctx.tenant,
            work_order.id,
            WorkOrderTransitionRequest(
                status=WorkOrderStatus.IN_PROGRESS,
                reason=f"Operation {work_order.operation_code} under way",
            ),
        )
        ctx.note(STAGE, created=1)


async def _advance_job(ctx: DemoContext, order: ProductionOrder, spec: _JobSpec) -> None:
    """Walk one job from wherever it is to wherever the spec wants it.

    Written as a resumption rather than a sequence so a run interrupted halfway
    picks up where it stopped. Each step is guarded by the status the order is
    actually in, which is also what makes a second run a no-op: every guard is
    false.
    """
    target_index = _JOB_PATH.index(spec.target)

    if target_index >= 1 and ProductionOrderStatus(order.status) is ProductionOrderStatus.DRAFT:
        await production_service.approve_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            ApproveRequest(note="Approved against the production plan"),
        )
        ctx.note(STAGE, created=1)

    if target_index >= 2 and ProductionOrderStatus(order.status) is ProductionOrderStatus.APPROVED:
        # No structure and no routing are named: release resolves the item's own
        # default approved bill of material and its effective routing, which is
        # what the engineering stage set up and what a planner would rely on.
        # Components are left to their own default stores, so each line is drawn
        # from where procurement actually received it.
        await production_service.release_order(
            ctx.session,
            ctx.tenant,
            order.id,
            ReleaseRequest(note="Released to the floor", reserve_material=True),
        )
        ctx.note(STAGE, created=1)

    # Combined rather than nested: the kit either goes out or this order stops
    # where it is. Carrying on past a failed issue would start an order whose
    # components never left the store.
    if (
        target_index >= 3
        and ProductionOrderStatus(order.status) is ProductionOrderStatus.RELEASED
        and not await _issue_kit(ctx, order, spec)
    ):
        return

    if (
        target_index >= 4
        and ProductionOrderStatus(order.status) is ProductionOrderStatus.MATERIAL_ISSUED
    ):
        await production_service.start_order(
            ctx.session,
            ctx.tenant,
            order.id,
            StartRequest(
                actual_start_date=_anchor() + timedelta(days=spec.start_offset),
                note="First operation started on the floor",
            ),
        )
        ctx.note(STAGE, created=1)

    if (
        target_index >= 4
        and ProductionOrderStatus(order.status) is ProductionOrderStatus.IN_PROCESS
    ):
        await _run_the_floor(ctx, order, spec)
        await _return_material(ctx, order, spec)

    if (
        target_index >= 5
        and ProductionOrderStatus(order.status) is ProductionOrderStatus.IN_PROCESS
    ):
        await production_service.complete_order(
            ctx.session,
            ctx.tenant,
            order.id,
            ProductionCompleteRequest(
                produced_quantity=Decimal(spec.produced),
                rejected_quantity=Decimal(spec.rejected),
                rejection_reason=spec.rejection_reason,
                entry_date=_anchor() + timedelta(days=spec.end_offset),
                actual_end_date=_anchor() + timedelta(days=spec.end_offset),
                shift_id=await _shift_id(ctx, "A"),
                note="Job finished and output received",
            ),
        )
        ctx.note(STAGE, created=1)


async def _seed_jobs(ctx: DemoContext, look: _Lookup, specs: Sequence[_JobSpec]) -> None:
    """Raise each job and drive it, one at a time and independently.

    Independently is the point: a shortage on the released job must not cost the
    finished one the receipt that puts stock in the warehouse for everything
    downstream of it.
    """
    for spec in specs:
        tag = ctx.code(*spec.tag)
        item = await look.item(*spec.item)
        warehouse_id = await look.warehouse(spec.warehouse)
        if item is None or warehouse_id is None:
            _skip(ctx, f"{tag} not raised: no {ctx.code(*spec.item)} or no {spec.warehouse}")
            continue

        existing = await _find_tagged(ctx, ProductionOrder, tag)
        if existing is not None:
            order = existing
            _seen(ctx, "production_order", tag, order.id, created=False)
        else:
            try:
                order = await production_service.create_order(
                    ctx.session,
                    ctx.tenant,
                    ProductionOrderCreate(
                        item_id=item.id,
                        planned_quantity=Decimal(spec.quantity),
                        warehouse_id=warehouse_id,
                        # Charged to Production; the masters stage registers
                        # it as DM-CC-PROD. Absent, the job is unallocated.
                        cost_centre_id=ctx.maybe_id("cost_centre", ctx.code("CC", "PROD")),
                        planned_start_date=_anchor() + timedelta(days=spec.start_offset),
                        planned_end_date=_anchor() + timedelta(days=spec.end_offset),
                        priority=spec.priority,
                        notes=_tagged(tag, spec.sentence),
                    ),
                )
            except (BusinessRuleError, NotFoundError, ValidationError) as exc:
                _skip(ctx, f"{tag} not raised: {exc}")
                continue
            _seen(ctx, "production_order", tag, order.id, created=True)

        # Read before the call, not after: a caught failure rolls back the
        # savepoint the service opened, and that expires every row in the
        # identity map - so ``order.status`` in the handler below would be a
        # refresh, and a refresh inside async code raises MissingGreenlet
        # instead of answering (CLAUDE.md).
        order_code = order.code
        try:
            await _advance_job(ctx, order, spec)
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            # The job exists and is registered; it simply did not get as far down
            # the lifecycle as the spec wanted.
            _skip(ctx, f"{tag} ({order_code}) did not reach {spec.target}: {exc}")


# =============================================================================
# Assembly
# =============================================================================
# One order, taken all the way to COMPLETED, because the polished rose plate is
# the only item engineering gave an ASSEMBLY structure and the handle sets above
# cannot be built without a stock of them.
#
# On serial genealogy: the brief for this file asks for an assembly that consumes
# a serial-tracked component, and the demonstration item master cannot supply
# one. Only the digital lock is serial tracked, and it is a finished good, so
# ``products.default_capabilities`` leaves ``can_be_bom_component`` false on it -
# which means no structure may name it, which means no assembly can consume it.
# Rather than flip a capability flag on another stage's item to make a sentence
# true, this stage says so in ctx.skipped and gives the serial trail a real
# answer the other way round: six digital locks are registered and stocked below,
# packed into a carton by serial number and shipped on a consignment, so
# "where did unit DM-DL900-0003 go?" has an answer that runs all the way to a
# customer.


ROSE_ORDER_TAG = ("ASM", "ROSE")


async def _stage_assembly_kit(
    ctx: DemoContext, order: AssemblyOrder, components: Sequence[AssemblyOrderComponent]
) -> bool:
    """Bring every component of the order to the store it draws from.

    Reads the requirement off the order's own exploded component rows rather than
    recomputing it, so the quantities moved are exactly what the structure asks
    for however the structure is edited. Returns whether the whole kit is now
    covered - the release reserves all or nothing, so a partially staged kit is
    not worth attempting.
    """
    # Codes only, and as plain strings: they are wanted for one message, and a
    # transfer that fails partway would expire an ORM row read before it.
    codes: dict[uuid.UUID, str] = {
        row[0]: row[1]
        for row in (
            await ctx.session.execute(
                select(Item.id, Item.code).where(Item.id.in_([c.item_id for c in components]))
            )
        ).all()
    }
    order_code = order.code
    warehouse_id = order.warehouse_id

    covered = True
    for component in components:
        required = Decimal(component.required_quantity)
        free = await _stage_to(
            ctx,
            item_id=component.item_id,
            warehouse_id=warehouse_id,
            required=required,
            reference_code=order_code,
            reason=f"Kit moved to the assembly store for {order_code}",
        )
        if free < required:
            covered = False
            _skip(
                ctx,
                f"{order_code} is short of {codes.get(component.item_id, component.item_id)}: "
                f"{free} at the assembly store against {required} required",
            )
    return covered


async def _seed_assembly(ctx: DemoContext, look: _Lookup) -> None:
    """Build the rose plates, and leave the order closed out at COMPLETED."""
    tag = ctx.code(*ROSE_ORDER_TAG)
    rose = await look.item("SF", "ROSE-SS")
    warehouse_id = await look.warehouse("WH-ASM")
    if rose is None or warehouse_id is None:
        _skip(ctx, f"{tag} not raised: no rose plate item or no assembly store")
        return

    existing = await _find_tagged(ctx, AssemblyOrder, tag)
    if existing is not None:
        order = existing
        _seen(ctx, "assembly_order", tag, order.id, created=False)
    else:
        try:
            order, _ = await assembly_service.create_order(
                ctx.session,
                ctx.tenant,
                AssemblyOrderCreate(
                    item_id=rose.id,
                    planned_quantity=Decimal("250"),
                    warehouse_id=warehouse_id,
                    output_warehouse_id=warehouse_id,
                    assembly_line="Rose bench",
                    planned_start=_at(_days_before(30), 6),
                    planned_end=_at(_days_before(25), 14),
                    notes=_tagged(
                        tag,
                        "Tray build of 52 mm rose plates for the antique brass and "
                        "satin stainless ranges.",
                    ),
                ),
            )
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            # No approved ASSEMBLY structure, or a component the module refuses
            # to handle. Costs the rose plates and nothing else.
            _skip(ctx, f"{tag} not raised: {exc}")
            return
        _seen(ctx, "assembly_order", tag, order.id, created=True)

    order_code = order.code
    try:
        await _advance_assembly(ctx, order)
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        _skip(ctx, f"{tag} ({order_code}) was not completed: {exc}")


async def _advance_assembly(ctx: DemoContext, order: AssemblyOrder) -> None:
    """Approve, kit, release, issue, start and complete - resuming from anywhere."""
    status = AssemblyOrderStatus(order.status)

    if status is AssemblyOrderStatus.DRAFT:
        await assembly_service.approve_order(
            ctx.session, ctx.tenant, order.id, reason="Signed off against the assembly plan"
        )
        ctx.note(STAGE, created=1)

    if AssemblyOrderStatus(order.status) is AssemblyOrderStatus.APPROVED:
        stmt = (
            select(AssemblyOrderComponent)
            .where(
                AssemblyOrderComponent.company_id == ctx.company_id,
                AssemblyOrderComponent.assembly_order_id == order.id,
            )
            .order_by(AssemblyOrderComponent.line_number)
        )
        components = list((await ctx.session.execute(stmt)).scalars())
        if not await _stage_assembly_kit(ctx, order, components):
            # The release refuses a short kit outright rather than reserving what
            # it can, so there is nothing to gain by trying it.
            return
        await assembly_service.release_order(
            ctx.session, ctx.tenant, order.id, reason="Released to the rose bench"
        )
        ctx.note(STAGE, created=1)

    if AssemblyOrderStatus(order.status) is AssemblyOrderStatus.RELEASED:
        # No line overrides: every component is handed over at its full
        # requirement, which is the ordinary kitting case.
        await assembly_service.issue_components(
            ctx.session,
            ctx.tenant,
            order.id,
            IssueComponentsRequest(reason="Kit handed to the bench"),
        )
        ctx.note(STAGE, created=1)

    if AssemblyOrderStatus(order.status) is AssemblyOrderStatus.COMPONENTS_ISSUED:
        await assembly_service.start_assembly(
            ctx.session, ctx.tenant, order.id, reason="Bench started on the tray"
        )
        ctx.note(STAGE, created=1)

    if AssemblyOrderStatus(order.status) is AssemblyOrderStatus.IN_PROGRESS:
        # 245 good and 5 rejected. Both consumed their components - a plate that
        # failed the finish check ate its bar and its screws exactly as a good one
        # did - which is why completion scales consumption by the sum of the two.
        #
        # `occurred_at` is deliberately NOT back-dated with the rest of this
        # file's dates. `start_assembly` stamps `actual_start = utcnow()` and
        # takes no override, so an order started during this run cannot have
        # finished last month: the service refuses `actual_end < actual_start`
        # before the CHECK constraint can, and the whole assembly - and every
        # serial trail that depends on its output - is lost. Leaving it unset
        # lets the service stamp the completion itself, which is the only value
        # that can be consistent with a start it also chose.
        await assembly_service.complete_assembly(
            ctx.session,
            ctx.tenant,
            order.id,
            AssemblyCompleteRequest(
                assembled_quantity=Decimal("245"),
                rejected_quantity=Decimal("5"),
                reason="Tray finished; five plates marked off for pitting on the face",
            ),
        )
        ctx.note(STAGE, created=1)


# =============================================================================
# Serialised finished goods
# =============================================================================
# Six digital locks, registered as units and opened into finished goods.
#
# This is the only movement in this stage that is not the by-product of a
# document, and it is here because nothing else can put a serialised unit on the
# shelf: the digital lock is a MAKE item with no structure, so no production
# order can receive it, and it is not bought, so no goods receipt will either.
# Without it the serial register, the serial search on a carton and the "which
# consignment did this unit leave on" trail are all empty screens.
#
# OPENING_BALANCE is the honest transaction type for stock that predates the
# demonstration, and the engine moves each unit to IN_STOCK as it posts. One
# movement per serial, because a serial identifies one physical object and the
# engine refuses any other quantity against one.

SERIAL_ITEM = ("FG", "DIGILOCK")
SERIAL_NUMBERS: tuple[str, ...] = (
    "DL900-24A0117",
    "DL900-24A0118",
    "DL900-24A0119",
    "DL900-24A0120",
    "DL900-24A0121",
    "DL900-24A0122",
)
#: What the six units were carried in at. The item master's standard cost, so the
#: opening layer agrees with every valuation report that reads it.
SERIAL_UNIT_COST = Decimal("4250.00")


async def _seed_serial_stock(ctx: DemoContext, look: _Lookup) -> list[uuid.UUID]:
    """Register the six units and receive them. Returns the ones in stock."""
    item = await look.item(*SERIAL_ITEM)
    warehouse_id = await look.warehouse("WH-FG")
    if item is None or warehouse_id is None:
        _skip(ctx, "no serial-tracked finished good or no finished-goods store")
        return []

    traceability = inventory_service.TraceabilityService(ctx.session, ctx.tenant)
    in_stock: list[uuid.UUID] = []

    for number in SERIAL_NUMBERS:
        code = ctx.code("SN", number)
        existing = (
            (
                await ctx.session.execute(
                    select(StockSerial).where(
                        StockSerial.company_id == ctx.company_id,
                        StockSerial.item_id == item.id,
                        StockSerial.serial_number == number,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            in_stock.append(_seen(ctx, "serial", code, existing.id, created=False))
            continue

        serial = await traceability.create_serial(
            StockSerialCreate(
                item_id=item.id,
                serial_number=number,
                warehouse_id=warehouse_id,
                received_on=_days_before(60),
                unit_cost=SERIAL_UNIT_COST,
                notes="Opening stock of the DL-900 keypad lock.",
            )
        )
        # The registration says where the unit is expected; the movement is what
        # actually puts it there and takes it to IN_STOCK. Both are needed: a
        # registered unit with no receipt is a serial nothing can be shipped
        # against, because the balance it would draw on holds nothing.
        await inventory_service.post_movement(
            ctx.session,
            ctx.tenant,
            item_id=item.id,
            warehouse_id=warehouse_id,
            quantity=Decimal("1"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="demo_opening_stock",
            reference_id=serial.id,
            reference_code=number,
            reason="Opening stock of serialised finished goods for the demonstration",
            serial_id=serial.id,
            unit_cost=SERIAL_UNIT_COST,
            occurred_at=_at(_days_before(60), 9),
        )
        in_stock.append(_seen(ctx, "serial", code, serial.id, created=True))

    _skip(
        ctx,
        "no assembly consumes a serialised component: the only serial-tracked demonstration "
        "item is a finished good, and finished goods are not flagged usable as BOM "
        "components, so no approved structure can name one. The serial trail is carried "
        "by packing and despatch instead",
    )
    return in_stock


# =============================================================================
# Job work
# =============================================================================
# Two orders at the plater, deliberately at the two ends of the cycle: one whose
# material is still sitting at the vendor, and one that has come back. The first
# is what the "pending at vendor" screen and the ageing on it are for; the second
# is what proves the job-work pool empties again.
#
# Both send machined levers for antique brass plating, which is genuinely how
# this trade works - the finish is bought, not applied in house - and both are
# priced per piece, which is how a plater quotes.


@dataclass(frozen=True, slots=True)
class _JobWorkSpec:
    tag: tuple[str, ...]
    item: tuple[str, ...]
    quantity: str
    rate: str
    #: Days ahead of the anchor. Always in the future, even on the order that
    #: has already been received: ``create_order`` refuses a return date in the
    #: past, so there is no way to state one for a consignment that went out a
    #: fortnight ago. The challan date below carries the real history.
    return_offset: int
    #: Days *before* the anchor, so it is negative: when the lorry left.
    challan_offset: int
    sentence: str
    #: ``None`` leaves the material at the vendor. Otherwise the good output, the
    #: rejected output and the day it came back.
    receipt: tuple[str, str, int] | None = None


JOB_WORK: tuple[_JobWorkSpec, ...] = (
    _JobWorkSpec(
        tag=("JW", "AT-VENDOR"),
        item=("SF", "LEVER-BRASS"),
        quantity="100",
        rate="18.50",
        return_offset=9,
        challan_offset=-3,
        sentence=(
            "Levers out for antique brass plating. Sent on Tuesday, due back next "
            "week; the material is on the company's books at the vendor."
        ),
    ),
    _JobWorkSpec(
        tag=("JW", "RECEIVED"),
        item=("SF", "LEVER-BRASS"),
        quantity="60",
        rate="18.50",
        return_offset=6,
        challan_offset=-12,
        # Two came back with the plating burnt at the lever tip. Returned to
        # stock as damaged rather than written off: they are physically present,
        # countable and not promisable, which is exactly what that pool is for.
        receipt=("58", "2", -5),
        sentence=(
            "Levers back from the plater against challan. Fifty-eight passed, two "
            "burnt at the tip and returned as damaged."
        ),
    ),
)


async def _seed_job_work(ctx: DemoContext, look: _Lookup) -> None:
    vendor_id = await look.vendor("VEN", "PLATE")
    warehouse_id = await look.warehouse("WH-ASM")
    # NOT TRN-CITY: masters retires that one on purpose, so the transporter
    # screen has an inactive row to show. A retired carrier cannot be named on a
    # live consignment - the service refuses it - so real movements here use a
    # carrier that is still trading.
    transporter_id = await look.transporter("TRN", "VRL")
    if vendor_id is None or warehouse_id is None:
        _skip(ctx, "no approved plating vendor or no assembly store, so no job work")
        return

    for spec in JOB_WORK:
        tag = ctx.code(*spec.tag)
        item = await look.item(*spec.item)
        if item is None:
            _skip(ctx, f"{tag} not raised: no {ctx.code(*spec.item)}")
            continue

        existing = await _find_tagged(ctx, JobWorkOrder, tag)
        if existing is not None:
            order = existing
            _seen(ctx, "job_work_order", tag, order.id, created=False)
        else:
            try:
                order, _ = await job_work_service.create_order(
                    ctx.session,
                    ctx.tenant,
                    JobWorkOrderCreate(
                        vendor_id=vendor_id,
                        process_type=JobWorkProcess.COATING,
                        process_description=(
                            "Antique brass electroplate over nickel, lacquered, to the "
                            "master sample held at the works."
                        ),
                        expected_return_date=_days_after(spec.return_offset),
                        warehouse_id=warehouse_id,
                        return_warehouse_id=warehouse_id,
                        # Plating is job-worked: the vendor stands in for the
                        # DM-CC-PROD-PLAT process the masters stage registers.
                        cost_centre_id=ctx.maybe_id("cost_centre", ctx.code("CC", "PROD", "PLAT")),
                        rate_per_unit=Decimal(spec.rate),
                        rate_basis=RateBasis.PER_PIECE,
                        notes=_tagged(tag, spec.sentence),
                        lines=[
                            JobWorkOrderLineCreate(
                                line_number=1,
                                item_id=item.id,
                                quantity_to_issue=Decimal(spec.quantity),
                                # No output item: plating changes the finish, not
                                # the part. The same code comes back.
                                expected_output_quantity=Decimal(spec.quantity),
                                expected_loss_percentage=Decimal("2"),
                                notes="Antique brass, lacquered.",
                            )
                        ],
                    ),
                )
            except (BusinessRuleError, NotFoundError, ValidationError) as exc:
                _skip(ctx, f"{tag} not raised: {exc}")
                continue
            _seen(ctx, "job_work_order", tag, order.id, created=True)

        order_code = order.code
        try:
            await _advance_job_work(ctx, order, spec, transporter_id=transporter_id)
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            _skip(ctx, f"{tag} ({order_code}) did not reach the vendor and back: {exc}")


async def _advance_job_work(
    ctx: DemoContext,
    order: JobWorkOrder,
    spec: _JobWorkSpec,
    *,
    transporter_id: uuid.UUID | None,
) -> None:
    """Approve, send, and where the spec asks for it, receive back."""
    if JobWorkOrderStatus(order.status) is JobWorkOrderStatus.DRAFT:
        # Approval is what reserves the levers. From here they belong to this
        # order and no sales order or production job can be promised them.
        await job_work_service.approve_order(
            ctx.session,
            ctx.tenant,
            order.id,
            reason="Approved; rate confirmed against the plater's quotation",
        )
        ctx.note(STAGE, created=1)

    if JobWorkOrderStatus(order.status) is JobWorkOrderStatus.APPROVED:
        # The issue posts JOB_WORK_ISSUE, which moves the quantity out of on-hand
        # and into the job-work pool. The company still owns it and still values
        # it; it is simply standing in somebody else's shed.
        await job_work_service.issue_order(
            ctx.session,
            ctx.tenant,
            order.id,
            IssueRequest(
                reason="Sent to the plater against a delivery challan",
                challan_date=_anchor() + timedelta(days=spec.challan_offset),
                vehicle_number=PLATER_VEHICLE,
                transporter_id=transporter_id,
                expected_return_date=_days_after(spec.return_offset),
                challan_notes="Job work challan under rule 45; goods to return after plating.",
                occurred_at=_at(_anchor() + timedelta(days=spec.challan_offset), 11),
                lines=[IssueLineIn(line_number=1, issued_quantity=Decimal(spec.quantity))],
            ),
        )
        ctx.note(STAGE, created=1)

    if spec.receipt is None:
        return

    good, rejected, offset = spec.receipt
    if JobWorkOrderStatus(order.status) in (
        JobWorkOrderStatus.ISSUED,
        JobWorkOrderStatus.PARTIALLY_RECEIVED,
    ):
        await job_work_service.receive(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            ReceiveRequest(
                reason="Plated levers received back and counted against the challan",
                receipt_date=_anchor() + timedelta(days=offset),
                vendor_challan_number="CL/24/1188",
                vendor_challan_date=_anchor() + timedelta(days=offset),
                occurred_at=_at(_anchor() + timedelta(days=offset), 15),
                notes="Finish checked against the master sample at goods inward.",
                lines=[
                    ReceiptLineIn(
                        line_number=1,
                        output_quantity=Decimal(good),
                        rejected_quantity=Decimal(rejected),
                        rejection_action=RejectionAction.RETURN_TO_STOCK,
                        is_final=True,
                        notes=(
                            "Two burnt at the lever tip, back on the shelf as damaged. "
                            "Line closed; nothing left at the vendor."
                        ),
                    )
                ],
            ),
        )
        ctx.note(STAGE, created=1)


# =============================================================================
# Quality
# =============================================================================
# Four inspections against the three templates engineering wrote: one accepted,
# one rejected on a critical characteristic, one split between accepted and
# rejected, and one left in the queue with no readings on it at all.
#
# Every one of them holds its own stock. A manual inspection is a supervisor
# pulling a lot off the shelf, so the quantity has to leave the available pool
# when the inspection is raised and come back - or not - when it is decided.
# That is the whole reason these are worth seeding: the QC hold column on the
# stock screen is otherwise always zero.
#
# The readings are derived from whatever the template actually says rather than
# typed against parameters by name. A template edited in the engineering stage
# then produces a different check sheet here and the inspection still decides,
# instead of failing on a parameter that no longer exists.


@dataclass(frozen=True, slots=True)
class _InspectionSpec:
    tag: tuple[str, ...]
    item: tuple[str, ...]
    warehouse: str
    stage: QcStage
    quantity: str
    sentence: str
    vendor: tuple[str, ...] | None = None
    #: ``None`` leaves the inspection PENDING with no readings - the queue.
    verdict: tuple[str, str] | None = None
    rejection_reason: str | None = None
    #: Which characteristic to record out of specification, if any.
    fail: str = "none"


INSPECTIONS: tuple[_InspectionSpec, ...] = (
    _InspectionSpec(
        tag=("QC", "ACCEPTED"),
        item=("RM", "BRASS"),
        warehouse="WH-RM",
        stage=QcStage.INCOMING,
        # A mill lot, and deliberately smaller than a delivery: quality runs
        # after the shop floor here, so the bar this holds has to be bar the
        # lever batches did not already consume.
        quantity="180",
        vendor=("VEN", "BRASS"),
        verdict=("180", "0"),
        sentence="Goods-inward check on a delivery of 12 mm free-cutting brass bar.",
    ),
    _InspectionSpec(
        tag=("QC", "REJECTED"),
        item=("RM", "BRASS"),
        warehouse="WH-RM",
        stage=QcStage.INCOMING,
        quantity="45",
        vendor=("VEN", "BRASS"),
        verdict=("0", "45"),
        rejection_reason="Bar undersize across the whole lot; will not clean up at 12 mm",
        # A critical characteristic out of specification rejects the lot on its
        # own, whatever every other reading says. That is the rule this
        # inspection exists to show working.
        fail="critical",
        sentence="Second lot off the same delivery, measured undersize at both ends.",
    ),
    _InspectionSpec(
        tag=("QC", "PARTIAL"),
        item=("SF", "LEVER-BRASS"),
        warehouse="WH-ASM",
        stage=QcStage.IN_PROCESS,
        quantity="50",
        verdict=("44", "6"),
        rejection_reason="Six levers over length; the parting-off stop had crept",
        # Non-critical: the lot is not condemned, it is sorted. Which is why the
        # disposition splits rather than rejecting the lot outright.
        fail="ordinary",
        sentence="Batch check off the turning centre, sorted rather than condemned.",
    ),
    _InspectionSpec(
        tag=("QC", "PENDING"),
        item=("FG", "LEVER-AB"),
        warehouse="WH-FG",
        stage=QcStage.FINAL,
        quantity="5",
        sentence=(
            "Pre-despatch check on packed handle sets, raised this morning and "
            "waiting for the inspector. The stock is on QC hold until it is decided."
        ),
    ),
)


def _pass_value(parameter: QcTemplateParameter) -> Decimal:
    """A numeric reading comfortably inside the specification.

    The target first, because that is what the process is set to. Failing that
    the middle of the band, which is inside it by construction whatever the band
    is; failing that a value a whole unit clear of whichever single limit exists.
    """
    if parameter.target_value is not None:
        return Decimal(parameter.target_value)
    if parameter.min_value is not None and parameter.max_value is not None:
        return (Decimal(parameter.min_value) + Decimal(parameter.max_value)) / Decimal("2")
    if parameter.max_value is not None:
        return Decimal(parameter.max_value) - Decimal("1")
    if parameter.min_value is not None:
        return Decimal(parameter.min_value) + Decimal("1")
    return Decimal("1")


def _fail_value(parameter: QcTemplateParameter) -> Decimal | None:
    """A numeric reading outside it, or ``None`` if the parameter has no limits.

    An unbounded characteristic cannot be failed by a number, and inventing a
    failure for one would make the check sheet say something the specification
    does not.
    """
    if parameter.max_value is not None:
        return Decimal(parameter.max_value) + Decimal("1")
    if parameter.min_value is not None:
        return Decimal(parameter.min_value) - Decimal("1")
    return None


def _measurement(parameter: QcTemplateParameter, *, fail: bool) -> MeasurementIn | None:
    """One reading against one characteristic, judged by the service, not here.

    Returns ``None`` when the parameter cannot be failed as asked - the caller
    then leaves it to another parameter rather than recording a reading that says
    nothing.
    """
    kind = ParameterType(parameter.parameter_type)

    if kind is ParameterType.NUMERIC:
        value = _fail_value(parameter) if fail else _pass_value(parameter)
        if value is None:
            return None
        return MeasurementIn(
            parameter_id=parameter.id,
            measured_value_numeric=value.quantize(Decimal("0.000001")),
            remark="Out of specification" if fail else None,
        )

    if kind is ParameterType.BOOLEAN:
        return MeasurementIn(
            parameter_id=parameter.id,
            measured_value_bool=not fail,
            remark="Not acceptable" if fail else None,
        )

    if kind is ParameterType.SELECT:
        allowed = list(parameter.allowed_values or ())
        if fail or not allowed:
            return None
        return MeasurementIn(parameter_id=parameter.id, measured_value_text=allowed[0])

    return MeasurementIn(
        parameter_id=parameter.id,
        measured_value_text="Checked against the master sample and found conforming.",
    )


async def _record_check_sheet(
    ctx: DemoContext, inspection: QcInspection, spec: _InspectionSpec
) -> None:
    """Fill in the check sheet, failing at most one characteristic.

    Every mandatory reading has to be present before the lot can be decided, so
    the whole sheet is recorded in one request - which is also how an inspector
    keys it, off one printed sheet at the bench.
    """
    if inspection.template_id is None:
        return

    stmt = (
        select(QcTemplateParameter)
        .where(
            QcTemplateParameter.company_id == ctx.company_id,
            QcTemplateParameter.template_id == inspection.template_id,
        )
        .order_by(QcTemplateParameter.sequence)
    )
    parameters = list((await ctx.session.execute(stmt)).scalars())
    if not parameters:
        return

    failing: QcTemplateParameter | None = None
    if spec.fail == "critical":
        failing = next((p for p in parameters if p.is_critical), None)
    elif spec.fail == "ordinary":
        failing = next(
            (
                p
                for p in parameters
                if not p.is_critical and ParameterType(p.parameter_type) is ParameterType.NUMERIC
            ),
            None,
        )

    measurements: list[MeasurementIn] = []
    for parameter in parameters:
        reading = _measurement(parameter, fail=parameter.id == (failing.id if failing else None))
        if reading is None and parameter.is_mandatory:
            reading = _measurement(parameter, fail=False)
        if reading is not None:
            measurements.append(reading)

    if not measurements:
        return

    await quality_service.record_results(
        ctx.session,
        ctx.tenant,
        inspection.id,
        RecordResultsRequest(
            measurements=measurements,
            remarks="Readings taken at the bench against the printed check sheet.",
        ),
    )
    ctx.note(STAGE, created=1)


async def _seed_quality(ctx: DemoContext, look: _Lookup) -> None:
    for spec in INSPECTIONS:
        tag = ctx.code(*spec.tag)
        item = await look.item(*spec.item)
        warehouse_id = await look.warehouse(spec.warehouse)
        if item is None or warehouse_id is None:
            _skip(ctx, f"{tag} not raised: no {ctx.code(*spec.item)} or no {spec.warehouse}")
            continue

        existing = (
            (
                await ctx.session.execute(
                    select(QcInspection).where(
                        QcInspection.company_id == ctx.company_id,
                        QcInspection.reference_code == tag,
                    )
                )
            )
            .scalars()
            .first()
        )

        if existing is not None:
            inspection = existing
            _seen(ctx, "qc_inspection", tag, inspection.id, created=False)
        else:
            try:
                inspection = await quality_service.create_inspection(
                    ctx.session,
                    ctx.tenant,
                    InspectionCreate(
                        item_id=item.id,
                        warehouse_id=warehouse_id,
                        qc_stage=spec.stage,
                        inspected_quantity=Decimal(spec.quantity),
                        vendor_id=(
                            await look.vendor(*spec.vendor) if spec.vendor is not None else None
                        ),
                        reference_type="demo_operations",
                        reference_code=tag,
                        # The lot leaves the available pool while it is being
                        # judged. Nothing else in this file puts stock on QC hold,
                        # and a hold column that is always zero teaches nobody
                        # what the pool is for.
                        hold_stock=True,
                        reason="Lot pulled for inspection",
                        remarks=spec.sentence,
                    ),
                )
            except (BusinessRuleError, NotFoundError, ValidationError) as exc:
                # Almost always insufficient stock to hold: the item was never
                # received, or an earlier document has taken it.
                _skip(ctx, f"{tag} not raised: {exc}")
                continue
            _seen(ctx, "qc_inspection", tag, inspection.id, created=True)

        if spec.verdict is None:
            # Left PENDING on purpose - the inspection queue is a screen too, and
            # an empty one says nothing about how the module works.
            continue

        inspection_code = inspection.code
        try:
            await _decide(ctx, inspection, spec)
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            _skip(ctx, f"{tag} ({inspection_code}) was left undecided: {exc}")


async def _decide(ctx: DemoContext, inspection: QcInspection, spec: _InspectionSpec) -> None:
    """Record the readings and apply the verdict, if it has not been decided."""
    if InspectionStatus(inspection.status) not in OPEN_STATUSES:
        # Already judged. Deciding it again would either be refused by the state
        # machine or move the stock a second time, and both are worse than a
        # re-run that does nothing.
        return
    if spec.verdict is None:  # pragma: no cover - guarded by the caller
        return

    await _record_check_sheet(ctx, inspection, spec)

    accepted, rejected = spec.verdict
    await quality_service.decide_inspection(
        ctx.session,
        ctx.tenant,
        inspection.id,
        DecideRequest(
            accepted_quantity=Decimal(accepted),
            rejected_quantity=Decimal(rejected),
            reason="Verdict recorded by the quality engineer",
            rejection_reason=spec.rejection_reason,
            inspected_at=_at(_days_before(2), 10),
            # Closed in the same call: nothing is left on hold, so there is no
            # second decision owed to anybody.
            close=True,
        ),
    )
    ctx.note(STAGE, created=1)


# =============================================================================
# Sales, and the reservations confirming them takes
# =============================================================================
# Five orders, and the four states the brief asks for fall out of the stock
# position rather than being set: confirmation reserves what it finds and reports
# what it cannot, so an order is "fully allocated" because the goods were there
# and "partially allocated" because they were not. Nothing here assigns a status.
#
# That is also why this file makes no reservation of its own. Every hold in the
# demonstration database is taken by the document that needs it, through
# ``allocation.service`` - sales confirmation, production release, assembly
# release and job-work approval all call it - so the reservation list, the
# per-item availability and the ``reserved_quantity`` on every balance agree with
# each other by construction. A reservation written here would belong to nothing.
#
# The stock they are competing over is the 30 handle sets the completed
# production order received, less the 5 the final inspection is holding: 25 to
# promise, against 12 + 8 + 10 asked for. The third order is therefore short by
# design, and MRP picks that up below.


@dataclass(frozen=True, slots=True)
class _SalesLineSpec:
    item: tuple[str, ...]
    quantity: str
    note: str


@dataclass(frozen=True, slots=True)
class _SalesSpec:
    tag: tuple[str, ...]
    customer: tuple[str, ...]
    po_number: str
    order_offset: int
    required_offset: int
    #: Confirmed orders reserve; a draft is a quotation and holds nothing.
    confirm: bool
    sentence: str
    lines: tuple[_SalesLineSpec, ...]
    #: One to nine, and *not* the hundred-point scale production and allocation
    #: use: sales has its own, narrower one, checked by the schema and again by a
    #: CHECK constraint. Lower is more urgent on both, and confirmation copies
    #: this number onto every reservation it takes - which is how a rush order
    #: outranks a shop-floor hold on the same part.
    priority: int = 5
    is_urgent: bool = False
    transporter: tuple[str, ...] | None = None


SALES_ORDERS: tuple[_SalesSpec, ...] = (
    _SalesSpec(
        tag=("SO", "DEALER"),
        customer=("DLR", "VASTU"),
        po_number="VHM/PO/2214",
        order_offset=-9,
        required_offset=4,
        confirm=True,
        priority=4,
        sentence=(
            "Counter stock for the Relief Road shop. Covered in full from the "
            "finished-goods build, packed into three cartons and shipped."
        ),
        lines=(
            _SalesLineSpec(
                item=("FG", "LEVER-AB"),
                quantity="12",
                note="Antique brass, 52 mm rose. Dealer price list.",
            ),
        ),
    ),
    _SalesSpec(
        tag=("SO", "OEM"),
        customer=("CUST", "KALYAN"),
        po_number="KMF/SCH/8871",
        order_offset=-11,
        required_offset=2,
        confirm=True,
        priority=3,
        sentence=(
            "OEM schedule call-off, fitted into the customer's own wardrobe range "
            "and shipped unbranded. Delivered on the local carrier."
        ),
        lines=(
            _SalesLineSpec(
                item=("FG", "LEVER-AB"),
                quantity="8",
                note="Supplied loose, without the printed carton.",
            ),
        ),
    ),
    _SalesSpec(
        tag=("SO", "PARTIAL"),
        customer=("CUST", "SKYLINE"),
        po_number="SKY/WF/PO/3390",
        order_offset=-5,
        required_offset=12,
        confirm=True,
        priority=2,
        is_urgent=True,
        sentence=(
            "Site release for the Whitefield tower. The handles are covered out of "
            "what is left of the build; the locksets are not made yet, so the order "
            "sits part-allocated and waits for planning."
        ),
        lines=(
            _SalesLineSpec(
                item=("FG", "LEVER-AB"),
                quantity="10",
                note="Second floor, east wing.",
            ),
            _SalesLineSpec(
                item=("FG", "MORTISE"),
                quantity="6",
                note="Entrance doors. Nothing in stock; to be planned.",
            ),
        ),
    ),
    _SalesSpec(
        tag=("SO", "CONFIRMED"),
        customer=("DIST", "WEST"),
        po_number="WLD/MTH/0442",
        order_offset=-2,
        required_offset=27,
        confirm=True,
        priority=6,
        transporter=("TRN", "VRL"),
        sentence=(
            "The distributor's monthly consolidated order. Nothing in stock against "
            "it at all, which is precisely the demand MRP has to plan for."
        ),
        lines=(
            _SalesLineSpec(
                item=("FG", "LEVER-SS"),
                quantity="60",
                note="Satin stainless. Whole month's pull in one order.",
            ),
        ),
    ),
    _SalesSpec(
        tag=("SO", "DRAFT"),
        customer=("CUST", "ARCH"),
        po_number="AIP/QT/1177",
        order_offset=-1,
        required_offset=35,
        confirm=False,
        priority=7,
        sentence=(
            "Quotation against the Lower Parel fit-out, not yet committed to. A "
            "draft reserves nothing - holding stock for an order nobody has "
            "accepted is how a confirmed order ends up starved."
        ),
        lines=(
            _SalesLineSpec(
                item=("FG", "LEVER-AB"),
                quantity="25",
                note="Schedule A doors.",
            ),
            _SalesLineSpec(
                item=("FG", "DIGILOCK"),
                quantity="10",
                note="Apartment entrance doors; keypad and card.",
            ),
        ),
    ),
)


async def _sales_line(
    ctx: DemoContext, order_id: uuid.UUID, item_id: uuid.UUID
) -> SalesOrderLine | None:
    """The line of one order that carries one item.

    Packing and despatch both have to point back at the line they are filling -
    that link is what keeps the fulfilment arithmetic on the order honest - and
    the line number is assigned by this file, so it is found by item rather than
    by position.
    """
    stmt = (
        select(SalesOrderLine)
        .where(
            SalesOrderLine.company_id == ctx.company_id,
            SalesOrderLine.sales_order_id == order_id,
            SalesOrderLine.item_id == item_id,
        )
        .order_by(SalesOrderLine.line_number)
    )
    return (await ctx.session.execute(stmt)).scalars().first()


async def _seed_sales(ctx: DemoContext, look: _Lookup) -> None:
    for spec in SALES_ORDERS:
        tag = ctx.code(*spec.tag)
        customer_id = await look.customer(*spec.customer)
        warehouse_id = await look.warehouse("WH-FG")
        if customer_id is None or warehouse_id is None:
            _skip(ctx, f"{tag} not raised: no {ctx.code(*spec.customer)} or no WH-FG")
            continue

        lines: list[SalesOrderLineIn] = []
        for line_spec in spec.lines:
            item = await look.item(*line_spec.item)
            if item is None:
                _skip(ctx, f"{tag} line dropped: no {ctx.code(*line_spec.item)}")
                continue
            lines.append(
                SalesOrderLineIn(
                    line_number=(len(lines) + 1) * 10,
                    item_id=item.id,
                    quantity=Decimal(line_spec.quantity),
                    # No unit price: the sales module resolves it from the price
                    # list for this customer's channel and falls back to the item
                    # master. Stating one here would make the order agree with
                    # nothing.
                    required_date=_days_after(spec.required_offset),
                    notes=line_spec.note,
                )
            )
        if not lines:
            _skip(ctx, f"{tag} not raised: none of its items exist")
            continue

        existing = await _find_tagged(ctx, SalesOrder, tag)
        if existing is not None:
            order = existing
            _seen(ctx, "sales_order", tag, order.id, created=False)
        else:
            try:
                order, _ = await sales_service.create_order(
                    ctx.session,
                    ctx.tenant,
                    ctx.principal,
                    SalesOrderCreate(
                        customer_id=customer_id,
                        customer_po_number=spec.po_number,
                        order_date=_days_after(spec.order_offset),
                        required_date=_days_after(spec.required_offset),
                        promised_date=_days_after(spec.required_offset),
                        warehouse_id=warehouse_id,
                        priority=spec.priority,
                        is_urgent=spec.is_urgent,
                        transporter_id=(
                            await look.transporter(*spec.transporter)
                            if spec.transporter is not None
                            else None
                        ),
                        notes=_tagged(tag, spec.sentence),
                        lines=lines,
                    ),
                )
            except (BusinessRuleError, NotFoundError, ValidationError) as exc:
                _skip(ctx, f"{tag} not raised: {exc}")
                continue
            _seen(ctx, "sales_order", tag, order.id, created=True)

        if not spec.confirm or SalesOrderStatus(order.status) is not SalesOrderStatus.DRAFT:
            continue

        try:
            # Confirmation is the only moment stock is promised. It reserves what
            # it finds line by line and reports the shortfall; a partly covered
            # order is a success, not a failure (master rules section 72), so
            # nothing here treats a shortage as an error.
            await sales_service.confirm_order(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                order.id,
                reason="Customer order accepted and acknowledged",
            )
            ctx.note(STAGE, created=1)
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            # Credit headroom, most likely. The order stays a draft, which is the
            # correct outcome, and the next order still gets its chance.
            _skip(ctx, f"{tag} left in draft: {exc}")


# =============================================================================
# MRP
# =============================================================================
# One planning run over the order book the sales orders just created, so the run
# list, the requirement detail and the suggestion queue all have something on
# them - and so the shortage this demonstration builds on purpose has somewhere
# to surface. Sixty satin stainless handle sets and six locksets are ordered
# against nothing at all; the engine nets them, explodes what it has to make and
# raises the purchase and manufacture suggestions that cover them.
#
# The run executes inline. That is the service's decision, not this file's - it
# compares the demand line count against ``settings.mrp_sync_threshold_demand_lines``
# and only hands the large ones to Celery - and a demonstration order book is
# comfortably below the threshold, so no worker has to be running for this to
# produce a finished plan.

DEMO_RUN_NOTE = "Demonstration planning run over the confirmed order book."


async def _seed_mrp(ctx: DemoContext, look: _Lookup) -> None:
    existing = (
        (
            await ctx.session.execute(
                select(MrpRun).where(
                    MrpRun.company_id == ctx.company_id,
                    MrpRun.parameters.op("->>")("notes") == DEMO_RUN_NOTE,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        _seen(ctx, "mrp_run", ctx.code("MRP", "RUN"), existing.id, created=False)
        return

    # One manual line on top of the order book: a forecast the planner believes
    # in but nobody has ordered yet. It is the only kind of demand with no source
    # document, and the demand list should show that such a thing exists.
    manual: list[ManualDemandIn] = []
    forecast_item = await look.item("FG", "HINGE-SS")
    if forecast_item is not None:
        manual.append(
            ManualDemandIn(
                item_id=forecast_item.id,
                quantity=Decimal("400"),
                required_date=_days_after(45),
                warehouse_id=await look.warehouse("WH-FG"),
                notes="Forecast pull on butt hinges for the next distributor cycle.",
            )
        )

    try:
        run, queued = await mrp_service.start_run(
            ctx.session,
            ctx.tenant,
            MrpRunRequest(
                # A quarter. Long enough that the distributor's order and the
                # forecast line both fall inside it, short enough that the plan is
                # about this quarter's work rather than a year of speculation.
                horizon_days=90,
                respect_safety_stock=True,
                manual_demand=manual,
                notes=DEMO_RUN_NOTE,
            ),
        )
    except AppError as exc:
        # Three different application errors mean the same thing here - a run is
        # already in flight, the company's run budget is spent, or the order book
        # is larger than one run may plan - and all three cost the plan and
        # nothing else.
        _skip(ctx, f"no planning run: {exc}")
        return

    _seen(ctx, "mrp_run", ctx.code("MRP", "RUN"), run.id, created=True)
    if queued:  # pragma: no cover - only above the async threshold
        _skip(
            ctx,
            "the planning run was handed to Celery rather than executed inline, so its "
            "requirements and suggestions appear once a worker has picked it up",
        )


# =============================================================================
# Packing
# =============================================================================
# One shipment, boxed properly: two cartons of handle sets and a third holding
# four digital locks by serial number.
#
# The locks are on the packing order without a sales-order line behind them, which
# the module explicitly allows - a shipment routinely carries something the
# customer did not order on a line, a sample or a replacement - and here they are
# showroom samples for the dealer. They are also the only way a serialised unit
# gets into a carton in this database, which is what makes "which box is unit
# DL900-24A0119 in?" a question with an answer.
#
# Cartons are not free: sealing one issues the box from stock at the packing
# order's own warehouse. Finished goods are in WH-FG and the printed cartons live
# in the packing store, so a dozen are moved across first - the same staging move
# the assembly bench needs, for the same reason.

PACKING_TAG = ("PK", "DEALER")
#: Six sets to a carton, which is how this range is palletised, and roughly what
#: a packer can lift.
SETS_PER_CARTON = 6
#: Kilograms. A lever-on-rose set with its screws and spindle is about 1.6 kg,
#: the printed carton about 0.35 kg empty, a DL-900 about 2.2 kg.
SET_WEIGHT = Decimal("1.600")
CARTON_TARE = Decimal("0.350")
LOCK_WEIGHT = Decimal("2.200")


@dataclass(frozen=True, slots=True)
class _PackedOrder:
    order_id: uuid.UUID
    carton_ids: tuple[uuid.UUID, ...]
    sales_order_id: uuid.UUID


async def _cartons_of(ctx: DemoContext, order_id: uuid.UUID) -> list[Carton]:
    stmt = (
        select(Carton)
        .where(Carton.company_id == ctx.company_id, Carton.packing_order_id == order_id)
        .order_by(Carton.carton_number)
    )
    return list((await ctx.session.execute(stmt)).scalars())


async def _seed_packing(
    ctx: DemoContext, look: _Lookup, serials: Sequence[uuid.UUID]
) -> _PackedOrder | None:
    tag = ctx.code(*PACKING_TAG)
    sales_order_id = ctx.maybe_id("sales_order", ctx.code("SO", "DEALER"))
    customer_id = await look.customer("DLR", "VASTU")
    warehouse_id = await look.warehouse("WH-FG")
    handle = await look.item("FG", "LEVER-AB")
    lock = await look.item("FG", "DIGILOCK")
    carton_item = await look.item("PK", "BOX")

    if sales_order_id is None or customer_id is None or warehouse_id is None or handle is None:
        _skip(ctx, f"{tag} not raised: the dealer order, the customer or the store is missing")
        return None

    sample_serials = tuple(serials[:4])
    packs_locks = lock is not None and len(sample_serials) == 4

    existing = await _find_tagged(ctx, PackingOrder, tag)
    if existing is not None:
        order = existing
        _seen(ctx, "packing_order", tag, order.id, created=False)
    else:
        handle_line = await _sales_line(ctx, sales_order_id, handle.id)
        lines = [
            PackingOrderLineIn(
                item_id=handle.id,
                quantity_to_pack=Decimal("12"),
                uom_id=handle.base_uom_id,
                sales_order_line_id=handle_line.id if handle_line is not None else None,
            )
        ]
        if packs_locks and lock is not None:
            lines.append(
                PackingOrderLineIn(
                    item_id=lock.id,
                    quantity_to_pack=Decimal("4"),
                    uom_id=lock.base_uom_id,
                    # No order line: these are samples the dealer did not order.
                    sales_order_line_id=None,
                )
            )
        try:
            order, _ = await packing_service.create_order(
                ctx.session,
                ctx.tenant,
                PackingOrderCreate(
                    customer_id=customer_id,
                    warehouse_id=warehouse_id,
                    planned_date=_days_before(3),
                    sales_order_id=sales_order_id,
                    notes=_tagged(
                        tag,
                        "Dealer shipment: twelve handle sets in two cartons, plus four "
                        "DL-900 samples in a third.",
                    ),
                    lines=lines,
                ),
            )
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            _skip(ctx, f"{tag} not raised: {exc}")
            return None
        _seen(ctx, "packing_order", tag, order.id, created=True)

    order_code = order.code
    try:
        await _pack_the_order(
            ctx,
            order,
            carton_item=carton_item,
            serials=sample_serials if packs_locks else (),
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        _skip(ctx, f"{tag} ({order_code}) was not packed: {exc}")
        return None

    if PackingOrderStatus(order.status) not in (
        PackingOrderStatus.PACKED,
        PackingOrderStatus.HANDED_OVER,
    ):
        _skip(
            ctx,
            f"{tag} ({order_code}) is {order.status}: only a packed order can be loaded "
            "onto a consignment, so the despatch that would carry it is not raised",
        )
        return None

    cartons = await _cartons_of(ctx, order.id)
    return _PackedOrder(
        order_id=order.id,
        carton_ids=tuple(carton.id for carton in cartons),
        sales_order_id=sales_order_id,
    )


async def _pack_the_order(
    ctx: DemoContext,
    order: PackingOrder,
    *,
    carton_item: _ItemRef | None,
    serials: Sequence[uuid.UUID],
) -> None:
    """Open the bench, fill three boxes, seal them and freeze the totals."""
    if PackingOrderStatus(order.status) is PackingOrderStatus.DRAFT:
        await packing_service.start_packing(
            ctx.session, ctx.tenant, order.id, reason="Bench started on the dealer shipment"
        )
        ctx.note(STAGE, created=1)

    if PackingOrderStatus(order.status) is not PackingOrderStatus.IN_PROGRESS:
        return

    if carton_item is None:
        _skip(ctx, f"{order.code} has no packing material item, so no carton can be sealed")
        return

    # Three boxes have to be at the despatch bench before any of them can be
    # sealed: the seal issues the carton from the packing order's own warehouse.
    staged = await _stage_to(
        ctx,
        item_id=carton_item.id,
        warehouse_id=order.warehouse_id,
        required=Decimal("3"),
        reference_code=order.code,
        reason=f"Cartons staged at the despatch bench for {order.code}",
    )
    if staged < Decimal("3"):
        _skip(
            ctx,
            f"{order.code} has only {staged} carton(s) at the despatch bench; sealing "
            "consumes one box each and the shipment cannot be closed without them",
        )
        return

    if not await _cartons_of(ctx, order.id):
        await _fill_cartons(ctx, order, material=carton_item, serials=serials)

    for carton in await _cartons_of(ctx, order.id):
        if carton.is_sealed:
            continue
        await packing_service.seal_carton(
            ctx.session,
            ctx.tenant,
            order.id,
            carton.id,
            SealCartonRequest(
                reason="Carton sealed, strapped and weighed",
                occurred_at=_at(_days_before(3), 16),
            ),
        )
        ctx.note(STAGE, created=1)

    await packing_service.complete_packing(
        ctx.session,
        ctx.tenant,
        order.id,
        PackingCompleteRequest(reason="Shipment packed and weighed; ready for the carrier"),
    )
    ctx.note(STAGE, created=1)


async def _fill_cartons(
    ctx: DemoContext,
    order: PackingOrder,
    *,
    material: _ItemRef,
    serials: Sequence[uuid.UUID],
) -> None:
    """Two cartons of six sets, then the samples box if there are serials for it.

    Contents are given as each carton is opened rather than added afterwards,
    because a box is packed in one go at a bench, and because the packed
    quantities on the order's lines are maintained from these rows either way.
    """
    for _ in range(2):
        await packing_service.add_carton(
            ctx.session,
            ctx.tenant,
            order.id,
            CartonCreate(
                packing_material_item_id=material.id,
                packing_material_quantity=Decimal("1"),
                carton_type=CartonType.CARTON,
                tare_weight_kg=CARTON_TARE,
                length_cm=Decimal("48"),
                width_cm=Decimal("32"),
                height_cm=Decimal("22"),
                contents=[
                    CartonContentIn(
                        line_number=1,
                        quantity=Decimal(SETS_PER_CARTON),
                        net_weight_kg=SET_WEIGHT * Decimal(SETS_PER_CARTON),
                    )
                ],
            ),
        )
        ctx.note(STAGE, created=1)

    if not serials:
        return

    await packing_service.add_carton(
        ctx.session,
        ctx.tenant,
        order.id,
        CartonCreate(
            packing_material_item_id=material.id,
            packing_material_quantity=Decimal("1"),
            carton_type=CartonType.BOX,
            tare_weight_kg=CARTON_TARE,
            length_cm=Decimal("40"),
            width_cm=Decimal("30"),
            height_cm=Decimal("18"),
            contents=[
                CartonContentIn(
                    line_number=2,
                    quantity=Decimal(len(serials)),
                    net_weight_kg=LOCK_WEIGHT * Decimal(len(serials)),
                    # A serialised item names every unit it packed: the count has
                    # to equal the quantity, because a serial is one object.
                    serial_ids=list(serials),
                )
            ],
        ),
    )
    ctx.note(STAGE, created=1)


# =============================================================================
# Despatch
# =============================================================================
# Three consignments, one at each of the three states the tracking board has to
# be able to show: one that has left with a carrier tracking number against it,
# one that has arrived and been signed for, and one still being loaded.
#
# The first two draw on sales orders that were fully allocated, and that is not a
# presentational choice: the sales lifecycle has no CONFIRMED -> DISPATCHED edge,
# so an order that was only partly covered cannot ship until the rest of it is.
# Trying it would be refused by the state machine, which is the correct answer -
# a part-allocated order is a promise nobody has finished making.
#
# The samples travel on the first consignment on a line of their own with no
# order line behind it. That is also the only way they *can* travel: a
# reservation cannot name a serial number, so the despatch module refuses to
# consume one for a serialised line and posts the units directly instead - one
# ledger row per unit, each naming the object that left.


async def _seed_dispatches(
    ctx: DemoContext, look: _Lookup, packed: _PackedOrder | None, serials: Sequence[uuid.UUID]
) -> None:
    handle = await look.item("FG", "LEVER-AB")
    warehouse_id = await look.warehouse("WH-FG")
    if handle is None or warehouse_id is None:
        _skip(ctx, "no handle set or no finished-goods store, so no consignments")
        return

    await _dispatch_shipped(
        ctx, look, packed, handle=handle, warehouse_id=warehouse_id, serials=serials
    )
    await _dispatch_delivered(ctx, look, handle=handle, warehouse_id=warehouse_id)
    await _dispatch_being_loaded(ctx, look, warehouse_id=warehouse_id)


async def _dispatch_shipped(
    ctx: DemoContext,
    look: _Lookup,
    packed: _PackedOrder | None,
    *,
    handle: _ItemRef,
    warehouse_id: uuid.UUID,
    serials: Sequence[uuid.UUID],
) -> None:
    """The dealer's boxes, on the road with a tracking number against them."""
    tag = ctx.code("DSP", "SHIPPED")
    if packed is None:
        _skip(ctx, f"{tag} not raised: the dealer shipment was never packed")
        return

    existing = await _find_tagged(ctx, Dispatch, tag)
    if existing is None:
        handle_line = await _sales_line(ctx, packed.sales_order_id, handle.id)
        lock = await look.item("FG", "DIGILOCK")
        cartons = list(packed.carton_ids)

        lines = [
            DispatchLineIn(
                item_id=handle.id,
                quantity=Decimal("12"),
                sales_order_line_id=handle_line.id if handle_line is not None else None,
                carton_ids=cartons[:2],
                notes="Two cartons, six sets each.",
            )
        ]
        if lock is not None and len(serials) >= 4 and len(cartons) >= 3:
            lines.append(
                DispatchLineIn(
                    item_id=lock.id,
                    quantity=Decimal("4"),
                    serial_ids=list(serials[:4]),
                    carton_ids=cartons[2:3],
                    notes="Showroom samples; each unit identified by serial number.",
                )
            )

        try:
            detail = await dispatch_service.create_dispatch(
                ctx.session,
                ctx.tenant,
                DispatchCreate(
                    dispatch_type=DispatchType.SALES,
                    warehouse_id=warehouse_id,
                    dispatch_date=_days_before(2),
                    expected_delivery_date=_days_after(1),
                    customer_id=await look.customer("DLR", "VASTU"),
                    sales_order_id=packed.sales_order_id,
                    packing_order_id=packed.order_id,
                    transporter_id=await look.transporter("TRN", "VRL"),
                    tracking_id=DISPATCH_TRACKING_ID,
                    lr_number=DISPATCH_LR_NUMBER,
                    vehicle_number="GJ 03 AT 8802",
                    driver_name="Mahesh Solanki",
                    driver_mobile="+91 98240 11227",
                    total_boxes=len(cartons),
                    # Twelve sets at 1.6 kg, four locks at 2.2, three empty
                    # cartons at 0.35. A weight nobody added up is a weight the
                    # freight bill will disagree with.
                    total_weight_kg=Decimal("29.05"),
                    freight_amount=Decimal("1450.00"),
                    freight_paid_by=FreightPaidBy.CONSIGNOR,
                    notes=_tagged(
                        tag,
                        "Dealer shipment on VRL. Tracking number recorded, so the "
                        "carrier lookup on the tracking board has something to open.",
                    ),
                    lines=lines,
                ),
            )
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            _skip(ctx, f"{tag} not raised: {exc}")
            return
        dispatch_id = _seen(ctx, "dispatch", tag, detail.id, created=True)
    else:
        dispatch_id = _seen(ctx, "dispatch", tag, existing.id, created=False)

    await _advance_dispatch(
        ctx,
        dispatch_id,
        tag=tag,
        target=DispatchStatus.DISPATCHED,
        location="Rajkot - VRL hub",
    )


async def _dispatch_delivered(
    ctx: DemoContext, look: _Lookup, *, handle: _ItemRef, warehouse_id: uuid.UUID
) -> None:
    """The OEM's consignment, arrived and signed for."""
    tag = ctx.code("DSP", "DELIVERED")
    sales_order_id = ctx.maybe_id("sales_order", ctx.code("SO", "OEM"))
    if sales_order_id is None:
        _skip(ctx, f"{tag} not raised: the OEM order does not exist")
        return

    existing = await _find_tagged(ctx, Dispatch, tag)
    if existing is None:
        line = await _sales_line(ctx, sales_order_id, handle.id)
        try:
            detail = await dispatch_service.create_dispatch(
                ctx.session,
                ctx.tenant,
                DispatchCreate(
                    dispatch_type=DispatchType.SALES,
                    warehouse_id=warehouse_id,
                    dispatch_date=_days_before(7),
                    expected_delivery_date=_days_before(3),
                    customer_id=await look.customer("CUST", "KALYAN"),
                    sales_order_id=sales_order_id,
                    transporter_id=await look.transporter("TRN", "GATI"),
                    tracking_id=DELIVERED_TRACKING_ID,
                    lr_number=DELIVERED_LR_NUMBER,
                    vehicle_number="TN 22 BX 5514",
                    total_boxes=2,
                    # Eight sets at 1.6 kg, bulk packed in two of the customer's
                    # own returnable crates rather than printed cartons.
                    total_weight_kg=Decimal("12.80"),
                    freight_amount=Decimal("980.00"),
                    freight_paid_by=FreightPaidBy.CONSIGNEE,
                    notes=_tagged(
                        tag,
                        "Schedule call-off to Ambattur, shipped loose and signed for a "
                        "day inside the promised date.",
                    ),
                    lines=[
                        DispatchLineIn(
                            item_id=handle.id,
                            quantity=Decimal("8"),
                            sales_order_line_id=line.id if line is not None else None,
                            notes="Bulk packed to the customer's own standard.",
                        )
                    ],
                ),
            )
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            _skip(ctx, f"{tag} not raised: {exc}")
            return
        dispatch_id = _seen(ctx, "dispatch", tag, detail.id, created=True)
    else:
        dispatch_id = _seen(ctx, "dispatch", tag, existing.id, created=False)

    await _advance_dispatch(
        ctx,
        dispatch_id,
        tag=tag,
        target=DispatchStatus.DELIVERED,
        location="Chennai - Ambattur",
    )


async def _dispatch_being_loaded(
    ctx: DemoContext, look: _Lookup, *, warehouse_id: uuid.UUID
) -> None:
    """The distributor's consignment, still on the loading plan.

    Left in ``NOT_DISPATCHED`` on purpose. Nothing has moved and nothing is
    promised: this is the plan a despatch clerk works from in the morning, and a
    tracking board with no such row on it hides half of what the screen is for.
    """
    tag = ctx.code("DSP", "LOADING")
    sales_order_id = ctx.maybe_id("sales_order", ctx.code("SO", "CONFIRMED"))
    lever_ss = await look.item("FG", "LEVER-SS")
    if sales_order_id is None or lever_ss is None:
        _skip(ctx, f"{tag} not raised: the distributor order or its item is missing")
        return

    found = await _find_tagged(ctx, Dispatch, tag)
    if found is not None:
        _seen(ctx, "dispatch", tag, found.id, created=False)
        return

    line = await _sales_line(ctx, sales_order_id, lever_ss.id)
    try:
        detail = await dispatch_service.create_dispatch(
            ctx.session,
            ctx.tenant,
            DispatchCreate(
                dispatch_type=DispatchType.SALES,
                warehouse_id=warehouse_id,
                dispatch_date=_days_after(3),
                expected_delivery_date=_days_after(7),
                customer_id=await look.customer("DIST", "WEST"),
                sales_order_id=sales_order_id,
                transporter_id=await look.transporter("TRN", "VRL"),
                total_boxes=4,
                freight_paid_by=FreightPaidBy.CONSIGNOR,
                notes=_tagged(
                    tag,
                    "First tranche of the distributor's monthly order. Being made up "
                    "at the bench; no lorry booked and no LR number yet.",
                ),
                lines=[
                    DispatchLineIn(
                        item_id=lever_ss.id,
                        quantity=Decimal("20"),
                        sales_order_line_id=line.id if line is not None else None,
                        notes="Part shipment against the month's order.",
                    )
                ],
            ),
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        _skip(ctx, f"{tag} not raised: {exc}")
        return
    _seen(ctx, "dispatch", tag, detail.id, created=True)


async def _advance_dispatch(
    ctx: DemoContext,
    dispatch_id: uuid.UUID,
    *,
    tag: str,
    target: DispatchStatus,
    location: str,
) -> None:
    """Walk a consignment to the status it is meant to be sitting at.

    Each hop is a separate service call because each is a separate real event
    with its own tracking row: loaded, gone, moving, arrived. Guarded by the
    status the consignment is actually in, so a re-run does nothing and an
    interrupted run resumes.
    """
    try:
        dispatch = (
            (await ctx.session.execute(select(Dispatch).where(Dispatch.id == dispatch_id)))
            .scalars()
            .first()
        )
        if dispatch is None:  # pragma: no cover - it was just created
            return

        if DispatchStatus(dispatch.status) is DispatchStatus.NOT_DISPATCHED:
            await dispatch_service.mark_ready(
                ctx.session,
                ctx.tenant,
                dispatch_id,
                reason="Loaded, checked against the packing list and papers signed",
            )
            ctx.note(STAGE, created=1)

        if DispatchStatus(dispatch.status) is DispatchStatus.READY_FOR_DISPATCH:
            # The only call here that moves stock: it consumes the sales order's
            # reservations, posts a DISPATCH movement per line - per *unit* for
            # the serialised one - and walks the sales order forward with it.
            await dispatch_service.dispatch_consignment(
                ctx.session,
                ctx.tenant,
                dispatch_id,
                DispatchRequest(
                    reason="Consignment handed to the carrier",
                    location=location,
                ),
            )
            ctx.note(STAGE, created=1)

        if target is DispatchStatus.DISPATCHED:
            return

        if DispatchStatus(dispatch.status) is DispatchStatus.DISPATCHED:
            await dispatch_service.mark_in_transit(
                ctx.session,
                ctx.tenant,
                dispatch_id,
                InTransitRequest(
                    reason="Carrier scan: consignment left the hub",
                    location=location,
                    event_time=_at(_days_before(6), 20),
                    source=TrackingEventSource.COURIER_API,
                ),
            )
            ctx.note(STAGE, created=1)

        if DispatchStatus(dispatch.status) is DispatchStatus.IN_TRANSIT:
            await dispatch_service.confirm_delivery(
                ctx.session,
                ctx.tenant,
                dispatch_id,
                DeliveryConfirmation(
                    # A day inside the promised date, so no delay reason is owed.
                    # The module demands one for a late arrival, and an on-time
                    # delivery is the case worth showing first.
                    actual_delivery_date=_days_before(4),
                    delivery_note="Received at the stores gate; challan signed and returned.",
                    location=location,
                    event_time=_at(_days_before(4), 11),
                    source=TrackingEventSource.MANUAL,
                ),
            )
            ctx.note(STAGE, created=1)
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        _skip(ctx, f"{tag} stopped short of {target}: {exc}")


# =============================================================================
# Entry point
# =============================================================================


async def seed(ctx: DemoContext) -> None:
    """Run the shop floor and the despatch bay, in dependency order.

    The order is not the order the screens are laid out in; it is the order the
    goods have to physically exist in. Levers before the rose plates that share
    the same store, both before the handle sets that consume them, all of it
    before the sales orders that promise the result, and the planning run after
    the order book exists so it has something to plan against.
    """
    look = _Lookup(ctx)

    # Without a GST state on the company, every taxed sales order below is
    # refused at validation. See _ensure_seller_state.
    await _ensure_seller_state(ctx)

    # --- supply -------------------------------------------------------------
    await _seed_jobs(ctx, look, LEVER_SUPPLY)
    await _seed_assembly(ctx, look)
    serials = await _seed_serial_stock(ctx, look)

    # --- the floor ----------------------------------------------------------
    await _seed_jobs(ctx, look, HANDLE_JOBS)
    await _seed_job_work(ctx, look)

    # Quality runs after the floor and before sales, and the order matters: an
    # inspection holds its lot, so the five handle sets on final inspection have
    # to leave the available pool *before* the sales orders are confirmed. Doing
    # it the other way round would promise stock that is standing on a QC bench.
    await _seed_quality(ctx, look)

    # --- demand -------------------------------------------------------------
    await _seed_sales(ctx, look)
    await _seed_mrp(ctx, look)

    # --- out of the door ----------------------------------------------------
    packed = await _seed_packing(ctx, look, serials)
    await _seed_dispatches(ctx, look, packed, serials)
