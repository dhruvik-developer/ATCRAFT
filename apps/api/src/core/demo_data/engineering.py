"""Engineering demonstration data: how the products are actually made.

Fills in the middle of the ERP — the part between "we have items" and "we can
run production":

* **work centres and machines** with capacity and cost rates, so the WIP board
  has cells to schedule against and the costed routing produces real money
  rather than zeros;
* **routings** with operations in sequence — one per manufactured item, approved
  so production can release against them, plus a draft revision so the routing
  screen shows a process under review beside a live one;
* **bills of material**, deliberately multi-level: a lever handle set assembled
  from a machined lever and a polished rose plate, each of which is a
  manufactured sub-assembly with a structure of its own. The same M4 screw is
  consumed on all three branches, because common-part consolidation is one of
  the hard problems this ERP exists to solve and an explosion with no shared
  part demonstrates nothing;
* **QC templates** with dimensional, visual and finish characteristics and three
  different sampling plans, so the quality stage has something to inspect
  against and the template screen is not empty.

Everything is created through the module services, so the state machines, the
version numbering, the cost roll-up and the audit trail all run exactly as they
do for a user (see the package docstring).

Resolving the items
-------------------
This stage needs specific *parts* — a finished handle, the lever and rose
sub-assemblies, rod, a screw, a carton. The masters stage owns their codes, so
each part is looked for by the code that stage registers and, failing that, by
capability flag and keyword against the demonstration items already in the
database, deterministically (lowest code wins). The fallback matters: a rename in
the masters stage should cost this one a slightly different-looking BOM, not a
traceback. A part that cannot be matched at all is recorded in ``ctx.skipped``
and the structure that needed it degrades — a flatter BOM rather than no BOM.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import utcnow
from src.core.demo_data import DEMO_PREFIX, DemoContext, find_by_code
from src.core.errors import BusinessRuleError, DuplicateError
from src.modules.bom import service as bom_service
from src.modules.bom.models import Bom, BomStatus, BomType
from src.modules.bom.schemas import BomCreate, BomLineIn, BomOperationIn
from src.modules.products.models import Item, ItemCategory
from src.modules.quality import service as quality_service
from src.modules.quality.models import ParameterType, QcStage, QcTemplate, SamplingPlan
from src.modules.quality.schemas import TemplateCreate, TemplateParameterIn
from src.modules.routing.models import Machine, MachineStatus, Routing, RoutingStatus, WorkCenter
from src.modules.routing.schemas import (
    CopyAsNewVersionRequest,
    MachineCreate,
    MachineStatusChange,
    RoutingCreate,
    RoutingOperationIn,
    WorkCenterCreate,
    WorkCenterUpdate,
)
from src.modules.routing.service import MachineService, RoutingService, WorkCenterService

STAGE = "engineering"


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------
# Every date here is derived from one anchor, so two runs a week apart produce
# the same *shape* of history — a process released four months ago, a revision
# that took over five months ago, a service already overdue — without any of it
# drifting into the future or becoming absurdly stale.
#
# A function rather than a module constant: a constant is evaluated at import
# time, which is a different moment from the run under a frozen clock, and the
# whole point of routing time through ``src.core.clock`` is that it can be
# frozen.


def _anchor() -> date:
    """The date every other date here is measured from."""
    return utcnow().date()


def _days_before(days: int) -> date:
    return _anchor() - timedelta(days=days)


def _days_after(days: int) -> date:
    return _anchor() + timedelta(days=days)


# -----------------------------------------------------------------------------
# Work centres and machines
# -----------------------------------------------------------------------------
# Four cells, in the order a handle actually travels through the shop: bar goes
# to the machine shop, comes back to be polished, is assembled by hand, and is
# packed. Rates are blended labour + machine + overhead per hour in INR, at the
# level a small architectural-hardware works in India would recognise: a CNC
# turning cell costs three times what a packing bench costs, and that ratio is
# what makes the costed routing worth looking at.


@dataclass(frozen=True)
class _Centre:
    suffix: str
    name: str
    description: str
    #: Pieces per hour the cell can put through when it is running.
    capacity_per_hour: str
    cost_per_hour: str
    #: Charged once per changeover, on top of the setup minutes.
    setup_cost: str
    #: The cost pool the cell's hours are charged to - a suffix of
    #: :data:`src.core.demo_data.masters.COST_CENTRES`.
    cost_centre: tuple[str, ...]


WORK_CENTRES: tuple[_Centre, ...] = (
    _Centre(
        suffix="WC-MACHINE",
        name="Machine Shop",
        description="CNC turning, drilling and tapping of brass and stainless bar.",
        capacity_per_hour="55",
        cost_per_hour="850.0000",
        setup_cost="350.0000",
        cost_centre=("PROD", "MACH"),
    ),
    _Centre(
        suffix="WC-POLISH",
        name="Polishing Line",
        description="Linishing, mopping and buffing to satin or mirror finish.",
        capacity_per_hour="120",
        cost_per_hour="420.0000",
        setup_cost="150.0000",
        cost_centre=("PROD", "BUFF"),
    ),
    _Centre(
        suffix="WC-ASSEMBLY",
        name="Assembly Bench",
        description="Hand assembly of levers, roses and springs, with function test.",
        capacity_per_hour="90",
        cost_per_hour="380.0000",
        setup_cost="100.0000",
        cost_centre=("PROD", "ASSY"),
    ),
    _Centre(
        suffix="WC-PACKING",
        name="Packing Bench",
        description="Cartoning, labelling and strapping for dispatch.",
        capacity_per_hour="200",
        cost_per_hour="260.0000",
        setup_cost="60.0000",
        cost_centre=("PROD", "PACK"),
    ),
)

#: Rates by centre suffix, so a BOM operation can be costed at the rate the
#: routing charges without a second round trip to read the centre back.
_CENTRE_RATES: dict[str, str] = {centre.suffix: centre.cost_per_hour for centre in WORK_CENTRES}


@dataclass(frozen=True)
class _MachineSpec:
    suffix: str
    name: str
    #: A machine is only valid inside its own work centre — the routing module
    #: enforces it with a composite foreign key — so this is the centre it
    #: belongs to and the only one whose operations may name it.
    centre: str
    machine_type: str
    capacity_per_hour: str
    cost_per_hour: str
    #: Days before the anchor for the last service, and *after* it for the next.
    #: A negative "next" is a machine already overdue.
    last_service_days_ago: int
    next_service_days_ahead: int


MACHINES: tuple[_MachineSpec, ...] = (
    _MachineSpec(
        suffix="MC-LATHE-01",
        name="CNC Turning Centre",
        centre="WC-MACHINE",
        machine_type="CNC_LATHE",
        capacity_per_hour="55",
        cost_per_hour="640.0000",
        last_service_days_ago=45,
        next_service_days_ahead=45,
    ),
    _MachineSpec(
        suffix="MC-DRILL-01",
        name="Pillar Drill and Tapping Head",
        centre="WC-MACHINE",
        machine_type="DRILL",
        capacity_per_hour="140",
        cost_per_hour="180.0000",
        # Overdue by five days on a ninety-day cycle, and still AVAILABLE: the
        # maintenance board filters on "due before today", and a board with
        # nothing on it teaches an evaluator nothing about what it is for.
        last_service_days_ago=95,
        next_service_days_ahead=-5,
    ),
    _MachineSpec(
        suffix="MC-BUFF-01",
        name="Twin-spindle Buffing Machine",
        centre="WC-POLISH",
        machine_type="BUFFING",
        capacity_per_hour="120",
        cost_per_hour="220.0000",
        last_service_days_ago=75,
        next_service_days_ahead=15,
    ),
    _MachineSpec(
        suffix="MC-STRAP-01",
        name="Carton Strapping Machine",
        centre="WC-PACKING",
        machine_type="PACKING",
        capacity_per_hour="300",
        cost_per_hour="90.0000",
        last_service_days_ago=30,
        next_service_days_ahead=60,
    ),
)

#: The buffing machine is taken off line after the routings are approved, so the
#: machine list shows a cell that is down without making the routing that names
#: it invalid — the routing module allows exactly that ("a machine down for
#: maintenance today does not make tomorrow's routing wrong"), and a machine
#: sitting in a non-default state is the only way to prove it.
MAINTENANCE_MACHINE = "MC-BUFF-01"

# The assembly bench deliberately has no machine: it is bench work, and a work
# centre whose capacity is people rather than equipment is the normal case in
# this trade. It also keeps the machine grid honest about which cells are
# equipment-constrained.


# -----------------------------------------------------------------------------
# Process steps
# -----------------------------------------------------------------------------
# One table per manufactured item, read twice: once to write the routing
# operations, once to write the BOM's costed operation snapshot. A BOM stores its
# own times and rates so an approved structure's cost does not move when a
# work-centre rate changes, but the two must *start* identical, and two
# hand-maintained copies of the same steps would not stay that way for a week.
#
# The split matters and is the point of the whole file: machining lives on the
# sub-assemblies, so the finished handle's own process is two steps — put it
# together, put it in a box.


@dataclass(frozen=True)
class _Step:
    sequence: int
    code: str
    name: str
    centre: str
    machine: str | None
    setup_minutes: str
    run_minutes: str
    #: Waiting in front of the cell, and travelling to the next one. Elapsed but
    #: not charged — the routing module costs setup and run only.
    queue_minutes: str
    move_minutes: str
    qc_required: bool = False


#: The machined brass lever. Times are per lever, not per handle set.
LEVER_STEPS: tuple[_Step, ...] = (
    _Step(
        sequence=10,
        code="TURN",
        name="Turn lever from 12 mm brass bar",
        centre="WC-MACHINE",
        machine="MC-LATHE-01",
        setup_minutes="45.0000",
        run_minutes="1.6000",
        queue_minutes="30.0000",
        move_minutes="10.0000",
    ),
    _Step(
        sequence=20,
        code="DRILL",
        name="Drill and tap the M4 grub screw seat",
        centre="WC-MACHINE",
        machine="MC-DRILL-01",
        setup_minutes="15.0000",
        run_minutes="0.7000",
        queue_minutes="20.0000",
        move_minutes="10.0000",
    ),
    _Step(
        sequence=30,
        code="POLISH",
        name="Linish, mop and lacquer to satin finish",
        centre="WC-POLISH",
        machine="MC-BUFF-01",
        setup_minutes="20.0000",
        run_minutes="1.3000",
        # An hour of queue in front of polishing is real: the line runs in
        # batches and parts wait for one. It is the largest single contributor
        # to the lead time this routing reports, which is why it is here.
        queue_minutes="60.0000",
        move_minutes="15.0000",
        qc_required=True,
    ),
)

#: The stainless rose plate.
ROSE_STEPS: tuple[_Step, ...] = (
    _Step(
        sequence=10,
        code="TURN",
        name="Part off and face the 52 mm rose plate",
        centre="WC-MACHINE",
        machine="MC-LATHE-01",
        setup_minutes="30.0000",
        run_minutes="0.9000",
        queue_minutes="30.0000",
        move_minutes="10.0000",
    ),
    _Step(
        sequence=20,
        code="POLISH",
        name="Mop the face to a satin stainless finish",
        centre="WC-POLISH",
        machine="MC-BUFF-01",
        setup_minutes="15.0000",
        run_minutes="0.8000",
        queue_minutes="45.0000",
        move_minutes="10.0000",
        qc_required=True,
    ),
)

#: The finished set: everything above arrives already machined and polished.
HANDLE_STEPS: tuple[_Step, ...] = (
    _Step(
        sequence=10,
        code="ASSY",
        name="Fit levers, roses, springs and spindle; function test",
        centre="WC-ASSEMBLY",
        machine=None,
        setup_minutes="10.0000",
        run_minutes="4.5000",
        queue_minutes="30.0000",
        move_minutes="10.0000",
        qc_required=True,
    ),
    _Step(
        sequence=20,
        code="PACK",
        name="Pack the pair and the screw pack into a printed carton",
        centre="WC-PACKING",
        machine="MC-STRAP-01",
        setup_minutes="5.0000",
        run_minutes="1.8000",
        queue_minutes="15.0000",
        move_minutes="5.0000",
    ),
)

#: What the handle line did before the lever and rose cells existed: every step
#: on one level. Carried only as the costed operation set of the superseded BOM
#: revision, never as a live routing — it is history, and it is the reason that
#: revision's cost looks different from the current one's.
LEGACY_HANDLE_STEPS: tuple[_Step, ...] = (
    *LEVER_STEPS,
    *(replace(step, sequence=step.sequence + 30) for step in HANDLE_STEPS),
)

# Nothing here is outsourced. Plating genuinely is, in this trade, but a job-work
# operation names a vendor and vendors belong to the procurement stage, which
# runs after this one. Leaving the flag off is better than releasing a routing
# with an outsourced step and nobody to send the work to.


# -----------------------------------------------------------------------------
# Item resolution
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Parts:
    """The parts this stage builds structures out of. ``None`` means unmatched."""

    handle: Item | None
    lever: Item | None
    rose: Item | None
    brass_rod: Item | None
    steel_rod: Item | None
    screw: Item | None
    spring: Item | None
    carton: Item | None
    lacquer: Item | None


def _fits(item: Item, claimed: set[uuid.UUID], manufactured: bool, component: bool) -> bool:
    """Whether this row can play the part.

    Capability flags are checked here rather than left to the service, so a
    near-miss — a finished handle where a *component* was wanted — is filtered
    out before it reaches a BOM line the service would then reject.
    """
    if item.id in claimed:
        return False
    if manufactured and not item.can_manufacture:
        return False
    return not (component and not item.can_be_bom_component)


def _claim(
    pool: list[Item],
    claimed: set[uuid.UUID],
    *,
    codes: tuple[str, ...] = (),
    keywords: tuple[str, ...] = (),
    manufactured: bool = False,
    component: bool = False,
) -> Item | None:
    """Find the part: by the code the masters stage registers, else by keyword.

    Codes come first because they are exact. Keywords are the fallback for a
    database whose masters stage named things differently, and are tried in
    priority order against a pool ordered by code, so the answer is the same on
    every run against the same data.
    """
    for code in codes:
        for item in pool:
            if item.code == code and _fits(item, claimed, manufactured, component):
                claimed.add(item.id)
                return item
    for keyword in keywords:
        for item in pool:
            if keyword in f"{item.code} {item.name}".upper() and _fits(
                item, claimed, manufactured, component
            ):
                claimed.add(item.id)
                return item
    return None


async def _resolve_parts(ctx: DemoContext) -> _Parts:
    """Match the parts this stage needs against the demonstration item master.

    Only ``DM-`` items are considered: a development database usually also holds
    rows left behind by end-to-end tests, and a demonstration BOM built out of a
    test fixture would be a confusing thing to hand an evaluator.
    """
    stmt = (
        select(Item)
        .where(
            Item.company_id == ctx.company_id,
            Item.deleted_at.is_(None),
            Item.is_active.is_(True),
            Item.code.startswith(f"{DEMO_PREFIX}-"),
        )
        .order_by(Item.code)
    )
    pool = list((await ctx.session.execute(stmt)).scalars())
    claimed: set[uuid.UUID] = set()

    # Order matters: the finished handle claims its code before the keyword
    # "LEVER" can reach the machined lever, and the raw-material codes are tried
    # before the bare metal names, which also appear in the description of every
    # brass cylinder and stainless hinge in the catalogue.
    handle = _claim(
        pool,
        claimed,
        codes=(ctx.code("FG", "LEVER-AB"), ctx.code("FG", "LEVER-SS")),
        keywords=("HANDLE ON ROSE", "HANDLE", "LEVER"),
        manufactured=True,
    )
    lever = _claim(
        pool,
        claimed,
        codes=(ctx.code("SF", "LEVER-BRASS"),),
        keywords=("LEVER HANDLE CASTING", "CASTING", "LEVER"),
        manufactured=True,
        component=True,
    )
    rose = _claim(
        pool,
        claimed,
        codes=(ctx.code("SF", "ROSE-SS"),),
        keywords=("ROSE PLATE", "ROSE"),
        manufactured=True,
        component=True,
    )
    brass_rod = _claim(
        pool,
        claimed,
        codes=(ctx.code("RM", "BRASS"),),
        keywords=("BRASS ROD", "BRASS BAR", "BRASS"),
        component=True,
    )
    steel_rod = _claim(
        pool,
        claimed,
        codes=(ctx.code("RM", "SS"),),
        keywords=("STAINLESS STEEL ROD", "STAINLESS", "STEEL"),
        component=True,
    )
    screw = _claim(
        pool,
        claimed,
        codes=(ctx.code("BO", "SCREW-CSK"),),
        keywords=("SCREW", "FASTEN"),
        component=True,
    )
    spring = _claim(
        pool, claimed, codes=(ctx.code("BO", "SPRING"),), keywords=("SPRING",), component=True
    )
    carton = _claim(
        pool,
        claimed,
        codes=(ctx.code("PK", "BOX"),),
        keywords=("CARTON", "BOX"),
        component=True,
    )
    lacquer = _claim(
        pool,
        claimed,
        codes=(ctx.code("CN", "LACQUER"),),
        keywords=("LACQUER", "VARNISH"),
        component=True,
    )

    parts = _Parts(
        handle=handle,
        lever=lever,
        rose=rose,
        brass_rod=brass_rod,
        steel_rod=steel_rod,
        screw=screw,
        spring=spring,
        carton=carton,
        lacquer=lacquer,
    )

    named = (
        ("finished handle set", handle),
        ("machined lever", lever),
        ("rose plate", rose),
        ("brass rod", brass_rod),
        ("stainless rod", steel_rod),
        ("screw", screw),
        ("spring", spring),
        ("carton", carton),
        ("lacquer", lacquer),
    )

    # Register what was matched under the item's own code. The masters stage
    # already did, under the same key; doing it again costs nothing and means
    # this stage still hands its successors a usable register if it was run on
    # its own with ``--only engineering``.
    for _, item in named:
        if item is not None:
            ctx.remember("item", item.code, item.id)

    missing = [label for label, item in named if item is None]
    if missing:
        ctx.skipped.append(
            (
                STAGE,
                f"no demonstration item matched: {', '.join(missing)}; "
                "the structures that need them are built without those lines",
            )
        )
    if screw is None:
        # Worth saying separately: without a part on more than one branch the
        # explosion consolidates nothing, and consolidation is the thing this
        # BOM exists to demonstrate.
        ctx.skipped.append(
            (STAGE, "no shared fastener, so the multi-level BOM has no common part to merge")
        )
    return parts


# -----------------------------------------------------------------------------
# Work centres
# -----------------------------------------------------------------------------


async def _seed_work_centres(ctx: DemoContext) -> dict[str, uuid.UUID]:
    """Create the four cells. Returns ``suffix -> id`` for whatever now exists.

    A cell this tool made before cost centres existed is given its cost pool on
    the next run, through the service so the change is audited; one a person has
    since pointed elsewhere is left alone.
    """
    service = WorkCenterService(ctx.session, ctx.tenant)
    resolved: dict[str, uuid.UUID] = {}

    for centre in WORK_CENTRES:
        code = ctx.code(centre.suffix)
        # The masters stage registers cost centres under ``DM-CC-<suffix>``.
        cost_centre_id = ctx.maybe_id("cost_centre", ctx.code("CC", *centre.cost_centre))
        existing = await find_by_code(ctx.session, WorkCenter, ctx.company_id, code)
        if existing is not None:
            resolved[centre.suffix] = ctx.remember("work_centre", code, existing.id)
            ctx.note(STAGE, existing=1)
            if existing.cost_centre_id is None and cost_centre_id is not None:
                await service.update_work_center(
                    existing.id, WorkCenterUpdate(cost_centre_id=cost_centre_id)
                )
            continue

        created = await service.create_work_center(
            WorkCenterCreate(
                code=code,
                name=centre.name,
                description=centre.description,
                capacity_per_hour=Decimal(centre.capacity_per_hour),
                cost_per_hour=Decimal(centre.cost_per_hour),
                setup_cost=Decimal(centre.setup_cost),
                # One eight-hour shift throughout, so the lead time a routing
                # reports divides into readable whole days.
                working_hours_per_day=Decimal("8"),
                cost_centre_id=cost_centre_id,
            )
        )
        resolved[centre.suffix] = ctx.remember("work_centre", code, created.id)
        ctx.note(STAGE, created=1)

    return resolved


async def _seed_machines(ctx: DemoContext, centres: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    """Create the equipment inside each cell."""
    service = MachineService(ctx.session, ctx.tenant)
    resolved: dict[str, uuid.UUID] = {}

    for machine in MACHINES:
        code = ctx.code(machine.suffix)
        existing = await find_by_code(ctx.session, Machine, ctx.company_id, code)
        if existing is not None:
            resolved[machine.suffix] = ctx.remember("machine", code, existing.id)
            ctx.note(STAGE, existing=1)
            continue

        centre_id = centres.get(machine.centre)
        if centre_id is None:
            ctx.skipped.append(
                (STAGE, f"{code} needs work centre {machine.centre}, which was not created")
            )
            continue

        created = await service.create_machine(
            MachineCreate(
                code=code,
                name=machine.name,
                work_center_id=centre_id,
                machine_type=machine.machine_type,
                capacity_per_hour=Decimal(machine.capacity_per_hour),
                cost_per_hour=Decimal(machine.cost_per_hour),
                last_maintenance_date=_days_before(machine.last_service_days_ago),
                next_maintenance_date=_days_after(machine.next_service_days_ahead),
            )
        )
        resolved[machine.suffix] = ctx.remember("machine", code, created.id)
        ctx.note(STAGE, created=1)

    return resolved


async def _stop_for_maintenance(ctx: DemoContext, machines: dict[str, uuid.UUID]) -> None:
    """Take the buffing machine off line, through the state machine.

    Run after the routings are approved: approval re-checks every machine an
    operation names, and doing it in this order proves the re-check tolerates a
    machine that is down today — only retirement invalidates a process.
    """
    machine_id = machines.get(MAINTENANCE_MACHINE)
    if machine_id is None:
        return

    service = MachineService(ctx.session, ctx.tenant)
    machine = await service.get_machine(machine_id)
    if machine.status is not MachineStatus.AVAILABLE:
        return  # already moved, on this run or a previous one

    # No ``next_maintenance_date`` on the way *in*: the field exists so the
    # fitter can state the next due date when the service is signed off, and
    # writing it now would replace a live schedule with a guess.
    await service.change_status(
        machine_id,
        MachineStatusChange(
            status=MachineStatus.MAINTENANCE,
            reason="Planned service: spindle bearings and mop shaft re-balance",
        ),
    )
    ctx.note(STAGE, created=1)


# -----------------------------------------------------------------------------
# Routings
# -----------------------------------------------------------------------------


def _routing_operations(
    steps: tuple[_Step, ...],
    centres: dict[str, uuid.UUID],
    machines: dict[str, uuid.UUID],
) -> list[RoutingOperationIn]:
    """Turn a step table into operation payloads, dropping unbuildable steps.

    A step whose work centre does not exist is dropped rather than pointed at a
    different cell: a routing that plans turning on the packing bench is worse
    demonstration data than a routing with one step fewer.
    """
    operations: list[RoutingOperationIn] = []
    for step in steps:
        centre_id = centres.get(step.centre)
        if centre_id is None:
            continue
        operations.append(
            RoutingOperationIn(
                sequence=step.sequence,
                operation_code=step.code,
                operation_name=step.name,
                work_center_id=centre_id,
                machine_id=machines.get(step.machine) if step.machine else None,
                setup_time_minutes=Decimal(step.setup_minutes),
                run_time_per_unit_minutes=Decimal(step.run_minutes),
                queue_time_minutes=Decimal(step.queue_minutes),
                move_time_minutes=Decimal(step.move_minutes),
                qc_required=step.qc_required,
            )
        )
    return operations


async def _seed_routing(
    ctx: DemoContext,
    *,
    suffix: str,
    name: str,
    item: Item,
    steps: tuple[_Step, ...],
    centres: dict[str, uuid.UUID],
    machines: dict[str, uuid.UUID],
    notes: str,
) -> uuid.UUID | None:
    """Create and approve one routing. Returns its id, or ``None`` if skipped."""
    service = RoutingService(ctx.session, ctx.tenant)
    code = ctx.code(suffix)

    routing = await find_by_code(ctx.session, Routing, ctx.company_id, code)
    if routing is None:
        operations = _routing_operations(steps, centres, machines)
        if not operations:
            ctx.skipped.append((STAGE, f"{code} has no buildable operations without its cells"))
            return None
        detail = await service.create_routing(
            RoutingCreate(
                code=code,
                name=name,
                item_id=item.id,
                # The default process for the item, so MRP's lead-time lookup
                # and production's routing lookup both resolve to it.
                is_default=True,
                effective_from=_days_before(120),
                notes=notes,
                operations=operations,
            )
        )
        routing_id = detail.id
        status = detail.status
        ctx.note(STAGE, created=1)
    else:
        routing_id = routing.id
        status = RoutingStatus(routing.status)
        ctx.note(STAGE, existing=1)

    ctx.remember("routing", code, routing_id)

    # Approved on a re-run too, so a run interrupted between create and approve
    # heals itself rather than leaving production without a process.
    if status is RoutingStatus.DRAFT:
        await service.approve_routing(
            ctx.principal,
            routing_id,
            reason="Process signed off by engineering after first-article approval",
        )
    return routing_id


async def _operation_ids(ctx: DemoContext, routing_id: uuid.UUID) -> dict[int, uuid.UUID]:
    """``sequence -> routing operation id``, for the BOM's link back to it."""
    service = RoutingService(ctx.session, ctx.tenant)
    return {op.sequence: op.id for op in await service.list_operations(routing_id)}


