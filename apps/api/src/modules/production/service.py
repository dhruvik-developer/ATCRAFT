"""Production business rules: releasing, issuing, booking, completing, costing.

Master rules section 18, database contract sections 7 and 8. The module's shape
follows five decisions, each made because the alternative is wrong rather than
because it is tidier.

**Release freezes the structure.** ``release_order`` explodes the approved BOM
once, writes the result into ``production_order_components`` and never looks at
``bom_lines`` again for this job. An engineering change tomorrow belongs to
tomorrow's orders. See the models module for the full argument.

**A shortage is reported, not raised.** Release reserves what it can find and
returns a per-component shortage list. A planner releasing a twenty-component
job needs to know that three parts are short, not that the first one was - and
raising would leave the job unreleased and the other seventeen parts unheld.

**Stock moves once.** ``MATERIAL_ISSUE`` takes the component out of the store
when the issue is posted. Completion then sets ``consumed_quantity`` on the
component, which is a work-in-progress fact and posts nothing: the same stock
must not leave twice. The only movements this module writes are
``MATERIAL_ISSUE``, ``MATERIAL_RETURN`` and ``PRODUCTION_RECEIPT``, and every one
of them goes through the inventory engine.

**There is exactly one place that posts finished output.** A production entry
booked against the last operation posts the receipt. Completion books such an
entry when it is given a quantity, and posts nothing of its own. Two code paths
that both post output are two code paths that can each be run once.

**Cancellation is blocked by physical reality, not by a status.** The state
graph allows ``CANCELLED`` before the job is under way, but a job holding issued
material cannot be cancelled at all: that material is on the floor and has to be
returned to the store first. A cancel that skipped the return would leave stock
that the ledger says was consumed by a job that never existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.common.warehouse_scope import assert_warehouse_scope
from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
)
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity, to_unit_price
from src.modules.allocation import service as allocation_service
from src.modules.allocation.models import DemandType, ReservationType, StockReservation
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.bom import engine as bom_engine
from src.modules.bom.models import Bom, BomStatus
from src.modules.bom.repository import BomRepository
from src.modules.inventory.models import InventoryTransaction, StockState, TransactionType
from src.modules.inventory.repository import BalanceGrain
from src.modules.inventory.service import MovementEngine, MovementRequest, post_movements
from src.modules.organization.service import require_cost_centre
from src.modules.production.models import (
    ISSUABLE_PRODUCTION_STATUSES,
    OVER_ISSUE_MULTIPLIER,
    OVER_ISSUE_TOLERANCE,
    OVER_PRODUCTION_MULTIPLIER,
    OVER_PRODUCTION_TOLERANCE,
    MaterialIssue,
    MaterialIssueLine,
    MaterialIssueStatus,
    MaterialIssueType,
    ProductionEntry,
    ProductionOrder,
    ProductionOrderComponent,
    ProductionOrderStatus,
    ProductionSourceType,
    WorkOrder,
    WorkOrderStatus,
)
from src.modules.production.repository import (
    ComponentAvailabilityRepository,
    IssuedComponentTotals,
    MaterialIssueLineRepository,
    MaterialIssueRepository,
    OperatorLookupRepository,
    PlantLookupRepository,
    ProductionEntryRepository,
    ProductionItem,
    ProductionItemRepository,
    ProductionOrderComponentRepository,
    ProductionOrderRepository,
    ReservationCoverageRepository,
    StorageLocationLookupRepository,
    WarehouseLookupRepository,
    WorkCentreRateRepository,
    WorkOrderRepository,
)
from src.modules.production.schemas import (
    ApproveRequest,
    CancelRequest,
    CloseRequest,
    CloseResult,
    CompleteRequest,
    CompleteResult,
    ComponentRead,
    ComponentShortage,
    MaterialIssueCreate,
    MaterialIssueDetail,
    MaterialIssueFilters,
    MaterialIssueLineRead,
    MaterialIssuePostRequest,
    MaterialIssuePostResult,
    MaterialIssueRead,
    PostedMovement,
    ProductionCostMaterialLine,
    ProductionCostOperationLine,
    ProductionCostRead,
    ProductionEntryCreate,
    ProductionEntryFilters,
    ProductionEntryRead,
    ProductionEntryResult,
    ProductionOrderCreate,
    ProductionOrderDetail,
    ProductionOrderFilters,
    ProductionOrderRead,
    ProductionOrderSummary,
    ProductionOrderUpdate,
    ReleaseRequest,
    ReleaseResult,
    StartRequest,
    WipFilters,
    WipOrderRow,
    WipWorkCentreRow,
    WorkOrderFilters,
    WorkOrderRead,
    WorkOrderTransitionRequest,
    WorkOrderUpdate,
)
from src.modules.routing.models import Routing, RoutingStatus
from src.modules.routing.repository import (
    MachineRepository,
    RoutingOperationRepository,
    RoutingRepository,
)
from src.modules.routing.service import MINUTES_PER_HOUR, calculate_operation_time

# =============================================================================
# Constants
# =============================================================================

AUDIT_PRODUCTION_ORDER = "production_order"
AUDIT_WORK_ORDER = "work_order"
AUDIT_MATERIAL_ISSUE = "material_issue"
AUDIT_PRODUCTION_ENTRY = "production_entry"

#: ``reference_type`` on the ledger rows this module writes. The output receipt
#: is referenced to the *order* rather than to the booking that posted it: "what
#: did this job make?" is the question people ask, and it is one indexed lookup
#: on ``(company_id, reference_type, reference_id)``. The reverse link lives on
#: ``production_entries.output_transaction_id``.
MOVEMENT_REFERENCE_ORDER = "production_order"
MOVEMENT_REFERENCE_ISSUE = "material_issue"
MOVEMENT_REFERENCE_RETURN = "material_return"

#: Decimal, never 60.0 - see :func:`_elapsed_minutes`.
SECONDS_PER_MINUTE = Decimal(60)

#: Role that may approve its own document. Same escape hatch, and the same name,
#: as procurement and BOM use, so a small plant relaxes segregation of duties in
#: one place.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"

# ASSUMPTION: an exploded structure with more components than this is not a
# manufacturing order, it is a data problem. The bound matters because release
# writes one row and takes one reservation per component inside a single
# transaction.
MAX_COMPONENTS_PER_ORDER = 500

# ASSUMPTION: batch- and serial-tracked finished goods cannot be received
# through a production booking. Every movement of such an item needs a batch or
# a serial (the inventory engine enforces it), and ``production_entries`` has
# nowhere to record the lot the shift produced. Refusing with an explicit
# message is better than posting output nobody can trace afterwards - the same
# decision procurement made for serial-tracked receipts.
TRACKED_OUTPUT_MESSAGE = (
    "is batch or serial tracked. Finished-goods lots cannot be captured on a production "
    "booking; receive the output through inventory once the lot has been registered."
)


# =============================================================================
# State machines - the only things permitted to move a status column
# =============================================================================

PRODUCTION_ORDER_FLOW: StateMachine[ProductionOrderStatus] = StateMachine(
    entity="Production order",
    initial=ProductionOrderStatus.DRAFT,
    transitions=(
        Transition(
            ProductionOrderStatus.DRAFT,
            ProductionOrderStatus.APPROVED,
            permission=Permission.PRODUCTION_ORDER_APPROVE,
            label="Approve",
        ),
        Transition(
            ProductionOrderStatus.APPROVED,
            ProductionOrderStatus.RELEASED,
            permission=Permission.PRODUCTION_ORDER_RELEASE,
            label="Release to the floor",
            # Release explodes the structure, freezes it and holds stock against
            # it. Reversing that is a cancellation, not an edit.
            is_irreversible=True,
        ),
        Transition(
            ProductionOrderStatus.RELEASED,
            ProductionOrderStatus.MATERIAL_ISSUED,
            permission=Permission.MATERIAL_ISSUE_CREATE,
            label="Material issued",
            is_irreversible=True,
        ),
        Transition(
            ProductionOrderStatus.MATERIAL_ISSUED,
            ProductionOrderStatus.IN_PROCESS,
            permission=Permission.WORK_ORDER_MANAGE,
            label="Start production",
        ),
        Transition(
            ProductionOrderStatus.IN_PROCESS,
            ProductionOrderStatus.COMPLETED,
            permission=Permission.PRODUCTION_ENTRY_CREATE,
            label="Complete",
            is_irreversible=True,
        ),
        Transition(
            ProductionOrderStatus.COMPLETED,
            ProductionOrderStatus.CLOSED,
            permission=Permission.PRODUCTION_ORDER_CLOSE,
            label="Close",
            is_irreversible=True,
        ),
        # CANCELLED is reachable only before the job is physically under way
        # (database.md section 7). The edge from MATERIAL_ISSUED exists because
        # the contract allows it; the *service* additionally refuses while any
        # material is still out, which is a rule about stock, not about status.
        Transition(
            ProductionOrderStatus.DRAFT,
            ProductionOrderStatus.CANCELLED,
            permission=Permission.PRODUCTION_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            ProductionOrderStatus.APPROVED,
            ProductionOrderStatus.CANCELLED,
            permission=Permission.PRODUCTION_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            ProductionOrderStatus.RELEASED,
            ProductionOrderStatus.CANCELLED,
            permission=Permission.PRODUCTION_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            ProductionOrderStatus.MATERIAL_ISSUED,
            ProductionOrderStatus.CANCELLED,
            permission=Permission.PRODUCTION_ORDER_CANCEL,
            label="Cancel",
        ),
    ),
    terminal=frozenset({ProductionOrderStatus.CLOSED, ProductionOrderStatus.CANCELLED}),
)

WORK_ORDER_FLOW: StateMachine[WorkOrderStatus] = StateMachine(
    entity="Work order",
    initial=WorkOrderStatus.PENDING,
    transitions=(
        Transition(
            WorkOrderStatus.PENDING,
            WorkOrderStatus.IN_PROGRESS,
            permission=Permission.WORK_ORDER_MANAGE,
            label="Start",
        ),
        Transition(
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.COMPLETED,
            permission=Permission.WORK_ORDER_MANAGE,
            label="Complete",
            is_irreversible=True,
        ),
        Transition(
            WorkOrderStatus.PENDING,
            WorkOrderStatus.CANCELLED,
            permission=Permission.WORK_ORDER_MANAGE,
            label="Cancel",
        ),
        Transition(
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.CANCELLED,
            permission=Permission.WORK_ORDER_MANAGE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({WorkOrderStatus.COMPLETED, WorkOrderStatus.CANCELLED}),
)

MATERIAL_ISSUE_FLOW: StateMachine[MaterialIssueStatus] = StateMachine(
    entity="Material issue",
    initial=MaterialIssueStatus.DRAFT,
    transitions=(
        Transition(
            MaterialIssueStatus.DRAFT,
            MaterialIssueStatus.POSTED,
            permission=Permission.MATERIAL_ISSUE_CREATE,
            label="Post",
            is_irreversible=True,
        ),
        Transition(
            MaterialIssueStatus.DRAFT,
            MaterialIssueStatus.CANCELLED,
            permission=Permission.MATERIAL_ISSUE_CREATE,
            label="Cancel",
        ),
    ),
    # POSTED is terminal: stock has moved, and a correction is a return
    # document, never an edit of the issue.
    terminal=frozenset({MaterialIssueStatus.POSTED, MaterialIssueStatus.CANCELLED}),
)


# =============================================================================
# Pure rule helpers - unit tested without a database
# =============================================================================


class RoutingStep(Protocol):
    """The routing facts work-order planning reads.

    A protocol rather than the ORM class so the planning functions stay pure and
    testable with a throwaway stub, which is the only way they can be exercised
    without a routing, a work centre and a database behind them.
    """

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def sequence(self) -> int: ...

    @property
    def operation_code(self) -> str: ...

    @property
    def operation_name(self) -> str: ...

    @property
    def work_center_id(self) -> uuid.UUID: ...

    @property
    def machine_id(self) -> uuid.UUID | None: ...

    @property
    def setup_time_minutes(self) -> Decimal: ...

    @property
    def run_time_per_unit_minutes(self) -> Decimal: ...

    @property
    def queue_time_minutes(self) -> Decimal: ...

    @property
    def move_time_minutes(self) -> Decimal: ...

    @property
    def process_cost(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class ComponentPlan:
    """One line of the snapshot release is about to write."""

    line_number: int
    item_id: uuid.UUID
    item_code: str
    uom_id: uuid.UUID
    bom_level: int
    required_quantity: Decimal
    warehouse_id: uuid.UUID
    is_backflushed: bool


@dataclass(frozen=True, slots=True)
class WorkOrderPlan:
    """One operation release is about to issue to the floor."""

    sequence: int
    routing_operation_id: uuid.UUID
    operation_code: str
    operation_name: str
    work_center_id: uuid.UUID
    machine_id: uuid.UUID | None
    planned_setup_minutes: Decimal
    planned_run_minutes: Decimal
    process_cost: Decimal
    planned_start_at: datetime
    planned_end_at: datetime


@dataclass(frozen=True, slots=True)
class OperationCostInput:
    """Everything costing one operation needs, with the rate already resolved."""

    work_order_id: uuid.UUID
    sequence: int
    operation_code: str
    work_center_id: uuid.UUID
    status: WorkOrderStatus
    planned_setup_minutes: Decimal
    planned_run_minutes: Decimal
    process_cost: Decimal
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    cost_per_hour: Decimal
    work_centre_setup_cost: Decimal


def assert_may_force_partial_issue(principal: Principal, *, order_code: str) -> None:
    """Forcing an order forward on a short pick is a management decision.

    It needs the approval right rather than the picking right, and the refusal
    spells the right out the way an administrator has to grant it - the same
    shape the route gate and the direction check in :func:`post_material_issue`
    already use, so a client can name what to ask for.
    """
    if principal.has(Permission.PRODUCTION_ORDER_APPROVE):
        return
    raise AuthorizationError(
        f"Forcing a partial issue on {order_code} requires {Permission.PRODUCTION_ORDER_APPROVE}.",
        code=ErrorCode.PERMISSION_DENIED,
        details={"required_permission": str(Permission.PRODUCTION_ORDER_APPROVE)},
    )


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Segregation of duties on approval.

    ASSUMPTION: the approver of a production order may not be the person who
    raised it, unless they hold ``SUPER_ADMIN`` (or are a platform superuser).
    Approval commits material and capacity, and an order approved by its own
    author is not evidence of a second person's judgement (master rules
    section 42). Same rule and same escape hatch as BOM, routing and
    procurement, so a small team relaxes all four in one place.
    """
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


