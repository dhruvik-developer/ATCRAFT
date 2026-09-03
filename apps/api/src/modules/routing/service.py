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
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any, Protocol

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
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_money, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.routing.models import (
    DEFAULT_WORKING_HOURS_PER_DAY,
    Machine,
    MachineStatus,
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from src.modules.routing.repository import (
    CostedOperation,
    CostedRouting,
    ItemLookupRepository,
    MachineInfo,
    MachineRepository,
    RoutingItemInfo,
    RoutingOperationRepository,
    RoutingRepository,
    WorkCenterRates,
    WorkCenterRepository,
)
from src.modules.routing.schemas import (
    CopyAsNewVersionRequest,
    LeadTimeRead,
    MachineCreate,
    MachineFilters,
    MachineRead,
    MachineStatusChange,
    MachineUpdate,
    OperationCostRead,
    RoutingCostRead,
    RoutingCreate,
    RoutingDetail,
    RoutingFilters,
    RoutingOperationIn,
    RoutingOperationRead,
    RoutingSummary,
    RoutingUpdate,
    WorkCenterCreate,
    WorkCenterFilters,
    WorkCenterRead,
    WorkCenterUpdate,
)

log = get_logger(__name__)

AUDIT_WORK_CENTER = "work_center"
AUDIT_MACHINE = "machine"
AUDIT_ROUTING = "routing"

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
    entity="WorkCenter",
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
        time_cost = to_money(
            charged / MINUTES_PER_HOUR * operation.work_center_cost_per_hour
        )
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
        lead_time_days=total_lead_time_days(
            routing, quantity, working_hours_per_day=hours_per_day
        ),
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
            raise BusinessRuleError("No fields were supplied to update.")

        async with transaction(self.session):
            work_center = await self.repo.get_or_404(work_center_id)
            before = snapshot(work_center, _WORK_CENTER_AUDITED_FIELDS)

            if changes.get("code") and changes["code"] != work_center.code:
                await self._assert_code_free(changes["code"], exclude_id=work_center.id)
            await self._assert_organisation_refs(
                changes.get("plant_id"), changes.get("department_id")
            )

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
    return (
        MasterLifecycleState.ACTIVE if work_center.is_active else MasterLifecycleState.INACTIVE
    )


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
            raise BusinessRuleError("No fields were supplied to update.")

        async with transaction(self.session):
            machine = await self.repo.get_or_404(machine_id)
            before = snapshot(machine, _MACHINE_AUDITED_FIELDS)

            if changes.get("code") and changes["code"] != machine.code:
                await self._assert_code_free(changes["code"], exclude_id=machine.id)

            new_work_center = changes.get("work_center_id")
            if new_work_center is not None and new_work_center != machine.work_center_id:
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

            if (
                current is MachineStatus.MAINTENANCE
                and payload.status is MachineStatus.AVAILABLE
            ):
                machine.last_maintenance_date = today()
            if payload.next_maintenance_date is not None:
                machine.next_maintenance_date = payload.next_maintenance_date
            if payload.status is MachineStatus.RETIRED:
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

    async def _assert_work_center_usable(self, work_center_id: uuid.UUID) -> None:
        rates = await self.work_centers.rates([work_center_id])
        work_center = rates.get(work_center_id)
        if work_center is None:
            raise NotFoundError("WorkCenter", work_center_id)
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

    async def routing_cost(
        self, routing_id: uuid.UUID, quantity: Decimal
    ) -> RoutingCostRead:
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

    async def update_routing(
        self, routing_id: uuid.UUID, payload: RoutingUpdate
    ) -> RoutingDetail:
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
                raise NotFoundError("WorkCenter", operation.work_center_id)
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

    async def _assert_operations_still_valid(
        self, operations: Sequence[RoutingOperation]
    ) -> None:
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
    result = await RoutingService(session, tenant).item_lead_time(
        item_id, quantity, as_of=as_of
    )
    return result.lead_time_days if result.routing_id is not None else None


__all__ = [
    "AUDIT_MACHINE",
    "AUDIT_ROUTING",
    "AUDIT_WORK_CENTER",
    "MACHINE_AVAILABILITY",
    "MINUTES_PER_HOUR",
    "ROUTING_LIFECYCLE",
    "SUPER_ADMIN_ROLE_CODE",
    "WORK_CENTER_LIFECYCLE",
    "MachineService",
    "MasterLifecycleState",
    "OperationTimings",
    "RoutingService",
    "WorkCenterService",
    "calculate_operation_cost",
    "calculate_operation_time",
    "calculate_routing_cost",
    "can_approve",
    "chargeable_operation_time",
    "cost_breakdown",
    "effectivity_contains",
    "is_editable",
    "lifecycle_state",
    "manufacture_lead_time_days",
    "referenced_in_use",
    "resolve_working_hours_per_day",
    "total_lead_time_days",
    "total_routing_minutes",
]