async def _seed_draft_revision(ctx: DemoContext, source_id: uuid.UUID) -> None:
    """Copy the approved lever routing into a draft revision.

    This is how an approved routing is edited — the released version stays
    exactly as the orders raised against it remember it — and it leaves the
    routing screen showing an APPROVED process and a DRAFT one for the same
    item, which is the state an engineering department is normally in.
    """
    code = ctx.code("RT-LEVER-02")
    if await find_by_code(ctx.session, Routing, ctx.company_id, code) is not None:
        ctx.note(STAGE, existing=1)
        return

    service = RoutingService(ctx.session, ctx.tenant)
    detail = await service.copy_as_new_version(
        source_id,
        CopyAsNewVersionRequest(
            code=code,
            name="Machined lever - single-setup turning (proposed)",
            effective_from=_days_after(30),
            notes=(
                "Proposal: drill and tap the grub screw seat on the turning centre in "
                "the same setup, and lose the pillar drill operation. Awaiting a time "
                "study before review."
            ),
        ),
    )
    ctx.remember("routing", code, detail.id)
    ctx.note(STAGE, created=1)


async def _seed_routings(
    ctx: DemoContext,
    centres: dict[str, uuid.UUID],
    machines: dict[str, uuid.UUID],
    parts: _Parts,
) -> dict[str, dict[int, uuid.UUID]]:
    """One routing per manufactured item, plus the draft revision.

    Returns each routing's operation ids so the matching BOM can point its costed
    operations back at the live process.
    """
    operations: dict[str, dict[int, uuid.UUID]] = {}

    wanted: tuple[tuple[str, str, Item | None, tuple[_Step, ...], str], ...] = (
        (
            "RT-LEVER-01",
            "Machined brass lever - turn, drill, polish",
            parts.lever,
            LEVER_STEPS,
            "Released process for the brass lever. Batches of 250 off the turning centre.",
        ),
        (
            "RT-ROSE-01",
            "Stainless rose plate - part off, face, polish",
            parts.rose,
            ROSE_STEPS,
            "Released process for the 52 mm rose plate. Fed to assembly in trays of 100.",
        ),
        (
            "RT-HANDLE-01",
            "Lever handle set - assemble and pack",
            parts.handle,
            HANDLE_STEPS,
            "Released process for the lever-on-rose range. One 8-hour shift.",
        ),
    )

    for suffix, name, item, steps, notes in wanted:
        if item is None:
            ctx.skipped.append((STAGE, f"{ctx.code(suffix)} skipped: its item was not matched"))
            continue
        routing_id = await _seed_routing(
            ctx,
            suffix=suffix,
            name=name,
            item=item,
            steps=steps,
            centres=centres,
            machines=machines,
            notes=notes,
        )
        if routing_id is not None:
            operations[suffix] = await _operation_ids(ctx, routing_id)

    lever_routing = ctx.maybe_id("routing", ctx.code("RT-LEVER-01"))
    if lever_routing is not None:
        await _seed_draft_revision(ctx, lever_routing)

    return operations