def is_editable(status: ProductionOrderStatus) -> bool:
    """Only a draft may be edited. After approval the order is a commitment."""
    return status is ProductionOrderStatus.DRAFT


def net_issued(component: ProductionOrderComponent) -> Decimal:
    """Issued minus returned: what the job is actually holding."""
    return to_quantity(Decimal(component.issued_quantity) - Decimal(component.returned_quantity))


def outstanding_requirement(component: ProductionOrderComponent) -> Decimal:
    """How much of this component the job still has to be given."""
    return max(ZERO, to_quantity(Decimal(component.required_quantity) - net_issued(component)))


def issue_ceiling(required_quantity: Decimal) -> Decimal:
    """The most that may ever be issued against one requirement."""
    return to_quantity(Decimal(required_quantity) * OVER_ISSUE_MULTIPLIER)


def assert_within_over_issue_tolerance(
    *,
    item_code: str,
    line_number: int,
    required_quantity: Decimal,
    already_issued: Decimal,
    incoming_quantity: Decimal,
) -> None:
    """Refuse an issue that would push a component past its tolerance.

    Checked against the *locked* component row at posting time; the same check
    at draft time is advisory, because another storekeeper may issue in between.
    """
    proposed = to_quantity(Decimal(already_issued) + Decimal(incoming_quantity))
    ceiling = issue_ceiling(required_quantity)
    if proposed <= ceiling:
        return
    raise BusinessRuleError(
        f"Line {line_number} ({item_code}): issuing "
        f"{to_quantity(Decimal(incoming_quantity))} would bring the total issued to {proposed}, "
        f"above the {ceiling} allowed against a requirement of "
        f"{to_quantity(Decimal(required_quantity))} "
        f"({OVER_ISSUE_TOLERANCE * 100:.0f}% over-issue tolerance). "
        "Revise the order's structure if the job genuinely needs more.",
        details={
            "line_number": line_number,
            "item_code": item_code,
            "required_quantity": str(to_quantity(Decimal(required_quantity))),
            "already_issued": str(to_quantity(Decimal(already_issued))),
            "incoming_quantity": str(to_quantity(Decimal(incoming_quantity))),
            "maximum_issuable": str(ceiling),
            "tolerance_percentage": str(OVER_ISSUE_TOLERANCE * 100),
        },
    )


def assert_within_over_production_tolerance(
    *, order_code: str, planned_quantity: Decimal, already_produced: Decimal, incoming: Decimal
) -> None:
    """Refuse a booking that would take output far past the planned quantity."""
    proposed = to_quantity(Decimal(already_produced) + Decimal(incoming))
    ceiling = to_quantity(Decimal(planned_quantity) * OVER_PRODUCTION_MULTIPLIER)
    if proposed <= ceiling:
        return
    raise BusinessRuleError(
        f"{order_code}: booking {to_quantity(Decimal(incoming))} would bring output to "
        f"{proposed}, above the {ceiling} allowed for a planned quantity of "
        f"{to_quantity(Decimal(planned_quantity))} "
        f"({OVER_PRODUCTION_TOLERANCE * 100:.0f}% over-production tolerance).",
        details={
            "order_code": order_code,
            "planned_quantity": str(to_quantity(Decimal(planned_quantity))),
            "already_produced": str(to_quantity(Decimal(already_produced))),
            "incoming_quantity": str(to_quantity(Decimal(incoming))),
            "maximum_producible": str(ceiling),
            "tolerance_percentage": str(OVER_PRODUCTION_TOLERANCE * 100),
        },
    )


def mandatory_components_covered(components: Sequence[ProductionOrderComponent]) -> bool:
    """Whether every component the floor has to be given has been given.

    Backflushed components are excluded: by definition they are issued
    automatically at completion, so waiting for them would leave a job that can
    never reach ``MATERIAL_ISSUED``.
    """
    return all(
        net_issued(component) >= Decimal(component.required_quantity)
        for component in components
        if not component.is_backflushed
    )


def plan_reservation(
    required_quantity: Decimal, available_quantity: Decimal
) -> tuple[Decimal, Decimal]:
    """Split a requirement into what can be held now and what is short.

    Returns ``(reserve, shortage)``. Reserving the available part rather than
    refusing the whole line is what lets a planner release a job knowing exactly
    which parts still have to be found - and it stops somebody else taking the
    stock that *is* there while they look.
    """
    required = to_quantity(required_quantity)
    available = to_quantity(available_quantity)
    if available <= ZERO:
        return ZERO, required
    if available >= required:
        return required, ZERO
    return available, to_quantity(required - available)


def backflush_quantity(
    *,
    required_quantity: Decimal,
    planned_quantity: Decimal,
    produced_quantity: Decimal,
    already_issued: Decimal,
) -> Decimal:
    """How much of a backflushed component completion still has to issue.

    Scaled by what was actually produced, not by what was planned: a job that
    made 80 of 100 consumed 80 units' worth of material, and charging it the
    full kit would overstate the cost of every short run.

    ASSUMPTION: output above plan backflushes proportionally too, so a job that
    over-delivers is charged for the material it really used.
    """
    planned = to_quantity(planned_quantity)
    if planned <= ZERO:  # pragma: no cover - a CHECK constraint forbids it
        return ZERO
    entitled = to_quantity(Decimal(required_quantity) * to_quantity(produced_quantity) / planned)
    return max(ZERO, to_quantity(entitled - to_quantity(already_issued)))


def plan_work_orders(
    operations: Sequence[RoutingStep],
    *,
    planned_quantity: Decimal,
    start_at: datetime,
) -> list[WorkOrderPlan]:
    """Turn a routing into a schedule for one job.

    ASSUMPTION: operations are scheduled strictly back to back, each starting
    when the previous one finishes. Real shops overlap a long run with the next
    setup, but modelling that needs a transfer-batch size the routing does not
    carry, and the sequential schedule is the conservative one - it never
    promises a date the plant cannot hit. The same assumption the routing
    module's lead-time calculation already makes, so the two agree.
    """
    quantity = to_quantity(planned_quantity)
    plans: list[WorkOrderPlan] = []
    cursor = start_at
    for operation in sorted(operations, key=lambda o: o.sequence):
        elapsed = calculate_operation_time(operation, quantity)
        # Seconds, as an int: a schedule is a duration, and converting a
        # Decimal through float to build one would reintroduce the binary
        # rounding the rest of the module is careful to avoid.
        finish = cursor + timedelta(seconds=int(elapsed * 60))
        plans.append(
            WorkOrderPlan(
                sequence=operation.sequence,
                routing_operation_id=operation.id,
                operation_code=operation.operation_code,
                operation_name=operation.operation_name,
                work_center_id=operation.work_center_id,
                machine_id=operation.machine_id,
                planned_setup_minutes=to_quantity(operation.setup_time_minutes),
                planned_run_minutes=to_quantity(
                    Decimal(operation.run_time_per_unit_minutes) * quantity
                ),
                process_cost=to_money(operation.process_cost),
                planned_start_at=cursor,
                planned_end_at=finish,
            )
        )
        cursor = finish
    return plans


def _elapsed_minutes(start: datetime, end: datetime) -> Decimal:
    """Exact minutes between two instants, without touching binary floating point.

    ``timedelta.total_seconds()`` returns a **float**, and this number is
    multiplied by a work-centre rate and stored on a closed job. A float here is
    not a theoretical impurity: an operation clocked at 137.253000 seconds costs
    out at 2.2875 minutes through ``total_seconds()`` and 2.2876 minutes in exact
    arithmetic, because the binary value lands a hair below the HALF_UP boundary.
    Roughly one clocked operation in sixteen thousand is charged the wrong
    number of minutes that way, and a closed job's stored cost stops being
    reproducible from its own timestamps. The components of a ``timedelta`` are
    integers, so summing them keeps the whole calculation decimal.
    """
    delta = end - start
    seconds = (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    return seconds / SECONDS_PER_MINUTE


def charged_minutes(operation: OperationCostInput) -> tuple[Decimal, bool]:
    """Minutes this operation is costed for, and whether they were measured.

    ASSUMPTION about the three cases the shop floor actually produces:

    * clocked (both actual times present) - charge the elapsed time. Downtime
      recorded on the bookings is *inside* that elapsed time and is deliberately
      not deducted: the centre was occupied by this job while it stood idle.
    * completed but never clocked - charge the planned setup plus run. The
      operation certainly happened, and costing it at zero would understate
      every job whose operators forgot to press start.
    * pending or cancelled and never clocked - charge nothing. It did not happen,
      and charging planned time for work never done would inflate the cost of
      every short-closed job.
    """
    if operation.actual_start_at is not None and operation.actual_end_at is not None:
        elapsed = _elapsed_minutes(operation.actual_start_at, operation.actual_end_at)
        return to_quantity(max(ZERO, elapsed)), True
    if operation.status is WorkOrderStatus.COMPLETED:
        return to_quantity(
            Decimal(operation.planned_setup_minutes) + Decimal(operation.planned_run_minutes)
        ), False
    return ZERO, True


def operation_cost_line(operation: OperationCostInput) -> ProductionCostOperationLine:
    """Cost one operation at its work centre's rate.

    The work centre's flat ``setup_cost`` is charged once per operation that
    actually ran - which is precisely what the routing module documents it for,
    and why the routing cost roll-up deliberately leaves it out.
    """
    minutes, is_actual = charged_minutes(operation)
    ran = minutes > ZERO or operation.status is WorkOrderStatus.COMPLETED
    time_cost = to_money(minutes / MINUTES_PER_HOUR * Decimal(operation.cost_per_hour))
    setup_cost = to_money(operation.work_centre_setup_cost) if ran else ZERO
    process_cost = to_money(operation.process_cost) if ran else ZERO
    return ProductionCostOperationLine(
        work_order_id=operation.work_order_id,
        sequence=operation.sequence,
        operation_code=operation.operation_code,
        work_center_id=operation.work_center_id,
        status=operation.status,
        charged_minutes=minutes,
        is_actual_time=is_actual,
        cost_per_hour=to_money(operation.cost_per_hour),
        time_cost=time_cost,
        setup_cost=setup_cost,
        process_cost=process_cost,
        total_cost=to_money(time_cost + setup_cost + process_cost),
    )


def material_cost_line(
    component: ProductionOrderComponent, totals: IssuedComponentTotals | None
) -> ProductionCostMaterialLine:
    """Cost one component at what its movements were actually valued at.

    Never at standard cost: the whole point of closing a job is to find out what
    it really cost, and the movement carried the weighted average the inventory
    engine applied when the material left the store.
    """
    issued = totals.issued_quantity if totals else ZERO
    returned = totals.returned_quantity if totals else ZERO
    net_quantity = totals.net_quantity if totals else ZERO
    net_cost = to_money(totals.net_cost) if totals else ZERO
    unit_cost = to_unit_price(net_cost / net_quantity) if net_quantity > ZERO else ZERO
    return ProductionCostMaterialLine(
        component_id=component.id,
        item_id=component.item_id,
        item_code=component.item_code,
        required_quantity=to_quantity(component.required_quantity),
        issued_quantity=to_quantity(issued),
        returned_quantity=to_quantity(returned),
        net_issued_quantity=to_quantity(net_quantity),
        unit_cost=unit_cost,
        total_cost=net_cost,
    )


def summarise_cost(
    *,
    order: ProductionOrder,
    materials: Sequence[ProductionCostMaterialLine],
    operations: Sequence[ProductionCostOperationLine],
    is_final: bool,
) -> ProductionCostRead:
    """Add the two halves up.

    Totals are the sum of the *rounded* line costs rather than the rounding of an
    unrounded sum, so the breakdown a user reads always adds up to the total
    printed under it - the same convention invoicing and the routing roll-up use.
    """
    material_cost = sum((line.total_cost for line in materials), start=ZERO)
    operation_cost = sum((line.total_cost for line in operations), start=ZERO)
    total = to_money(material_cost + operation_cost)
    produced = to_quantity(order.produced_quantity)
    return ProductionCostRead(
        production_order_id=order.id,
        code=order.code,
        status=ProductionOrderStatus(order.status),
        is_final=is_final,
        planned_quantity=to_quantity(order.planned_quantity),
        produced_quantity=produced,
        material_cost=to_money(material_cost),
        operation_cost=to_money(operation_cost),
        total_cost=total,
        cost_per_unit=to_money(total / produced) if produced > ZERO else None,
        materials=list(materials),
        operations=list(operations),
        uncosted_operation_count=sum(
            1 for line in operations if not line.is_actual_time and line.charged_minutes > ZERO
        ),
    )


def plan_components(
    explosion: bom_engine.BomExplosion,
    *,
    items: dict[uuid.UUID, ProductionItem],
    fallback_warehouse_id: uuid.UUID,
    component_warehouse_id: uuid.UUID | None,
    backflush_item_ids: frozenset[uuid.UUID],
) -> list[ComponentPlan]:
    """Turn an explosion into the snapshot rows release will write.

    Warehouse resolution, in order: the store the release names, the item's own
    default store, then the job's output store. A component with no home of its
    own is drawn from where the job lives, which is the only answer that cannot
    be wrong for a single-store plant.

    ASSUMPTION: a batch- or serial-tracked component may not be backflushed.
    Backflushing posts a movement nobody is looking at, and the inventory engine
    requires a lot or a serial on every movement of such an item - there is
    nothing to pick it automatically from.
    """
    plans: list[ComponentPlan] = []
    for index, component in enumerate(explosion.components, start=1):
        item = items.get(component.item_id)
        warehouse_id = (
            component_warehouse_id
            or (item.default_warehouse_id if item else None)
            or fallback_warehouse_id
        )
        tracked = bool(item and (item.is_batch_tracked or item.is_serial_tracked))
        plans.append(
            ComponentPlan(
                line_number=index,
                item_id=component.item_id,
                item_code=component.item_code,
                uom_id=component.uom_id,
                bom_level=component.level,
                # The scrap-adjusted figure is the one to plan against: the
                # allowance is consumed by the process, so holding the
                # un-inflated quantity is how a job runs short on its last piece.
                required_quantity=to_quantity(component.scrap_adjusted_quantity),
                warehouse_id=warehouse_id,
                is_backflushed=component.item_id in backflush_item_ids and not tracked,
            )
        )
    return plans


# =============================================================================
# Shared helpers
# =============================================================================


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    machine: Any,
    entity: Any,
    entity_type: str,
    entity_code: str | None,
    target: Any,
    reason: str,
    status_enum: Any,
    action: AuditAction = AuditAction.PRODUCTION_STATUS_CHANGE,
) -> None:
    """Move a document's status and record it.

    Every status change in this module funnels through here, so there is exactly
    one place that writes a production status column and exactly one place that
    writes the matching audit row (master rules section 73).
    """
    current = status_enum(entity.status)
    machine.assert_transition(current, target)
    entity.status = target
    if hasattr(entity, "updated_by_id"):
        entity.updated_by_id = tenant.user_id
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=entity_type,
        entity_id=entity.id,
        entity_code=entity_code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )


