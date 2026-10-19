"""Routing business rules: how long a job takes, what it costs, and who may say so.

The rules that matter, and why each one lives here rather than in a caller:

1. **An approved routing is immutable.** Editing is only possible in ``DRAFT``.
   A change to an approved process is a *new version* created by
   :func:`RoutingService.copy_as_new_version`, because production orders and MRP
   runs already reference the old one and must keep resolving to the process
   they were planned against.
2. **Approval is a second person's judgement.** The approver may not be the
   author unless they hold ``SUPER_ADMIN`` - see :func:`can_approve`.
3. **One default approved routing per item**, demoted *before* the new one is
   promoted. The partial unique index is checked per statement, so
   promote-then-demote is a valid end state that still fails mid-transaction.
4. **The time, cost and lead-time functions are pure.** They take a
   :class:`~src.modules.routing.repository.CostedRouting` - operations with the
   work-centre rate already joined in - so they can be unit-tested without a
   database and so no calculation can trigger a lazy load per operation.
5. **Every write is one transaction with its audit row**, and every status
   change goes through a :class:`~src.core.state_machine.StateMachine`.

ASSUMPTION: there is no ``routing.approve`` permission in the registry - the
catalogue defines only ``routing.view`` and ``routing.manage`` - so approval,
rejection to draft and obsoleting are all gated on ``ROUTING_MANAGE``, with the
author/approver separation in :func:`can_approve` carrying the segregation of
duties instead. If routings later need their own approver role, add the
permission to the catalogue and change the two transitions below.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.messages import NO_FIELDS_TO_UPDATE, SHIFT_ASSIGNMENT_OVERLAP, inactive_record
from src.core.pagination import PageParams, SortParams, paginate
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_money, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.organization.service import require_cost_centre
from src.modules.routing.models import (
    DEFAULT_WORKING_HOURS_PER_DAY,
    Machine,
    MachineStatus,
    Routing,
    RoutingOperation,
    RoutingStatus,
    Shift,
    ShiftAssignment,
    ShiftAssignmentStatus,
    WorkCenter,
    shift_crosses_midnight,
    shift_minutes,
)
from src.modules.routing.repository import (
    OPERATOR_ENTITY,
    SHIFT_ASSIGNMENT_ENTITY,
    SHIFT_ENTITY,
    WORK_CENTER_ENTITY,
    CompanyZoneRepository,
    CostedOperation,
    CostedRouting,
    ItemLookupRepository,
    MachineInfo,
    MachineRepository,
    RosterOperatorRepository,
    RoutingItemInfo,
    RoutingOperationRepository,
    RoutingRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
    WorkCenterRates,
    WorkCenterRepository,
)
from src.modules.routing.schemas import (
    MAX_ROSTER_BULK,
    CopyAsNewVersionRequest,
    LeadTimeRead,
    MachineCreate,
    MachineFilters,
    MachineRead,
    MachineStatusChange,
    MachineUpdate,
    OperationCostRead,
    RosterOperatorRead,
    RoutingCostRead,
    RoutingCreate,
    RoutingDetail,
    RoutingFilters,
    RoutingOperationIn,
    RoutingOperationRead,
    RoutingSummary,
    RoutingUpdate,
    ShiftAssignmentCreate,
    ShiftAssignmentRead,
    ShiftAssignmentUpdate,
    ShiftCreate,
    ShiftFilters,
    ShiftRead,
    ShiftRosterCopyRequest,
    ShiftRosterFilters,
    ShiftUpdate,
    WorkCenterCreate,
    WorkCenterFilters,
    WorkCenterRead,
    WorkCenterUpdate,
    break_problem,
)

log = get_logger(__name__)

AUDIT_WORK_CENTER = "work_center"
AUDIT_MACHINE = "machine"
AUDIT_ROUTING = "routing"
AUDIT_SHIFT = "shift"
AUDIT_SHIFT_ASSIGNMENT = "shift_assignment"

#: The advisory-lock key every roster write takes first - and every retirement
#: or move that asks "is this still on the roster?", so the answer cannot change
#: between the question and the write. Per company, not per cell: one operator
#: can be planned in any cell, so two cells' writes can conflict.
ROSTER_LOCK = "shift_roster"

MINUTES_PER_HOUR = Decimal("60")

#: The role that may approve its own work. Named rather than inlined so the
#: segregation-of-duties rule is one editable decision, and identical to the one
#: the BOM module uses.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"

_WORK_CENTER_AUDITED_FIELDS = [
    "code",
    "name",
    "description",
    "plant_id",
    "department_id",
    "cost_centre_id",
    "capacity_per_hour",
    "cost_per_hour",
    "setup_cost",
    "working_hours_per_day",
    "is_active",
]

_MACHINE_AUDITED_FIELDS = [
    "code",
    "name",
    "work_center_id",
    "machine_type",
    "capacity_per_hour",
    "cost_per_hour",
    "status",
    "last_maintenance_date",
    "next_maintenance_date",
    "is_active",
]

_ROUTING_AUDITED_FIELDS = [
    "code",
    "name",
    "item_id",
    "version",
    "status",
    "is_default",
    "effective_from",
    "effective_to",
    "notes",
]

_SHIFT_AUDITED_FIELDS = [
    "code",
    "name",
    "start_time",
    "end_time",
    "crosses_midnight",
    "break_minutes",
    "sort_order",
    "is_active",
]

_ASSIGNMENT_AUDITED_FIELDS = [
    "roster_date",
    "shift_id",
    "work_center_id",
    "machine_id",
    "operator_user_id",
    "starts_at",
    "ends_at",
    "status",
    "notes",
]


# -----------------------------------------------------------------------------
# State machines - the only things permitted to move a status
# -----------------------------------------------------------------------------


class MasterLifecycleState(StrEnum):
    """Lifecycle of a work-centre master row.

    There is no ``status`` column: the state is derived from ``is_active`` and
    ``deleted_at``, the two flags the rest of the system already reads. A third,
    redundant column could disagree with them. Routing the moves through a
    :class:`StateMachine` still gives one place where the legal transitions are
    declared, tested and rendered for the UI (master rules section 18).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


WORK_CENTER_LIFECYCLE: StateMachine[MasterLifecycleState] = StateMachine(
    entity=WORK_CENTER_ENTITY,
    initial=MasterLifecycleState.ACTIVE,
    transitions=(
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.INACTIVE,
            permission=Permission.WORK_CENTER_MANAGE,
            label="Deactivate",
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ACTIVE,
            permission=Permission.WORK_CENTER_MANAGE,
            label="Activate",
        ),
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.WORK_CENTER_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.WORK_CENTER_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        # A restored centre comes back INACTIVE, never ACTIVE: someone must look
        # at its rate and capacity before routings can plan work at it again.
        Transition(
            MasterLifecycleState.ARCHIVED,
            MasterLifecycleState.INACTIVE,
            permission=Permission.WORK_CENTER_MANAGE,
            label="Restore",
        ),
    ),
)