# -----------------------------------------------------------------------------
# Bills of material
# -----------------------------------------------------------------------------
# The structure being built, and why it is shaped like this:
#
#   Lever handle set (rev B, APPROVED, default)          1 SET
#     +- Machined brass lever  x2   <- manufactured, has its own approved BOM
#     |    +- Brass rod       0.21 kg   6% scrap: swarf and the parting allowance
#     |    +- M4 CSK screw      x1  <-- the common part, two levels down
#     |    +- Clear lacquer   0.015 L   8% scrap: overspray on a hand-sprayed line
#     +- Stainless rose plate  x2   <- manufactured, has its own approved BOM
#     |    +- Stainless rod   0.09 kg
#     |    +- M4 CSK screw      x2  <-- the same screw again, other branch
#     +- M4 CSK screw          x4   <-- and again, at the top level
#     +- Torsion spring        x2
#     +- Printed carton        x1
#
# Exploding one handle set therefore asks for 4 + (2 x 1) + (2 x 2) = 10 screws,
# merged from three branches carrying three different scrap allowances. That is
# the case consolidation exists for, and it is why the masters stage was careful
# to make the screw usable by more than one parent.
#
# Revision A is the same product as it was built before the lever and rose cells
# existed — bar straight onto the handle line — and is left OBSOLETE so the BOM
# list shows a real supersession rather than a manufactured one.