async def _load_items(
    session: AsyncSession, tenant: TenantContext, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ProductionItem]:
    """Batched item facts, with every unknown id reported at once."""
    items = await ProductionItemRepository(session, tenant).load(item_ids)
    missing = [i for i in dict.fromkeys(item_ids) if i not in items]
    if missing:
        raise NotFoundError("Item", ", ".join(str(i) for i in missing))
    return items


async def _assert_warehouse(
    session: AsyncSession, tenant: TenantContext, warehouse_id: uuid.UUID
) -> None:
    if not await WarehouseLookupRepository(session, tenant).exists_in_tenant(warehouse_id):
        raise NotFoundError("Warehouse", warehouse_id)


async def _assert_line_positions(
    session: AsyncSession,
    tenant: TenantContext,
    positions: dict[int, tuple[uuid.UUID, uuid.UUID | None]],
) -> None:
    """Every store and bin a document's lines name, checked inside this tenant.

    Two statements for the whole document, never one per line.

    The header warehouse is checked by the caller; the *line* overrides were not
    checked anywhere. Both end up on a stock movement: the warehouse would at
    least be refused by the inventory engine at posting time - leaving a draft
    that can never post - but ``location_id`` is validated by nothing at all.
    Neither the foreign key nor the movement engine looks at the company, so a
    line naming another tenant's bin posts a real movement and keys an
    ``inventory_balances`` row on a position this company cannot see, and the
    stock it holds becomes invisible to every location report either tenant runs.
    A bin must also belong to the store the line draws from, or the balance grain
    describes a place that does not exist.
    """
    if not positions:
        return

    warehouse_ids = [warehouse_id for warehouse_id, _ in positions.values()]
    known_warehouses = await WarehouseLookupRepository(session, tenant).existing_ids(warehouse_ids)
    missing_warehouse = sorted(
        line_number
        for line_number, (warehouse_id, _) in positions.items()
        if warehouse_id not in known_warehouses
    )
    if missing_warehouse:
        raise NotFoundError(
            "Warehouse",
            ", ".join(str(positions[line_number][0]) for line_number in missing_warehouse),
        )

    location_ids = [location_id for _, location_id in positions.values() if location_id is not None]
    if not location_ids:
        return
    location_warehouses = await StorageLocationLookupRepository(session, tenant).warehouses_of(
        location_ids
    )

    for line_number, (warehouse_id, location_id) in sorted(positions.items()):
        if location_id is None:
            continue
        owner = location_warehouses.get(location_id)
        if owner is None:
            raise NotFoundError("Storage location", location_id)
        if owner != warehouse_id:
            raise BusinessRuleError(
                f"Line {line_number}: the named storage location is not in the "
                "warehouse this line draws from.",
                details={
                    "line_number": line_number,
                    "location_id": str(location_id),
                    "warehouse_id": str(warehouse_id),
                },
            )


async def _assert_operator(
    session: AsyncSession, tenant: TenantContext, user_id: uuid.UUID | None
) -> None:
    if user_id is None:
        return
    if user_id not in await OperatorLookupRepository(session, tenant).exist_in_tenant([user_id]):
        raise NotFoundError("User", user_id)


async def _assert_machine(
    session: AsyncSession,
    tenant: TenantContext,
    machine_id: uuid.UUID | None,
    *,
    work_center_id: uuid.UUID | None = None,
) -> None:
    """A machine must exist in this tenant and, when a centre is named, in it.

    The composite foreign key already refuses the mismatch; checking here turns
    an opaque integrity error into a message naming the machine.
    """
    if machine_id is None:
        return
    machines = await MachineRepository(session, tenant).load([machine_id])
    machine = machines.get(machine_id)
    if machine is None:
        raise NotFoundError("Machine", machine_id)
    if work_center_id is not None and machine.work_center_id != work_center_id:
        raise BusinessRuleError(
            f"Machine {machine.code} does not belong to this operation's work centre.",
            details={"machine_id": str(machine_id), "work_center_id": str(work_center_id)},
        )


def _order_read(order: ProductionOrder) -> ProductionOrderRead:
    return ProductionOrderRead.model_validate(order)


def issue_detail(issue: MaterialIssue, lines: Sequence[MaterialIssueLine]) -> MaterialIssueDetail:
    """Assemble the detail from the header and its already-loaded lines.

    Built field by field rather than with ``model_validate(issue)``: the ``lines``
    relationship is ``lazy="raise"``, and Pydantic's attribute reader would trip
    over it - which is exactly the loud failure that setting is there to produce.
    """
    return MaterialIssueDetail(
        **MaterialIssueRead.model_validate(issue).model_dump(),
        lines=[MaterialIssueLineRead.model_validate(line) for line in lines],
    )


def _posted_movement(txn: InventoryTransaction) -> PostedMovement:
    return PostedMovement(
        transaction_id=txn.id,
        item_id=txn.item_id,
        warehouse_id=txn.warehouse_id,
        quantity=to_quantity(txn.quantity),
        unit_cost=to_unit_price(txn.unit_cost),
        total_cost=to_money(txn.total_cost),
        transaction_type=str(txn.transaction_type),
    )


async def _get_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionOrder:
    return await ProductionOrderRepository(session, tenant).get_or_404(order_id)


async def _lock_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionOrder:
    order = await ProductionOrderRepository(session, tenant).lock_for_update(order_id)
    if order is None:
        raise NotFoundError("Production order", order_id)
    assert_warehouse_scope(tenant, order.warehouse_id)
    return order


# =============================================================================
# Production order - reads
# =============================================================================


async def get_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionOrderRead:
    return _order_read(await _get_order(session, tenant, order_id))


async def get_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionOrderDetail:
    """Order header plus its operations, in three statements."""
    order = await _get_order(session, tenant, order_id)
    work_orders = await WorkOrderRepository(session, tenant).list_for_order(order_id)
    component_count = await ProductionOrderComponentRepository(session, tenant).count_for_order(
        order_id
    )

    # Built field by field rather than with ``model_validate(order)``: the
    # ``work_orders`` and ``components`` relationships are ``lazy="raise"``, and
    # Pydantic's attribute reader would trip over them.
    return ProductionOrderDetail(
        **ProductionOrderRead.model_validate(order).model_dump(),
        work_orders=[WorkOrderRead.model_validate(wo) for wo in work_orders],
        component_count=component_count,
    )


async def list_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: ProductionOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[ProductionOrderSummary], OffsetPagination]:
    rows, pagination = await ProductionOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [ProductionOrderSummary.model_validate(row) for row in rows], pagination


# =============================================================================
# Production order - create, update, delete
# =============================================================================


async def _validate_order_references(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    plant_id: uuid.UUID | None,
    bom_id: uuid.UUID | None,
    routing_id: uuid.UUID | None,
) -> ProductionItem:
    """Every reference on the order, checked inside this tenant."""
    items = await _load_items(session, tenant, [item_id])
    item = items[item_id]
    # Master rules section 1: the capability flag decides what an item may be
    # used for, never its reporting group.
    if not item.can_manufacture:
        raise BusinessRuleError(
            f"Item {item.code} is not flagged as manufacturable.",
            details={"item_code": item.code, "item_id": str(item_id)},
        )
    if not item.is_active:
        raise BusinessRuleError(
            f"Item {item.code} is inactive and cannot be produced.",
            details={"item_code": item.code},
        )

    await _assert_warehouse(session, tenant, warehouse_id)
    if plant_id is not None and not await PlantLookupRepository(session, tenant).exists_in_tenant(
        plant_id
    ):
        raise NotFoundError("Plant", plant_id)

    if bom_id is not None:
        bom = await BomRepository(session, tenant).get_or_404(bom_id)
        if bom.item_id != item_id:
            raise BusinessRuleError(
                "The named bill of materials does not belong to this item.",
                details={"bom_id": str(bom_id), "item_id": str(item_id)},
            )
    if routing_id is not None:
        routing = await RoutingRepository(session, tenant).get_or_404(routing_id)
        if routing.item_id != item_id:
            raise BusinessRuleError(
                "The named routing does not belong to this item.",
                details={"routing_id": str(routing_id), "item_id": str(item_id)},
            )
    return item


async def create_order(
    session: AsyncSession, tenant: TenantContext, payload: ProductionOrderCreate
) -> ProductionOrder:
    """Raise a DRAFT job. Nothing is planned against stock or capacity yet."""
    async with transaction(session):
        item = await _validate_order_references(
            session,
            tenant,
            item_id=payload.item_id,
            warehouse_id=payload.warehouse_id,
            plant_id=payload.plant_id,
            bom_id=payload.bom_id,
            routing_id=payload.routing_id,
        )
        # ASSUMPTION: the cost centre is explicit or absent - an order raised
        # from a planning suggestion or a sales order carries none, because
        # neither source names one. See docs/assumptions.md §42.
        if payload.cost_centre_id is not None:
            await require_cost_centre(session, tenant, payload.cost_centre_id)

        repo = ProductionOrderRepository(session, tenant)
        order = ProductionOrder(
            code=await next_document_number(session, tenant.company_id, DocTypes.PRODUCTION_ORDER),
            item_id=payload.item_id,
            bom_id=payload.bom_id,
            routing_id=payload.routing_id,
            planned_quantity=to_quantity(payload.planned_quantity),
            warehouse_id=payload.warehouse_id,
            plant_id=payload.plant_id,
            cost_centre_id=payload.cost_centre_id,
            planned_start_date=payload.planned_start_date,
            planned_end_date=payload.planned_end_date,
            priority=payload.priority,
            status=ProductionOrderStatus.DRAFT,
            source_type=payload.source_type,
            source_id=payload.source_id,
            notes=payload.notes,
        )
        repo.add(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Raised production order {order.code} for {order.planned_quantity} {item.code}"
            ),
            user_id=tenant.user_id,
            new_values={
                "item_code": item.code,
                "planned_quantity": order.planned_quantity,
                "warehouse_id": order.warehouse_id,
                "cost_centre_id": order.cost_centre_id,
                "planned_start_date": order.planned_start_date,
                "planned_end_date": order.planned_end_date,
                "priority": order.priority,
                "source_type": str(order.source_type),
                "source_id": order.source_id,
            },
        )
    return order