MACHINE_AVAILABILITY: StateMachine[MachineStatus] = StateMachine(
    entity="Machine",
    initial=MachineStatus.AVAILABLE,
    transitions=(
        Transition(
            MachineStatus.AVAILABLE,
            MachineStatus.RUNNING,
            permission=Permission.MACHINE_MANAGE,
            label="Start job",
        ),
        Transition(
            MachineStatus.RUNNING,
            MachineStatus.AVAILABLE,
            permission=Permission.MACHINE_MANAGE,
            label="Job finished",
        ),
        # A breakdown can happen at any moment, including mid-cut, so it is
        # reachable from both working states.
        Transition(
            MachineStatus.RUNNING,
            MachineStatus.BREAKDOWN,
            permission=Permission.MACHINE_MANAGE,
            label="Report breakdown",
        ),
        Transition(
            MachineStatus.AVAILABLE,
            MachineStatus.BREAKDOWN,
            permission=Permission.MACHINE_MANAGE,
            label="Report breakdown",
        ),
        Transition(
            MachineStatus.AVAILABLE,
            MachineStatus.MAINTENANCE,
            permission=Permission.MACHINE_MANAGE,
            label="Start planned maintenance",
        ),
        # Deliberately no RUNNING -> MAINTENANCE edge: a machine mid-job is
        # stopped first, so the job's end is recorded before the downtime starts.
        Transition(
            MachineStatus.BREAKDOWN,
            MachineStatus.MAINTENANCE,
            permission=Permission.MACHINE_MANAGE,
            label="Start repair",
        ),
        Transition(
            MachineStatus.MAINTENANCE,
            MachineStatus.AVAILABLE,
            permission=Permission.MACHINE_MANAGE,
            label="Maintenance complete",
        ),
        # Retiring is the end of the line: production entries still name the
        # machine, so the row survives, but it can never take work again.
        Transition(
            MachineStatus.AVAILABLE,
            MachineStatus.RETIRED,
            permission=Permission.MACHINE_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MachineStatus.MAINTENANCE,
            MachineStatus.RETIRED,
            permission=Permission.MACHINE_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MachineStatus.BREAKDOWN,
            MachineStatus.RETIRED,
            permission=Permission.MACHINE_MANAGE,
            label="Scrap beyond repair",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({MachineStatus.RETIRED}),
)


ROUTING_LIFECYCLE: StateMachine[RoutingStatus] = StateMachine(
    entity="Routing",
    initial=RoutingStatus.DRAFT,
    transitions=(
        Transition(
            RoutingStatus.DRAFT,
            RoutingStatus.APPROVED,
            permission=Permission.ROUTING_MANAGE,
            label="Approve",
            # Approval releases the process to planning and production, and is
            # the point from which it can no longer be edited.
            is_irreversible=True,
        ),
        Transition(
            RoutingStatus.APPROVED,
            RoutingStatus.OBSOLETE,
            permission=Permission.ROUTING_MANAGE,
            label="Make obsolete",
            is_irreversible=True,
        ),
        # A draft that was never approved can be abandoned without leaving a
        # half-finished process in the engineering list forever.
        Transition(
            RoutingStatus.DRAFT,
            RoutingStatus.OBSOLETE,
            permission=Permission.ROUTING_MANAGE,
            label="Abandon",
        ),
    ),
    terminal=frozenset({RoutingStatus.OBSOLETE}),
)


SHIFT_LIFECYCLE: StateMachine[MasterLifecycleState] = StateMachine(
    entity=SHIFT_ENTITY,
    initial=MasterLifecycleState.ACTIVE,
    transitions=(
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.INACTIVE,
            permission=Permission.SHIFT_MANAGE,
            label="Deactivate",
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ACTIVE,
            permission=Permission.SHIFT_MANAGE,
            label="Activate",
        ),
        Transition(
            MasterLifecycleState.ACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.SHIFT_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
        Transition(
            MasterLifecycleState.INACTIVE,
            MasterLifecycleState.ARCHIVED,
            permission=Permission.SHIFT_MANAGE,
            label="Retire",
            is_irreversible=True,
        ),
    ),
    # No restore, unlike a work centre: a retired shift's code is immutable and
    # already on history, so "we need it back" is answered by a new shift.
    terminal=frozenset({MasterLifecycleState.ARCHIVED}),
)


SHIFT_ASSIGNMENT_FLOW: StateMachine[ShiftAssignmentStatus] = StateMachine(
    entity=SHIFT_ASSIGNMENT_ENTITY,
    initial=ShiftAssignmentStatus.PLANNED,
    transitions=(
        # A cancelled plan is kept and never revived: planning the window again
        # is a new row, so the history shows the change of mind.
        Transition(
            ShiftAssignmentStatus.PLANNED,
            ShiftAssignmentStatus.CANCELLED,
            permission=Permission.SHIFT_ROSTER_MANAGE,
            label="Cancel",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({ShiftAssignmentStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Pure rule helpers - unit tested without a database
# -----------------------------------------------------------------------------


class OperationTimings(Protocol):
    """The four time components any timing calculation needs.

    A protocol rather than a concrete type so the same functions serve the ORM
    row, the costed projection and a test's throwaway stub, without any of them
    having to import the others. The members are read-only properties rather
    than plain attributes because a frozen dataclass does not satisfy a protocol
    that declares them settable - and these calculations must never write back
    to what they are given.
    """

    @property
    def setup_time_minutes(self) -> Decimal: ...

    @property
    def run_time_per_unit_minutes(self) -> Decimal: ...

    @property
    def queue_time_minutes(self) -> Decimal: ...

    @property
    def move_time_minutes(self) -> Decimal: ...


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Segregation of duties on approval.

    ASSUMPTION: the approver of a routing may not be the person who created it,
    unless they hold ``SUPER_ADMIN`` (or are a platform superuser). Approval is
    evidence of a second person's judgement (master rules section 42), and a
    process approved by its own author is not evidence of anything. Same rule and
    same escape hatch as the BOM module, so a two-person engineering team relaxes
    both in one place.
    """
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


def is_editable(status: RoutingStatus) -> bool:
    """Only a draft may be edited in place; everything else is a new version."""
    return status is RoutingStatus.DRAFT


def effectivity_contains(routing: Routing, on: date) -> bool:
    """Whether ``on`` falls inside the routing's effectivity window."""
    if on < routing.effective_from:
        return False
    return routing.effective_to is None or on <= routing.effective_to


def calculate_operation_time(operation: OperationTimings, quantity: Decimal) -> Decimal:
    """Elapsed minutes for one operation at ``quantity`` units.

    ``setup + run_time_per_unit x quantity + queue + move``. Setup is charged
    once per run, not per unit - that is the whole reason a bigger batch has a
    lower cost per piece - and queue and move are fixed waits that do not scale
    with the quantity either.
    """
    if quantity < 0:
        raise ValueError("Quantity cannot be negative.")
    return to_quantity(
        operation.setup_time_minutes
        + operation.run_time_per_unit_minutes * quantity
        + operation.queue_time_minutes
        + operation.move_time_minutes
    )


def chargeable_operation_time(operation: OperationTimings, quantity: Decimal) -> Decimal:
    """The part of the elapsed time the work centre is actually paid for.

    ASSUMPTION: queue and move time are elapsed but not charged. Nobody is
    working on the part while it waits in front of a machine or travels between
    cells, so charging the work-centre rate for that time would inflate the
    standard cost of every part that crosses more than one centre. The elapsed
    figure is still returned beside this one, so the difference is visible rather
    than hidden inside a single number.
    """
    if quantity < 0:
        raise ValueError("Quantity cannot be negative.")
    return to_quantity(
        operation.setup_time_minutes + operation.run_time_per_unit_minutes * quantity
    )


def calculate_operation_cost(operation: CostedOperation, quantity: Decimal) -> Decimal:
    """Cost of one operation: work-centre rate x charged hours, plus process cost.

    The work centre's ``setup_cost`` is deliberately absent: the setup *time* is
    already charged at ``cost_per_hour`` above, and adding the flat charge as
    well would count one changeover twice. Production-order costing applies that
    flat charge once per released run, where it belongs.
    """
    hours = chargeable_operation_time(operation, quantity) / MINUTES_PER_HOUR
    return to_money(hours * operation.work_center_cost_per_hour + operation.process_cost)


def calculate_routing_cost(routing: CostedRouting, quantity: Decimal) -> Decimal:
    """Total cost of running ``routing`` for ``quantity`` units.

    The total is the sum of the *rounded* per-operation costs rather than the
    rounding of an unrounded sum, so the breakdown a user reads always adds up to
    the total printed under it - the same convention invoicing uses.
    """
    return sum(
        (calculate_operation_cost(operation, quantity) for operation in routing.operations),
        start=Decimal("0"),
    )


def total_routing_minutes(routing: CostedRouting, quantity: Decimal) -> Decimal:
    """Elapsed minutes for the whole routing.

    ASSUMPTION: operations run strictly one after another, with no overlap
    between them. Real shops do overlap a long run with the next setup, but
    modelling that needs a transfer-batch size the routing does not carry, and
    the sequential figure is the conservative one - it never promises a date the
    plant cannot hit.
    """
    return to_quantity(
        sum(
            (calculate_operation_time(operation, quantity) for operation in routing.operations),
            start=Decimal("0"),
        )
    )


def resolve_working_hours_per_day(routing: CostedRouting) -> Decimal:
    """The shift length to schedule this routing against.

    ASSUMPTION: the *shortest* working day among the work centres involved. If
    grinding runs 16 hours a day but plating only 8, the plating shift is what
    the job actually waits for, and taking the longest (or an average) would
    quietly promise a date the plant cannot hit.
    """
    hours = [
        operation.work_center_working_hours_per_day
        for operation in routing.operations
        if operation.work_center_working_hours_per_day > 0
    ]
    return min(hours) if hours else DEFAULT_WORKING_HOURS_PER_DAY


def total_lead_time_days(
    routing: CostedRouting,
    quantity: Decimal,
    working_hours_per_day: Decimal | None = None,
) -> int:
    """Whole calendar days of shop time this routing needs for ``quantity``.

    This is the manufacture lead time MRP subtracts from a required date to get
    a suggested order date, which is why the result is rounded **up** to a whole
    day: a job needing 8.1 hours of an 8-hour day still occupies a second day,
    and scheduling it as "one day" would release the order too late every single
    time. Rounding down or to nearest is never right for a lead time - the error
    is not symmetric, because finishing early costs storage while finishing late
    misses a customer promise.

    ``working_hours_per_day`` defaults to :func:`resolve_working_hours_per_day`.
    """
    minutes = total_routing_minutes(routing, quantity)
    if minutes <= 0:
        return 0

    if working_hours_per_day is None:
        working_hours_per_day = resolve_working_hours_per_day(routing)
    elif working_hours_per_day <= 0:
        raise ValueError("working_hours_per_day must be greater than zero.")

    hours = minutes / MINUTES_PER_HOUR
    return int((hours / working_hours_per_day).to_integral_value(rounding=ROUND_CEILING))


def cost_breakdown(
    routing: CostedRouting,
    quantity: Decimal,
    *,
    working_hours_per_day: Decimal | None = None,
) -> RoutingCostRead:
    """Per-operation costing plus the lead time it implies, in one pass."""
    lines: list[OperationCostRead] = []
    for operation in routing.operations:
        charged = chargeable_operation_time(operation, quantity)
        time_cost = to_money(charged / MINUTES_PER_HOUR * operation.work_center_cost_per_hour)
        total = calculate_operation_cost(operation, quantity)
        lines.append(
            OperationCostRead(
                sequence=operation.sequence,
                operation_code=operation.operation_code,
                operation_name=operation.operation_name,
                work_center_id=operation.work_center_id,
                total_minutes=calculate_operation_time(operation, quantity),
                charged_minutes=charged,
                work_center_cost_per_hour=operation.work_center_cost_per_hour,
                time_cost=time_cost,
                process_cost=to_money(operation.process_cost),
                total_cost=total,
                is_outsourced=operation.is_outsourced,
            )
        )

    total_cost = sum((line.total_cost for line in lines), start=Decimal("0"))
    hours_per_day = working_hours_per_day or resolve_working_hours_per_day(routing)
    return RoutingCostRead(
        routing_id=routing.id,
        routing_code=routing.code,
        item_id=routing.item_id,
        quantity=quantity,
        total_minutes=total_routing_minutes(routing, quantity),
        charged_minutes=to_quantity(
            sum((line.charged_minutes for line in lines), start=Decimal("0"))
        ),
        time_cost=sum((line.time_cost for line in lines), start=Decimal("0")),
        process_cost=sum((line.process_cost for line in lines), start=Decimal("0")),
        total_cost=total_cost,
        # Quantity is validated positive by the schema, so this cannot divide by
        # zero; it is money per unit, so it keeps money scale.
        cost_per_unit=to_money(total_cost / quantity),
        working_hours_per_day=hours_per_day,
        lead_time_days=total_lead_time_days(routing, quantity, working_hours_per_day=hours_per_day),
        operations=lines,
    )


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot retire this, something still points at it" error.

    One constructor so every such refusal carries the same machine-readable code
    and the frontend can react to it uniformly.
    """
    return BusinessRuleError(
        message, code=ErrorCode.REFERENCED_RECORD_IN_USE, details=details or None
    )


class ShiftTimes(Protocol):
    """What a window calculation reads off a shift.

    A protocol for the same reason :class:`OperationTimings` is one: the ORM row
    and a test's stub both satisfy it, and the calculation cannot write back.
    """

    @property
    def start_time(self) -> time: ...

    @property
    def end_time(self) -> time: ...

    @property
    def break_minutes(self) -> int: ...


def net_shift_minutes(shift: ShiftTimes) -> int:
    """Working minutes in one shift: its length less its break.

    What rostered capacity counts. The break stays inside the window - the
    operator is still on the floor and the window still blocks a second plan.
    """
    return shift_minutes(shift.start_time, shift.end_time) - shift.break_minutes


def local_date(moment: datetime, zone: ZoneInfo) -> date:
    """The calendar date a UTC instant falls on where the company is."""
    return moment.astimezone(zone).date()


def shift_window(
    roster_date: date, shift: ShiftTimes, zone: ZoneInfo | str
) -> tuple[datetime, datetime]:
    """The UTC instants one shift occupies when it starts on ``roster_date``.

    ASSUMPTION: ``roster_date`` is the LOCAL date the shift starts on, and the
    shift's times are wall-clock times in the company timezone; a shift that
    crosses midnight ends on the next local date. So the night shift 22:00-06:00
    planned on 11 October in Asia/Kolkata runs from 16:30 UTC on the 11th to
    00:30 UTC on the 12th. docs/assumptions.md §B2-5.

    A local time that a daylight-saving change skips or repeats resolves as
    Python's ``zoneinfo`` resolves it (the first occurrence); India observes
    none, which is the case this was written for.
    """
    tz = zone if isinstance(zone, ZoneInfo) else resolve_zone(zone)
    starts = datetime.combine(roster_date, shift.start_time, tzinfo=tz)
    end_date = roster_date
    if shift_crosses_midnight(shift.start_time, shift.end_time):
        end_date = roster_date + timedelta(days=1)
    ends = datetime.combine(end_date, shift.end_time, tzinfo=tz)
    return starts.astimezone(UTC), ends.astimezone(UTC)


def windows_overlap(first: tuple[datetime, datetime], second: tuple[datetime, datetime]) -> bool:
    """Whether two half-open ``[start, end)`` windows share any instant.

    Half-open is the point: A (06:00-14:00) and B (14:00-22:00) are back to back,
    not overlapping, exactly as the ``'[)'`` ranges of the EXCLUDE constraints
    read them.
    """
    return first[0] < second[1] and second[0] < first[1]


def resolve_zone(name: str) -> ZoneInfo:
    """The company's timezone, or a refusal a person can act on.

    The organisation schema validates the name when it is saved, but only where
    a tz database is installed; a name this server cannot resolve must not
    surface on the shop floor as a 500.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BusinessRuleError(
            f"The company timezone '{name}' is not known to this server, so shift times "
            "cannot be placed on the calendar. Correct it in the organisation settings.",
            details={"timezone": name},
        ) from exc


# -----------------------------------------------------------------------------
# Work centres
# -----------------------------------------------------------------------------


class WorkCenterService:
    """Work-centre master data. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = WorkCenterRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_work_centers(
        self, filters: WorkCenterFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[WorkCenterRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [WorkCenterRead.model_validate(row) for row in rows], pagination

    async def get_work_center(self, work_center_id: uuid.UUID) -> WorkCenterRead:
        return WorkCenterRead.model_validate(await self.repo.get_or_404(work_center_id))

    # --- writes -------------------------------------------------------------

    async def create_work_center(self, payload: WorkCenterCreate) -> WorkCenterRead:
        async with transaction(self.session):
            await self._assert_code_free(payload.code)
            await self._assert_organisation_refs(payload.plant_id, payload.department_id)
            if payload.cost_centre_id is not None:
                await require_cost_centre(self.session, self.tenant, payload.cost_centre_id)

            work_center = WorkCenter(**payload.model_dump())
            self.repo.add(work_center)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_WORK_CENTER,
                entity_id=work_center.id,
                entity_code=work_center.code,
                summary=f"Created work centre {work_center.code} ({work_center.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(work_center, _WORK_CENTER_AUDITED_FIELDS),
            )

        return WorkCenterRead.model_validate(work_center)

    async def update_work_center(
        self, work_center_id: uuid.UUID, payload: WorkCenterUpdate
    ) -> WorkCenterRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            work_center = await self.repo.get_or_404(work_center_id)
            before = snapshot(work_center, _WORK_CENTER_AUDITED_FIELDS)

            if changes.get("code") and changes["code"] != work_center.code:
                await self._assert_code_free(changes["code"], exclude_id=work_center.id)
            await self._assert_organisation_refs(
                changes.get("plant_id"), changes.get("department_id")
            )
            # Only a new assignment is validated; re-sending the centre already
            # held is not one (and the deactivation guard keeps it active).
            new_centre = changes.get("cost_centre_id")
            if new_centre is not None and new_centre != work_center.cost_centre_id:
                await require_cost_centre(self.session, self.tenant, new_centre)

            # A rate change moves the cost of every approved routing that uses
            # this centre, which is the intended standard-costing behaviour but
            # is worth being able to find in the log afterwards.
            for field, value in changes.items():
                setattr(work_center, field, value)
            self.repo.touch(work_center)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_WORK_CENTER,
                entity_id=work_center.id,
                entity_code=work_center.code,
                summary=f"Updated work centre {work_center.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(work_center, _WORK_CENTER_AUDITED_FIELDS),
            )

        return WorkCenterRead.model_validate(work_center)

    async def set_active(
        self, work_center_id: uuid.UUID, *, active: bool, reason: str
    ) -> WorkCenterRead:
        """Switch a work centre on or off through the lifecycle state machine."""
        target = MasterLifecycleState.ACTIVE if active else MasterLifecycleState.INACTIVE

        async with transaction(self.session):
            work_center = await self.repo.get_or_404(work_center_id)
            current = lifecycle_state(work_center)
            WORK_CENTER_LIFECYCLE.assert_transition(current, target)

            work_center.is_active = active
            self.repo.touch(work_center)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_WORK_CENTER,
                entity_id=work_center.id,
                entity_code=work_center.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return WorkCenterRead.model_validate(work_center)

    async def retire_work_center(self, work_center_id: uuid.UUID, *, reason: str) -> None:
        """Soft-delete a work centre that nothing live still depends on.

        Refused while machines sit in it or a draft/approved routing still plans
        work there: the routing would keep resolving to a centre that no longer
        appears in any list, and its cost roll-up would silently keep using the
        retired rate.
        """
        async with transaction(self.session):
            work_center = await self.repo.get_or_404(work_center_id)
            current = lifecycle_state(work_center)
            WORK_CENTER_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            counts = await self.repo.machine_counts([work_center.id])
            if counts.get(work_center.id, 0):
                raise referenced_in_use(
                    f"{work_center.code} still has {counts[work_center.id]} machine(s). "
                    "Move or retire them first.",
                    work_center_code=work_center.code,
                    machine_count=counts[work_center.id],
                )
            if await self.repo.used_by_live_routing(work_center.id):
                raise referenced_in_use(
                    f"{work_center.code} is still used by a draft or approved routing.",
                    work_center_code=work_center.code,
                )

            centre_id = work_center.id
            await _assert_off_the_roster(
                self.session,
                self.tenant,
                label=f"Work centre {work_center.code}",
                planned_from=lambda day: self.repo.used_by_live_roster(centre_id, from_date=day),
                action="retired",
            )
            code = work_center.code
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_WORK_CENTER,
                entity_id=work_center.id,
                entity_code=code,
                from_status=str(current),
                to_status=str(MasterLifecycleState.ARCHIVED),
                user_id=self.tenant.user_id,
                action=AuditAction.DELETE,
                reason=reason,
            )
            work_center.is_active = False
            await self.repo.soft_delete(work_center)

    # --- helpers ------------------------------------------------------------

    async def _assert_code_free(self, code: str, *, exclude_id: uuid.UUID | None = None) -> None:
        if await self.repo.code_taken(code, exclude_id=exclude_id):
            raise DuplicateError(
                f"Work centre code '{code}' is already used in this company.",
                details={"code": code},
            )

    async def _assert_organisation_refs(
        self, plant_id: uuid.UUID | None, department_id: uuid.UUID | None
    ) -> None:
        plant_ok, department_ok = await self.repo.organisation_refs_exist(
            plant_id=plant_id, department_id=department_id
        )
        if not plant_ok:
            raise NotFoundError("Plant", plant_id)
        if not department_ok:
            raise NotFoundError("Department", department_id)


def lifecycle_state(work_center: WorkCenter) -> MasterLifecycleState:
    """Derive the lifecycle state of a work centre from its flags."""
    if work_center.deleted_at is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if work_center.is_active else MasterLifecycleState.INACTIVE


# -----------------------------------------------------------------------------
# Machines
# -----------------------------------------------------------------------------


class MachineService:
    """Machine master data and shop-floor availability."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = MachineRepository(session, tenant)
        self.work_centers = WorkCenterRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_machines(
        self, filters: MachineFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[MachineRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [MachineRead.model_validate(row) for row in rows], pagination

    async def get_machine(self, machine_id: uuid.UUID) -> MachineRead:
        return MachineRead.model_validate(await self.repo.get_or_404(machine_id))

    # --- writes -------------------------------------------------------------

    async def create_machine(self, payload: MachineCreate) -> MachineRead:
        async with transaction(self.session):
            await self._assert_code_free(payload.code)
            await self._assert_work_center_usable(payload.work_center_id)

            machine = Machine(**payload.model_dump(), status=MachineStatus.AVAILABLE)
            self.repo.add(machine)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_MACHINE,
                entity_id=machine.id,
                entity_code=machine.code,
                summary=f"Created machine {machine.code} ({machine.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(machine, _MACHINE_AUDITED_FIELDS),
            )

        return MachineRead.model_validate(machine)

    async def update_machine(self, machine_id: uuid.UUID, payload: MachineUpdate) -> MachineRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            machine = await self.repo.get_or_404(machine_id)
            before = snapshot(machine, _MACHINE_AUDITED_FIELDS)

            if changes.get("code") and changes["code"] != machine.code:
                await self._assert_code_free(changes["code"], exclude_id=machine.id)

            new_work_center = changes.get("work_center_id")
            if new_work_center is not None and new_work_center != machine.work_center_id:
                await self._assert_not_on_roster_history(machine)
                await self._assert_work_center_usable(new_work_center)
                # Moving a machine between centres would break the composite
                # foreign key on every operation that names it there, so the
                # move is refused while any live routing still points at it.
                if await self.repo.used_by_live_routing(machine.id):
                    raise referenced_in_use(
                        f"{machine.code} is named on a draft or approved routing operation "
                        "and cannot be moved to another work centre.",
                        machine_code=machine.code,
                    )

            for field, value in changes.items():
                setattr(machine, field, value)
            self.repo.touch(machine)
            self._assert_maintenance_dates_ordered(machine)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_MACHINE,
                entity_id=machine.id,
                entity_code=machine.code,
                summary=f"Updated machine {machine.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(machine, _MACHINE_AUDITED_FIELDS),
            )

        return MachineRead.model_validate(machine)

    async def change_status(
        self, machine_id: uuid.UUID, payload: MachineStatusChange
    ) -> MachineRead:
        """Move a machine to another availability state.

        Completing maintenance stamps ``last_maintenance_date``: that transition
        *is* the record of the service having happened, and asking the fitter to
        type the date afterwards is how a maintenance history goes stale.
        """
        async with transaction(self.session):
            machine = await self.repo.get_or_404(machine_id)
            current = MachineStatus(machine.status)
            MACHINE_AVAILABILITY.assert_transition(current, payload.status)

            if current is MachineStatus.MAINTENANCE and payload.status is MachineStatus.AVAILABLE:
                machine.last_maintenance_date = today()
            if payload.next_maintenance_date is not None:
                machine.next_maintenance_date = payload.next_maintenance_date
            if payload.status is MachineStatus.RETIRED:
                await self._assert_off_the_roster(machine, "retired")
                # A retired machine leaves the selectable master list too, so a
                # routing cannot be pointed at it tomorrow.
                machine.is_active = False

            machine.status = payload.status
            self.repo.touch(machine)
            self._assert_maintenance_dates_ordered(machine)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_MACHINE,
                entity_id=machine.id,
                entity_code=machine.code,
                from_status=str(current),
                to_status=str(payload.status),
                user_id=self.tenant.user_id,
                reason=payload.reason,
            )

        return MachineRead.model_validate(machine)

    async def retire_machine(self, machine_id: uuid.UUID, *, reason: str) -> None:
        """Retire and soft-delete a machine no live routing still names.

        Status and record retirement happen together: a soft-deleted machine
        left ``AVAILABLE`` would still read as usable to anything that looks at
        the status column alone.
        """
        async with transaction(self.session):
            machine = await self.repo.get_or_404(machine_id)
            current = MachineStatus(machine.status)
            MACHINE_AVAILABILITY.assert_transition(current, MachineStatus.RETIRED)

            if await self.repo.used_by_live_routing(machine.id):
                raise referenced_in_use(
                    f"{machine.code} is named on a draft or approved routing operation.",
                    machine_code=machine.code,
                )

            await self._assert_off_the_roster(machine, "retired")
            code = machine.code
            machine.status = MachineStatus.RETIRED
            machine.is_active = False
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_MACHINE,
                entity_id=machine.id,
                entity_code=code,
                from_status=str(current),
                to_status=str(MachineStatus.RETIRED),
                user_id=self.tenant.user_id,
                action=AuditAction.DELETE,
                reason=reason,
            )
            await self.repo.soft_delete(machine)

    # --- helpers ------------------------------------------------------------

    async def _assert_code_free(self, code: str, *, exclude_id: uuid.UUID | None = None) -> None:
        if await self.repo.code_taken(code, exclude_id=exclude_id):
            raise DuplicateError(
                f"Machine code '{code}' is already used in this company.",
                details={"code": code},
            )

    async def _assert_off_the_roster(self, machine: Machine, action: str) -> None:
        machine_id = machine.id
        await _assert_off_the_roster(
            self.session,
            self.tenant,
            label=f"Machine {machine.code}",
            planned_from=lambda day: self.repo.used_by_live_roster(machine_id, from_date=day),
            action=action,
        )

    async def _assert_not_on_roster_history(self, machine: Machine) -> None:
        """Refuse to move a machine any roster row names, past or cancelled included.

        Those rows record the machine at its current centre, and the composite
        foreign key keeps them true - so the move would fail in the database with a
        message nobody could act on. The roster lock is taken first so no plan can
        be added between this answer and the move.
        """
        await advisory_xact_lock(self.session, ROSTER_LOCK, self.tenant.company_id)
        if await self.repo.named_on_roster(machine.id):
            raise referenced_in_use(
                f"Machine {machine.code} appears on the shift roster at its current work "
                "centre and cannot be moved: the roster history would no longer match. "
                "Retire it and add the machine to the other centre as a new one.",
                machine_code=machine.code,
            )

    async def _assert_work_center_usable(self, work_center_id: uuid.UUID) -> None:
        rates = await self.work_centers.rates([work_center_id])
        work_center = rates.get(work_center_id)
        if work_center is None:
            raise NotFoundError(WORK_CENTER_ENTITY, work_center_id)
        if not work_center.is_active:
            raise BusinessRuleError(
                f"Work centre {work_center.code} is inactive and cannot take machines.",
                details={"work_center_code": work_center.code},
            )

    @staticmethod
    def _assert_maintenance_dates_ordered(machine: Machine) -> None:
        # Also a CHECK constraint; raised here so the message names the dates
        # instead of surfacing an integrity error as a generic 409.
        if (
            machine.last_maintenance_date is not None
            and machine.next_maintenance_date is not None
            and machine.next_maintenance_date < machine.last_maintenance_date
        ):
            raise BusinessRuleError(
                "The next service cannot fall before the last one.",
                details={
                    "last_maintenance_date": machine.last_maintenance_date.isoformat(),
                    "next_maintenance_date": machine.next_maintenance_date.isoformat(),
                },
            )


# -----------------------------------------------------------------------------
# Routings
# -----------------------------------------------------------------------------


class RoutingService:
    """Routing versioning, approval, costing and lead time."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = RoutingRepository(session, tenant)
        self.operations = RoutingOperationRepository(session, tenant)
        self.work_centers = WorkCenterRepository(session, tenant)
        self.machines = MachineRepository(session, tenant)
        self.items = ItemLookupRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_routings(
        self, filters: RoutingFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[RoutingSummary], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [RoutingSummary.model_validate(row) for row in rows], pagination

    async def list_versions(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[list[RoutingSummary], OffsetPagination]:
        rows, pagination = await self.repo.list_page(
            params, stmt=self.repo.versions_stmt(item_id), order_by=Routing.version.desc()
        )
        return [RoutingSummary.model_validate(row) for row in rows], pagination

    async def get_routing_detail(self, routing_id: uuid.UUID) -> RoutingDetail:
        routing = await self.repo.get_or_404(routing_id)
        operations = await self.operations.list_for_routing(routing.id)
        return _detail(routing, operations)

    async def list_operations(self, routing_id: uuid.UUID) -> list[RoutingOperationRead]:
        # get_or_404 first: an unknown routing must 404, not answer with [].
        routing = await self.repo.get_or_404(routing_id)
        operations = await self.operations.list_for_routing(routing.id)
        return [RoutingOperationRead.model_validate(op) for op in operations]

    async def routing_cost(self, routing_id: uuid.UUID, quantity: Decimal) -> RoutingCostRead:
        """Cost and lead time for a quantity, recomputed live and never stored.

        Nothing is written back: opening a costing screen must not mutate an
        approved document, and the work-centre rates it reads are current by
        definition.
        """
        routing = await self.repo.get_or_404(routing_id)
        costed = await self._costed(routing)
        return cost_breakdown(costed, quantity)

    async def item_lead_time(
        self, item_id: uuid.UUID, quantity: Decimal, *, as_of: date | None = None
    ) -> LeadTimeRead:
        """Manufacture lead time for an item - the MRP-facing read.

        Answers with ``routing_id=None`` rather than raising when the item has no
        effective approved routing: MRP then falls back to ``items.lead_time_days``
        instead of treating a missing process as zero days.
        """
        on = as_of or today()
        routings = await self.repo.effective_routings_for_items([item_id], on)
        routing = routings.get(item_id)
        if routing is None:
            return LeadTimeRead(
                item_id=item_id,
                quantity=quantity,
                routing_id=None,
                routing_code=None,
                total_minutes=Decimal("0"),
                working_hours_per_day=DEFAULT_WORKING_HOURS_PER_DAY,
                lead_time_days=0,
            )

        costed = await self._costed(routing)
        hours_per_day = resolve_working_hours_per_day(costed)
        return LeadTimeRead(
            item_id=item_id,
            quantity=quantity,
            routing_id=routing.id,
            routing_code=routing.code,
            total_minutes=total_routing_minutes(costed, quantity),
            working_hours_per_day=hours_per_day,
            lead_time_days=total_lead_time_days(
                costed, quantity, working_hours_per_day=hours_per_day
            ),
        )

    # --- writes -------------------------------------------------------------

    async def create_routing(self, payload: RoutingCreate) -> RoutingDetail:
        """Create a DRAFT routing with its operations."""
        async with transaction(self.session):
            await self._assert_code_free(payload.code)
            item = await self._load_manufacturable_item(payload.item_id)
            await self._validate_operations(payload.operations)

            version = await self._next_version(payload.item_id)
            routing = Routing(
                code=payload.code,
                name=payload.name,
                item_id=payload.item_id,
                version=version,
                status=RoutingStatus.DRAFT,
                is_default=payload.is_default,
                effective_from=payload.effective_from or today(),
                effective_to=payload.effective_to,
                notes=payload.notes,
            )
            self.repo.add(routing)
            await self.session.flush()

            operations = await self._persist_operations(routing, payload.operations)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ROUTING,
                entity_id=routing.id,
                entity_code=routing.code,
                summary=(
                    f"Created routing {routing.code} v{routing.version} for {item.code} "
                    f"with {len(operations)} operation(s)"
                ),
                user_id=self.tenant.user_id,
                new_values={
                    **snapshot(routing, _ROUTING_AUDITED_FIELDS),
                    "operation_count": len(operations),
                },
            )

        return _detail(routing, operations)

    async def update_routing(self, routing_id: uuid.UUID, payload: RoutingUpdate) -> RoutingDetail:
        """Edit a DRAFT routing. Operations are replaced wholesale when supplied."""
        async with transaction(self.session):
            routing = await self.repo.get_or_404(routing_id)
            if not is_editable(RoutingStatus(routing.status)):
                raise BusinessRuleError(
                    f"{routing.code} is {routing.status} and can no longer be edited. "
                    "Create a new version instead, so orders planned against this one "
                    "still resolve.",
                    details={"routing_id": str(routing.id), "status": str(routing.status)},
                )

            before = snapshot(routing, _ROUTING_AUDITED_FIELDS)
            changes = payload.model_dump(exclude_unset=True, exclude={"operations"})

            if changes.get("code") and changes["code"] != routing.code:
                await self._assert_code_free(changes["code"], exclude_id=routing.id)

            # An explicit null for a NOT NULL column is dropped rather than
            # turned into an IntegrityError; ``exclude_unset`` already means an
            # omitted key leaves the stored value alone.
            for field, value in changes.items():
                if value is None and field in {"effective_from", "is_default"}:
                    continue
                setattr(routing, field, value)
            self.repo.touch(routing)

            if routing.effective_to is not None and routing.effective_to < routing.effective_from:
                raise BusinessRuleError(
                    "The effectivity window would end before it starts.",
                    details={
                        "effective_from": routing.effective_from.isoformat(),
                        "effective_to": routing.effective_to.isoformat(),
                    },
                )

            operations = await self.operations.list_for_routing(routing.id)
            if payload.operations is not None:
                await self._validate_operations(payload.operations)
                await self.operations.delete_for_routing(routing.id)
                operations = await self._persist_operations(routing, payload.operations)

            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ROUTING,
                entity_id=routing.id,
                entity_code=routing.code,
                summary=f"Updated routing {routing.code} v{routing.version}",
                user_id=self.tenant.user_id,
                old_values={**before, "operation_count": len(operations)},
                new_values={
                    **snapshot(routing, _ROUTING_AUDITED_FIELDS),
                    "operation_count": len(operations),
                },
            )

        return _detail(routing, operations)

    async def approve_routing(
        self, principal: Principal, routing_id: uuid.UUID, *, reason: str
    ) -> RoutingSummary:
        """Approve a routing and, if it is flagged default, promote it.

        Everything happens in one transaction: the demotion of the previous
        default, the status change and the audit rows. A partial result -
        approved but not default, or default twice - is exactly what the partial
        unique index and master rules section 29 exist to prevent.
        """
        async with transaction(self.session):
            routing = await self.repo.get_or_404(routing_id)

            if not can_approve(principal, routing.created_by_id):
                raise AuthorizationError(
                    "A routing must be approved by someone other than its author.",
                    details={
                        "routing_id": str(routing.id),
                        "created_by_id": str(routing.created_by_id),
                    },
                )

            # Validate the transition before doing any work, so a wrong-state
            # request costs one comparison rather than a full validation pass.
            ROUTING_LIFECYCLE.assert_transition(
                RoutingStatus(routing.status), RoutingStatus.APPROVED
            )

            operations = await self.operations.list_for_routing(routing.id)
            if not operations:
                raise BusinessRuleError(
                    f"{routing.code} has no operations, so approving it would release a "
                    "process that describes no work.",
                    details={"routing_id": str(routing.id)},
                )
            await self._assert_operations_still_valid(operations)

            if routing.is_default:
                # Demote first. The partial unique index is evaluated per
                # statement, so promoting before demoting is a valid end state
                # that still fails in the middle of the transaction.
                for existing in await self.repo.list_default_candidates(routing.item_id):
                    if existing.id == routing.id:
                        continue
                    existing.is_default = False
                    self.repo.touch(existing)
                    await record_audit(
                        self.session,
                        company_id=self.tenant.company_id,
                        action=AuditAction.UPDATE,
                        entity_type=AUDIT_ROUTING,
                        entity_id=existing.id,
                        entity_code=existing.code,
                        summary=(
                            f"{existing.code} is no longer the default routing; superseded "
                            f"by {routing.code} v{routing.version}"
                        ),
                        user_id=self.tenant.user_id,
                        old_values={"is_default": True},
                        new_values={"is_default": False},
                    )
                await self.session.flush()

            routing.approved_by_id = self.tenant.user_id
            routing.approved_at = utcnow()
            await self._transition(
                routing, RoutingStatus.APPROVED, reason=reason, action=AuditAction.APPROVE
            )

        return RoutingSummary.model_validate(routing)

    async def obsolete_routing(self, routing_id: uuid.UUID, *, reason: str) -> RoutingSummary:
        """Retire a routing.

        The default flag is cleared at the same time: an obsolete routing still
        flagged default would be invisible to the effective-routing query (which
        only looks at APPROVED rows) while still occupying the partial unique
        index slot, so its replacement could never be approved.
        """
        async with transaction(self.session):
            routing = await self.repo.get_or_404(routing_id)
            routing.is_default = False
            await self._transition(
                routing, RoutingStatus.OBSOLETE, reason=reason, action=AuditAction.CANCEL
            )
        return RoutingSummary.model_validate(routing)

    async def delete_draft_routing(self, routing_id: uuid.UUID, *, reason: str) -> None:
        """Physically remove a draft that was never approved.

        Hard deletion is safe here and only here: a DRAFT has never been
        released, so nothing outside engineering can reference it. Anything
        approved is retired with :meth:`obsolete_routing` instead - master rules
        section 28 forbids destroying a record other documents point at.
        """
        async with transaction(self.session):
            routing = await self.repo.get_or_404(routing_id)
            if RoutingStatus(routing.status) is not RoutingStatus.DRAFT:
                raise BusinessRuleError(
                    f"{routing.code} is {routing.status}. Only a draft can be deleted; "
                    "make it obsolete instead.",
                    details={"routing_id": str(routing.id), "status": str(routing.status)},
                )

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ROUTING,
                entity_id=routing.id,
                entity_code=routing.code,
                summary=f"Deleted draft routing {routing.code} v{routing.version}",
                user_id=self.tenant.user_id,
                old_values=snapshot(routing, _ROUTING_AUDITED_FIELDS),
                context={"reason": reason},
            )
            # Operations go with it through ON DELETE CASCADE rather than a
            # second round trip.
            await self.repo.hard_delete(routing)

    async def copy_as_new_version(
        self, routing_id: uuid.UUID, payload: CopyAsNewVersionRequest
    ) -> RoutingDetail:
        """Create the next DRAFT version from an existing routing.

        This is how an approved routing is "edited": the old version stays
        exactly as the orders raised against it remember it, and the new one has
        to be reviewed again.

        ASSUMPTION: the copy inherits ``is_default`` from its source. A revision
        is normally meant to replace the process it came from, and approving it
        then demotes the old default in the same transaction. The flag has no
        effect until approval - the unique index only covers APPROVED rows.
        """
        async with transaction(self.session):
            source = await self.repo.get_or_404(routing_id)
            source_operations = await self.operations.list_for_routing(source.id)
            if not source_operations:
                raise BusinessRuleError(
                    f"{source.code} has no operations to copy.",
                    details={"routing_id": str(source.id)},
                )

            await self._assert_code_free(payload.code)
            version = await self._next_version(source.item_id)

            copy = Routing(
                code=payload.code,
                name=payload.name if payload.name is not None else source.name,
                item_id=source.item_id,
                version=version,
                status=RoutingStatus.DRAFT,
                is_default=source.is_default,
                effective_from=payload.effective_from or today(),
                effective_to=payload.effective_to,
                notes=payload.notes if payload.notes is not None else source.notes,
            )
            self.repo.add(copy)
            await self.session.flush()

            operations = [
                RoutingOperation(
                    routing_id=copy.id,
                    sequence=operation.sequence,
                    operation_code=operation.operation_code,
                    operation_name=operation.operation_name,
                    work_center_id=operation.work_center_id,
                    machine_id=operation.machine_id,
                    setup_time_minutes=operation.setup_time_minutes,
                    run_time_per_unit_minutes=operation.run_time_per_unit_minutes,
                    queue_time_minutes=operation.queue_time_minutes,
                    move_time_minutes=operation.move_time_minutes,
                    process_cost=operation.process_cost,
                    is_outsourced=operation.is_outsourced,
                    job_work_vendor_id=operation.job_work_vendor_id,
                    qc_required=operation.qc_required,
                    notes=operation.notes,
                )
                for operation in source_operations
            ]
            for operation in operations:
                self.operations.add(operation)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ROUTING,
                entity_id=copy.id,
                entity_code=copy.code,
                summary=(
                    f"Created routing {copy.code} v{copy.version} as a copy of "
                    f"{source.code} v{source.version}"
                ),
                user_id=self.tenant.user_id,
                new_values={
                    **snapshot(copy, _ROUTING_AUDITED_FIELDS),
                    "operation_count": len(operations),
                    "copied_from_routing_id": str(source.id),
                },
            )

        return _detail(copy, operations)

    # --- helpers ------------------------------------------------------------

    async def _costed(self, routing: Routing) -> CostedRouting:
        operations = await self.operations.costed_operations(routing.id)
        return CostedRouting(
            id=routing.id,
            code=routing.code,
            item_id=routing.item_id,
            operations=tuple(operations),
        )

    async def _next_version(self, item_id: uuid.UUID) -> int:
        """Claim the next version number for an item's routing.

        ``MAX(version) + 1`` is only safe under a lock: two engineers creating a
        revision at the same moment would otherwise both read the same maximum
        and the second insert would fail on the unique constraint. The advisory
        lock is transaction-scoped, so it is released on commit or rollback with
        no unlock path to forget.
        """
        await advisory_xact_lock(self.session, "routing_version", self.tenant.company_id, item_id)
        return await self.repo.max_version_for_item(item_id) + 1

    async def _assert_code_free(self, code: str, *, exclude_id: uuid.UUID | None = None) -> None:
        if await self.repo.code_taken(code, exclude_id=exclude_id):
            raise DuplicateError(
                f"Routing code '{code}' is already used in this company.",
                details={"code": code},
            )

    async def _load_manufacturable_item(self, item_id: uuid.UUID) -> RoutingItemInfo:
        info = await self.items.load(item_id)
        if info is None:
            raise NotFoundError("Item", item_id)
        # Master rules section 1: read the capability flag, never item_group. An
        # item nobody can manufacture has no process to describe.
        if not info.can_manufacture:
            raise BusinessRuleError(
                f"{info.code} is not flagged as manufacturable, so it cannot have a "
                "routing. Enable 'can manufacture' on the item first.",
                details={"item_id": str(item_id), "item_code": info.code},
            )
        if not info.is_active:
            raise BusinessRuleError(
                f"{info.code} is inactive and cannot be given a routing.",
                details={"item_id": str(item_id), "item_code": info.code},
            )
        return info

    async def _validate_operations(self, operations: Sequence[RoutingOperationIn]) -> None:
        """Check every step's work centre and machine in two batched reads.

        A loop of single lookups here would be an N+1 on the write path, which is
        where a 40-step routing arrives in one request.
        """
        rates = await self.work_centers.rates([op.work_center_id for op in operations])
        machines = await self.machines.load(
            [op.machine_id for op in operations if op.machine_id is not None]
        )

        for operation in operations:
            centre = rates.get(operation.work_center_id)
            if centre is None:
                raise NotFoundError(WORK_CENTER_ENTITY, operation.work_center_id)
            if not centre.is_active:
                raise BusinessRuleError(
                    f"Operation {operation.sequence}: work centre {centre.code} is "
                    "inactive and cannot be planned against.",
                    details={
                        "sequence": operation.sequence,
                        "work_center_code": centre.code,
                    },
                )
            if operation.machine_id is not None:
                self._assert_machine_fits(
                    sequence=operation.sequence,
                    machine=machines.get(operation.machine_id),
                    machine_id=operation.machine_id,
                    centre=centre,
                )

    async def _assert_operations_still_valid(self, operations: Sequence[RoutingOperation]) -> None:
        """Re-check work centres and machines at approval time.

        A routing can sit in draft for weeks, and the centre it plans work at may
        have been deactivated or the machine scrapped in the meantime. Releasing
        it to production without re-checking would push that problem onto the
        shop floor.
        """
        rates = await self.work_centers.rates([op.work_center_id for op in operations])
        machines = await self.machines.load(
            [op.machine_id for op in operations if op.machine_id is not None]
        )

        for operation in operations:
            centre = rates.get(operation.work_center_id)
            if centre is None or not centre.is_active:
                raise BusinessRuleError(
                    f"Operation {operation.sequence} plans work at a work centre that is "
                    "no longer active. Update the routing before approving it.",
                    details={
                        "sequence": operation.sequence,
                        "work_center_id": str(operation.work_center_id),
                    },
                )
            if operation.machine_id is not None:
                self._assert_machine_fits(
                    sequence=operation.sequence,
                    machine=machines.get(operation.machine_id),
                    machine_id=operation.machine_id,
                    centre=centre,
                )

    @staticmethod
    def _assert_machine_fits(
        *,
        sequence: int,
        machine: MachineInfo | None,
        machine_id: uuid.UUID,
        centre: WorkCenterRates,
    ) -> None:
        """One machine's fitness for one operation.

        The work-centre match is also a composite foreign key; it is checked here
        so the message names the step and the machine rather than surfacing a
        constraint name. Availability is deliberately *not* checked: a machine
        down for maintenance today does not make tomorrow's routing wrong. Only
        retirement does, because a retired machine never comes back.
        """
        if machine is None:
            raise NotFoundError("Machine", machine_id)
        if machine.work_center_id != centre.id:
            raise BusinessRuleError(
                f"Operation {sequence}: machine {machine.code} is not in work centre "
                f"{centre.code}.",
                details={
                    "sequence": sequence,
                    "machine_code": machine.code,
                    "work_center_code": centre.code,
                },
            )
        if machine.status is MachineStatus.RETIRED or not machine.is_active:
            raise BusinessRuleError(
                f"Operation {sequence}: machine {machine.code} is retired and cannot be "
                "planned against.",
                details={"sequence": sequence, "machine_code": machine.code},
            )

    async def _persist_operations(
        self, routing: Routing, payload_operations: Sequence[RoutingOperationIn]
    ) -> list[RoutingOperation]:
        records = [
            RoutingOperation(routing_id=routing.id, **operation.model_dump())
            for operation in payload_operations
        ]
        for record in records:
            self.operations.add(record)
        await self.session.flush()
        return records

    async def _transition(
        self,
        routing: Routing,
        target: RoutingStatus,
        *,
        reason: str,
        action: AuditAction = AuditAction.STATUS_CHANGE,
    ) -> Routing:
        """Move a routing's status and record it.

        Every status change in this module funnels through here, so there is
        exactly one place that can write ``routings.status`` and exactly one
        place that writes the matching audit row.
        """
        current = RoutingStatus(routing.status)
        ROUTING_LIFECYCLE.assert_transition(current, target)
        routing.status = target
        self.repo.touch(routing)
        await self.session.flush()

        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ROUTING,
            entity_id=routing.id,
            entity_code=routing.code,
            from_status=str(current),
            to_status=str(target),
            user_id=self.tenant.user_id,
            action=action,
            reason=reason,
        )
        return routing


def _detail(routing: Routing, operations: Sequence[RoutingOperation]) -> RoutingDetail:
    return RoutingDetail(
        **{field: getattr(routing, field) for field in RoutingSummary.model_fields},
        notes=routing.notes,
        operations=[RoutingOperationRead.model_validate(op) for op in operations],
    )


# -----------------------------------------------------------------------------
# Shifts and the roster
# -----------------------------------------------------------------------------


async def company_zone(session: AsyncSession, tenant: TenantContext) -> ZoneInfo:
    """The timezone the company's shift times are read in."""
    return resolve_zone(await CompanyZoneRepository(session, tenant).timezone())


async def company_today(session: AsyncSession, tenant: TenantContext) -> date:
    """Today's date where the company is.

    ASSUMPTION: "from today" on the roster means the company-local date, because
    roster dates are local dates. The UTC date in India is still yesterday until
    05:30, and a probe keyed on it would treat this morning's shift as already
    worked. docs/assumptions.md §B2-5.
    """
    return local_date(utcnow(), await company_zone(session, tenant))


async def _assert_off_the_roster(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    label: str,
    planned_from: Callable[[date], Awaitable[bool]],
    action: str,
    advice: str = "Cancel those assignments first, or plan them elsewhere.",
) -> None:
    """Refuse to retire, move or re-time something a future plan still names.

    The roster lock is taken first, so no plan can be written between this
    answer and the caller's own write - the write path takes the same lock before
    it reads the asset it validates.
    """
    await advisory_xact_lock(session, ROSTER_LOCK, tenant.company_id)
    from_date = await company_today(session, tenant)
    if await planned_from(from_date):
        raise referenced_in_use(
            f"{label} is on the shift roster on or after {from_date.isoformat()} and cannot be "
            f"{action}. {advice}",
            label=label,
            from_date=from_date.isoformat(),
        )


def shift_lifecycle_state(shift: Shift) -> MasterLifecycleState:
    """Derive the lifecycle state of a shift from its flags, as for a work centre."""
    if shift.deleted_at is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if shift.is_active else MasterLifecycleState.INACTIVE


class ShiftService:
    """The shift master. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = ShiftRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_shifts(
        self, filters: ShiftFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ShiftRead], OffsetPagination]:
        rows, pagination = await paginate(
            self.session, self.repo.build_list_stmt(filters, sort), params
        )
        return [ShiftRead.model_validate(row) for row in rows], pagination

    async def get_shift(self, shift_id: uuid.UUID) -> ShiftRead:
        return ShiftRead.model_validate(await self.repo.get_or_404(shift_id))

    # --- writes -------------------------------------------------------------

    async def create_shift(self, payload: ShiftCreate) -> ShiftRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Shift code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            shift = Shift(
                **payload.model_dump(),
                crosses_midnight=shift_crosses_midnight(payload.start_time, payload.end_time),
            )
            self.repo.add(shift)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_SHIFT,
                entity_id=shift.id,
                entity_code=shift.code,
                summary=f"Created shift {shift.code} ({shift.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(shift, _SHIFT_AUDITED_FIELDS),
            )

        return ShiftRead.model_validate(shift)

    async def update_shift(self, shift_id: uuid.UUID, payload: ShiftUpdate) -> ShiftRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            shift = await self.repo.get_or_404(shift_id)
            before = snapshot(shift, _SHIFT_AUDITED_FIELDS)

            start = changes.get("start_time", shift.start_time)
            end = changes.get("end_time", shift.end_time)
            pause = changes.get("break_minutes", shift.break_minutes)
            problem = break_problem(start, end, pause)
            if problem:
                raise ValidationError(problem, details={"fields": {"break_minutes": [problem]}})

            # ASSUMPTION: a shift that future plans name is not re-timed under
            # them - their windows were fixed when they were planned, and a break
            # changes the capacity they count. Renaming and re-ordering are free.
            # docs/assumptions.md §B2-1.
            if (start, end, pause) != (shift.start_time, shift.end_time, shift.break_minutes):
                await self._assert_off_the_roster(
                    shift,
                    "re-timed",
                    advice=(
                        "To change the times of a shift in use, create a new shift, move the "
                        "plan onto it and retire this one."
                    ),
                )

            for field, value in changes.items():
                setattr(shift, field, value)
            shift.crosses_midnight = shift_crosses_midnight(shift.start_time, shift.end_time)
            self.repo.touch(shift)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_SHIFT,
                entity_id=shift.id,
                entity_code=shift.code,
                summary=f"Updated shift {shift.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(shift, _SHIFT_AUDITED_FIELDS),
            )

        return ShiftRead.model_validate(shift)

    async def set_active(self, shift_id: uuid.UUID, *, active: bool, reason: str) -> ShiftRead:
        """Offer a shift on pickers again, or stop offering it."""
        target = MasterLifecycleState.ACTIVE if active else MasterLifecycleState.INACTIVE

        async with transaction(self.session):
            shift = await self.repo.get_or_404(shift_id)
            current = shift_lifecycle_state(shift)
            SHIFT_LIFECYCLE.assert_transition(current, target)
            if not active:
                await self._assert_off_the_roster(shift, "deactivated")

            shift.is_active = active
            self.repo.touch(shift)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_SHIFT,
                entity_id=shift.id,
                entity_code=shift.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return ShiftRead.model_validate(shift)

    async def retire_shift(self, shift_id: uuid.UUID, *, reason: str) -> None:
        """Soft-delete a shift no future plan names. Its code stays on history."""
        async with transaction(self.session):
            shift = await self.repo.get_or_404(shift_id)
            current = shift_lifecycle_state(shift)
            SHIFT_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)
            await self._assert_off_the_roster(shift, "retired")

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_SHIFT,
                entity_id=shift.id,
                entity_code=shift.code,
                from_status=str(current),
                to_status=str(MasterLifecycleState.ARCHIVED),
                user_id=self.tenant.user_id,
                action=AuditAction.DELETE,
                reason=reason,
            )
            shift.is_active = False
            await self.repo.soft_delete(shift)

    async def _assert_off_the_roster(
        self, shift: Shift, action: str, *, advice: str | None = None
    ) -> None:
        shift_id = shift.id
        await _assert_off_the_roster(
            self.session,
            self.tenant,
            label=f"Shift {shift.code}",
            planned_from=lambda day: self.repo.used_by_live_roster(shift_id, from_date=day),
            action=action,
            **({"advice": advice} if advice else {}),
        )


@dataclass(frozen=True, slots=True)
class PlannedWindow:
    """One validated assignment about to be written, with the labels a clash prints."""

    index: int
    roster_date: date
    shift_id: uuid.UUID
    shift_code: str
    work_center_id: uuid.UUID
    work_center_code: str
    machine_id: uuid.UUID | None
    machine_code: str | None
    operator_user_id: uuid.UUID
    operator_name: str
    starts_at: datetime
    ends_at: datetime
    notes: str | None


def _where(roster_date: date, shift_code: str, work_center_code: str) -> str:
    return f"shift {shift_code} at {work_center_code} on {roster_date.isoformat()}"


def _clashes(
    window: PlannedWindow,
    *,
    operator_user_id: uuid.UUID,
    machine_id: uuid.UUID | None,
    elsewhere: str,
    conflicting_assignment_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """Every resource ``window`` shares with one overlapping plan, as a named clash."""
    clashes: list[tuple[str, str]] = []
    if window.operator_user_id == operator_user_id:
        clashes.append(("operator", f"{window.operator_name} is already planned on {elsewhere}."))
    if window.machine_id is not None and window.machine_id == machine_id:
        clashes.append(
            ("machine", f"Machine {window.machine_code} is already staffed on {elsewhere}.")
        )
    return [
        {
            "resource": resource,
            "index": window.index,
            "roster_date": window.roster_date.isoformat(),
            "shift_code": window.shift_code,
            "work_center_code": window.work_center_code,
            "machine_code": window.machine_code,
            "operator_name": window.operator_name,
            "conflicting_assignment_id": (
                str(conflicting_assignment_id) if conflicting_assignment_id else None
            ),
            "message": message,
        }
        for resource, message in clashes
    ]


def _with_warnings(message: str, warnings: Sequence[str]) -> str:
    return f"{message} Warning: {' '.join(warnings)}" if warnings else message


class ShiftRosterService:
    """Who is planned where, on which shift. One instance per request.

    Every write - one row, a bulk, a copied day, a re-staffing - takes the roster
    lock, validates the whole request in one batch of reads, checks every window
    against PLANNED rows in one query, and then writes. The EXCLUDE constraints
    are the backstop for anything that slips past; the checks here exist so the
    refusal names each clash.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = ShiftAssignmentRepository(session, tenant)
        self.shifts = ShiftRepository(session, tenant)
        self.work_centers = WorkCenterRepository(session, tenant)
        self.machines = MachineRepository(session, tenant)
        self.operators = RosterOperatorRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_assignments(
        self, filters: ShiftRosterFilters, params: PageParams
    ) -> tuple[list[ShiftAssignmentRead], OffsetPagination]:
        return await self.repo.page(filters, params)

    async def list_operators(
        self, search: str | None, params: PageParams
    ) -> tuple[list[RosterOperatorRead], OffsetPagination]:
        rows, pagination = await self.operators.page(search, params)
        return [RosterOperatorRead.model_validate(row) for row in rows], pagination

    # --- writes -------------------------------------------------------------

    async def plan(
        self, payloads: Sequence[ShiftAssignmentCreate]
    ) -> tuple[list[ShiftAssignmentRead], str]:
        """Write assignments all together or not at all.

        Returns the rows written and the response message, which carries any
        warning - a machine under maintenance is planned, and said so.
        """
        if not payloads:
            raise BusinessRuleError("There is nothing to plan.")
        if len(payloads) > MAX_ROSTER_BULK:
            raise BusinessRuleError(
                f"At most {MAX_ROSTER_BULK} assignments are planned at once.",
                details={"requested": len(payloads), "limit": MAX_ROSTER_BULK},
            )

        async with transaction(self.session):
            await advisory_xact_lock(self.session, ROSTER_LOCK, self.tenant.company_id)
            windows, warnings = await self._validate(payloads)
            await self._assert_no_overlaps(windows)

            rows = [
                ShiftAssignment(
                    roster_date=window.roster_date,
                    shift_id=window.shift_id,
                    work_center_id=window.work_center_id,
                    machine_id=window.machine_id,
                    operator_user_id=window.operator_user_id,
                    starts_at=window.starts_at,
                    ends_at=window.ends_at,
                    status=ShiftAssignmentStatus.PLANNED,
                    notes=window.notes,
                )
                for window in windows
            ]
            for row in rows:
                self.repo.add(row)
            await self.session.flush()

            # One flush for the whole request's audit rows, as the inventory
            # posting loop does: each record_audit only stages its row.
            for row, window in zip(rows, windows, strict=True):
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CREATE,
                    entity_type=AUDIT_SHIFT_ASSIGNMENT,
                    entity_id=row.id,
                    entity_code=f"{window.roster_date.isoformat()} {window.shift_code}",
                    summary=(
                        f"Planned {window.operator_name} on "
                        f"{_where(window.roster_date, window.shift_code, window.work_center_code)}"
                    ),
                    user_id=self.tenant.user_id,
                    new_values=snapshot(row, _ASSIGNMENT_AUDITED_FIELDS),
                    flush=False,
                )
            await self.session.flush()
            written = await self.repo.reads([row.id for row in rows])

        noun = "assignment" if len(written) == 1 else "assignments"
        return written, _with_warnings(f"{len(written)} {noun} planned.", warnings)

    async def restaff(
        self, assignment_id: uuid.UUID, payload: ShiftAssignmentUpdate
    ) -> tuple[ShiftAssignmentRead, str]:
        """Change who, where or the notes of a planned row; the window stays.

        Re-validated exactly like a new row, and checked for overlaps against
        everything except itself.
        """
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            await advisory_xact_lock(self.session, ROSTER_LOCK, self.tenant.company_id)
            row = await self.repo.get_or_404(assignment_id)
            current = ShiftAssignmentStatus(row.status)
            if current is not ShiftAssignmentStatus.PLANNED:
                raise BusinessRuleError(
                    f"This assignment is {current.lower()} and can no longer be changed.",
                    details={"status": str(current)},
                )

            merged = ShiftAssignmentCreate(
                roster_date=row.roster_date,
                shift_id=row.shift_id,
                work_center_id=changes.get("work_center_id", row.work_center_id),
                machine_id=changes.get("machine_id", row.machine_id),
                operator_user_id=changes.get("operator_user_id", row.operator_user_id),
                notes=changes.get("notes", row.notes),
            )
            validated, warnings = await self._validate([merged])
            # The stored window, not one recomputed from today's shift times: the
            # plan keeps the window it was made with.
            window = replace(validated[0], starts_at=row.starts_at, ends_at=row.ends_at)
            await self._assert_no_overlaps([window], exclude_ids=[row.id])

            before = snapshot(row, _ASSIGNMENT_AUDITED_FIELDS)
            row.work_center_id = window.work_center_id
            row.machine_id = window.machine_id
            row.operator_user_id = window.operator_user_id
            row.notes = window.notes
            self.repo.touch(row)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_SHIFT_ASSIGNMENT,
                entity_id=row.id,
                entity_code=f"{window.roster_date.isoformat()} {window.shift_code}",
                summary="Re-staffed "
                + _where(window.roster_date, window.shift_code, window.work_center_code),
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(row, _ASSIGNMENT_AUDITED_FIELDS),
            )
            updated = (await self.repo.reads([row.id]))[0]

        return updated, _with_warnings("Assignment updated.", warnings)

    async def cancel(self, assignment_id: uuid.UUID, *, reason: str) -> ShiftAssignmentRead:
        """Take a planned row off the roster. Irreversible; the row is kept."""
        async with transaction(self.session):
            await advisory_xact_lock(self.session, ROSTER_LOCK, self.tenant.company_id)
            row = await self.repo.get_or_404(assignment_id)
            current = ShiftAssignmentStatus(row.status)
            SHIFT_ASSIGNMENT_FLOW.assert_transition(current, ShiftAssignmentStatus.CANCELLED)

            row.status = ShiftAssignmentStatus.CANCELLED
            self.repo.touch(row)
            await self.session.flush()
            cancelled = (await self.repo.reads([row.id]))[0]

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_SHIFT_ASSIGNMENT,
                entity_id=cancelled.id,
                entity_code=f"{cancelled.roster_date.isoformat()} {cancelled.shift_code}",
                from_status=str(current),
                to_status=str(ShiftAssignmentStatus.CANCELLED),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return cancelled

    async def copy_day(
        self, payload: ShiftRosterCopyRequest
    ) -> tuple[list[ShiftAssignmentRead], str]:
        """Plan one day's PLANNED rows again on every date of a range.

        All or nothing, like any bulk write, and bounded by the same limit -
        narrowing to one work centre is how a larger copy is split.
        """
        days = (payload.target_to - payload.target_from).days + 1
        async with transaction(self.session):
            await advisory_xact_lock(self.session, ROSTER_LOCK, self.tenant.company_id)
            source = await self.repo.planned_on(
                payload.source_date,
                work_center_id=payload.work_center_id,
                limit=MAX_ROSTER_BULK + 1,
            )
            if not source:
                raise BusinessRuleError(
                    f"Nothing is planned on {payload.source_date.isoformat()} to copy.",
                    details={"source_date": payload.source_date.isoformat()},
                )
            if len(source) * days > MAX_ROSTER_BULK:
                raise BusinessRuleError(
                    f"Copying {len(source)} assignment(s) onto {days} day(s) is more than "
                    f"{MAX_ROSTER_BULK} at once. Narrow the work centre or the dates.",
                    details={"limit": MAX_ROSTER_BULK, "days": days},
                )
            copies = [
                ShiftAssignmentCreate(
                    roster_date=payload.target_from + timedelta(days=offset),
                    shift_id=row.shift_id,
                    work_center_id=row.work_center_id,
                    machine_id=row.machine_id,
                    operator_user_id=row.operator_user_id,
                    notes=row.notes,
                )
                for offset in range(days)
                for row in source
            ]
            return await self.plan(copies)

    # --- helpers ------------------------------------------------------------

    async def _validate(
        self, payloads: Sequence[ShiftAssignmentCreate]
    ) -> tuple[list[PlannedWindow], list[str]]:
        """Resolve and check every reference of a request in one batch of reads.

        Four statements for the whole request - shifts, work centres, machines,
        operators - however many rows it carries. A machine under maintenance or
        broken down is planned with a warning rather than refused, as routing
        approval treats it: it may well be back before the shift starts.
        """
        shifts = await self.shifts.load([p.shift_id for p in payloads])
        centres = await self.work_centers.rates([p.work_center_id for p in payloads])
        machines = await self.machines.load(
            [p.machine_id for p in payloads if p.machine_id is not None]
        )
        operators = await self.operators.facts([p.operator_user_id for p in payloads])
        zone = await company_zone(self.session, self.tenant)

        windows: list[PlannedWindow] = []
        warnings: list[str] = []
        for index, payload in enumerate(payloads):
            shift = shifts.get(payload.shift_id)
            if shift is None:
                raise NotFoundError(SHIFT_ENTITY, payload.shift_id)
            if not shift.is_active:
                raise BusinessRuleError(
                    inactive_record(SHIFT_ENTITY, shift.code, "cannot be planned"),
                    details={"index": index, "shift_code": shift.code},
                )

            centre = centres.get(payload.work_center_id)
            if centre is None:
                raise NotFoundError(WORK_CENTER_ENTITY, payload.work_center_id)
            if not centre.is_active:
                raise BusinessRuleError(
                    inactive_record(WORK_CENTER_ENTITY, centre.code, "cannot be staffed"),
                    details={"index": index, "work_center_code": centre.code},
                )

            operator = operators.get(payload.operator_user_id)
            if operator is None:
                raise NotFoundError(OPERATOR_ENTITY, payload.operator_user_id)
            if operator.is_vendor_user:
                raise BusinessRuleError(
                    f"{operator.full_name} is a vendor-portal account and cannot be planned "
                    "on a shift.",
                    details={"index": index, "operator_user_id": str(operator.id)},
                )
            if not operator.is_active:
                raise BusinessRuleError(
                    f"{operator.full_name}'s account is inactive and cannot be planned on a shift.",
                    details={"index": index, "operator_user_id": str(operator.id)},
                )

            machine_code: str | None = None
            if payload.machine_id is not None:
                machine = machines.get(payload.machine_id)
                if machine is None:
                    raise NotFoundError("Machine", payload.machine_id)
                if machine.status is MachineStatus.RETIRED or not machine.is_active:
                    raise BusinessRuleError(
                        f"Machine {machine.code} is retired or inactive and cannot be planned.",
                        details={"index": index, "machine_code": machine.code},
                    )
                if machine.work_center_id != payload.work_center_id:
                    raise BusinessRuleError(
                        f"Machine {machine.code} does not belong to work centre {centre.code}.",
                        details={"index": index, "machine_code": machine.code},
                    )
                if machine.status in (MachineStatus.MAINTENANCE, MachineStatus.BREAKDOWN):
                    state = (
                        "under maintenance"
                        if machine.status is MachineStatus.MAINTENANCE
                        else "broken down"
                    )
                    warnings.append(
                        f"Machine {machine.code} is {state}; check it is back before the shift."
                    )
                machine_code = machine.code

            starts_at, ends_at = shift_window(payload.roster_date, shift, zone)
            windows.append(
                PlannedWindow(
                    index=index,
                    roster_date=payload.roster_date,
                    shift_id=shift.id,
                    shift_code=shift.code,
                    work_center_id=centre.id,
                    work_center_code=centre.code,
                    machine_id=payload.machine_id,
                    machine_code=machine_code,
                    operator_user_id=operator.id,
                    operator_name=operator.full_name,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    notes=payload.notes,
                )
            )
        return windows, list(dict.fromkeys(warnings))

    async def _assert_no_overlaps(
        self, windows: Sequence[PlannedWindow], *, exclude_ids: Sequence[uuid.UUID] = ()
    ) -> None:
        """Refuse the request if any window clashes - and name every clash.

        One query for PLANNED rows of the involved operators and machines across
        the request's whole span, then every pair in memory: against what is
        already planned, and against the request's own earlier rows.
        """
        existing = await self.repo.overlapping(
            starts_at=min(window.starts_at for window in windows),
            ends_at=max(window.ends_at for window in windows),
            operator_ids=sorted({window.operator_user_id for window in windows}),
            machine_ids=sorted(
                {window.machine_id for window in windows if window.machine_id is not None}
            ),
            exclude_ids=exclude_ids,
        )

        conflicts: list[dict[str, Any]] = []
        for position, window in enumerate(windows):
            span = (window.starts_at, window.ends_at)
            for other in existing:
                if windows_overlap(span, (other.starts_at, other.ends_at)):
                    conflicts += _clashes(
                        window,
                        operator_user_id=other.operator_user_id,
                        machine_id=other.machine_id,
                        elsewhere=(
                            f"{_where(other.roster_date, other.shift_code, other.work_center_code)}"
                            f" ({other.operator_name})"
                        ),
                        conflicting_assignment_id=other.id,
                    )
            for earlier in windows[:position]:
                if windows_overlap(span, (earlier.starts_at, earlier.ends_at)):
                    conflicts += _clashes(
                        window,
                        operator_user_id=earlier.operator_user_id,
                        machine_id=earlier.machine_id,
                        elsewhere=_where(
                            earlier.roster_date, earlier.shift_code, earlier.work_center_code
                        )
                        + " in this same request",
                        conflicting_assignment_id=None,
                    )

        if conflicts:
            raise BusinessRuleError(SHIFT_ASSIGNMENT_OVERLAP, details={"conflicts": conflicts})


# -----------------------------------------------------------------------------
# Cross-module entry point
# -----------------------------------------------------------------------------


async def manufacture_lead_time_days(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    quantity: Decimal,
    *,
    as_of: date | None = None,
) -> int | None:
    """Days of shop time to make ``quantity`` of ``item_id``, or ``None``.

    The contract MRP calls: ``None`` means the item has no effective approved
    routing, and the caller must fall back to ``items.lead_time_days`` rather
    than plan on zero days.
    """
    result = await RoutingService(session, tenant).item_lead_time(item_id, quantity, as_of=as_of)
    return result.lead_time_days if result.routing_id is not None else None


async def manufacture_lead_times(
    session: AsyncSession,
    tenant: TenantContext,
    quantities: Mapping[uuid.UUID, Decimal],
    *,
    as_of: date | None = None,
) -> dict[uuid.UUID, int]:
    """Batched ``manufacture_lead_time_days``: two statements, every item answered.

    The planner needs the shop time of a whole planning level at once - every
    short, manufactured item that level produced - and a per-item call would be
    an N+1 in the middle of the run (master rules section 32). Items without an
    effective approved routing are absent from the result, exactly as the
    single-item entry point answers ``None``, so the caller falls back to
    ``items.lead_time_days`` rather than planning on zero days.

    Each item's lead time is computed for *its own* quantity: shop time is
    dominated by run time, and answering a hundred-piece batch with a one-piece
    figure would under-state the delay the plan exists to surface.
    """
    if not quantities:
        return {}
    on = as_of or today()
    routings = await RoutingRepository(session, tenant).effective_routings_for_items(
        list(quantities), on
    )
    if not routings:
        return {}
    costed = await RoutingOperationRepository(session, tenant).costed_operations_for_routings(
        [routing.id for routing in routings.values()]
    )
    result: dict[uuid.UUID, int] = {}
    for item_id, routing in routings.items():
        plan = CostedRouting(
            id=routing.id,
            code=routing.code,
            item_id=routing.item_id,
            operations=tuple(costed.get(routing.id, ())),
        )
        hours_per_day = resolve_working_hours_per_day(plan)
        result[item_id] = total_lead_time_days(
            plan, quantities[item_id], working_hours_per_day=hours_per_day
        )
    return result


async def require_shift(
    session: AsyncSession,
    tenant: TenantContext,
    shift_id: uuid.UUID,
    *,
    field: str = "shift_id",
) -> Shift:
    """The shift a work order or a booking is about to name, if it may name it.

    Read through the tenant-scoped repository, never trusted from the foreign
    key: a UUID is satisfied by another company's shift just as well (CLAUDE.md,
    "a foreign key to a UUID is not a tenant check"). Live and in this tenant, or
    :class:`NotFoundError`; active, or a 422 against ``field`` so a form can show
    it. Only a new choice comes through here - a row keeps the shift it already
    names after that shift is deactivated.
    """
    shift = await ShiftRepository(session, tenant).get(shift_id)
    if shift is None:
        raise NotFoundError(SHIFT_ENTITY, shift_id)
    if not shift.is_active:
        message = inactive_record(SHIFT_ENTITY, shift.code, "cannot be chosen")
        raise ValidationError(message, details={"fields": {field: [message]}})
    return shift


async def roster_mismatch(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    operator_user_id: uuid.UUID | None,
    shift_id: uuid.UUID | None,
    roster_date: date,
    work_center_id: uuid.UUID | None = None,
    machine_id: uuid.UUID | None = None,
) -> bool:
    """Is the named operator *not* planned on the named shift there that day?

    ``False`` whenever the operator or the shift is missing: with only one of the
    two there is no plan to compare against. The caller warns on ``True`` and
    never refuses - the roster is advisory (docs/assumptions.md §B2-4).
    """
    if operator_user_id is None or shift_id is None:
        return False
    return not await ShiftAssignmentRepository(session, tenant).is_rostered(
        operator_user_id=operator_user_id,
        shift_id=shift_id,
        roster_date=roster_date,
        work_center_id=work_center_id,
        machine_id=machine_id,
    )


__all__ = [
    "AUDIT_MACHINE",
    "AUDIT_ROUTING",
    "AUDIT_SHIFT",
    "AUDIT_SHIFT_ASSIGNMENT",
    "AUDIT_WORK_CENTER",
    "MACHINE_AVAILABILITY",
    "MINUTES_PER_HOUR",
    "ROSTER_LOCK",
    "ROUTING_LIFECYCLE",
    "SHIFT_ASSIGNMENT_FLOW",
    "SHIFT_LIFECYCLE",
    "SUPER_ADMIN_ROLE_CODE",
    "WORK_CENTER_LIFECYCLE",
    "MachineService",
    "MasterLifecycleState",
    "OperationTimings",
    "PlannedWindow",
    "RoutingService",
    "ShiftRosterService",
    "ShiftService",
    "ShiftTimes",
    "WorkCenterService",
    "calculate_operation_cost",
    "calculate_operation_time",
    "calculate_routing_cost",
    "can_approve",
    "chargeable_operation_time",
    "company_today",
    "company_zone",
    "cost_breakdown",
    "effectivity_contains",
    "is_editable",
    "lifecycle_state",
    "local_date",
    "manufacture_lead_time_days",
    "manufacture_lead_times",
    "net_shift_minutes",
    "referenced_in_use",
    "require_shift",
    "resolve_working_hours_per_day",
    "resolve_zone",
    "roster_mismatch",
    "shift_lifecycle_state",
    "shift_minutes",
    "shift_window",
    "total_lead_time_days",
    "total_routing_minutes",
    "windows_overlap",
]