@dataclass(frozen=True)
class _LineSpec:
    item: Item | None
    quantity: str
    scrap: str
    #: Which of the BOM's own operations consumes it.
    operation: int | None
    note: str


def _lines(specs: tuple[_LineSpec, ...], operations: list[BomOperationIn]) -> list[BomLineIn]:
    """Build the line payloads, numbering by tens and dropping unmatched parts.

    Numbering by ten is the shop-floor convention: it leaves room to insert a
    line later without renumbering the whole structure.

    ``operations`` is the set this BOM will actually carry, not the set it was
    meant to. A line may only name an operation its own BOM defines, so if a step
    was dropped because its work centre is missing, the lines that fed it lose
    the reference rather than the BOM losing its lines.
    """
    defined = {operation.operation_sequence for operation in operations}
    lines: list[BomLineIn] = []
    for spec in specs:
        if spec.item is None:
            continue
        lines.append(
            BomLineIn(
                line_number=(len(lines) + 1) * 10,
                component_item_id=spec.item.id,
                quantity_per=Decimal(spec.quantity),
                scrap_percentage=Decimal(spec.scrap),
                operation_sequence=spec.operation if spec.operation in defined else None,
                notes=spec.note,
            )
        )
    return lines


def _bom_operations(
    steps: tuple[_Step, ...],
    centres: dict[str, uuid.UUID],
    routing_operations: dict[int, uuid.UUID],
) -> list[BomOperationIn]:
    """The costed snapshot of the routing that the BOM carries.

    Setup and run only: queue and move are elapsed time nobody is working
    through, and charging the work-centre rate for them would inflate the
    standard cost of every part that crosses more than one cell. The rate is
    copied from the centre rather than referenced, because an approved BOM's
    cost must not move when someone re-rates a work centre next quarter.
    """
    operations: list[BomOperationIn] = []
    for step in steps:
        centre_id = centres.get(step.centre)
        if centre_id is None:
            continue
        operations.append(
            BomOperationIn(
                operation_sequence=step.sequence,
                name=step.name,
                work_center_id=centre_id,
                routing_operation_id=routing_operations.get(step.sequence),
                setup_time_minutes=Decimal(step.setup_minutes),
                run_time_minutes_per_unit=Decimal(step.run_minutes),
                cost_per_hour=Decimal(_CENTRE_RATES[step.centre]),
            )
        )
    return operations