def mrp_receiving_warehouse(
    *, scope: Sequence[uuid.UUID], default_warehouse_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Where an order raised from a planning suggestion receives its output.

    The run's scope beats the item master: a run is executed *for* a set of
    warehouses, so a suggestion made in the spares store is built for the spares
    store even when the item's default is the main one. A run scoped to nothing
    has no warehouse to pass on, so the item master decides - and ``None`` here
    means there is nowhere to receive, which the caller refuses.
    """
    if not scope:
        return default_warehouse_id
    # ASSUMPTION: a run scoped to several warehouses pools their stock, so a
    # suggestion has no warehouse of its own. The order is received into the
    # item's default warehouse when that lies inside the run's scope, otherwise
    # into the first warehouse the run named - a deterministic choice rather
    # than one that depends on the order the planner ticked the boxes in. See
    # docs/assumptions.md section 31.
    if default_warehouse_id is not None and default_warehouse_id in scope:
        return default_warehouse_id
    return scope[0]


async def create_order_from_mrp(
    session: AsyncSession, tenant: TenantContext, request: Any
) -> tuple[uuid.UUID, str]:
    """Planning's conversion hook (``mrp.service.CONVERSION_HOOKS``).

    Resolved by name at call time by the MRP module, which is why the request is
    typed loosely here: importing ``mrp.schemas`` would make production depend on
    a module that is built independently of it and may not be deployed.

    Runs inside the caller's transaction, so the suggestion and the order it
    became commit together or not at all.
    """
    items = await ProductionItemRepository(session, tenant).load([request.item_id])
    item = items.get(request.item_id)
    if item is None:
        raise NotFoundError("Item", request.item_id)

    warehouse_id = mrp_receiving_warehouse(
        scope=list(request.warehouse_ids or ()), default_warehouse_id=item.default_warehouse_id
    )
    if warehouse_id is None:
        raise BusinessRuleError(
            f"Item {item.code} has no default warehouse and the planning run named none, "
            "so there is nowhere to receive the output.",
            details={"item_code": item.code, "suggestion_id": str(request.suggestion_id)},
        )

    # The suggested order date is when the job must start to hit the required
    # date; a suggestion already late is scheduled to start today rather than in
    # the past, which would produce an order that reads as overdue on creation.
    start = max(request.suggested_order_date, today())
    order = await create_order(
        session,
        tenant,
        ProductionOrderCreate(
            item_id=request.item_id,
            planned_quantity=to_quantity(request.quantity),
            warehouse_id=warehouse_id,
            planned_start_date=start,
            planned_end_date=max(request.required_date, start),
            source_type=ProductionSourceType.MRP_SUGGESTION,
            source_id=request.suggestion_id,
            notes=request.notes,
        ),
    )
    return order.id, order.code


async def update_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: ProductionOrderUpdate,
) -> ProductionOrder:
    """Edit a draft. After approval the order is a commitment, not a plan."""
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if not is_editable(ProductionOrderStatus(order.status)):
            raise BusinessRuleError(
                f"{order.code} is {order.status} and can no longer be edited. "
                "Cancel it and raise a new order.",
                details={"status": str(order.status)},
            )

        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return order

        before = {key: getattr(order, key) for key in changes}
        await _validate_order_references(
            session,
            tenant,
            item_id=order.item_id,
            warehouse_id=changes.get("warehouse_id", order.warehouse_id),
            plant_id=changes.get("plant_id", order.plant_id),
            bom_id=changes.get("bom_id", order.bom_id),
            routing_id=changes.get("routing_id", order.routing_id),
        )
        # A new cost centre is validated; the one the draft already carries is
        # not re-checked - a document keeps its reference after deactivation.
        new_centre = changes.get("cost_centre_id")
        if new_centre is not None and new_centre != order.cost_centre_id:
            await require_cost_centre(session, tenant, new_centre)

        if "planned_quantity" in changes:
            changes["planned_quantity"] = to_quantity(changes["planned_quantity"])
        for key, value in changes.items():
            setattr(order, key, value)

        start = order.planned_start_date
        if order.planned_end_date < start:
            raise BusinessRuleError(
                "The planned finish cannot be before the planned start.",
                details={
                    "planned_start_date": str(start),
                    "planned_end_date": str(order.planned_end_date),
                },
            )

        ProductionOrderRepository(session, tenant).touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated production order {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=changes,
        )
    return order


async def delete_draft_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    """Discard a draft.

    A hard delete rather than a soft one: ``production_orders`` is a document,
    not master data, and section 28 reserves soft deletion for records history
    points at. A draft has no components, no work orders and no movements, so
    there is nothing pointing at it - and the audit row survives it, carrying
    ``reason``: a deletion is never unexplained (master rules section 83).
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if not is_editable(ProductionOrderStatus(order.status)):
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only a draft may be deleted. Cancel it instead.",
                details={"status": str(order.status)},
            )
        code = order.code
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=code,
            summary=f"Deleted draft production order {code}",
            user_id=tenant.user_id,
            old_values={
                "item_id": order.item_id,
                "planned_quantity": order.planned_quantity,
                "status": str(order.status),
            },
            context={"reason": reason},
        )
        await ProductionOrderRepository(session, tenant).hard_delete(order)


# =============================================================================
# Production order - lifecycle
# =============================================================================


async def approve_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    payload: ApproveRequest,
) -> ProductionOrder:
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if not can_approve(principal, order.created_by_id):
            raise AuthorizationError(
                "A production order cannot be approved by the person who raised it.",
                code=ErrorCode.APPROVAL_REQUIRED,
            )
        order.approved_by_id = principal.user_id
        order.approved_at = utcnow()
        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.APPROVED,
            reason=payload.note or "Approved",
            status_enum=ProductionOrderStatus,
            action=AuditAction.APPROVE,
        )
    return order


async def _resolve_bom(session: AsyncSession, tenant: TenantContext, order: ProductionOrder) -> Bom:
    """The structure this job is built against, and it must be approved.

    A named structure is honoured; otherwise the item's single default approved
    structure is taken. Either way the status is checked here rather than left
    to the explosion, so releasing against a draft fails with a message a
    planner can act on.
    """
    repo = BomRepository(session, tenant)
    if order.bom_id is not None:
        bom = await repo.get_or_404(order.bom_id)
    else:
        found = await repo.default_approved_for_item(order.item_id)
        if found is None:
            raise BusinessRuleError(
                f"No approved default bill of materials exists for the item on {order.code}.",
                code=ErrorCode.BOM_NOT_FOUND,
                details={"item_id": str(order.item_id), "order_code": order.code},
            )
        bom = found

    if BomStatus(bom.status) is not BomStatus.APPROVED:
        raise BusinessRuleError(
            f"Bill of materials {bom.code} is {bom.status}; a job may only be released "
            "against an approved structure.",
            code=ErrorCode.BOM_NOT_APPROVED,
            details={"bom_id": str(bom.id), "bom_code": bom.code, "status": str(bom.status)},
        )
    return bom


async def _resolve_routing(
    session: AsyncSession, tenant: TenantContext, order: ProductionOrder, *, as_of: date
) -> Routing | None:
    """The process this job follows, if the item has one.

    A missing routing is legitimate - a single-operation item may have none - so
    this returns ``None`` rather than raising. A *named* routing that is not
    approved is an error: somebody chose it deliberately.
    """
    repo = RoutingRepository(session, tenant)
    if order.routing_id is not None:
        routing = await repo.get_or_404(order.routing_id)
        if RoutingStatus(routing.status) is not RoutingStatus.APPROVED:
            raise BusinessRuleError(
                f"Routing {routing.code} is {routing.status}; a job may only be released "
                "against an approved process.",
                details={"routing_id": str(routing.id), "status": str(routing.status)},
            )
        return routing
    effective = await repo.effective_routings_for_items([order.item_id], as_of)
    return effective.get(order.item_id)


async def _reserve_components(
    session: AsyncSession,
    tenant: TenantContext,
    order: ProductionOrder,
    components: Sequence[ProductionOrderComponent],
    availability: dict[tuple[uuid.UUID, uuid.UUID], Decimal],
) -> list[ComponentShortage]:
    """Hold what is there, and report what is not.

    Availability is read in one batched statement *before* this loop, and each
    reservation is then taken for the smaller of the requirement and what was
    free. ``allocation.reserve`` re-reads the balance under its own row lock, so
    a component that somebody else took in between comes back as
    ``InsufficientStockError`` - which is recorded as a shortage rather than
    failing the release, for the same reason a known shortage is.

    Nothing loaded before this loop is written inside it: a caught failure rolls
    back the reservation's savepoint, and SQLAlchemy expires the rows that
    savepoint touched. Every result is therefore accumulated in plain values.
    """
    shortages: list[ComponentShortage] = []
    for component in components:
        required = to_quantity(component.required_quantity)
        available = availability.get((component.item_id, component.warehouse_id), ZERO)
        reserve_quantity, shortage_quantity = plan_reservation(required, available)
        detail: str | None = None

        if reserve_quantity > ZERO:
            try:
                await allocation_service.reserve(
                    session,
                    tenant,
                    item_id=component.item_id,
                    warehouse_id=component.warehouse_id,
                    quantity=reserve_quantity,
                    demand_type=DemandType.PRODUCTION_ORDER,
                    demand_id=order.id,
                    demand_line_id=component.id,
                    demand_code=order.code,
                    reservation_type=ReservationType.HARD,
                    priority=order.priority,
                    required_date=order.planned_start_date,
                    reason=f"Released production order {order.code}",
                )
            except BusinessRuleError as exc:
                # Includes InsufficientStockError: somebody took the stock
                # between the availability read and the lock.
                reserve_quantity, shortage_quantity = ZERO, required
                detail = exc.message

        if shortage_quantity > ZERO:
            shortages.append(
                ComponentShortage(
                    component_id=component.id,
                    item_id=component.item_id,
                    item_code=component.item_code,
                    warehouse_id=component.warehouse_id,
                    required_quantity=required,
                    reserved_quantity=reserve_quantity,
                    shortage_quantity=shortage_quantity,
                    detail=detail,
                )
            )
    return shortages


async def release_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: ReleaseRequest,
) -> ReleaseResult:
    """Explode the structure, freeze it, issue the traveller and hold the stock.

    All of it in one transaction: a job that is RELEASED with no components, or
    with components nothing is holding stock for, is worse than a job that was
    never released.
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        # Fail before any work: the state machine decides whether this job can
        # be released at all.
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus(order.status), ProductionOrderStatus.RELEASED
        )

        as_of = payload.as_of or order.planned_start_date
        bom = await _resolve_bom(session, tenant, order)
        routing = await _resolve_routing(session, tenant, order, as_of=as_of)

        explosion = await bom_engine.explode(
            session,
            tenant,
            order.item_id,
            to_quantity(order.planned_quantity),
            bom_id=bom.id,
            as_of=as_of,
        )
        if not explosion.components:
            raise BusinessRuleError(
                f"Bill of materials {bom.code} explodes to no components; there is nothing "
                "to issue to the floor.",
                details={"bom_code": bom.code},
            )
        if len(explosion.components) > MAX_COMPONENTS_PER_ORDER:
            raise BusinessRuleError(
                f"{bom.code} explodes to {len(explosion.components)} components, above the "
                f"{MAX_COMPONENTS_PER_ORDER} a single job may carry.",
                details={"component_count": len(explosion.components)},
            )

        if payload.component_warehouse_id is not None:
            await _assert_warehouse(session, tenant, payload.component_warehouse_id)

        items = await _load_items(session, tenant, [c.item_id for c in explosion.components])
        plans = plan_components(
            explosion,
            items=items,
            fallback_warehouse_id=order.warehouse_id,
            component_warehouse_id=payload.component_warehouse_id,
            backflush_item_ids=frozenset(payload.backflush_item_ids),
        )

        component_repo = ProductionOrderComponentRepository(session, tenant)
        components = [
            component_repo.add(
                ProductionOrderComponent(
                    production_order_id=order.id,
                    line_number=plan.line_number,
                    item_id=plan.item_id,
                    item_code=plan.item_code,
                    uom_id=plan.uom_id,
                    bom_level=plan.bom_level,
                    required_quantity=plan.required_quantity,
                    warehouse_id=plan.warehouse_id,
                    is_backflushed=plan.is_backflushed,
                )
            )
            for plan in plans
        ]

        work_orders = await _create_work_orders(session, tenant, order, routing)

        order.bom_id = bom.id
        if routing is not None:
            order.routing_id = routing.id
        await session.flush()

        # The status change and the audit row are written before the reservation
        # loop: a rolled-back reservation savepoint must not be able to expire an
        # unflushed change made to the order.
        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.RELEASED,
            reason=payload.note or f"Released against {bom.code}",
            status_enum=ProductionOrderStatus,
        )

        shortages: list[ComponentShortage] = []
        if payload.reserve_material:
            availability = await ComponentAvailabilityRepository(session, tenant).available_for(
                [(c.item_id, c.warehouse_id) for c in components]
            )
            shortages = await _reserve_components(session, tenant, order, components, availability)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Released {order.code}: {len(components)} component(s) snapshotted from "
                f"{bom.code}, {len(work_orders)} operation(s), "
                f"{len(shortages)} short"
            ),
            user_id=tenant.user_id,
            new_values={
                "bom_code": bom.code,
                "routing_id": routing.id if routing else None,
                "component_count": len(components),
                "work_order_count": len(work_orders),
                "shortage_count": len(shortages),
                "reserved": payload.reserve_material,
                "shortages": [
                    {"item_code": s.item_code, "short": s.shortage_quantity} for s in shortages
                ],
            },
        )

        result = ReleaseResult(
            order=_order_read(order),
            bom_id=bom.id,
            bom_code=bom.code,
            routing_id=routing.id if routing else None,
            component_count=len(components),
            work_order_count=len(work_orders),
            fully_reserved=payload.reserve_material and not shortages,
            shortages=shortages,
            message=(
                f"{order.code} released with {len(components)} component(s)."
                if not shortages
                else (
                    f"{order.code} released, but {len(shortages)} component(s) could not be "
                    "fully held. The job can still be issued against what was found."
                )
            ),
        )
    return result


async def _create_work_orders(
    session: AsyncSession,
    tenant: TenantContext,
    order: ProductionOrder,
    routing: Routing | None,
) -> list[WorkOrder]:
    """Issue one work order per routing operation.

    Document numbers are claimed one per operation because a work order is a
    shop-floor document in its own right - it is printed, signed and booked
    against - and a routing is bounded, so the loop is bounded with it.
    """
    if routing is None:
        return []

    operations = await RoutingOperationRepository(session, tenant).list_for_routing(routing.id)
    if not operations:
        raise BusinessRuleError(
            f"Routing {routing.code} has no operations; there is nothing to issue to the floor.",
            details={"routing_code": routing.code},
        )

    start_at = datetime.combine(order.planned_start_date, datetime.min.time(), tzinfo=UTC)
    plans = plan_work_orders(operations, planned_quantity=order.planned_quantity, start_at=start_at)

    # The rates the job is costed at, read once for every centre on the routing
    # and frozen onto the traveller. See ``WorkOrder.work_center_cost_per_hour``
    # for why an actual cost may not float with a later rate revision.
    rates = await WorkCentreRateRepository(session, tenant).rates(
        [plan.work_center_id for plan in plans]
    )

    repo = WorkOrderRepository(session, tenant)
    created: list[WorkOrder] = []
    for plan in plans:
        cost_per_hour, setup_cost = rates.get(plan.work_center_id, (ZERO, ZERO))
        created.append(
            repo.add(
                WorkOrder(
                    code=await next_document_number(
                        session, tenant.company_id, DocTypes.WORK_ORDER
                    ),
                    production_order_id=order.id,
                    sequence=plan.sequence,
                    routing_operation_id=plan.routing_operation_id,
                    operation_code=plan.operation_code,
                    operation_name=plan.operation_name,
                    work_center_id=plan.work_center_id,
                    machine_id=plan.machine_id,
                    planned_start_at=plan.planned_start_at,
                    planned_end_at=plan.planned_end_at,
                    planned_quantity=to_quantity(order.planned_quantity),
                    planned_setup_minutes=plan.planned_setup_minutes,
                    planned_run_minutes=plan.planned_run_minutes,
                    process_cost=plan.process_cost,
                    work_center_cost_per_hour=to_money(cost_per_hour),
                    work_center_setup_cost=to_money(setup_cost),
                    status=WorkOrderStatus.PENDING,
                )
            )
        )
    await session.flush()
    return created


async def start_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: StartRequest,
) -> ProductionOrder:
    """Put the job under way. From here it can no longer be cancelled."""
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        order.actual_start_date = payload.actual_start_date or today()
        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.IN_PROCESS,
            reason=payload.note or "Production started",
            status_enum=ProductionOrderStatus,
        )
    return order


async def complete_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CompleteRequest,
) -> CompleteResult:
    """Finish the job: book the last output, backflush, and account the material.

    Three effects, in this order and in one transaction:

    1. When a quantity is given, a final production entry is booked through the
       ordinary entry path - the only place that posts ``PRODUCTION_RECEIPT``.
    2. Components marked for backflush are issued automatically, in proportion
       to what was actually produced, as one system-raised issue document.
    3. Every component's ``consumed_quantity`` is set to what the job is holding.
       This posts nothing: the stock left the store when it was issued.
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus(order.status), ProductionOrderStatus.COMPLETED
        )

        output_movement: PostedMovement | None = None
        if payload.produced_quantity > ZERO or payload.rejected_quantity > ZERO:
            _, movement = await _book_entry(
                session,
                tenant,
                order,
                ProductionEntryCreate(
                    work_order_id=payload.work_order_id,
                    entry_date=payload.entry_date,
                    shift=payload.shift,
                    operator_user_id=payload.operator_user_id,
                    machine_id=payload.machine_id,
                    produced_quantity=payload.produced_quantity,
                    rejected_quantity=payload.rejected_quantity,
                    rejection_reason=payload.rejection_reason,
                    notes=payload.note,
                ),
                force_output=True,
            )
            output_movement = movement

        components = await ProductionOrderComponentRepository(session, tenant).lock_for_order(
            order.id
        )
        backflush_code, backflush_movements = await _backflush_components(
            session, tenant, order, components
        )

        consumed = 0
        for component in components:
            holding = net_issued(component)
            if holding != Decimal(component.consumed_quantity):
                component.consumed_quantity = holding
                consumed += 1

        order.scrap_quantity = to_quantity(Decimal(order.scrap_quantity) + payload.scrap_quantity)
        order.actual_end_date = payload.actual_end_date or today()
        if order.actual_start_date is None:  # pragma: no cover - IN_PROCESS sets it
            order.actual_start_date = order.actual_end_date

        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.COMPLETED,
            reason=payload.note or "Production completed",
            status_enum=ProductionOrderStatus,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Completed {order.code}: {order.produced_quantity} good, "
                f"{order.rejected_quantity} rejected, {order.scrap_quantity} scrapped"
            ),
            user_id=tenant.user_id,
            new_values={
                "produced_quantity": order.produced_quantity,
                "rejected_quantity": order.rejected_quantity,
                "scrap_quantity": order.scrap_quantity,
                "components_consumed": consumed,
                "backflush_issue_code": backflush_code,
            },
        )

        result = CompleteResult(
            order=_order_read(order),
            output_movement=output_movement,
            backflush_issue_code=backflush_code,
            backflush_movements=backflush_movements,
            consumed_component_count=consumed,
            message=(f"{order.code} completed with {order.produced_quantity} good output."),
        )
    return result