def _lever_lines(parts: _Parts) -> tuple[_LineSpec, ...]:
    """One machined lever."""
    return (
        _LineSpec(
            item=parts.brass_rod,
            quantity="0.2100",
            scrap="6",
            operation=10,
            # Six per cent covers the swarf and the parting-off allowance. It is
            # the reason MRP asks the buyer for more bar than the finished
            # weight of the levers implies.
            note="0.21 kg of 12 mm bar a lever, including the parting-off allowance.",
        ),
        _LineSpec(
            item=parts.screw,
            quantity="1",
            scrap="1.5",
            operation=20,
            note="Grub screw fitted into the tapped seat - shared with the parent set.",
        ),
        _LineSpec(
            item=parts.lacquer,
            quantity="0.0150",
            scrap="8",
            operation=30,
            note="Clear lacquer over the mopped finish; the scrap allowance is overspray.",
        ),
    )


def _rose_lines(parts: _Parts) -> tuple[_LineSpec, ...]:
    """One polished rose plate."""
    return (
        _LineSpec(
            item=parts.steel_rod,
            quantity="0.0900",
            scrap="5",
            operation=10,
            note="0.09 kg of SS 304 bar a plate, including the parting-off allowance.",
        ),
        _LineSpec(
            item=parts.screw,
            quantity="2",
            scrap="1.5",
            operation=20,
            note="Two fixing screws packed with each rose - the same screw as the parent.",
        ),
    )


def _handle_lines(parts: _Parts, *, machined: bool) -> tuple[_LineSpec, ...]:
    """The handle set's components.

    ``machined=False`` produces revision A: the same product built before the
    lever cell existed, when bar went straight onto the handle line and every
    operation happened on one level.
    """
    # The two structures number their operations differently, because the
    # legacy one carries the machining steps in front of the assembly: assembly
    # is step 40 there and step 10 here.
    assembly_op = 10 if machined else 40
    pack_op = 20 if machined else 50

    lever_lines: tuple[_LineSpec, ...]
    if machined:
        lever_lines = (
            _LineSpec(
                item=parts.lever,
                quantity="2",
                scrap="0",
                operation=10,
                note="Machined, polished and lacquered before it reaches the bench.",
            ),
        )
    else:
        lever_lines = (
            _LineSpec(
                item=parts.brass_rod,
                quantity="0.4200",
                scrap="6",
                operation=10,
                note="Bar for two levers, turned on the handle line (superseded).",
            ),
        )

    return (
        *lever_lines,
        _LineSpec(
            item=parts.rose,
            quantity="2",
            scrap="0",
            operation=assembly_op,
            note="One rose plate each side of the door.",
        ),
        _LineSpec(
            item=parts.screw,
            # Four fixing screws a set, on top of the ones each sub-assembly
            # already carries. The explosion has to add all three branches.
            quantity="4",
            scrap="2.5",
            operation=assembly_op,
            note="Rose fixing screws. Dropped screws are the usual bench loss.",
        ),
        _LineSpec(
            item=parts.spring,
            quantity="2",
            scrap="1.5",
            operation=assembly_op,
            note="Lever return spring, one per lever.",
        ),
        _LineSpec(
            item=parts.carton,
            quantity="1",
            scrap="0.5",
            operation=pack_op,
            note="Printed carton with the barcode label.",
        ),
    )


async def _find_bom(ctx: DemoContext, item_id: uuid.UUID, revision: str) -> Bom | None:
    """Idempotence key for a BOM.

    A BOM's ``code`` is issued from the document sequence, so it is not
    predictable and ``find_by_code`` cannot be used. ``(item, revision)`` is the
    stable identity: revision A of the handle is revision A on every run.
    """
    stmt = select(Bom).where(
        Bom.company_id == ctx.company_id,
        Bom.item_id == item_id,
        Bom.revision == revision,
    )
    return (await ctx.session.execute(stmt)).scalars().first()


async def _advance_to_approved(ctx: DemoContext, bom: Bom) -> None:
    """Walk a structure to APPROVED along the state machine's own path.

    Submit, then approve — never a status write. Written so it can be re-entered:
    a run that died between the two leaves a PENDING_APPROVAL structure, and the
    next run finishes the job instead of tripping over it.
    """
    status = BomStatus(bom.status)
    if status is BomStatus.DRAFT:
        await bom_service.submit_bom(
            ctx.session,
            ctx.tenant,
            bom.id,
            reason="Structure complete; sent for engineering review",
        )
        status = BomStatus.PENDING_APPROVAL
    if status is BomStatus.PENDING_APPROVAL:
        await bom_service.approve_bom(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            bom.id,
            reason="Reviewed against the first-article sample; released to planning",
        )


async def _seed_bom(
    ctx: DemoContext,
    *,
    item: Item,
    revision: str,
    lines: list[BomLineIn],
    operations: list[BomOperationIn],
    bom_type: BomType,
    is_default: bool,
    effective_from: date,
    effective_to: date | None,
    notes: str,
) -> Bom | None:
    """Create one structure if it is not already there, and count it."""
    if not lines:
        ctx.skipped.append(
            (STAGE, f"no components matched for {item.code} revision {revision}; BOM not built")
        )
        return None

    bom = await _find_bom(ctx, item.id, revision)
    if bom is not None:
        ctx.note(STAGE, existing=1)
        return bom

    created, _, _ = await bom_service.create_bom(
        ctx.session,
        ctx.tenant,
        BomCreate(
            item_id=item.id,
            revision=revision,
            bom_type=bom_type,
            output_quantity=Decimal("1"),
            is_default=is_default,
            effective_from=effective_from,
            effective_to=effective_to,
            notes=notes,
            lines=lines,
            operations=operations,
        ),
    )
    ctx.note(STAGE, created=1)
    return created