async def close_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CloseRequest,
) -> CloseResult:
    """Cost the job and shut it.

    Costing is computed here and *stored*: work-centre rates and stock valuations
    both move afterwards, and a closed job's cost must not move with them.
    Anything the job was still holding is released, so a short-closed order
    cannot keep stock promised to it forever.
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus(order.status), ProductionOrderStatus.CLOSED
        )

        cost = await _compute_cost(session, tenant, order, is_final=True)
        order.total_material_cost = cost.material_cost
        order.total_operation_cost = cost.operation_cost
        order.total_cost = cost.total_cost
        order.closure_note = payload.closure_note

        released = await _release_reservations(
            session, tenant, order, reason=f"Closed production order {order.code}"
        )

        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.CLOSED,
            reason=payload.closure_note or "Closed",
            status_enum=ProductionOrderStatus,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Closed {order.code}: material {cost.material_cost}, "
                f"operations {cost.operation_cost}, total {cost.total_cost}"
            ),
            user_id=tenant.user_id,
            new_values={
                "total_material_cost": cost.material_cost,
                "total_operation_cost": cost.operation_cost,
                "total_cost": cost.total_cost,
                "cost_per_unit": cost.cost_per_unit,
                "released_reservations": released,
                "closure_note": payload.closure_note,
            },
        )
        # Rebuilt from the stored columns so the response is the number that was
        # actually persisted, not the one that was calculated a moment earlier.
        final = cost.model_copy(update={"status": ProductionOrderStatus(order.status)})
        result = CloseResult(
            order=_order_read(order),
            cost=final,
            released_reservation_count=released,
            message=f"{order.code} closed at a total cost of {cost.total_cost}.",
        )
    return result


async def cancel_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CancelRequest,
) -> ProductionOrder:
    """Cancel a job that has not physically started.

    Refused outright while any material is still out with the job. That stock is
    on the floor; cancelling around it would leave the ledger saying it was
    consumed by an order that no longer exists. Post a material return first.
    """
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        PRODUCTION_ORDER_FLOW.assert_transition(
            ProductionOrderStatus(order.status), ProductionOrderStatus.CANCELLED
        )

        components = await ProductionOrderComponentRepository(session, tenant).lock_for_order(
            order.id
        )
        outstanding = [c for c in components if net_issued(c) > ZERO]
        if outstanding:
            raise BusinessRuleError(
                f"{order.code} is still holding material for "
                f"{len(outstanding)} component(s). Return it to the store before cancelling.",
                details={
                    "order_code": order.code,
                    "components": [
                        {"item_code": c.item_code, "net_issued": str(net_issued(c))}
                        for c in outstanding
                    ],
                },
            )

        released = await _release_reservations(
            session, tenant, order, reason=f"Cancelled production order {order.code}"
        )

        cancelled_operations = 0
        for work_order in await WorkOrderRepository(session, tenant).lock_for_order(order.id):
            if WorkOrderStatus(work_order.status) in (
                WorkOrderStatus.COMPLETED,
                WorkOrderStatus.CANCELLED,
            ):
                continue
            await _transition(
                session,
                tenant,
                machine=WORK_ORDER_FLOW,
                entity=work_order,
                entity_type=AUDIT_WORK_ORDER,
                entity_code=work_order.code,
                target=WorkOrderStatus.CANCELLED,
                reason=f"Production order {order.code} cancelled",
                status_enum=WorkOrderStatus,
            )
            cancelled_operations += 1

        await _transition(
            session,
            tenant,
            machine=PRODUCTION_ORDER_FLOW,
            entity=order,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_code=order.code,
            target=ProductionOrderStatus.CANCELLED,
            reason=payload.reason,
            status_enum=ProductionOrderStatus,
            action=AuditAction.CANCEL,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CANCEL,
            entity_type=AUDIT_PRODUCTION_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Cancelled {order.code}: {payload.reason}",
            user_id=tenant.user_id,
            new_values={
                "reason": payload.reason,
                "released_reservations": released,
                "cancelled_work_orders": cancelled_operations,
            },
        )
    return order


async def _release_reservations(
    session: AsyncSession, tenant: TenantContext, order: ProductionOrder, *, reason: str
) -> int:
    """Give back everything this job is still holding."""
    reservations: list[StockReservation] = await ReservationCoverageRepository(
        session, tenant
    ).open_for_order(order.id)
    for reservation in reservations:
        await allocation_service.release(
            session, tenant, reservation_id=reservation.id, reason=reason
        )
    return len(reservations)


# =============================================================================
# Components and shortage
# =============================================================================


async def list_components(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    params: PageParams,
    sort: SortParams,
    *,
    search: str | None = None,
    shortage_only: bool = False,
) -> tuple[list[ComponentRead], OffsetPagination]:
    """The kit list, with live coverage against it.

    Three statements for the whole page - the components, what they are holding,
    and what is free - never one lookup per component (master rules section 32).
    """
    order = await _get_order(session, tenant, order_id)
    rows, pagination = await ProductionOrderComponentRepository(session, tenant).page_for_order(
        order.id, params, sort, search=search
    )
    if not rows:
        return [], pagination

    reserved = await ReservationCoverageRepository(session, tenant).remaining_by_component(
        order.id, [row.id for row in rows]
    )
    available = await ComponentAvailabilityRepository(session, tenant).available_for(
        [(row.item_id, row.warehouse_id) for row in rows]
    )

    reads: list[ComponentRead] = []
    for row in rows:
        holding = reserved.get(row.id, ZERO)
        outstanding = outstanding_requirement(row)
        read = ComponentRead.model_validate(row)
        read.net_issued_quantity = net_issued(row)
        read.reserved_quantity = to_quantity(holding)
        read.available_quantity = to_quantity(available.get((row.item_id, row.warehouse_id), ZERO))
        # What still has to be *found*: the outstanding requirement less what is
        # already promised to this job.
        read.shortage_quantity = max(ZERO, to_quantity(outstanding - holding))
        reads.append(read)

    if shortage_only:
        reads = [read for read in reads if read.shortage_quantity > ZERO]
    return reads, pagination


# =============================================================================
# Material issue and return
# =============================================================================


async def create_material_issue(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: MaterialIssueCreate,
    *,
    issue_type: MaterialIssueType,
) -> tuple[MaterialIssue, list[MaterialIssueLine]]:
    """Draft a kit to pick, or a return to put back. Nothing moves yet."""
    async with transaction(session):
        order = await _get_order(session, tenant, order_id)
        if ProductionOrderStatus(order.status) not in ISSUABLE_PRODUCTION_STATUSES:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; material moves only against a released job.",
                details={"status": str(order.status), "order_code": order.code},
            )

        component_repo = ProductionOrderComponentRepository(session, tenant)
        components = {
            component.id: component for component in await component_repo.list_for_order(order.id)
        }
        unknown = [
            str(line.component_id) for line in payload.lines if line.component_id not in components
        ]
        if unknown:
            raise NotFoundError("Production order component", ", ".join(unknown))

        header_warehouse = payload.warehouse_id or next(
            iter(components[line.component_id].warehouse_id for line in payload.lines)
        )
        await _assert_warehouse(session, tenant, header_warehouse)
        line_positions = {
            payload_line.line_number: (
                payload_line.warehouse_id or components[payload_line.component_id].warehouse_id,
                payload_line.location_id,
            )
            for payload_line in payload.lines
        }
        await _assert_line_positions(session, tenant, line_positions)

        doc_type = (
            DocTypes.MATERIAL_ISSUE
            if issue_type is MaterialIssueType.ISSUE
            else DocTypes.MATERIAL_RETURN
        )
        issue = MaterialIssue(
            code=await next_document_number(session, tenant.company_id, doc_type),
            production_order_id=order.id,
            issue_type=issue_type,
            status=MaterialIssueStatus.DRAFT,
            issue_date=payload.issue_date or today(),
            warehouse_id=header_warehouse,
            reason=payload.reason,
            notes=payload.notes,
        )
        MaterialIssueRepository(session, tenant).add(issue)
        await session.flush()

        line_repo = MaterialIssueLineRepository(session, tenant)
        lines: list[MaterialIssueLine] = []
        for payload_line in sorted(payload.lines, key=lambda line: line.line_number):
            component = components[payload_line.component_id]
            quantity = to_quantity(payload_line.quantity)
            if issue_type is MaterialIssueType.ISSUE:
                # Advisory at draft time - the binding check runs against the
                # locked component row when the document is posted.
                assert_within_over_issue_tolerance(
                    item_code=component.item_code,
                    line_number=payload_line.line_number,
                    required_quantity=component.required_quantity,
                    already_issued=net_issued(component),
                    incoming_quantity=quantity,
                )
            elif quantity > net_issued(component):
                raise BusinessRuleError(
                    f"Line {payload_line.line_number} ({component.item_code}): the job is "
                    f"holding {net_issued(component)}, so {quantity} cannot be returned.",
                    details={
                        "line_number": payload_line.line_number,
                        "item_code": component.item_code,
                        "net_issued": str(net_issued(component)),
                    },
                )

            lines.append(
                line_repo.add(
                    MaterialIssueLine(
                        material_issue_id=issue.id,
                        line_number=payload_line.line_number,
                        component_id=component.id,
                        item_id=component.item_id,
                        warehouse_id=payload_line.warehouse_id or component.warehouse_id,
                        location_id=payload_line.location_id,
                        batch_id=payload_line.batch_id,
                        quantity=quantity,
                        notes=payload_line.notes,
                    )
                )
            )

        issue.line_count = len(lines)
        issue.total_quantity = to_quantity(sum((line.quantity for line in lines), start=ZERO))
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_MATERIAL_ISSUE,
            entity_id=issue.id,
            entity_code=issue.code,
            summary=(
                f"Drafted {issue_type.lower()} {issue.code} against {order.code}: "
                f"{issue.line_count} line(s), {issue.total_quantity} units"
            ),
            user_id=tenant.user_id,
            new_values={
                "production_order_code": order.code,
                "issue_type": str(issue_type),
                "warehouse_id": issue.warehouse_id,
                "line_count": issue.line_count,
                "total_quantity": issue.total_quantity,
                "reason": issue.reason,
            },
        )
    return issue, lines


@dataclass(frozen=True, slots=True)
class _LineDraw:
    """One line's plan: how much comes off reservations, how much is free stock."""

    line: MaterialIssueLine
    component: ProductionOrderComponent
    reservation_draws: tuple[tuple[uuid.UUID, Decimal], ...]
    direct_quantity: Decimal