async def _seed_sub_assembly(
    ctx: DemoContext,
    *,
    item: Item,
    centres: dict[str, uuid.UUID],
    steps: tuple[_Step, ...],
    routing_operations: dict[int, uuid.UUID],
    specs: tuple[_LineSpec, ...],
    notes: str,
) -> None:
    """One approved sub-assembly structure, registered under its item code."""
    operations = _bom_operations(steps, centres, routing_operations)
    bom = await _seed_bom(
        ctx,
        item=item,
        revision="A",
        lines=_lines(specs, operations),
        operations=operations,
        bom_type=BomType.ASSEMBLY,
        is_default=True,
        effective_from=_days_before(150),
        effective_to=None,
        notes=notes,
    )
    if bom is not None:
        ctx.remember("bom", item.code, bom.id)
        await _advance_to_approved(ctx, bom)


async def _seed_boms(
    ctx: DemoContext,
    centres: dict[str, uuid.UUID],
    parts: _Parts,
    routing_operations: dict[str, dict[int, uuid.UUID]],
) -> None:
    """The multi-level structure, in the order the cost roll-up needs it.

    Sub-assemblies are approved first: approving the parent rolls its cost up
    through the whole graph, and a child still in draft is invisible to that
    roll-up, so the parent would be approved carrying a materially understated
    cost.
    """
    if parts.lever is not None:
        await _seed_sub_assembly(
            ctx,
            item=parts.lever,
            centres=centres,
            steps=LEVER_STEPS,
            routing_operations=routing_operations.get("RT-LEVER-01", {}),
            specs=_lever_lines(parts),
            notes="Machined, polished and lacquered lever, drawn from stock by assembly.",
        )

    if parts.rose is not None:
        await _seed_sub_assembly(
            ctx,
            item=parts.rose,
            centres=centres,
            steps=ROSE_STEPS,
            routing_operations=routing_operations.get("RT-ROSE-01", {}),
            specs=_rose_lines(parts),
            notes="Polished rose plate, fed to the handle bench in trays of 100.",
        )

    if parts.handle is None:
        ctx.skipped.append((STAGE, "no manufacturable handle item, so no finished-goods BOM"))
        return

    current_operations = _bom_operations(
        HANDLE_STEPS, centres, routing_operations.get("RT-HANDLE-01", {})
    )
    # The superseded revision carries the process as it was: all five steps on
    # one level. It links to no routing operation, because the routing that
    # matched it has not existed since the machining moved down a level, and
    # pointing it at today's two-step process would be a lie about history.
    legacy_operations = _bom_operations(LEGACY_HANDLE_STEPS, centres, {})

    # --- revision A: superseded, and made obsolete --------------------------
    # Approved first and then retired, because that is the only way a structure
    # legitimately reaches OBSOLETE with history behind it. The effectivity
    # window closes the day before revision B opens.
    revision_a = await _seed_bom(
        ctx,
        item=parts.handle,
        revision="A",
        lines=_lines(_handle_lines(parts, machined=False), legacy_operations),
        operations=legacy_operations,
        bom_type=BomType.PRODUCTION,
        is_default=True,
        effective_from=_days_before(365),
        effective_to=_days_before(151),
        notes="Original build: bar turned, polished and assembled on the handle line.",
    )
    if revision_a is not None:
        ctx.remember("bom", f"{parts.handle.code}-REV-A", revision_a.id)
        await _advance_to_approved(ctx, revision_a)

    # --- revision B: the live structure -------------------------------------
    # Approving it demotes revision A from default in the same transaction, which
    # is the supersession the partial unique index is there to make safe.
    revision_b = await _seed_bom(
        ctx,
        item=parts.handle,
        revision="B",
        lines=_lines(_handle_lines(parts, machined=True), current_operations),
        operations=current_operations,
        bom_type=BomType.PRODUCTION,
        is_default=True,
        effective_from=_days_before(150),
        effective_to=None,
        notes=(
            "Current build. The lever and the rose arrive as finished sub-assemblies, "
            "which took the machining off the handle line altogether."
        ),
    )
    if revision_b is not None:
        # Registered under the plain item code: this is the structure MRP,
        # production and costing should resolve for the handle.
        ctx.remember("bom", parts.handle.code, revision_b.id)
        await _advance_to_approved(ctx, revision_b)

    # Retire revision A only once its replacement is live, so the item is never
    # left without an approved default structure.
    if revision_a is not None and BomStatus(revision_a.status) is BomStatus.APPROVED:
        await bom_service.obsolete_bom(
            ctx.session,
            ctx.tenant,
            revision_a.id,
            reason="Superseded by revision B (lever and rose supplied as sub-assemblies)",
        )
        ctx.note(STAGE, created=1)

    # --- revision C: left in draft ------------------------------------------
    # Not submitted: the BOM list needs a structure that is still being worked on
    # as well as one that is live and one that is dead. It is not the default —
    # it is a costing study, not a decision.
    revision_c = await _seed_bom(
        ctx,
        item=parts.handle,
        revision="C",
        lines=_lines(_handle_lines(parts, machined=True), current_operations),
        operations=current_operations,
        bom_type=BomType.PRODUCTION,
        is_default=False,
        effective_from=_days_after(30),
        effective_to=None,
        notes="Costing study: heavier rose plate and a longer fixing screw.",
    )
    if revision_c is not None:
        ctx.remember("bom", f"{parts.handle.code}-REV-C", revision_c.id)


# -----------------------------------------------------------------------------
# QC templates
# -----------------------------------------------------------------------------
# Three templates, deliberately at three different scopes and three different
# sampling plans, because scope resolution and sample size are the two things a
# quality manager will want to see working:
#
#   incoming    category-scoped, 10% of the lot        - what arrives from a mill
#   in-process  stage-wide default, 5 pieces a batch   - what comes off the lathe
#   final       item-scoped, AQL II                    - what goes in the carton
#
# Each carries a dimensional characteristic with a real tolerance, a visual
# characteristic and a finish characteristic, so the check sheet the inspector
# sees is a check sheet and not a placeholder.


async def _seed_qc_template(ctx: DemoContext, payload: TemplateCreate) -> None:
    existing = await find_by_code(ctx.session, QcTemplate, ctx.company_id, payload.code)
    if existing is not None:
        ctx.remember("qc_template", payload.code, existing.id)
        ctx.note(STAGE, existing=1)
        return

    try:
        template, _ = await quality_service.create_template(ctx.session, ctx.tenant, payload)
    except DuplicateError as exc:
        # Another active template already owns this scope for this stage. That is
        # a legitimate state in a database somebody has been using; it is not a
        # reason to abandon the other two templates.
        ctx.skipped.append((STAGE, f"{payload.code}: {exc}"))
        return

    ctx.remember("qc_template", payload.code, template.id)
    ctx.note(STAGE, created=1)