def _plan_line_draws(
    line: MaterialIssueLine,
    component: ProductionOrderComponent,
    reservations: Sequence[StockReservation],
) -> _LineDraw:
    """Decide which reservations an issue line draws down, oldest first.

    Only reservations at the *same stock position* are eligible: consuming one
    held against a different warehouse, location or batch would lower
    ``reserved_quantity`` on a row the movement never touches, and the two would
    permanently disagree. A reservation with no location or batch is eligible for
    a line that names one, because that is the position release held.
    """
    outstanding = to_quantity(line.quantity)
    draws: list[tuple[uuid.UUID, Decimal]] = []
    for reservation in reservations:
        if outstanding <= ZERO:
            break
        if reservation.warehouse_id != line.warehouse_id:
            continue
        if reservation.location_id is not None and reservation.location_id != line.location_id:
            continue
        if reservation.batch_id is not None and reservation.batch_id != line.batch_id:
            continue
        remaining = to_quantity(reservation.remaining_quantity)
        if remaining <= ZERO:
            continue
        take = min(remaining, outstanding)
        draws.append((reservation.id, take))
        outstanding = to_quantity(outstanding - take)

    return _LineDraw(
        line=line,
        component=component,
        reservation_draws=tuple(draws),
        direct_quantity=outstanding,
    )


def _document_grains(
    plans: Sequence[_LineDraw], reservations: dict[uuid.UUID, StockReservation]
) -> list[BalanceGrain]:
    """Every stock position one issue document will move.

    A reservation draw moves the position the *reservation* holds, which is not
    always the position the line names: release reserves without a location, and
    a line that names one still draws that reservation down. Both are included,
    because both are locked.

    Why this exists at all: the balance locks used to be acquired one at a time,
    in the document's own component order, as each ``allocation.consume`` ran.
    Two jobs built from different structures list a shared part at different line
    numbers, so job A would lock ``(washer)`` then ``(screw)`` while job B locked
    ``(screw)`` then ``(washer)`` - a textbook deadlock between two storekeepers
    picking two different kits from one store. Handing the whole set to the
    inventory engine at once makes both documents acquire in primary-key order.
    """
    grains: list[BalanceGrain] = []
    for plan in plans:
        for reservation_id, _quantity in plan.reservation_draws:
            reservation = reservations[reservation_id]
            grains.append(
                BalanceGrain(
                    item_id=reservation.item_id,
                    warehouse_id=reservation.warehouse_id,
                    location_id=reservation.location_id,
                    batch_id=reservation.batch_id,
                )
            )
        if plan.direct_quantity > ZERO:
            grains.append(
                BalanceGrain(
                    item_id=plan.line.item_id,
                    warehouse_id=plan.line.warehouse_id,
                    location_id=plan.line.location_id,
                    batch_id=plan.line.batch_id,
                )
            )
    return grains


async def post_material_issue(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    issue_id: uuid.UUID,
    payload: MaterialIssuePostRequest,
) -> MaterialIssuePostResult:
    """Move the stock. One transaction, and the only place this module issues.

    In order:

    1. Lock the issue, the order and every component it touches, in primary-key
       order, so two storekeepers picking the same kit serialise rather than both
       validating against the totals they read first.
    2. Re-check the over-issue tolerance against the *locked* component rows.
    3. Draw down the job's reservations through ``allocation.consume``, which
       lowers ``reserved_quantity`` and posts the movement together, then post
       anything left over as a direct movement batch.
    4. Roll the result into the components and advance the order when the kit is
       complete.

    One endpoint posts both directions, so the authority is checked here against
    the direction the document actually carries. ``material_return.create`` is a
    separate right precisely because putting stock back on the shelf is a
    different decision from taking it off, and the route cannot tell which it is
    being asked for until the document is loaded.
    """
    async with transaction(session):
        issue_repo = MaterialIssueRepository(session, tenant)
        issue = await issue_repo.lock_for_update(issue_id)
        if issue is None:
            raise NotFoundError("Material issue", issue_id)
        assert_warehouse_scope(tenant, issue.warehouse_id)

        is_issue = MaterialIssueType(issue.issue_type) is MaterialIssueType.ISSUE
        required_permission = (
            Permission.MATERIAL_ISSUE_CREATE if is_issue else Permission.MATERIAL_RETURN_CREATE
        )
        if not principal.has(required_permission):
            raise AuthorizationError(
                f"Posting {issue.code} moves stock as a {issue.issue_type}, which requires "
                f"{required_permission}.",
                code=ErrorCode.PERMISSION_DENIED,
                details={"required_permission": str(required_permission)},
            )

        MATERIAL_ISSUE_FLOW.assert_transition(
            MaterialIssueStatus(issue.status), MaterialIssueStatus.POSTED
        )

        order = await _lock_order(session, tenant, issue.production_order_id)
        if ProductionOrderStatus(order.status) not in ISSUABLE_PRODUCTION_STATUSES:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; {issue.code} cannot be posted against it.",
                details={"status": str(order.status)},
            )

        lines = await MaterialIssueLineRepository(session, tenant).list_for_issue(issue.id)
        if not lines:  # pragma: no cover - the create path requires at least one
            raise BusinessRuleError(f"{issue.code} has no lines to post.")

        component_repo = ProductionOrderComponentRepository(session, tenant)
        components = await component_repo.lock_many(order.id, [line.component_id for line in lines])

        transactions = (
            await _post_issue_lines(session, tenant, issue, order, lines, components)
            if is_issue
            else await _post_return_lines(session, tenant, issue, order, lines, components)
        )

        total_cost = ZERO
        for line in lines:
            total_cost += Decimal(line.total_cost)
        issue.total_cost = to_money(total_cost)
        issue.posted_at = utcnow()
        issue.posted_by_id = principal.user_id
        await _transition(
            session,
            tenant,
            machine=MATERIAL_ISSUE_FLOW,
            entity=issue,
            entity_type=AUDIT_MATERIAL_ISSUE,
            entity_code=issue.code,
            target=MaterialIssueStatus.POSTED,
            reason=f"Posted against {order.code}",
            status_enum=MaterialIssueStatus,
            action=AuditAction.INVENTORY_MOVEMENT,
        )

        # Re-read the whole snapshot: coverage is a property of every component,
        # not only the ones this document touched.
        all_components = await component_repo.list_for_order(order.id)
        covered = mandatory_components_covered(all_components)
        forced = False
        if is_issue and ProductionOrderStatus(order.status) is ProductionOrderStatus.RELEASED:
            if covered:
                await _transition(
                    session,
                    tenant,
                    machine=PRODUCTION_ORDER_FLOW,
                    entity=order,
                    entity_type=AUDIT_PRODUCTION_ORDER,
                    entity_code=order.code,
                    target=ProductionOrderStatus.MATERIAL_ISSUED,
                    reason=f"All mandatory components issued ({issue.code})",
                    status_enum=ProductionOrderStatus,
                )
            elif payload.allow_partial_issue:
                # The override is a management decision, so it needs the
                # approval right rather than the picking right - and it is
                # recorded with the shortfall it waved through.
                assert_may_force_partial_issue(principal, order_code=order.code)
                forced = True
                await _transition(
                    session,
                    tenant,
                    machine=PRODUCTION_ORDER_FLOW,
                    entity=order,
                    entity_type=AUDIT_PRODUCTION_ORDER,
                    entity_code=order.code,
                    target=ProductionOrderStatus.MATERIAL_ISSUED,
                    reason=f"Partial issue forced: {payload.override_reason}",
                    status_enum=ProductionOrderStatus,
                )

        outstanding = [
            ComponentShortage(
                component_id=component.id,
                item_id=component.item_id,
                item_code=component.item_code,
                warehouse_id=component.warehouse_id,
                required_quantity=to_quantity(component.required_quantity),
                reserved_quantity=ZERO,
                shortage_quantity=outstanding_requirement(component),
                detail=None,
            )
            for component in all_components
            if not component.is_backflushed and outstanding_requirement(component) > ZERO
        ]

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_MATERIAL_ISSUE,
            entity_id=issue.id,
            entity_code=issue.code,
            summary=(
                f"Posted {issue.code} against {order.code}: {issue.total_quantity} units "
                f"over {len(transactions)} movement(s), value {issue.total_cost}"
            ),
            user_id=tenant.user_id,
            new_values={
                "production_order_code": order.code,
                "production_order_status": str(order.status),
                "issue_type": str(issue.issue_type),
                "movements": len(transactions),
                "total_cost": issue.total_cost,
                "partial_issue_forced": forced,
                "override_reason": payload.override_reason if forced else None,
                "outstanding_components": len(outstanding),
            },
        )

        result = MaterialIssuePostResult(
            issue=issue_detail(issue, lines),
            order_status=ProductionOrderStatus(order.status),
            movements=[_posted_movement(txn) for txn in transactions],
            outstanding_components=outstanding,
            message=(
                f"{issue.code} posted; {order.code} is now {order.status}."
                if not outstanding
                else (
                    f"{issue.code} posted; {len(outstanding)} component(s) are still "
                    "short of their requirement."
                )
            ),
        )
    return result


async def _post_issue_lines(
    session: AsyncSession,
    tenant: TenantContext,
    issue: MaterialIssue,
    order: ProductionOrder,
    lines: Sequence[MaterialIssueLine],
    components: dict[uuid.UUID, ProductionOrderComponent],
) -> list[InventoryTransaction]:
    """Draw down reservations, then post whatever is left as free stock.

    Every stock position the document touches is locked *up front*, together,
    through the inventory engine's own primary-key-ordered acquisition. Component
    order is not a safe acquisition order: two jobs built from different
    structures list the same shared parts in different line orders, so ordering
    the balance locks by component id gives the two documents opposite
    acquisition orders on the same two rows. See ``_document_grains``.

    ``allocation.consume`` is necessarily one call per reservation - lowering
    ``reserved_quantity`` and posting the matching movement is a single atomic
    business operation the allocation module owns, not a lookup that could be
    batched. The count is bounded by the document's line count, and everything
    that *is* a lookup here (the reservations, the resulting ledger rows) is one
    statement for the whole document.
    """
    # Component order still decides the order the *component rows* are read in,
    # which matches the order ``lock_many`` took them in.
    ordered = sorted(lines, key=lambda line: line.component_id)
    # Every movement of one document carries the document's own business date.
    # ``allocation.consume`` defaults to "now" when it is not told otherwise,
    # which would put the reserved half of a back-dated issue in one accounting
    # period and the free-stock half in another.
    occurred_at = datetime.combine(issue.issue_date, datetime.min.time(), tzinfo=UTC)

    for line in ordered:
        component = components[line.component_id]
        assert_within_over_issue_tolerance(
            item_code=component.item_code,
            line_number=line.line_number,
            required_quantity=component.required_quantity,
            already_issued=net_issued(component),
            incoming_quantity=line.quantity,
        )

    held = await ReservationCoverageRepository(session, tenant).open_for_order(order.id)
    by_component: dict[uuid.UUID, list[StockReservation]] = {}
    for reservation in held:
        if reservation.demand_line_id is not None:
            by_component.setdefault(reservation.demand_line_id, []).append(reservation)

    plans = [
        _plan_line_draws(
            line, components[line.component_id], by_component.get(line.component_id, [])
        )
        for line in ordered
    ]

    # Take every balance lock this document needs before posting the first
    # movement, in the one order the inventory engine uses everywhere. The locks
    # the consume calls and the direct batch take below are then re-acquisitions
    # this transaction already holds, and cost nothing.
    grains = _document_grains(plans, {reservation.id: reservation for reservation in held})
    if grains:
        await MovementEngine(session, tenant).lock(grains)

    for plan in plans:
        for reservation_id, quantity in plan.reservation_draws:
            await allocation_service.consume(
                session,
                tenant,
                reservation_id=reservation_id,
                quantity=quantity,
                reference_type=MOVEMENT_REFERENCE_ISSUE,
                reference_id=issue.id,
                reference_code=issue.code,
                occurred_at=occurred_at,
            )

    direct = [
        MovementRequest(
            item_id=plan.line.item_id,
            warehouse_id=plan.line.warehouse_id,
            quantity=plan.direct_quantity,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type=MOVEMENT_REFERENCE_ISSUE,
            reference_id=issue.id,
            reference_code=issue.code,
            reason=f"Material issue {issue.code} to production order {order.code}"[:255],
            location_id=plan.line.location_id,
            batch_id=plan.line.batch_id,
            stock_state=StockState.AVAILABLE,
            occurred_at=occurred_at,
        )
        for plan in plans
        if plan.direct_quantity > ZERO
    ]
    if direct:
        await post_movements(session, tenant, direct)

    # One query for every ledger row this document produced, however it was
    # posted. Attribution is by item: a component appears at most once per order
    # (a unique constraint), and a line names exactly one component, so an item
    # identifies a line unambiguously inside one issue.
    transactions = await _movements_for_document(
        session, tenant, MOVEMENT_REFERENCE_ISSUE, issue.id
    )
    cost_by_item: dict[uuid.UUID, Decimal] = {}
    for txn in transactions:
        cost_by_item[txn.item_id] = cost_by_item.get(txn.item_id, ZERO) + Decimal(txn.total_cost)

    for plan in plans:
        cost = to_money(cost_by_item.get(plan.line.item_id, ZERO))
        plan.line.total_cost = cost
        plan.line.unit_cost = (
            to_unit_price(cost / Decimal(plan.line.quantity))
            if Decimal(plan.line.quantity) > ZERO
            else ZERO
        )
        plan.component.issued_quantity = to_quantity(
            Decimal(plan.component.issued_quantity) + Decimal(plan.line.quantity)
        )
    await session.flush()
    return transactions


async def _post_return_lines(
    session: AsyncSession,
    tenant: TenantContext,
    issue: MaterialIssue,
    order: ProductionOrder,
    lines: Sequence[MaterialIssueLine],
    components: dict[uuid.UUID, ProductionOrderComponent],
) -> list[InventoryTransaction]:
    """Put unused material back, valued at what it left the store at.

    Supplying the issued unit cost matters: an inbound movement re-averages the
    position, and letting the engine fall back to standard cost would quietly
    revalue every unit already on the shelf.
    """
    ordered = sorted(lines, key=lambda line: line.component_id)
    issued_totals = await MaterialIssueLineRepository(session, tenant).posted_totals_for_order(
        order.id
    )

    movements: list[MovementRequest] = []
    for line in ordered:
        component = components[line.component_id]
        holding = net_issued(component)
        if Decimal(line.quantity) > holding:
            raise BusinessRuleError(
                f"Line {line.line_number} ({component.item_code}): the job is holding "
                f"{holding}, so {to_quantity(line.quantity)} cannot be returned.",
                details={
                    "line_number": line.line_number,
                    "item_code": component.item_code,
                    "net_issued": str(holding),
                },
            )
        totals = issued_totals.get(component.id)
        unit_cost = (
            to_unit_price(totals.net_cost / totals.net_quantity)
            if totals and totals.net_quantity > ZERO
            else None
        )
        movements.append(
            MovementRequest(
                item_id=line.item_id,
                warehouse_id=line.warehouse_id,
                quantity=to_quantity(line.quantity),
                transaction_type=TransactionType.MATERIAL_RETURN,
                reference_type=MOVEMENT_REFERENCE_RETURN,
                reference_id=issue.id,
                reference_code=issue.code,
                reason=f"Material return {issue.code} from production order {order.code}"[:255],
                location_id=line.location_id,
                batch_id=line.batch_id,
                unit_cost=unit_cost,
                stock_state=StockState.AVAILABLE,
                occurred_at=datetime.combine(issue.issue_date, datetime.min.time(), tzinfo=UTC),
            )
        )

    transactions = await post_movements(session, tenant, movements)
    for line, txn in zip(ordered, transactions, strict=True):
        line.unit_cost = to_unit_price(txn.unit_cost)
        line.total_cost = to_money(txn.total_cost)
        component = components[line.component_id]
        component.returned_quantity = to_quantity(
            Decimal(component.returned_quantity) + Decimal(line.quantity)
        )
        # Consumption can never exceed what the job is still holding, and a
        # return lowers that. The CHECK constraint enforces it either way.
        holding = net_issued(component)
        if Decimal(component.consumed_quantity) > holding:
            component.consumed_quantity = holding
    await session.flush()
    return transactions


async def _movements_for_document(
    session: AsyncSession,
    tenant: TenantContext,
    reference_type: str,
    reference_id: uuid.UUID,
) -> list[InventoryTransaction]:
    """Every ledger row one document posted, in posting order.

    ``allocation.consume`` returns the reservation rather than the ledger row it
    wrote, so a document that mixes reserved and free stock has to read its own
    movements back. One statement, served by the
    ``(company_id, reference_type, reference_id)`` index that exists precisely
    for the "what did this document post?" question.
    """
    stmt = (
        select(InventoryTransaction)
        .where(
            InventoryTransaction.company_id == tenant.company_id,
            InventoryTransaction.reference_type == reference_type,
            InventoryTransaction.reference_id == reference_id,
        )
        .order_by(InventoryTransaction.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def cancel_material_issue(
    session: AsyncSession, tenant: TenantContext, issue_id: uuid.UUID, reason: str
) -> MaterialIssue:
    """Discard a draft document. A posted one is reversed by a return."""
    async with transaction(session):
        issue = await MaterialIssueRepository(session, tenant).lock_for_update(issue_id)
        if issue is None:
            raise NotFoundError("Material issue", issue_id)
        assert_warehouse_scope(tenant, issue.warehouse_id)
        await _transition(
            session,
            tenant,
            machine=MATERIAL_ISSUE_FLOW,
            entity=issue,
            entity_type=AUDIT_MATERIAL_ISSUE,
            entity_code=issue.code,
            target=MaterialIssueStatus.CANCELLED,
            reason=reason,
            status_enum=MaterialIssueStatus,
            action=AuditAction.CANCEL,
        )
    return issue


async def list_material_issues(
    session: AsyncSession,
    tenant: TenantContext,
    filters: MaterialIssueFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[MaterialIssueRead], OffsetPagination]:
    rows, pagination = await MaterialIssueRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [MaterialIssueRead.model_validate(row) for row in rows], pagination


async def get_material_issue_detail(
    session: AsyncSession, tenant: TenantContext, issue_id: uuid.UUID
) -> MaterialIssueDetail:
    issue = await MaterialIssueRepository(session, tenant).get_or_404(issue_id)
    lines = await MaterialIssueLineRepository(session, tenant).list_for_issue(issue.id)
    return issue_detail(issue, lines)


async def _backflush_components(
    session: AsyncSession,
    tenant: TenantContext,
    order: ProductionOrder,
    components: Sequence[ProductionOrderComponent],
) -> tuple[str | None, list[PostedMovement]]:
    """Issue the automatic components, scaled to what was actually produced.

    Raised as a real, posted ``material_issue`` document rather than as bare
    movements: the ledger, the component's issued quantity and the job's costing
    all read from issue lines, and a backflush that skipped the document would be
    invisible to every one of them.

    It also posts through exactly the same path a picked issue does, which is not
    a tidiness argument. Release reserves backflushed components like any other,
    so posting a bare movement would leave ``reserved_quantity`` standing against
    stock the movement had just taken away - and the balance's
    ``reserved <= on_hand`` constraint would (correctly) refuse the whole
    completion.
    """
    plans: list[tuple[ProductionOrderComponent, Decimal]] = []
    for component in components:
        if not component.is_backflushed:
            continue
        quantity = backflush_quantity(
            required_quantity=component.required_quantity,
            planned_quantity=order.planned_quantity,
            produced_quantity=order.produced_quantity,
            already_issued=net_issued(component),
        )
        if quantity > ZERO:
            plans.append((component, quantity))

    if not plans:
        return None, []

    issue = MaterialIssue(
        code=await next_document_number(session, tenant.company_id, DocTypes.MATERIAL_ISSUE),
        production_order_id=order.id,
        issue_type=MaterialIssueType.ISSUE,
        status=MaterialIssueStatus.DRAFT,
        issue_date=today(),
        warehouse_id=plans[0][0].warehouse_id,
        reason=f"Backflush on completion of {order.code}",
    )
    MaterialIssueRepository(session, tenant).add(issue)
    await session.flush()

    line_repo = MaterialIssueLineRepository(session, tenant)
    lines = [
        line_repo.add(
            MaterialIssueLine(
                material_issue_id=issue.id,
                line_number=index,
                component_id=component.id,
                item_id=component.item_id,
                warehouse_id=component.warehouse_id,
                quantity=quantity,
            )
        )
        for index, (component, quantity) in enumerate(plans, start=1)
    ]
    issue.line_count = len(lines)
    issue.total_quantity = to_quantity(sum((q for _, q in plans), start=ZERO))
    await session.flush()

    transactions = await _post_issue_lines(
        session,
        tenant,
        issue,
        order,
        lines,
        {component.id: component for component, _ in plans},
    )

    issue.total_cost = to_money(sum((Decimal(line.total_cost) for line in lines), start=ZERO))
    issue.posted_at = utcnow()
    issue.posted_by_id = tenant.user_id
    await _transition(
        session,
        tenant,
        machine=MATERIAL_ISSUE_FLOW,
        entity=issue,
        entity_type=AUDIT_MATERIAL_ISSUE,
        entity_code=issue.code,
        target=MaterialIssueStatus.POSTED,
        reason=f"Backflushed on completion of {order.code}",
        status_enum=MaterialIssueStatus,
        action=AuditAction.INVENTORY_MOVEMENT,
    )
    return issue.code, [_posted_movement(txn) for txn in transactions]


# =============================================================================
# Shop-floor bookings
# =============================================================================


async def _book_entry(
    session: AsyncSession,
    tenant: TenantContext,
    order: ProductionOrder,
    payload: ProductionEntryCreate,
    *,
    force_output: bool = False,
) -> tuple[ProductionEntry, PostedMovement | None]:
    """Write one booking, and post the receipt when it books final output.

    ``force_output`` is set by completion, which is booking the last of the job
    whether or not a work order was named.
    """
    work_order: WorkOrder | None = None
    if payload.work_order_id is not None:
        work_order = await WorkOrderRepository(session, tenant).lock_for_update(
            payload.work_order_id
        )
        if work_order is None or work_order.production_order_id != order.id:
            raise NotFoundError("Work order", payload.work_order_id)
        if WorkOrderStatus(work_order.status) is WorkOrderStatus.CANCELLED:
            raise BusinessRuleError(
                f"Work order {work_order.code} is cancelled and cannot be booked against.",
                details={"work_order_code": work_order.code},
            )

    await _assert_operator(session, tenant, payload.operator_user_id)
    await _assert_machine(
        session,
        tenant,
        payload.machine_id,
        work_center_id=work_order.work_center_id if work_order else None,
    )

    produced = to_quantity(payload.produced_quantity)
    rejected = to_quantity(payload.rejected_quantity)

    # Final output is what goes into the warehouse: the last operation of the
    # routing, or the whole job when there is no routing at all.
    last_sequence = await WorkOrderRepository(session, tenant).last_sequence_for_order(order.id)
    books_output = force_output or work_order is None or work_order.sequence == last_sequence

    if books_output and produced > ZERO:
        assert_within_over_production_tolerance(
            order_code=order.code,
            planned_quantity=order.planned_quantity,
            already_produced=order.produced_quantity,
            incoming=produced,
        )

    entry = ProductionEntry(
        production_order_id=order.id,
        work_order_id=work_order.id if work_order else None,
        entry_date=payload.entry_date or today(),
        shift=payload.shift,
        operator_user_id=payload.operator_user_id,
        machine_id=payload.machine_id,
        produced_quantity=produced,
        rejected_quantity=rejected,
        rejection_reason=payload.rejection_reason,
        downtime_minutes=to_quantity(payload.downtime_minutes),
        notes=payload.notes,
    )
    ProductionEntryRepository(session, tenant).add(entry)
    await session.flush()

    movement: PostedMovement | None = None
    if books_output and produced > ZERO:
        movement = await _post_output_receipt(session, tenant, order, entry, produced)
        order.produced_quantity = to_quantity(Decimal(order.produced_quantity) + produced)

    if rejected > ZERO:
        # Rejected output is never received: it was never good stock, so there
        # is nothing to post. It is counted so the yield is visible.
        order.rejected_quantity = to_quantity(Decimal(order.rejected_quantity) + rejected)

    if work_order is not None:
        work_order.completed_quantity = to_quantity(
            Decimal(work_order.completed_quantity) + produced
        )
        work_order.rejected_quantity = to_quantity(Decimal(work_order.rejected_quantity) + rejected)
        if WorkOrderStatus(work_order.status) is WorkOrderStatus.PENDING:
            work_order.actual_start_at = utcnow()
            await _transition(
                session,
                tenant,
                machine=WORK_ORDER_FLOW,
                entity=work_order,
                entity_type=AUDIT_WORK_ORDER,
                entity_code=work_order.code,
                target=WorkOrderStatus.IN_PROGRESS,
                reason=f"First booking on {order.code}",
                status_enum=WorkOrderStatus,
            )

    ProductionOrderRepository(session, tenant).touch(order)
    await session.flush()

    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.CREATE,
        entity_type=AUDIT_PRODUCTION_ENTRY,
        entity_id=entry.id,
        entity_code=order.code,
        summary=(
            f"Booked {produced} good and {rejected} rejected on {order.code}"
            + (f" at operation {work_order.operation_code}" if work_order else "")
        ),
        user_id=tenant.user_id,
        new_values={
            "production_order_code": order.code,
            "work_order_id": entry.work_order_id,
            "entry_date": entry.entry_date,
            "shift": entry.shift,
            "produced_quantity": produced,
            "rejected_quantity": rejected,
            "rejection_reason": entry.rejection_reason,
            "downtime_minutes": entry.downtime_minutes,
            "posted_output": movement is not None,
            "output_transaction_id": entry.output_transaction_id,
        },
    )
    return entry, movement


async def _post_output_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    order: ProductionOrder,
    entry: ProductionEntry,
    quantity: Decimal,
) -> PostedMovement:
    """Put finished goods into the output warehouse.

    ASSUMPTION: the receipt is valued at the item's standard cost, which is what
    the inventory engine falls back to when a movement carries no unit cost. The
    job's *actual* cost is not known until it closes - material is still being
    issued and operations are still running - and revaluing finished stock from
    standard to actual is a finance function this module does not own. The actual
    figure is computed at closure and reported by ``GET /production/{id}/cost``.
    """
    items = await _load_items(session, tenant, [order.item_id])
    item = items[order.item_id]
    if item.is_batch_tracked or item.is_serial_tracked:
        raise BusinessRuleError(
            f"Item {item.code} {TRACKED_OUTPUT_MESSAGE}",
            code=(ErrorCode.BATCH_REQUIRED if item.is_batch_tracked else ErrorCode.SERIAL_REQUIRED),
            details={"item_code": item.code, "order_code": order.code},
        )

    posted = await post_movements(
        session,
        tenant,
        [
            MovementRequest(
                item_id=order.item_id,
                warehouse_id=order.warehouse_id,
                quantity=quantity,
                transaction_type=TransactionType.PRODUCTION_RECEIPT,
                reference_type=MOVEMENT_REFERENCE_ORDER,
                reference_id=order.id,
                reference_code=order.code,
                reason=f"Production receipt from {order.code}"[:255],
                stock_state=StockState.AVAILABLE,
                occurred_at=datetime.combine(entry.entry_date, datetime.min.time(), tzinfo=UTC),
            )
        ],
    )
    entry.output_transaction_id = posted[0].id
    return _posted_movement(posted[0])


async def create_production_entry(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: ProductionEntryCreate,
) -> ProductionEntryResult:
    """Book a shift's work against a job."""
    async with transaction(session):
        order = await _lock_order(session, tenant, order_id)
        if ProductionOrderStatus(order.status) not in (
            ProductionOrderStatus.MATERIAL_ISSUED,
            ProductionOrderStatus.IN_PROCESS,
        ):
            raise BusinessRuleError(
                f"{order.code} is {order.status}; output can only be booked against a job "
                "that has its material and has not yet been completed.",
                details={"status": str(order.status), "order_code": order.code},
            )
        entry, movement = await _book_entry(session, tenant, order, payload)
        result = ProductionEntryResult(
            entry=ProductionEntryRead.model_validate(entry),
            order=_order_read(order),
            output_movement=movement,
            message=(
                f"Booked {entry.produced_quantity} on {order.code}."
                if movement is not None
                else f"Booked {entry.produced_quantity} at an intermediate operation "
                f"on {order.code}; no stock was received."
            ),
        )
    return result