async def _seed_qc_templates(ctx: DemoContext, parts: _Parts) -> None:
    # Scope the incoming check at the brass category from the baseline seed, so
    # it covers every brass item rather than the one rod this demo happens to
    # use. Falls back to a stage-wide default if the category is not there.
    brass_category = await find_by_code(ctx.session, ItemCategory, ctx.company_id, "RAW-BRASS")

    await _seed_qc_template(
        ctx,
        TemplateCreate(
            code=ctx.code("QCT-INCOMING-BRASS"),
            name="Incoming - brass bar stock",
            description=(
                "Goods-inward check on 12 mm free-cutting brass bar. Ten per cent of "
                "each delivery, measured at both ends of a bar."
            ),
            item_category_id=brass_category.id if brass_category else None,
            qc_stage=QcStage.INCOMING,
            sampling_plan=SamplingPlan.PERCENTAGE,
            sample_percentage=Decimal("10"),
            parameters=[
                TemplateParameterIn(
                    sequence=10,
                    parameter_name="Bar diameter",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MM",
                    min_value=Decimal("11.90"),
                    max_value=Decimal("12.10"),
                    target_value=Decimal("12.00"),
                    # Critical: undersize bar makes an undersize lever, and the
                    # error is only found after the whole batch is turned.
                    is_critical=True,
                ),
                TemplateParameterIn(
                    sequence=20,
                    parameter_name="Straightness over 1 m",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MM",
                    max_value=Decimal("1.50"),
                ),
                TemplateParameterIn(
                    sequence=30,
                    parameter_name="Free from scale, seams and dents",
                    parameter_type=ParameterType.BOOLEAN,
                ),
                TemplateParameterIn(
                    sequence=40,
                    parameter_name="Alloy grade on the mill certificate",
                    parameter_type=ParameterType.SELECT,
                    allowed_values=["CuZn39Pb3", "CuZn40Pb2", "IS 319 Pb1"],
                ),
            ],
        ),
    )

    await _seed_qc_template(
        ctx,
        TemplateCreate(
            code=ctx.code("QCT-INPROCESS-MACHINING"),
            name="In-process - machined lever",
            description=(
                "First-off and every-batch check at the machine shop. Five pieces "
                "whatever the batch size, taken across the run."
            ),
            qc_stage=QcStage.IN_PROCESS,
            sampling_plan=SamplingPlan.FIXED_SAMPLE,
            sample_size=5,
            parameters=[
                TemplateParameterIn(
                    sequence=10,
                    parameter_name="Spindle bore across flats",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MM",
                    min_value=Decimal("7.95"),
                    max_value=Decimal("8.10"),
                    target_value=Decimal("8.00"),
                    # An 8 mm spindle that will not enter the bore stops the
                    # whole assembly bench, so this rejects the batch on its own.
                    is_critical=True,
                ),
                TemplateParameterIn(
                    sequence=20,
                    parameter_name="Overall lever length",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MM",
                    min_value=Decimal("119.50"),
                    max_value=Decimal("120.50"),
                    target_value=Decimal("120.00"),
                ),
                TemplateParameterIn(
                    sequence=30,
                    parameter_name="M4 thread go / no-go gauge",
                    parameter_type=ParameterType.BOOLEAN,
                    is_critical=True,
                ),
                TemplateParameterIn(
                    sequence=40,
                    parameter_name="Deburred, free from tool marks",
                    parameter_type=ParameterType.BOOLEAN,
                ),
            ],
        ),
    )

    await _seed_qc_template(
        ctx,
        TemplateCreate(
            code=ctx.code("QCT-FINAL-HANDLE"),
            name="Final - lever handle set",
            description=(
                "Pre-dispatch check on the packed set. ISO 2859-1 level II, so the "
                "sample grows with the lot without growing linearly."
            ),
            item_id=parts.handle.id if parts.handle else None,
            qc_stage=QcStage.FINAL,
            sampling_plan=SamplingPlan.AQL,
            parameters=[
                TemplateParameterIn(
                    sequence=10,
                    parameter_name="Rose fixing centres",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MM",
                    min_value=Decimal("95.50"),
                    max_value=Decimal("96.50"),
                    target_value=Decimal("96.00"),
                    # Out of tolerance and the handle will not fit the door the
                    # architect drilled, which is a site return, not a rework.
                    is_critical=True,
                ),
                TemplateParameterIn(
                    sequence=20,
                    parameter_name="Lever returns fully under spring load",
                    parameter_type=ParameterType.BOOLEAN,
                    is_critical=True,
                ),
                TemplateParameterIn(
                    sequence=30,
                    parameter_name="Plating thickness",
                    parameter_type=ParameterType.NUMERIC,
                    unit="MICRON",
                    min_value=Decimal("8.00"),
                    target_value=Decimal("12.00"),
                ),
                TemplateParameterIn(
                    sequence=40,
                    parameter_name="Finish shade against the master sample",
                    parameter_type=ParameterType.SELECT,
                    allowed_values=[
                        "Antique Brass",
                        "Satin Stainless",
                        "Polished Chrome",
                        "Black Nickel",
                    ],
                ),
                TemplateParameterIn(
                    sequence=50,
                    parameter_name="No scratches, water marks or handling damage",
                    parameter_type=ParameterType.BOOLEAN,
                ),
                TemplateParameterIn(
                    sequence=60,
                    parameter_name="Carton contents: pair, spindle, screw pack, label",
                    parameter_type=ParameterType.BOOLEAN,
                ),
            ],
        ),
    )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


async def seed(ctx: DemoContext) -> None:
    """Work centres, machines, routings, bills of material and QC templates.

    Ordered by dependency: cells before the machines inside them, machines before
    the routings that name them, routings before the BOMs that snapshot their
    cost, and the sub-assemblies before the parent that consumes them.
    """
    centres = await _seed_work_centres(ctx)
    machines = await _seed_machines(ctx, centres)

    parts = await _resolve_parts(ctx)

    try:
        routing_operations = await _seed_routings(ctx, centres, machines, parts)
    except BusinessRuleError as exc:
        # A demo item flagged manufacturable in one place and not in another, or
        # a cell somebody deactivated by hand. Costs the routings and the BOM
        # operation snapshot; the structures and the templates still land.
        ctx.skipped.append((STAGE, f"routings not built: {exc}"))
        routing_operations = {}

    await _stop_for_maintenance(ctx, machines)

    try:
        await _seed_boms(ctx, centres, parts, routing_operations)
    except BusinessRuleError as exc:
        ctx.skipped.append((STAGE, f"bills of material not built: {exc}"))

    await _seed_qc_templates(ctx, parts)