async def list_production_entries(
    session: AsyncSession,
    tenant: TenantContext,
    filters: ProductionEntryFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[ProductionEntryRead], OffsetPagination]:
    rows, pagination = await ProductionEntryRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [ProductionEntryRead.model_validate(row) for row in rows], pagination


# =============================================================================
# Work orders
# =============================================================================


async def list_work_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: WorkOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[WorkOrderRead], OffsetPagination]:
    rows, pagination = await WorkOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [WorkOrderRead.model_validate(row) for row in rows], pagination


async def _assert_order_open_for_shop_floor(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionOrder:
    """Refuse to change an operation belonging to a finished job.

    Closure freezes a cost that was computed from these rows. Letting an
    operation be started, completed or reassigned afterwards would make the
    stored figure unreproducible from the job it describes.

    The order row is **locked**, not merely read. Reading it unlocked made this a
    check-then-act: an operator completing an operation would see the job still
    open while ``close_order`` - holding the same row - was costing that very
    operation from its pre-completion times. Both commit, and the job is closed
    with a stored total that its own work orders no longer add up to, which
    ``GET /production/{id}/cost`` then prints side by side. Locking the order
    serialises the two, and takes the locks order-first, which is the order every
    other write path in this module uses.
    """
    order = await _lock_order(session, tenant, order_id)
    if ProductionOrderStatus(order.status) in (
        ProductionOrderStatus.CLOSED,
        ProductionOrderStatus.CANCELLED,
    ):
        raise BusinessRuleError(
            f"{order.code} is {order.status}; its operations can no longer be changed.",
            details={"order_code": order.code, "status": str(order.status)},
        )
    return order


async def update_work_order(
    session: AsyncSession,
    tenant: TenantContext,
    work_order_id: uuid.UUID,
    payload: WorkOrderUpdate,
) -> WorkOrder:
    """Assign and schedule. Quantities only ever move through a booking."""
    async with transaction(session):
        repo = WorkOrderRepository(session, tenant)
        # One projection read to learn which job owns this operation, then the
        # locks in order-then-operation sequence. The projection returns a bare
        # column rather than the entity, so nothing stale is put in the identity
        # map ahead of the locking read below.
        order_id = await repo.order_id_of(work_order_id)
        if order_id is None:
            raise NotFoundError("Work order", work_order_id)
        await _assert_order_open_for_shop_floor(session, tenant, order_id)

        work_order = await repo.lock_for_update(work_order_id)
        if work_order is None:  # pragma: no cover - the projection just found it
            raise NotFoundError("Work order", work_order_id)
        if WorkOrderStatus(work_order.status) in (
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.CANCELLED,
        ):
            raise BusinessRuleError(
                f"Work order {work_order.code} is {work_order.status} and can no longer "
                "be changed.",
                details={"status": str(work_order.status)},
            )

        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return work_order
        before = {key: getattr(work_order, key) for key in changes}

        if "operator_user_id" in changes:
            await _assert_operator(session, tenant, changes["operator_user_id"])
        if "machine_id" in changes:
            await _assert_machine(
                session, tenant, changes["machine_id"], work_center_id=work_order.work_center_id
            )

        for key, value in changes.items():
            setattr(work_order, key, value)
        if (
            work_order.planned_start_at is not None
            and work_order.planned_end_at is not None
            and work_order.planned_end_at < work_order.planned_start_at
        ):
            raise BusinessRuleError(
                "The planned finish cannot be before the planned start.",
                details={"work_order_code": work_order.code},
            )

        repo.touch(work_order)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_WORK_ORDER,
            entity_id=work_order.id,
            entity_code=work_order.code,
            summary=f"Updated work order {work_order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=changes,
        )
    return work_order


async def transition_work_order(
    session: AsyncSession,
    tenant: TenantContext,
    work_order_id: uuid.UUID,
    payload: WorkOrderTransitionRequest,
) -> WorkOrder:
    """Start, finish or abandon one operation."""
    async with transaction(session):
        repo = WorkOrderRepository(session, tenant)
        # Order first, then the operation - see ``_assert_order_open_for_shop_floor``.
        order_id = await repo.order_id_of(work_order_id)
        if order_id is None:
            raise NotFoundError("Work order", work_order_id)
        await _assert_order_open_for_shop_floor(session, tenant, order_id)

        work_order = await repo.lock_for_update(work_order_id)
        if work_order is None:  # pragma: no cover - the projection just found it
            raise NotFoundError("Work order", work_order_id)

        moment = payload.occurred_at or utcnow()
        target = payload.status
        if target is WorkOrderStatus.IN_PROGRESS:
            work_order.actual_start_at = moment
        elif target is WorkOrderStatus.COMPLETED:
            # The CHECK constraint refuses a completed operation with no start,
            # and an operation completed in one keystroke genuinely started when
            # somebody began it - falling back to the same moment keeps the row
            # honest without inventing a duration.
            work_order.actual_start_at = work_order.actual_start_at or moment
            work_order.actual_end_at = moment
            if moment < work_order.actual_start_at:
                raise BusinessRuleError(
                    "An operation cannot finish before it started.",
                    details={"work_order_code": work_order.code},
                )

        await _transition(
            session,
            tenant,
            machine=WORK_ORDER_FLOW,
            entity=work_order,
            entity_type=AUDIT_WORK_ORDER,
            entity_code=work_order.code,
            target=target,
            reason=payload.reason or str(target),
            status_enum=WorkOrderStatus,
        )
    return work_order


# =============================================================================
# Work in progress
# =============================================================================


async def wip_orders(
    session: AsyncSession, tenant: TenantContext, filters: WipFilters, params: PageParams
) -> tuple[list[WipOrderRow], OffsetPagination]:
    """The planner's board: every live job, most urgent first."""
    as_of = today()
    rows, pagination = await ProductionOrderRepository(session, tenant).wip_page(
        filters, params, as_of=as_of
    )
    return [
        WipOrderRow(
            production_order_id=row.production_order_id,
            code=row.code,
            status=row.status,
            item_id=row.item_id,
            warehouse_id=row.warehouse_id,
            plant_id=row.plant_id,
            priority=row.priority,
            planned_quantity=to_quantity(row.planned_quantity),
            produced_quantity=to_quantity(row.produced_quantity),
            rejected_quantity=to_quantity(row.rejected_quantity),
            balance_quantity=max(ZERO, to_quantity(row.planned_quantity - row.produced_quantity)),
            planned_start_date=row.planned_start_date,
            planned_end_date=row.planned_end_date,
            actual_start_date=row.actual_start_date,
            days_late=max(0, (as_of - row.planned_end_date).days),
            open_work_orders=row.open_work_orders,
            total_work_orders=row.total_work_orders,
        )
        for row in rows
    ], pagination


async def wip_work_centres(
    session: AsyncSession, tenant: TenantContext, filters: WipFilters, params: PageParams
) -> tuple[list[WipWorkCentreRow], OffsetPagination]:
    """The same work in progress, grouped by where it physically is.

    A separate endpoint from the order board rather than a second block on it:
    the two have different row shapes and different page sizes, and one paginated
    response cannot honestly carry both.
    """
    rows, pagination = await WorkOrderRepository(session, tenant).load_by_work_centre(
        filters, params
    )
    return [
        WipWorkCentreRow(
            work_center_id=row.work_center_id,
            pending_work_orders=row.pending_work_orders,
            in_progress_work_orders=row.in_progress_work_orders,
            planned_quantity=to_quantity(row.planned_quantity),
            completed_quantity=to_quantity(row.completed_quantity),
            balance_quantity=max(ZERO, to_quantity(row.planned_quantity - row.completed_quantity)),
            remaining_minutes=to_quantity(row.remaining_minutes),
        )
        for row in rows
    ], pagination


# =============================================================================
# Costing
# =============================================================================


async def _compute_cost(
    session: AsyncSession, tenant: TenantContext, order: ProductionOrder, *, is_final: bool
) -> ProductionCostRead:
    """Material at movement cost, operations at actual time. Three statements.

    Every input is already frozen on the job - the component snapshot, the
    posted issue lines and the work orders' own rates - so the same order costs
    the same however long after the fact it is asked, and the per-operation
    breakdown always adds up to the total stored at closure.
    """
    components = await ProductionOrderComponentRepository(session, tenant).list_for_order(order.id)
    totals = await MaterialIssueLineRepository(session, tenant).posted_totals_for_order(order.id)
    work_orders = await WorkOrderRepository(session, tenant).list_for_order(order.id)

    materials = [
        material_cost_line(component, totals.get(component.id)) for component in components
    ]
    operations = [
        operation_cost_line(
            OperationCostInput(
                work_order_id=work_order.id,
                sequence=work_order.sequence,
                operation_code=work_order.operation_code,
                work_center_id=work_order.work_center_id,
                status=WorkOrderStatus(work_order.status),
                planned_setup_minutes=Decimal(work_order.planned_setup_minutes),
                planned_run_minutes=Decimal(work_order.planned_run_minutes),
                process_cost=Decimal(work_order.process_cost),
                actual_start_at=work_order.actual_start_at,
                actual_end_at=work_order.actual_end_at,
                cost_per_hour=Decimal(work_order.work_center_cost_per_hour),
                work_centre_setup_cost=Decimal(work_order.work_center_setup_cost),
            )
        )
        for work_order in work_orders
    ]
    return summarise_cost(
        order=order, materials=materials, operations=operations, is_final=is_final
    )


async def production_cost(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> ProductionCostRead:
    """What this job has cost.

    A closed job is answered from the columns written at closure; anything else
    is computed live. A closed job's cost must not move when a work-centre rate
    is revised or the stock it consumed is revalued.
    """
    order = await _get_order(session, tenant, order_id)
    live = await _compute_cost(session, tenant, order, is_final=False)
    if ProductionOrderStatus(order.status) is not ProductionOrderStatus.CLOSED:
        return live

    return live.model_copy(
        update={
            "is_final": True,
            "material_cost": to_money(order.total_material_cost),
            "operation_cost": to_money(order.total_operation_cost),
            "total_cost": to_money(order.total_cost),
            "cost_per_unit": (
                to_money(Decimal(order.total_cost) / Decimal(order.produced_quantity))
                if Decimal(order.produced_quantity) > ZERO
                else None
            ),
        }
    )


# =============================================================================
# Planning contract: what MRP nets in-progress output against
# =============================================================================
#
# ``src.modules.mrp.repository`` resolves this function by name at call time -
# it never imports these tables and never guesses at their columns. The
# signature below IS that contract; changing it silently turns MRP's netting
# stage off, which shows up only as a warning on the run and a plan that
# re-orders work already on the floor.


async def in_progress_output(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_ids: Sequence[uuid.UUID],
    warehouse_ids: Sequence[uuid.UUID] | None = None,
    upto: date,
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """Good output still expected from live jobs, by the date each job is due.

    The netting input for master rules section 14's "net against in-progress
    production". Read-only: no status moves, no stock moves, no audit row.

    Each item maps to its outstanding balances as ``(due_date, quantity)``
    tranches in date order, every one due on or before ``upto``. The dates are
    the contract: the planner nets a requirement only against the jobs that
    finish before it is needed. One summed figure per item - what this returned
    before - let a job landing next month cover a line shipping this week.

    Which jobs count is
    :data:`~src.modules.production.repository.NETTABLE_PRODUCTION_STATUSES`,
    which carries the argument for its membership; how much each still owes is
    ``planned_quantity - produced_quantity`` floored at zero, derived from this
    module's own over-production rule rather than assumed - see
    :meth:`~src.modules.production.repository.ProductionOrderRepository.
    in_progress_output_by_item`.

    ``warehouse_ids`` of ``None`` means the whole company. ``item_ids`` empty
    returns an empty mapping. Items with nothing in progress are absent rather
    than present as zero; the planner reads this with
    ``in_production.get(item_id, ())``, so the two are the same answer.
    """
    return await ProductionOrderRepository(session, tenant).in_progress_output_by_item(
        item_ids, warehouse_ids=warehouse_ids, upto=upto
    )


__all__ = [
    "AUDIT_MATERIAL_ISSUE",
    "AUDIT_PRODUCTION_ENTRY",
    "AUDIT_PRODUCTION_ORDER",
    "AUDIT_WORK_ORDER",
    "MATERIAL_ISSUE_FLOW",
    "MAX_COMPONENTS_PER_ORDER",
    "PRODUCTION_ORDER_FLOW",
    "WORK_ORDER_FLOW",
    "ComponentPlan",
    "OperationCostInput",
    "RoutingStep",
    "WorkOrderPlan",
    "approve_order",
    "assert_within_over_issue_tolerance",
    "assert_within_over_production_tolerance",
    "backflush_quantity",
    "can_approve",
    "cancel_material_issue",
    "cancel_order",
    "charged_minutes",
    "close_order",
    "complete_order",
    "create_material_issue",
    "create_order",
    "create_order_from_mrp",
    "create_production_entry",
    "delete_draft_order",
    "get_material_issue_detail",
    "get_order",
    "get_order_detail",
    "in_progress_output",
    "is_editable",
    "issue_ceiling",
    "issue_detail",
    "list_components",
    "list_material_issues",
    "list_orders",
    "list_production_entries",
    "list_work_orders",
    "mandatory_components_covered",
    "material_cost_line",
    "net_issued",
    "operation_cost_line",
    "outstanding_requirement",
    "plan_components",
    "plan_reservation",
    "plan_work_orders",
    "post_material_issue",
    "production_cost",
    "release_order",
    "start_order",
    "summarise_cost",
    "transition_work_order",
    "update_order",
    "update_work_order",
    "wip_orders",
    "wip_work_centres",
]
