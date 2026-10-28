"""Assembly line business rules.

Master rules section 19, worked through literally on the example the rules give:
a Lock Set consumes a Handle, a Cylinder, a Lock body, a set of Keys and a Screw
kit, and the system consumes those components and creates finished assembly
stock. The rules that matter, and why each lives here rather than in a caller:

1. **Completion is one posting.** Every ``ASSEMBLY_CONSUME`` for the components
   and the ``ASSEMBLY_RECEIPT`` for the output go into a single
   :func:`~src.modules.inventory.service.post_movements` call inside one
   transaction. That call locks every affected balance row in primary-key order
   before touching any of them, so two assemblies sharing a component queue
   instead of deadlocking (database.md section 4.4). Posting the components and
   the output separately would leave a window where the plant had eaten five
   parts and produced nothing.

2. **Components are committed at RELEASE, handed over at ISSUE, and eaten at
   COMPLETE.** Releasing takes a HARD ``stock_reservations`` row per component,
   so parts earmarked for a job on the bench cannot be promised to a sales order
   as well. Issuing records the physical hand-over and moves no stock. Only
   completion writes the ledger, because only then is it known what was actually
   built. The reservations are released inside the completing transaction, just
   before the movements are posted - see :func:`_release_reservations`.

3. **Consumption is scaled by what was produced, and bounded by what was
   issued.** A run planned for 100 that yielded 90 good and 5 rejected consumed
   95/100 of every component: a rejected lock set ate its cylinder exactly as a
   good one did. Consuming more than was issued to the line is refused rather
   than clamped, because the difference is either a mis-keyed quantity or parts
   nobody kitted.

4. **Serialised components are traced to the exact units consumed.** A
   serial identifies one physical unit, so the inventory engine demands one
   movement of quantity 1 per serial; the same set is written to
   ``consumed_serial_ids`` and constrained by the database to match the consumed
   quantity. A serial another assembly already swallowed is refused.

5. **QC holds the output instead of releasing it.** When the order requires
   inspection, the ``ASSEMBLY_RECEIPT`` posts with ``stock_state=QC_HOLD``, the
   order goes to QC_PENDING, and the quality module is asked for an inspection
   through a guarded import. When quality is not deployed the requirement is
   still recorded on the order (``qc_held_at`` with no ``qc_inspection_id``) and
   is queryable - the stock is held either way, which is the half that matters.

6. **One hold, one release path.** Deciding that inspection is what releases
   the held output, and quality calls :func:`record_quality_verdict` back in the
   same transaction to complete the order from the verdict - moving nothing
   itself. ``record_qc_result`` releases stock only for a hold no inspection
   covers; when the order names one it refuses, or completes the order from the
   inspection's already-recorded figures, and never posts a second movement.

Allocation and quality are reached through ports. The default implementations
delegate to ``allocation.service`` and ``quality.service`` exactly as
database.md sections 4.5 and 8 specify; the seams exist so this module's rules
can be exercised without standing up the reservation engine.

## Assumptions

Each of these is a rule the contract did not fix. The conservative option was
taken, named, and is changeable in one place.

* ``requires_qc`` is a **per-order flag**, not an item-master property. ``items``
  carries no inspection policy and inventing one would put the rule in two
  places that could disagree; ``goods_receipt_lines.requires_qc`` makes the same
  choice.
* **Tracked components and tracked output are supported.** A batch-tracked
  component is reserved by one ACTIVE, unexpired lot at release - chosen FEFO and
  recorded on the line, so the operator can see which lot was picked - and the
  consumed lot is confirmed at completion. A serial-tracked component names the
  exact units at completion. A tracked output names its lot and one serial per
  assembled unit, because the inventory engine refuses every other shape of
  movement.
* **Rejected output produces no stock.** Only ``assembled_quantity`` is
  received. A failed assembly is not a saleable unit, and receiving it would put
  scrap into the finished-goods balance.
* **Issued quantity may not exceed the requirement**, because the requirement is
  what was reserved; anything beyond it would be unreserved stock on the bench
  that another document could still be promised. A top-up only ever raises the
  issued totals (docs/assumptions.md §E4-5).
* **An order is built from an ASSEMBLY or a PRODUCTION structure**
  (:data:`ASSEMBLABLE_BOM_TYPES`, docs/assumptions.md §E4-4).
* **Component reservations carry the order's priority and its planned start**
  as their required date, read as a UTC business date (§E4-7).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    ErrorCode,
    InsufficientStockError,
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.messages import held_for_inspection, verdict_differs_from_inspection
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.allocation.models import (
    DEFAULT_RESERVATION_PRIORITY,
    DemandType,
    ReservationType,
)
from src.modules.assembly.models import (
    CANCELLABLE_STATUSES,
    COMPONENTS_HELD_STATUSES,
    MAX_COMPONENTS_PER_ORDER,
    NOT_STARTED_STATUSES,
    OPEN_SUPPLY_STATUSES,
    AssemblyOrder,
    AssemblyOrderComponent,
    AssemblyOrderStatus,
    AssemblySourceType,
)
from src.modules.assembly.repository import (
    AssemblyComponentRepository,
    AssemblyItem,
    AssemblyOrderRepository,
    AssemblyReservationRepository,
    ComponentAvailabilityRepository,
    ItemLookupRepository,
    SerialLookupRepository,
    WarehouseLookupRepository,
)
from src.modules.assembly.schemas import (
    READY_FOR_PACKING_MAX,
    AssemblyBoardFilters,
    AssemblyBoardOrder,
    AssemblyComponentAvailability,
    AssemblyComponentRead,
    AssemblyLineLoad,
    AssemblyOrderCreate,
    AssemblyOrderDetail,
    AssemblyOrderFilters,
    AssemblyOrderUpdate,
    CompleteRequest,
    CompletionResult,
    IssueComponentsRequest,
    QcResultRequest,
    ReadyForPackingRow,
)
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.bom.engine import apply_scrap, is_explodable, line_requirement
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.bom.repository import BomLineRepository, BomRepository
from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.service import (
    MovementRequest,
    TraceabilityService,
    change_stock_state,
    post_movements,
)

log = get_logger(__name__)

AUDIT_ASSEMBLY_ORDER = "assembly_order"


class KitShortageError(InsufficientStockError):
    """A release refused because the kit is short - every short component named.

    Still an ``InsufficientStockError`` (same code, same status), so a caller
    that handled the one-component refusal the reservation engine raises handles
    this too; it only carries the whole list in ``details.shortages``.
    """

    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        BusinessRuleError.__init__(self, message, details=details)


#: What the ledger rows this module posts point back at. A bare string, matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
#: It is also the reference an assembly's inspection carries, which is how
#: quality knows to call :func:`record_quality_verdict` back.
MOVEMENT_REFERENCE_ASSEMBLY = "assembly_order"

#: The structures an assembly order may be built from.
#:
#: ASSUMPTION: an ``ASSEMBLY`` structure or a ``PRODUCTION`` one - a lock set
#: whose recipe engineering filed as a production BOM is still put together on
#: the assembly bench, and refusing it would push the planner into a production
#: order for what is assembly work. A ``PHANTOM`` is never built, and an
#: ``ALTERNATE`` is reached by naming it as a production structure's alternative,
#: so neither is offered (docs/assumptions.md §E4-4).
ASSEMBLABLE_BOM_TYPES: tuple[BomType, ...] = (BomType.ASSEMBLY, BomType.PRODUCTION)

#: Draft fields an explicit ``null`` in a PATCH body legitimately clears. Every
#: other column on the update payload backs a NOT NULL column, where a null is a
#: client mistake rather than an instruction - see :func:`update_order`.
CLEARABLE_UPDATE_FIELDS = frozenset({"assembly_line", "planned_start", "planned_end", "notes"})

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
ORDER_AUDITED_FIELDS = [
    "code",
    "item_id",
    "bom_id",
    "planned_quantity",
    "assembled_quantity",
    "rejected_quantity",
    "warehouse_id",
    "output_warehouse_id",
    "assembly_line",
    "planned_start",
    "planned_end",
    "actual_start",
    "actual_end",
    "priority",
    "status",
    "requires_qc",
    "qc_held_at",
    "qc_inspection_id",
    "packing_order_id",
    "source_type",
    "source_id",
    "notes",
]


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move assembly_orders.status
# -----------------------------------------------------------------------------

ASSEMBLY_ORDER_LIFECYCLE: StateMachine[AssemblyOrderStatus] = StateMachine(
    entity="Assembly order",
    initial=AssemblyOrderStatus.DRAFT,
    transitions=(
        Transition(
            AssemblyOrderStatus.DRAFT,
            AssemblyOrderStatus.APPROVED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Approve",
        ),
        Transition(
            AssemblyOrderStatus.APPROVED,
            AssemblyOrderStatus.RELEASED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Release to the line",
            # Releasing takes hard reservations away from every other document
            # competing for the same components.
            is_irreversible=True,
        ),
        Transition(
            AssemblyOrderStatus.RELEASED,
            AssemblyOrderStatus.COMPONENTS_ISSUED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Issue components",
            is_irreversible=True,
        ),
        Transition(
            AssemblyOrderStatus.COMPONENTS_ISSUED,
            AssemblyOrderStatus.IN_PROGRESS,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Start assembly",
        ),
        # Completion splits on whether the output needs inspecting. Both edges
        # post stock; only the destination of the output differs.
        Transition(
            AssemblyOrderStatus.IN_PROGRESS,
            AssemblyOrderStatus.QC_PENDING,
            permission=Permission.ASSEMBLY_COMPLETE,
            label="Complete, hold for inspection",
            is_irreversible=True,
        ),
        Transition(
            AssemblyOrderStatus.IN_PROGRESS,
            AssemblyOrderStatus.COMPLETED,
            permission=Permission.ASSEMBLY_COMPLETE,
            label="Complete",
            is_irreversible=True,
        ),
        Transition(
            AssemblyOrderStatus.QC_PENDING,
            AssemblyOrderStatus.COMPLETED,
            permission=Permission.QC_DECIDE,
            label="Record inspection result",
            is_irreversible=True,
        ),
        Transition(
            AssemblyOrderStatus.COMPLETED,
            AssemblyOrderStatus.CLOSED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Close",
        ),
        # Cancellation is reachable only before the components have been handed
        # to the line. Past that the parts are physically gone from the store and
        # the order has to be completed or closed, so the trail survives.
        Transition(
            AssemblyOrderStatus.DRAFT,
            AssemblyOrderStatus.CANCELLED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Cancel",
        ),
        Transition(
            AssemblyOrderStatus.APPROVED,
            AssemblyOrderStatus.CANCELLED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Cancel",
        ),
        Transition(
            AssemblyOrderStatus.RELEASED,
            AssemblyOrderStatus.CANCELLED,
            permission=Permission.ASSEMBLY_ORDER_MANAGE,
            label="Cancel",
            # Reservations are given back, which changes what every other
            # document can be promised.
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({AssemblyOrderStatus.CLOSED, AssemblyOrderStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Pure rules - unit tested without a database
# -----------------------------------------------------------------------------


def requirement_for(
    *,
    planned_quantity: Decimal,
    quantity_per: Decimal,
    output_quantity: Decimal,
    scrap_percentage: Decimal,
) -> Decimal:
    """Scrap-adjusted requirement for one component over the whole order.

    Delegates to the BOM engine's arithmetic rather than restating it: a
    structure that yields ten handles from one billet must divide here exactly as
    it does in an explosion, or the kitting list and MRP would disagree about
    the same recipe.
    """
    net = line_requirement(planned_quantity, quantity_per, output_quantity)
    return apply_scrap(net, scrap_percentage)


def consumption_for(
    *, required_quantity: Decimal, planned_quantity: Decimal, produced_quantity: Decimal
) -> Decimal:
    """How much of a component a partially-completed run actually ate.

    Scaled by output attempted, not by output accepted: ``produced_quantity`` is
    assembled plus rejected, because a lock set that failed final inspection
    consumed its cylinder exactly as a good one did. A full run consumes exactly
    the requirement - the identity that keeps a normal completion free of
    rounding drift.
    """
    if planned_quantity <= ZERO:
        raise ValidationError("An assembly order's planned quantity must be positive.")
    if produced_quantity < ZERO:
        raise ValidationError("Produced quantity cannot be negative.")
    if produced_quantity == planned_quantity:
        return to_quantity(required_quantity)
    return to_quantity(Decimal(required_quantity) * Decimal(produced_quantity) / planned_quantity)


def is_whole_units(quantity: Decimal) -> bool:
    """Whether a quantity is a whole number of units.

    Serialised parts are counted, never measured: half a cylinder is not a
    quantity, it is a data error, and the inventory engine would reject the
    movement anyway.
    """
    return to_quantity(quantity) == to_quantity(quantity).to_integral_value()


@dataclass(frozen=True, slots=True)
class ComponentConsumption:
    """One component's contribution to a completed run."""

    component: AssemblyOrderComponent
    quantity: Decimal
    serial_ids: tuple[uuid.UUID, ...]
    #: One lot per component line. The model deliberately does not split a
    #: kitting line across lots; a pick that genuinely needs two is two lines.
    batch_id: uuid.UUID | None


def plan_consumption(
    components: Sequence[AssemblyOrderComponent],
    *,
    planned_quantity: Decimal,
    produced_quantity: Decimal,
    serials_by_line: dict[int, list[uuid.UUID]],
    batches_by_line: dict[int, uuid.UUID | None] | None = None,
) -> list[ComponentConsumption]:
    """Work out what each component gives up, and check it against what it has.

    Pure: everything it needs has been fetched already, so the arithmetic and the
    three rules it enforces can be exercised without a database.

    The rules, in the order they fail:

    1. nothing may be consumed that was never issued to the line;
    2. a serialised component must name exactly one serial per unit consumed -
       the inventory engine posts one movement per serial, and the database
       constrains the stored array to the same count;
    3. a component that is not serialised may not name serials at all.
    """
    plan: list[ComponentConsumption] = []

    for component in components:
        quantity = consumption_for(
            required_quantity=Decimal(component.required_quantity),
            planned_quantity=planned_quantity,
            produced_quantity=produced_quantity,
        )
        issued = to_quantity(Decimal(component.issued_quantity))
        if quantity > issued:
            raise BusinessRuleError(
                f"Component line {component.line_number} would consume {quantity} but only "
                f"{issued} was issued to the line. Issue the balance before completing.",
                code=ErrorCode.INSUFFICIENT_STOCK,
                details={
                    "line_number": component.line_number,
                    "item_id": str(component.item_id),
                    "required": str(quantity),
                    "issued": str(issued),
                },
            )

        serials = tuple(serials_by_line.get(component.line_number, ()))

        if component.is_serial_tracked_component:
            if not is_whole_units(quantity):
                raise BusinessRuleError(
                    f"Component line {component.line_number} is serialised and cannot consume "
                    f"a part unit ({quantity}).",
                    details={"line_number": component.line_number, "quantity": str(quantity)},
                )
            if len(serials) != int(quantity):
                raise BusinessRuleError(
                    f"Component line {component.line_number} consumes {quantity} serialised "
                    f"unit(s); {len(serials)} serial number(s) were named.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={
                        "line_number": component.line_number,
                        "expected_serials": str(quantity),
                        "supplied_serials": len(serials),
                    },
                )
        elif serials:
            raise BusinessRuleError(
                f"Component line {component.line_number} is not serial tracked, so it cannot "
                "name serial numbers.",
                details={"line_number": component.line_number},
            )

        requested_batch = (batches_by_line or {}).get(component.line_number)
        if requested_batch is None:
            requested_batch = component.issued_batch_id
        if component.is_batch_tracked_component:
            if requested_batch is None:
                raise BusinessRuleError(
                    f"Component line {component.line_number} is batch tracked; name the lot "
                    "it consumed.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"line_number": component.line_number},
                )
        elif requested_batch is not None:
            raise BusinessRuleError(
                f"Component line {component.line_number} is not batch tracked, so it cannot "
                "name a batch.",
                details={"line_number": component.line_number},
            )

        plan.append(
            ComponentConsumption(
                component=component,
                quantity=quantity,
                serial_ids=serials,
                batch_id=requested_batch,
            )
        )

    return plan


def build_movements(
    plan: Sequence[ComponentConsumption],
    *,
    order: AssemblyOrder,
    reason: str,
    occurred_at: datetime | None,
) -> list[MovementRequest]:
    """Every ledger row one completion writes, as one batch.

    Components first, output last, all in one list so
    :func:`~src.modules.inventory.service.post_movements` locks the whole set of
    balance rows in primary-key order before changing any of them. A serialised
    component contributes one movement per serial because a serial identifies a
    single physical unit and the engine refuses any other quantity for it.

    Rejected output is deliberately absent: it consumed components but it is not
    stock. See the module's Assumptions.
    """
    movements: list[MovementRequest] = []

    for entry in plan:
        if entry.quantity <= ZERO:
            continue
        common: dict[str, Any] = {
            "item_id": entry.component.item_id,
            "warehouse_id": order.warehouse_id,
            "transaction_type": TransactionType.ASSEMBLY_CONSUME,
            "reference_type": MOVEMENT_REFERENCE_ASSEMBLY,
            "reference_id": order.id,
            "reference_code": order.code,
            "reason": reason,
            "batch_id": entry.batch_id,
            "occurred_at": occurred_at,
        }
        if entry.serial_ids:
            movements.extend(
                MovementRequest(quantity=Decimal("1"), serial_id=serial_id, **common)
                for serial_id in entry.serial_ids
            )
        else:
            movements.append(MovementRequest(quantity=entry.quantity, **common))

    assembled = to_quantity(Decimal(order.assembled_quantity))
    if assembled > ZERO:
        output_common: dict[str, Any] = {
            "item_id": order.item_id,
            "warehouse_id": order.output_warehouse_id,
            "transaction_type": TransactionType.ASSEMBLY_RECEIPT,
            "reference_type": MOVEMENT_REFERENCE_ASSEMBLY,
            "reference_id": order.id,
            "reference_code": order.code,
            "reason": reason,
            "batch_id": order.output_batch_id,
            # The whole QC behaviour, in one argument: a held receipt raises
            # qc_hold_quantity instead of the available pool, so the output
            # exists and is countable but nothing can be promised from it.
            "stock_state": (StockState.QC_HOLD if order.requires_qc else StockState.AVAILABLE),
            "occurred_at": occurred_at,
        }
        serial_ids = list(order.output_serial_ids or [])
        if serial_ids:
            movements.extend(
                MovementRequest(quantity=Decimal("1"), serial_id=serial_id, **output_common)
                for serial_id in serial_ids
            )
        else:
            movements.append(MovementRequest(quantity=assembled, **output_common))

    return movements


def reservation_required_date(planned_start: datetime | None) -> date | None:
    """The date a component reservation is wanted by: the order's planned start.

    ASSUMPTION: read as a UTC business date, the way every business date here is
    compared (``core.clock.today``). An order with no planned start reserves with
    no date, as it always did (docs/assumptions.md §E4-7).
    """
    if planned_start is None:
        return None
    return planned_start.astimezone(UTC).date()


@dataclass(frozen=True, slots=True)
class ComponentNeed:
    """One component line, reduced to what a release decision reads."""

    line_number: int
    item_id: uuid.UUID
    required: Decimal
    is_reserved: bool


def component_shortages(
    needs: Sequence[ComponentNeed], available: Mapping[uuid.UUID, Decimal]
) -> dict[int, Decimal]:
    """What a release would still lack, per component line.

    Lines are walked in the order release reserves them - by item, then line -
    and each consumes the stock it would reserve, so two lines of one item
    cannot both be told the same shelf is theirs. A line already reserved lacks
    nothing.
    """
    remaining = {item_id: max(quantity, ZERO) for item_id, quantity in available.items()}
    shortages: dict[int, Decimal] = {}
    for need in sorted(needs, key=lambda row: (str(row.item_id), row.line_number)):
        if need.is_reserved:
            shortages[need.line_number] = ZERO
            continue
        have = remaining.get(need.item_id, ZERO)
        shortages[need.line_number] = to_quantity(max(need.required - have, ZERO))
        remaining[need.item_id] = max(have - need.required, ZERO)
    return shortages


@dataclass(frozen=True, slots=True)
class Lateness:
    """How an open order stands against its own plan, at one instant."""

    is_late: bool
    start_overdue: bool
    days_late: int


def lateness(
    *,
    status: AssemblyOrderStatus,
    planned_start: datetime | None,
    planned_end: datetime | None,
    actual_start: datetime | None,
    now: datetime,
) -> Lateness:
    """The board's lateness badge, worked out on the server.

    ASSUMPTION: an order is late once its planned end has passed while it still
    owes finished stock - QC_PENDING included, because held output is not yet
    usable - and its start is overdue once the planned start has passed with the
    bench not begun. An order with no plan is never late (docs/assumptions.md
    §E4-10).
    """
    is_open = status in OPEN_SUPPLY_STATUSES
    is_late = is_open and planned_end is not None and planned_end < now
    start_overdue = (
        status in NOT_STARTED_STATUSES
        and actual_start is None
        and planned_start is not None
        and planned_start < now
    )
    days_late = (now - planned_end).days if is_late and planned_end is not None else 0
    return Lateness(is_late=is_late, start_overdue=start_overdue, days_late=days_late)


def plan_issue(
    components: Sequence[AssemblyOrderComponent],
    overrides: Mapping[int, Decimal],
    *,
    top_up: bool,
) -> dict[int, Decimal]:
    """The issued total each component line ends at after one issue request.

    A line the request names takes the stated total; a line it omits is issued
    in full. Either way the total is bounded by the requirement, because only
    the requirement was reserved.

    ASSUMPTION: a top-up states new totals, like the first issue does, and may
    only raise them - parts already on the bench are not handed back through
    this path - and a top-up that would change nothing is refused rather than
    recorded as an empty hand-over (docs/assumptions.md §E4-5).
    """
    by_line = {component.line_number: component for component in components}
    for line_number in overrides:
        if line_number not in by_line:
            raise NotFoundError("Assembly order component", line_number)

    plan: dict[int, Decimal] = {}
    for component in components:
        required = to_quantity(Decimal(component.required_quantity))
        target = to_quantity(overrides.get(component.line_number, required))
        if target > required:
            raise BusinessRuleError(
                f"Component line {component.line_number} may be issued at most "
                f"{required}; only that much is reserved for it.",
                details={
                    "line_number": component.line_number,
                    "required": str(required),
                    "requested": str(target),
                },
            )
        issued = to_quantity(Decimal(component.issued_quantity))
        if top_up and target < issued:
            raise BusinessRuleError(
                f"Component line {component.line_number} already has {issued} on the bench; "
                f"a top-up cannot lower it to {target}.",
                details={
                    "line_number": component.line_number,
                    "issued": str(issued),
                    "requested": str(target),
                },
            )
        plan[component.line_number] = target

    if top_up and all(
        plan[component.line_number] == to_quantity(Decimal(component.issued_quantity))
        for component in components
    ):
        raise BusinessRuleError(
            "Every component line already has that much on the bench; there is nothing to top up."
        )
    return plan


# -----------------------------------------------------------------------------
# Cross-module ports
# -----------------------------------------------------------------------------


class AllocationPort(Protocol):
    """The two allocation calls this module makes (database.md section 4.5)."""

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        quantity: Decimal,
        demand_type: DemandType,
        demand_id: uuid.UUID,
        reason: str,
        demand_line_id: uuid.UUID | None = None,
        demand_code: str | None = None,
        reservation_type: ReservationType = ReservationType.HARD,
        priority: int = DEFAULT_RESERVATION_PRIORITY,
        required_date: date | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> Any: ...

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> Any: ...


class AllocationService:
    """Default port: the real reservation engine."""

    async def reserve(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        quantity: Decimal,
        demand_type: DemandType,
        demand_id: uuid.UUID,
        reason: str,
        demand_line_id: uuid.UUID | None = None,
        demand_code: str | None = None,
        reservation_type: ReservationType = ReservationType.HARD,
        priority: int = DEFAULT_RESERVATION_PRIORITY,
        required_date: date | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> Any:
        from src.modules.allocation.service import reserve as _reserve

        return await _reserve(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=quantity,
            demand_type=demand_type,
            demand_id=demand_id,
            demand_line_id=demand_line_id,
            demand_code=demand_code,
            reservation_type=reservation_type,
            priority=priority,
            required_date=required_date,
            reason=reason,
            batch_id=batch_id,
        )

    async def release(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        reservation_id: uuid.UUID,
        reason: str,
        quantity: Decimal | None = None,
    ) -> Any:
        from src.modules.allocation.service import release as _release

        return await _release(
            session, tenant, reservation_id=reservation_id, reason=reason, quantity=quantity
        )


_DEFAULT_ALLOCATION = AllocationService()


def _allocation(port: AllocationPort | None) -> AllocationPort:
    return port if port is not None else _DEFAULT_ALLOCATION


class QualityPort(Protocol):
    """What assembly asks of the quality module when it is deployed.

    Deliberately narrow: one call raising one in-process inspection against one
    completed assembly. Anything wider would couple completion to a module that
    is built independently of it.
    """

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        quantity: Decimal,
        warehouse_id: uuid.UUID,
        reference_type: str,
        reference_id: uuid.UUID,
        reference_code: str,
    ) -> Any: ...


def resolve_quality_port() -> QualityPort | None:
    """The quality module's in-process inspection factory, if it is deployed.

    Only the *absence* of the module is tolerated. A failure inside it is not
    swallowed: an inspection that could not be raised must roll the completion
    back with everything else, rather than leaving stock on QC hold with nobody
    told to look at it.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    factory = getattr(quality_service, "create_in_process_inspection", None)
    return factory if callable(factory) else None


class InspectionReaderPort(Protocol):
    """The quality module's read of every inspection one document raised."""

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        reference_type: str,
        reference_id: uuid.UUID,
    ) -> Sequence[Any]: ...


def resolve_inspection_reader_port() -> InspectionReaderPort | None:
    """The quality module's ``inspections_for_reference``, if it is deployed.

    Read by the owner-side verdict endpoint, which must know whether a quality
    inspection already owns the hold before it releases anything itself.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    reader = getattr(quality_service, "inspections_for_reference", None)
    return reader if callable(reader) else None


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------


def order_detail(
    order: AssemblyOrder, components: Sequence[AssemblyOrderComponent]
) -> AssemblyOrderDetail:
    detail = AssemblyOrderDetail.model_validate(order)
    detail.components = [AssemblyComponentRead.model_validate(row) for row in components]
    return detail


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    target: AssemblyOrderStatus,
    *,
    reason: str,
    repo: AssemblyOrderRepository,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> AssemblyOrder:
    """Move an order's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``assembly_orders.status`` and exactly one place that writes the
    matching audit row (master rules section 73).
    """
    current = AssemblyOrderStatus(order.status)
    ASSEMBLY_ORDER_LIFECYCLE.assert_transition(current, target)
    order.status = target
    repo.touch(order)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_ASSEMBLY_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return order


async def _resolve_bom(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    bom_id: uuid.UUID | None,
) -> Bom:
    """The approved structure this order will consume against.

    An explicit id is honoured but still checked: an order built from another
    item's recipe, or from a structure an engineer is still editing, would
    consume the wrong parts and could never be reconciled against engineering's
    view of the product (database.md section 5). The structure must be one of
    :data:`ASSEMBLABLE_BOM_TYPES`.
    """
    repo = BomRepository(session, tenant)

    if bom_id is not None:
        bom = await repo.get(bom_id)
        if bom is None:
            raise NotFoundError("Bill of materials", bom_id)
    else:
        bom = await repo.default_approved_for_item(item_id)
        if bom is None:
            raise BusinessRuleError(
                "This item has no default approved bill of materials; name one explicitly.",
                code=ErrorCode.BOM_NOT_FOUND,
                details={"item_id": str(item_id)},
            )

    if bom.item_id != item_id:
        raise BusinessRuleError(
            f"BOM {bom.code} builds a different item and cannot be used for this assembly.",
            details={"bom_id": str(bom.id), "item_id": str(item_id)},
        )
    if BomStatus(bom.status) is not BomStatus.APPROVED:
        raise BusinessRuleError(
            f"BOM {bom.code} is {bom.status}; only an approved structure may be assembled.",
            code=ErrorCode.BOM_NOT_APPROVED,
            details={"bom_id": str(bom.id), "status": str(bom.status)},
        )
    if BomType(bom.bom_type) not in ASSEMBLABLE_BOM_TYPES:
        raise BusinessRuleError(
            f"BOM {bom.code} is a {bom.bom_type} structure; an assembly order needs an "
            "ASSEMBLY or PRODUCTION one.",
            details={
                "bom_id": str(bom.id),
                "bom_type": str(bom.bom_type),
                "allowed_bom_types": [str(kind) for kind in ASSEMBLABLE_BOM_TYPES],
            },
        )
    return bom


async def _explode_components(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    bom: Bom,
    planned_quantity: Decimal,
) -> list[tuple[BomLine, AssemblyItem, Decimal]]:
    """One level of the structure, scaled to the order and validated.

    One level, not a full explosion: an assembly consumes the parts its own
    recipe names. A sub-assembly that is itself made in-house is consumed as a
    stocked part and has its own assembly order - exploding through it here would
    consume its children twice, once for it and once for this order.
    """
    lines = await BomLineRepository(session, tenant).list_for_bom(bom.id)
    buildable = [line for line in lines if is_explodable(line)]

    if not buildable:
        raise BusinessRuleError(
            f"BOM {bom.code} has no consumable component lines.",
            details={"bom_id": str(bom.id), "line_count": len(lines)},
        )
    if len(buildable) > MAX_COMPONENTS_PER_ORDER:
        raise BusinessRuleError(
            f"BOM {bom.code} has {len(buildable)} component lines; an assembly order "
            f"carries at most {MAX_COMPONENTS_PER_ORDER}.",
            details={"bom_id": str(bom.id), "line_count": len(buildable)},
        )

    items = await ItemLookupRepository(session, tenant).load(
        [line.component_item_id for line in buildable]
    )

    exploded: list[tuple[BomLine, AssemblyItem, Decimal]] = []
    for line in buildable:
        item = items.get(line.component_item_id)
        if item is None:
            raise NotFoundError("Item", line.component_item_id)
        if not item.is_active:
            raise BusinessRuleError(
                f"Component {item.code} is inactive and cannot be assembled with.",
                details={"item_code": item.code, "line_number": line.line_number},
            )
        if not item.can_be_assembly_component:
            raise BusinessRuleError(
                f"{item.code} is not flagged as usable in an assembly.",
                details={"item_code": item.code, "line_number": line.line_number},
            )
        exploded.append(
            (
                line,
                item,
                requirement_for(
                    planned_quantity=planned_quantity,
                    quantity_per=Decimal(line.quantity_per),
                    output_quantity=Decimal(bom.output_quantity),
                    scrap_percentage=Decimal(line.scrap_percentage),
                ),
            )
        )
    return exploded


async def _persist_components(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    exploded: Sequence[tuple[BomLine, AssemblyItem, Decimal]],
) -> list[AssemblyOrderComponent]:
    repo = AssemblyComponentRepository(session, tenant)
    components = [
        repo.add(
            AssemblyOrderComponent(
                assembly_order_id=order.id,
                line_number=line.line_number,
                item_id=item.id,
                uom_id=line.uom_id,
                required_quantity=quantity,
                is_serial_tracked_component=item.is_serial_tracked,
                is_batch_tracked_component=item.is_batch_tracked,
                consumed_serial_ids=[],
                notes=line.notes,
            )
        )
        for line, item, quantity in exploded
    ]
    await session.flush()
    return components


async def _assert_warehouses(session: AsyncSession, tenant: TenantContext, *ids: uuid.UUID) -> None:
    """Both warehouses in one statement, and both must be able to hold stock."""
    found = await WarehouseLookupRepository(session, tenant).load(list(ids))
    for warehouse_id in ids:
        row = found.get(warehouse_id)
        if row is None:
            raise NotFoundError("Warehouse", warehouse_id)
        code, is_active = row
        if not is_active:
            raise BusinessRuleError(
                f"Warehouse {code} is inactive and cannot be used by an assembly order.",
                details={"warehouse_id": str(warehouse_id), "warehouse_code": code},
            )


async def _kit_position(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    components: Sequence[AssemblyOrderComponent],
) -> tuple[dict[int, Decimal], dict[uuid.UUID, Decimal]]:
    """Per-line shortages and per-item availability, in one balance read."""
    available = await ComponentAvailabilityRepository(session, tenant).reservable_by_item(
        [component.item_id for component in components], order.warehouse_id
    )
    # A batch-tracked component's stock lives on lot rows, never the batch-less
    # position the reservation engine uses for untracked items. Count both so
    # the pre-flight shortage report reflects what is really promisable.
    batches = await ComponentAvailabilityRepository(session, tenant).reservable_batches(
        [component.item_id for component in components], order.warehouse_id
    )
    for item_id, lots in batches.items():
        available[item_id] = available.get(item_id, ZERO) + sum(
            (lot.available_quantity for lot in lots), ZERO
        )
    needs = [
        ComponentNeed(
            line_number=component.line_number,
            item_id=component.item_id,
            required=to_quantity(Decimal(component.required_quantity)),
            is_reserved=component.reservation_id is not None,
        )
        for component in components
    ]
    return component_shortages(needs, available), available


async def _assert_kit_available(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    components: Sequence[AssemblyOrderComponent],
) -> None:
    """Refuse a release the kit cannot cover, naming every short component."""
    shortages, available = await _kit_position(session, tenant, order, components)
    short = [component for component in components if shortages[component.line_number] > ZERO]
    if not short:
        return

    items = await ItemLookupRepository(session, tenant).load([row.item_id for row in short])
    rows = [
        {
            "line_number": component.line_number,
            "item_id": str(component.item_id),
            "item_code": (
                items[component.item_id].code
                if component.item_id in items
                else str(component.item_id)
            ),
            "required": str(to_quantity(Decimal(component.required_quantity))),
            "available": str(to_quantity(max(available.get(component.item_id, ZERO), ZERO))),
            "short": str(shortages[component.line_number]),
        }
        for component in sorted(short, key=lambda row: row.line_number)
    ]
    raise KitShortageError(
        f"{order.code} cannot be released: {len(rows)} component(s) are short at its "
        "component warehouse - "
        + ", ".join(f"{row['item_code']} short {row['short']}" for row in rows)
        + ". Nothing was reserved.",
        details={
            "order_code": order.code,
            "warehouse_id": str(order.warehouse_id),
            "shortages": rows,
        },
    )


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: AssemblyOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[AssemblyOrder], OffsetPagination]:
    return await AssemblyOrderRepository(session, tenant).list_page_filtered(filters, params, sort)


async def get_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> AssemblyOrderDetail:
    order = await AssemblyOrderRepository(session, tenant).get_or_404(order_id)
    components = await AssemblyComponentRepository(session, tenant).list_for_order(order.id)
    return order_detail(order, components)


async def list_components(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[AssemblyComponentRead]:
    """The kitting list. The header is fetched first so a missing order 404s
    rather than returning an empty component set that looks like a built order."""
    await AssemblyOrderRepository(session, tenant).get_or_404(order_id)
    rows = await AssemblyComponentRepository(session, tenant).list_for_order(order_id)
    return [AssemblyComponentRead.model_validate(row) for row in rows]


async def ready_for_packing(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID | None = None,
    limit: int = READY_FOR_PACKING_MAX,
) -> list[ReadyForPackingRow]:
    """Completed assemblies whose output is packable - the packing handover.

    This is the contract the ``packing`` module consumes. An assembly appears
    here when it is COMPLETED, no packing order has claimed it, and the output
    position still has promisable stock. An output on QC hold is in QC_PENDING
    and never appears; an output already reserved by a sales order drops out
    because ``available_quantity`` accounts for it.

    ``limit`` is capped rather than trusted: this returns a plain list, so the
    bound has to live here (section 31).
    """
    bounded = max(1, min(int(limit), READY_FOR_PACKING_MAX))
    return await AssemblyOrderRepository(session, tenant).ready_for_packing(
        warehouse_id=warehouse_id, limit=bounded
    )


async def list_ready_for_packing(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID | None,
    params: PageParams,
) -> tuple[list[ReadyForPackingRow], OffsetPagination]:
    """Paginated form of the same feed, for the HTTP surface."""
    return await AssemblyOrderRepository(session, tenant).ready_for_packing_page(
        warehouse_id=warehouse_id, params=params
    )


async def component_availability(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[AssemblyComponentAvailability]:
    """Each component's live stock position against what a release would reserve.

    Read before releasing, so the planner sees the kit's whole gap before the
    release refuses it. Two statements: the components and one aggregate over
    the balance table - never one availability read per line.
    """
    order = await AssemblyOrderRepository(session, tenant).get_or_404(order_id)
    components = await AssemblyComponentRepository(session, tenant).list_for_order(order.id)
    shortages, available = await _kit_position(session, tenant, order, components)
    return [
        AssemblyComponentAvailability(
            line_number=component.line_number,
            item_id=component.item_id,
            required_quantity=to_quantity(Decimal(component.required_quantity)),
            issued_quantity=to_quantity(Decimal(component.issued_quantity)),
            is_reserved=component.reservation_id is not None,
            available_quantity=to_quantity(max(available.get(component.item_id, ZERO), ZERO)),
            shortage_quantity=shortages[component.line_number],
        )
        for component in components
    ]


def _board_row(order: AssemblyOrder, *, now: datetime) -> AssemblyBoardOrder:
    status = AssemblyOrderStatus(order.status)
    late = lateness(
        status=status,
        planned_start=order.planned_start,
        planned_end=order.planned_end,
        actual_start=order.actual_start,
        now=now,
    )
    return AssemblyBoardOrder(
        id=order.id,
        code=order.code,
        item_id=order.item_id,
        status=status,
        assembly_line=order.assembly_line,
        warehouse_id=order.warehouse_id,
        output_warehouse_id=order.output_warehouse_id,
        priority=order.priority,
        planned_quantity=Decimal(order.planned_quantity),
        assembled_quantity=Decimal(order.assembled_quantity),
        rejected_quantity=Decimal(order.rejected_quantity),
        planned_start=order.planned_start,
        planned_end=order.planned_end,
        actual_start=order.actual_start,
        actual_end=order.actual_end,
        requires_qc=order.requires_qc,
        is_late=late.is_late,
        start_overdue=late.start_overdue,
        days_late=late.days_late,
    )


async def board_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: AssemblyBoardFilters,
    params: PageParams,
) -> tuple[list[AssemblyBoardOrder], OffsetPagination]:
    """The assembly board: open orders, line by line, planned against actual."""
    now = utcnow()
    rows, pagination = await AssemblyOrderRepository(session, tenant).board_page(
        filters, params, now=now
    )
    return [_board_row(row, now=now) for row in rows], pagination


async def board_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID | None,
    params: PageParams,
) -> tuple[list[AssemblyLineLoad], OffsetPagination]:
    """Open work per assembly line, for the board's line summary."""
    return await AssemblyOrderRepository(session, tenant).line_load_page(
        warehouse_id=warehouse_id, params=params, now=utcnow()
    )


@dataclass(frozen=True, slots=True)
class PackingHandoverFacts:
    """What the packing module needs to raise a packing order from an assembly."""

    assembly_order_id: uuid.UUID
    code: str
    item_id: uuid.UUID
    assembled_quantity: Decimal
    output_warehouse_id: uuid.UUID
    source_type: AssemblySourceType
    source_id: uuid.UUID | None


async def packing_handover_facts(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> PackingHandoverFacts:
    """Lock a completed assembly for hand-over and say what it produced.

    The packing module calls this before raising its packing order, inside the
    same transaction, and :func:`mark_packed` after. Taking the row lock here
    rather than only in ``mark_packed`` is what makes a second packer wait and
    then be refused, instead of both raising a packing order and one of them
    failing only at the very end.
    """
    async with transaction(session):
        order = await AssemblyOrderRepository(session, tenant).lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        _assert_packable(order)
        assembled = to_quantity(Decimal(order.assembled_quantity))
        if assembled <= ZERO:
            raise BusinessRuleError(
                f"{order.code} produced no good output, so there is nothing to pack.",
                details={"order_id": str(order.id)},
            )
        return PackingHandoverFacts(
            assembly_order_id=order.id,
            code=order.code,
            item_id=order.item_id,
            assembled_quantity=assembled,
            output_warehouse_id=order.output_warehouse_id,
            source_type=AssemblySourceType(order.source_type),
            source_id=order.source_id,
        )


def _assert_packable(order: AssemblyOrder, *, packing_order_id: uuid.UUID | None = None) -> None:
    """A completed order that no *other* packing order has taken."""
    if AssemblyOrderStatus(order.status) is not AssemblyOrderStatus.COMPLETED:
        raise BusinessRuleError(
            f"{order.code} is {order.status}; only a completed assembly can be packed.",
            details={"order_id": str(order.id), "status": str(order.status)},
        )
    if order.packing_order_id is not None and order.packing_order_id != packing_order_id:
        raise BusinessRuleError(
            f"{order.code} has already been taken by another packing order.",
            code=ErrorCode.CONFLICT,
            details={
                "order_id": str(order.id),
                "packing_order_id": str(order.packing_order_id),
            },
        )


# -----------------------------------------------------------------------------
# Create / update / delete
# -----------------------------------------------------------------------------


async def create_order(
    session: AsyncSession,
    tenant: TenantContext,
    payload: AssemblyOrderCreate,
) -> tuple[AssemblyOrder, list[AssemblyOrderComponent]]:
    """Raise a DRAFT assembly order with its component set exploded and frozen.

    No stock is reserved and none is consumed: a draft is a plan, and holding
    components against it would let an unapproved order starve a released one.
    """
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        items = await ItemLookupRepository(session, tenant).load([payload.item_id])
        output_item = items.get(payload.item_id)
        if output_item is None:
            raise NotFoundError("Item", payload.item_id)
        if not output_item.is_active:
            raise BusinessRuleError(
                f"Item {output_item.code} is inactive and cannot be assembled.",
                details={"item_id": str(payload.item_id)},
            )

        output_warehouse_id = payload.output_warehouse_id or payload.warehouse_id
        await _assert_warehouses(session, tenant, payload.warehouse_id, output_warehouse_id)

        bom = await _resolve_bom(session, tenant, item_id=payload.item_id, bom_id=payload.bom_id)
        exploded = await _explode_components(
            session, tenant, bom=bom, planned_quantity=payload.planned_quantity
        )

        code = await next_document_number(session, tenant.company_id, DocTypes.ASSEMBLY_ORDER)
        order = repo.add(
            AssemblyOrder(
                code=code,
                item_id=payload.item_id,
                bom_id=bom.id,
                planned_quantity=to_quantity(payload.planned_quantity),
                warehouse_id=payload.warehouse_id,
                output_warehouse_id=output_warehouse_id,
                assembly_line=payload.assembly_line,
                planned_start=payload.planned_start,
                planned_end=payload.planned_end,
                priority=payload.priority,
                status=AssemblyOrderStatus.DRAFT,
                requires_qc=payload.requires_qc,
                source_type=payload.source_type,
                source_id=payload.source_id,
                notes=payload.notes,
            )
        )
        await session.flush()

        components = await _persist_components(session, tenant, order, exploded)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Created {order.code}: assemble {order.planned_quantity} of "
                f"{output_item.code} from {len(components)} component(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "bom_code": bom.code,
                "component_count": len(components),
            },
        )

    return order, components


async def update_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: AssemblyOrderUpdate,
) -> tuple[AssemblyOrder, list[AssemblyOrderComponent]]:
    """Amend a draft.

    Changing the planned quantity re-explodes the component set from the same
    structure. Patching the requirements arithmetically instead would let the
    kitting list drift away from the recipe after two or three edits.
    """
    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.get_or_404(order_id)
        if AssemblyOrderStatus(order.status) is not AssemblyOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only a draft assembly order may be edited.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        before = snapshot(order, ORDER_AUDITED_FIELDS)
        fields = payload.model_dump(exclude_unset=True)

        warehouse_id = fields.get("warehouse_id", order.warehouse_id)
        output_warehouse_id = fields.get("output_warehouse_id", order.output_warehouse_id)
        if {"warehouse_id", "output_warehouse_id"} & fields.keys():
            await _assert_warehouses(session, tenant, warehouse_id, output_warehouse_id)

        for field, value in fields.items():
            # An explicit null clears a column that is nullable and is ignored
            # for one that is not: PATCH cannot be allowed to write NULL into a
            # NOT NULL column and fail with an integrity error naming a
            # constraint rather than the field.
            if value is None and field not in CLEARABLE_UPDATE_FIELDS:
                continue
            setattr(order, field, value)
        if fields.get("planned_quantity") is not None:
            order.planned_quantity = to_quantity(fields["planned_quantity"])
        if (
            order.planned_start is not None
            and order.planned_end is not None
            and order.planned_end < order.planned_start
        ):
            raise ValidationError("The planned end cannot be before the planned start.")

        repo.touch(order)

        components: list[AssemblyOrderComponent]
        if fields.get("planned_quantity") is not None:
            bom = await _resolve_bom(session, tenant, item_id=order.item_id, bom_id=order.bom_id)
            exploded = await _explode_components(
                session, tenant, bom=bom, planned_quantity=Decimal(order.planned_quantity)
            )
            await component_repo.delete_for_order(order.id)
            components = await _persist_components(session, tenant, order, exploded)
        else:
            await session.flush()
            components = await component_repo.list_for_order(order.id)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated draft {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={"component_count": len(components)},
        )

    return order, components


async def delete_draft_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a draft that was never committed.

    A hard delete, and only from DRAFT: nothing references an unapproved order
    and no stock was reserved against it. Anything past DRAFT is cancelled
    instead, so the trail survives (section 28).
    """
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.get_or_404(order_id)
        if AssemblyOrderStatus(order.status) is not AssemblyOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status} and must be cancelled rather than deleted, "
                "so the record of it survives.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Deleted draft {order.code}",
            user_id=tenant.user_id,
            old_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={"reason": reason},
        )
        # Components go with it through ON DELETE CASCADE.
        await repo.hard_delete(order)


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


async def approve_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> AssemblyOrder:
    """Sign off a draft so it can be scheduled and released."""
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)

        order.approved_by_id = tenant.user_id
        order.approved_at = utcnow()
        await _transition(
            session,
            tenant,
            order,
            AssemblyOrderStatus.APPROVED,
            reason=reason,
            repo=repo,
            action=AuditAction.APPROVE,
        )
    return order


async def release_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> tuple[AssemblyOrder, list[AssemblyOrderComponent]]:
    """Release to the line and reserve every component it will eat.

    Reserving is what makes a release mean something: from here the parts belong
    to this order and no sales order or other assembly can be promised them. A
    component that is short refuses the whole release rather than reserving what
    it can - a half-kitted job on a bench blocks the line and cannot be finished
    either way, so the shortage is better surfaced before the parts are moved.

    Every short component is named in one refusal (``details.shortages``), read
    in one statement before anything is reserved, so the planner sees the whole
    kit's gap rather than one part per attempt. The reservations themselves are
    still the gate: stock taken by someone else in between is refused by the
    reservation engine under its row lock.

    Each reservation carries the order's priority and its planned start as the
    required date, so allocation ranks this order's claim the way the planner
    ranked the order.
    """
    port = _allocation(allocation)
    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(order.status), AssemblyOrderStatus.RELEASED
        )

        components = await component_repo.list_for_order(order.id)
        await _assert_kit_available(session, tenant, order, components)

        required_date = reservation_required_date(order.planned_start)
        batch_lots = await ComponentAvailabilityRepository(session, tenant).reservable_batches(
            [component.item_id for component in components], order.warehouse_id
        )
        remaining_by_batch = {
            lot.batch_id: lot.available_quantity for lots in batch_lots.values() for lot in lots
        }
        # Reserving in a fixed order across every caller of this path is what
        # stops two releases that share components from taking the same balance
        # locks in opposite orders and deadlocking.
        for component in sorted(components, key=lambda row: (row.item_id, row.line_number)):
            if component.reservation_id is not None:
                continue
            quantity = Decimal(component.required_quantity)
            batch_id: uuid.UUID | None = None
            if component.is_batch_tracked_component:
                lots = batch_lots.get(component.item_id, [])
                preferred = component.issued_batch_id
                chosen = next(
                    (
                        lot
                        for lot in lots
                        if lot.batch_id == preferred
                        and remaining_by_batch[lot.batch_id] >= quantity
                    ),
                    None,
                )
                if chosen is None:
                    chosen = next(
                        (lot for lot in lots if remaining_by_batch[lot.batch_id] >= quantity),
                        None,
                    )
                if chosen is None:
                    raise KitShortageError(
                        f"{order.code} cannot be released: component line "
                        f"{component.line_number} is batch tracked and no single lot "
                        f"can cover {to_quantity(quantity)}.",
                        details={
                            "line_number": component.line_number,
                            "item_id": str(component.item_id),
                            "required": str(to_quantity(quantity)),
                            "lots": [
                                {
                                    "batch_id": str(lot.batch_id),
                                    "batch_number": lot.batch_number,
                                    "available": str(lot.available_quantity),
                                }
                                for lot in lots
                            ],
                        },
                    )
                batch_id = chosen.batch_id
                remaining_by_batch[batch_id] -= quantity
            reservation = await port.reserve(
                session,
                tenant,
                item_id=component.item_id,
                warehouse_id=order.warehouse_id,
                quantity=quantity,
                demand_type=DemandType.ASSEMBLY_ORDER,
                demand_id=order.id,
                demand_line_id=component.id,
                demand_code=order.code,
                reservation_type=ReservationType.HARD,
                priority=order.priority,
                required_date=required_date,
                reason=f"{order.code} released to the assembly line: {reason}"[:255],
                batch_id=batch_id,
            )
            component.reservation_id = reservation.id
            if batch_id is not None:
                component.issued_batch_id = batch_id
            component_repo.touch(component)
        await session.flush()

        await _transition(
            session, tenant, order, AssemblyOrderStatus.RELEASED, reason=reason, repo=repo
        )

    return order, components


async def issue_components(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: IssueComponentsRequest,
) -> tuple[AssemblyOrder, list[AssemblyOrderComponent]]:
    """Record the physical hand-over of components to the bench.

    No ledger row is written here, by design. The components are still owned,
    still in the warehouse position they were reserved at, and still countable;
    what changed is who is holding them. The ledger learns about them at
    completion, when it is known what they were turned into - see the module
    docstring. ``issued_quantity`` is what bounds that consumption.

    The first issue, from RELEASED, moves the order to COMPONENTS_ISSUED. A
    *top-up* - the balance of a short issue arriving later - is accepted while
    the components are on the bench (COMPONENTS_ISSUED or IN_PROGRESS): it
    raises the issued totals, is audited the same way, and leaves the status
    where it is, because the state machine has no edge back into the state the
    order is already in. See :func:`plan_issue` for the rules on the totals.
    """
    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        status = AssemblyOrderStatus(order.status)
        top_up = status in COMPONENTS_HELD_STATUSES
        if not top_up:
            ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
                status, AssemblyOrderStatus.COMPONENTS_ISSUED
            )

        components = await component_repo.list_for_order(order.id)
        before = {
            str(component.line_number): to_quantity(Decimal(component.issued_quantity))
            for component in components
        }
        overrides = {line.line_number: to_quantity(line.issued_quantity) for line in payload.lines}
        batches = {line.line_number: line.batch_id for line in payload.lines}
        plan = plan_issue(components, overrides, top_up=top_up)

        for component in components:
            component.issued_quantity = plan[component.line_number]
            requested_batch = batches.get(component.line_number)
            if requested_batch is not None and not component.is_batch_tracked_component:
                raise BusinessRuleError(
                    f"Component line {component.line_number} is not batch tracked, so it "
                    "cannot name a lot.",
                    details={"line_number": component.line_number},
                )
            if requested_batch is not None:
                if component.issued_batch_id not in (None, requested_batch):
                    raise BusinessRuleError(
                        f"Component line {component.line_number} already has a different lot "
                        "issued to the bench.",
                        code=ErrorCode.CONFLICT,
                        details={"line_number": component.line_number},
                    )
                component.issued_batch_id = requested_batch
            component_repo.touch(component)
        await session.flush()

        if not top_up:
            await _transition(
                session,
                tenant,
                order,
                AssemblyOrderStatus.COMPONENTS_ISSUED,
                reason=payload.reason,
                repo=repo,
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Topped up the components on the bench for {order.code}"
                if top_up
                else f"Issued {len(components)} component line(s) to {order.code}"
            ),
            user_id=tenant.user_id,
            old_values={"issued": before},
            new_values={
                "issued": {
                    str(component.line_number): component.issued_quantity
                    for component in components
                }
            },
            context={
                "reason": payload.reason,
                "top_up": top_up,
                "status": str(status),
                "short_issued_lines": sum(
                    1
                    for component in components
                    if Decimal(component.issued_quantity) < Decimal(component.required_quantity)
                ),
            },
        )

    return order, components


async def start_assembly(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> AssemblyOrder:
    """Mark work as begun and stamp ``actual_start``.

    A separate step from issuing because kitting and building are separate
    events, often hours apart and performed by different people; collapsing them
    would make every order look as though it started the moment its parts were
    picked, and "are we late?" would be answered against a fiction.
    """
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)

        if order.actual_start is None:
            order.actual_start = utcnow()
        await _transition(
            session, tenant, order, AssemblyOrderStatus.IN_PROGRESS, reason=reason, repo=repo
        )
    return order


async def _validate_serials(
    session: AsyncSession,
    tenant: TenantContext,
    plan: Sequence[ComponentConsumption],
    *,
    warehouse_id: uuid.UUID,
) -> None:
    """Check every named serial in one batch, not one query per component.

    Four things have to be true of a serial before an assembly may swallow it:
    it exists in this tenant, it belongs to the component item, it is physically
    at the warehouse the components are drawn from, and no other assembly has
    already consumed it. The last one is a containment query rather than a
    constraint because "appears in at most one array across the table" is not
    expressible as a CHECK.
    """
    wanted = [serial_id for entry in plan for serial_id in entry.serial_ids]
    if not wanted:
        return

    repo = SerialLookupRepository(session, tenant)
    facts = await repo.load(wanted)
    already = await repo.already_consumed(wanted)

    for entry in plan:
        for serial_id in entry.serial_ids:
            serial = facts.get(serial_id)
            if serial is None:
                raise NotFoundError("Stock serial", serial_id)
            if serial.item_id != entry.component.item_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} belongs to a different item than "
                    f"component line {entry.component.line_number}.",
                    details={
                        "serial_id": str(serial_id),
                        "line_number": entry.component.line_number,
                    },
                )
            if serial.warehouse_id != warehouse_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} is not held at the warehouse this order "
                    "draws components from.",
                    details={
                        "serial_id": str(serial_id),
                        "serial_warehouse_id": str(serial.warehouse_id),
                        "order_warehouse_id": str(warehouse_id),
                    },
                )
            if entry.batch_id is not None and serial.batch_id != entry.batch_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} belongs to a different lot than component "
                    f"line {entry.component.line_number}.",
                    details={
                        "serial_id": str(serial_id),
                        "line_number": entry.component.line_number,
                        "serial_batch_id": (
                            str(serial.batch_id) if serial.batch_id is not None else None
                        ),
                        "consumed_batch_id": str(entry.batch_id),
                    },
                )
            consumed_by = already.get(serial_id)
            if consumed_by is not None:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} was already consumed by assembly "
                    f"{consumed_by}.",
                    code=ErrorCode.CONFLICT,
                    details={"serial_id": str(serial_id), "assembly_code": consumed_by},
                )


async def _release_reservations(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    components: Sequence[AssemblyOrderComponent],
    *,
    port: AllocationPort,
    reason: str,
) -> int:
    """Hand back everything the order still holds before the movements are posted.

    The order matters and is not a style choice. ``reserved_quantity`` has to
    come down before ``on_hand_quantity`` does, or the movement engine would see
    a position whose reservation exceeds the stock it is about to reduce and
    would correctly refuse it. Both happen inside the completing transaction, so
    no other document can slip into the gap.

    What is held is re-read from the reservations in one statement, never taken
    from ``component.reservation_id``: allocation closes a reservation on its
    own - released from the Reservations screen, expired, or taken by an
    approved reallocation - and asking it to release a closed one again is
    refused, which left the order unable to complete or be cancelled.

    ASSUMPTION: a reservation closed elsewhere is no reservation, so it is
    skipped rather than refused, and a reservation an approved reallocation
    granted *to* this order is given back with the rest - the order needs no
    held stock once it is completed or cancelled. Completion then consumes free
    stock like any unreserved movement: the stock engine still refuses it when
    free stock cannot cover a component, because the order lost its promise, it
    did not gain a right to stock promised since (docs/assumptions.md §E2-1).

    The iteration order is fixed - by item, then line - across every caller, so
    two completions sharing a component cannot take the same balance locks in
    opposite orders.
    """
    held = await AssemblyReservationRepository(session, tenant).open_for_order(order.id)
    line_numbers = {
        component.reservation_id: component.line_number
        for component in components
        if component.reservation_id is not None
    }
    for reservation in sorted(
        held,
        key=lambda row: (
            row.item_id,
            row.id not in line_numbers,
            line_numbers.get(row.id, 0),
            row.id,
        ),
    ):
        await port.release(session, tenant, reservation_id=reservation.id, reason=reason)
    for component in components:
        if component.reservation_id is not None:
            component.reservation_id = None
    await session.flush()
    return len(held)


async def complete_assembly(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CompleteRequest,
    *,
    allocation: AllocationPort | None = None,
    quality: QualityPort | None = None,
) -> CompletionResult:
    """Consume the components and create the finished assembly stock.

    This is the operation the whole module exists for, and it is one transaction
    from end to end: reservations released, every ``ASSEMBLY_CONSUME`` and the
    ``ASSEMBLY_RECEIPT`` posted in a single batch, component and header
    quantities written, the status moved, the inspection raised if one is owed,
    and the audit entries recorded. Either all of that commits or none of it
    does - there is no path that eats a cylinder without producing a lock set.
    """
    port = _allocation(allocation)
    quality_port = quality if quality is not None else resolve_quality_port()

    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)

        assembled = to_quantity(payload.assembled_quantity)
        rejected = to_quantity(payload.rejected_quantity)
        produced = assembled + rejected
        planned = to_quantity(Decimal(order.planned_quantity))
        if produced > planned:
            raise BusinessRuleError(
                f"{order.code} planned {planned} but {produced} was reported. Components were "
                "only drawn for the planned quantity; raise a second order for the excess.",
                details={
                    "planned_quantity": str(planned),
                    "assembled_quantity": str(assembled),
                    "rejected_quantity": str(rejected),
                },
            )

        target = (
            AssemblyOrderStatus.QC_PENDING
            if order.requires_qc and assembled > ZERO
            else AssemblyOrderStatus.COMPLETED
        )
        # Checked before any stock is touched, so an order in the wrong state
        # fails without having released a single reservation.
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(AssemblyOrderStatus(order.status), target)

        output_items = await ItemLookupRepository(session, tenant).load([order.item_id])
        output_item = output_items.get(order.item_id)
        if output_item is None:
            raise NotFoundError("Item", order.item_id)
        if assembled <= ZERO:
            if payload.output_batch_id is not None or payload.output_serial_numbers:
                raise BusinessRuleError(
                    "A run that assembled nothing has no output lot or serials to record."
                )
        else:
            if output_item.is_batch_tracked and payload.output_batch_id is None:
                raise BusinessRuleError(
                    f"{output_item.code} is batch tracked; name the output lot.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"item_code": output_item.code, "order_code": order.code},
                )
            if not output_item.is_batch_tracked and payload.output_batch_id is not None:
                raise BusinessRuleError(
                    f"{output_item.code} is not batch tracked; remove the output lot.",
                    details={"item_code": output_item.code, "order_code": order.code},
                )
            if output_item.is_serial_tracked:
                if len(payload.output_serial_numbers) != int(assembled):
                    raise BusinessRuleError(
                        f"{output_item.code} is serial tracked; name one serial per assembled "
                        "unit.",
                        code=ErrorCode.SERIAL_REQUIRED,
                        details={
                            "item_code": output_item.code,
                            "assembled_quantity": str(assembled),
                            "serial_count": len(payload.output_serial_numbers),
                        },
                    )
                order.output_serial_ids = await TraceabilityService(
                    session, tenant
                ).resolve_serial_ids(
                    order.item_id,
                    payload.output_serial_numbers,
                    batch_id=payload.output_batch_id,
                    warehouse_id=order.output_warehouse_id,
                    location_id=None,
                    received_on=(payload.occurred_at or utcnow()).date(),
                    source=order.code,
                )
            elif payload.output_serial_numbers:
                raise BusinessRuleError(
                    f"{output_item.code} is not serial tracked; remove the output serials.",
                    details={"item_code": output_item.code, "order_code": order.code},
                )
            order.output_batch_id = payload.output_batch_id

        components = await component_repo.list_for_order(order.id)
        plan = plan_consumption(
            components,
            planned_quantity=planned,
            produced_quantity=produced,
            serials_by_line={line.line_number: line.serial_ids for line in payload.components},
            batches_by_line={line.line_number: line.batch_id for line in payload.components},
        )
        await _validate_serials(session, tenant, plan, warehouse_id=order.warehouse_id)

        order.assembled_quantity = assembled
        order.rejected_quantity = rejected
        order.actual_end = payload.occurred_at or utcnow()
        if order.actual_start is None:
            order.actual_start = order.actual_end
        elif order.actual_end < order.actual_start:
            # Caught here rather than at the CHECK constraint, which would
            # surface as an integrity error naming a constraint instead of the
            # back-dated timestamp the operator actually typed.
            raise ValidationError(
                "The completion time is before the assembly started.",
                details={
                    "actual_start": order.actual_start.isoformat(),
                    "occurred_at": order.actual_end.isoformat(),
                },
            )

        released = await _release_reservations(
            session,
            tenant,
            order,
            components,
            port=port,
            reason=f"{order.code} completed: {payload.reason}"[:255],
        )

        movement_reason = f"Assembly {order.code}: {payload.reason}"[:255]
        movements = build_movements(
            plan, order=order, reason=movement_reason, occurred_at=payload.occurred_at
        )
        posted = await post_movements(session, tenant, movements)

        for entry in plan:
            entry.component.consumed_quantity = entry.quantity
            entry.component.consumed_serial_ids = list(entry.serial_ids)
            entry.component.consumed_batch_id = entry.batch_id
            component_repo.touch(entry.component)

        held_for_qc = target is AssemblyOrderStatus.QC_PENDING
        if held_for_qc:
            order.qc_held_at = order.actual_end
        repo.touch(order)
        await session.flush()

        inspection_created = False
        if held_for_qc and quality_port is not None:
            inspection = await quality_port(
                session,
                tenant,
                item_id=order.item_id,
                quantity=assembled,
                warehouse_id=order.output_warehouse_id,
                reference_type=MOVEMENT_REFERENCE_ASSEMBLY,
                reference_id=order.id,
                reference_code=order.code,
                **({"serial_ids": order.output_serial_ids} if order.output_serial_ids else {}),
                **(
                    {"batch_id": order.output_batch_id} if order.output_batch_id is not None else {}
                ),
            )
            order.qc_inspection_id = getattr(inspection, "id", None)
            inspection_created = order.qc_inspection_id is not None
            await session.flush()

        await _transition(session, tenant, order, target, reason=payload.reason, repo=repo)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"{order.code} assembled {assembled} (rejected {rejected}) consuming "
                f"{len(plan)} component line(s) in {len(posted)} movement(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                "assembled_quantity": assembled,
                "rejected_quantity": rejected,
                "consumed": {str(entry.component.line_number): entry.quantity for entry in plan},
                "consumed_serials": {
                    str(entry.component.line_number): list(entry.serial_ids)
                    for entry in plan
                    if entry.serial_ids
                },
                "consumed_batches": {
                    str(entry.component.line_number): entry.batch_id
                    for entry in plan
                    if entry.batch_id is not None
                },
                "output_batch_id": order.output_batch_id,
                "output_serial_ids": list(order.output_serial_ids or []),
            },
            context={
                "reason": payload.reason,
                "movement_count": len(posted),
                "reservations_released": released,
                "output_stock_state": str(
                    StockState.QC_HOLD if held_for_qc else StockState.AVAILABLE
                ),
                "qc_inspection_created": inspection_created,
                # Recorded explicitly: this is how an operations team finds the
                # assemblies whose output is held with nobody asked to inspect it.
                "qc_requirement_unserviced": held_for_qc and not inspection_created,
            },
        )

        if held_for_qc and not inspection_created:
            log.warning(
                "assembly.qc_requirement_recorded_without_inspection",
                assembly_order=order.code,
                item_id=str(order.item_id),
                quantity=str(assembled),
            )

        detail = order_detail(order, components)

    return CompletionResult(
        order=detail,
        consume_movement_count=sum(
            1
            for movement in movements
            if movement.transaction_type is TransactionType.ASSEMBLY_CONSUME
        ),
        output_quantity=assembled,
        output_held_for_qc=held_for_qc,
        qc_inspection_created=inspection_created,
    )


def _assert_verdict_covers_hold(
    order: AssemblyOrder, *, accepted: Decimal, refused: Decimal
) -> None:
    held = to_quantity(Decimal(order.assembled_quantity))
    if accepted + refused != held:
        raise BusinessRuleError(
            f"{order.code} is holding {held} unit(s) for inspection; the verdict accounts "
            f"for {accepted + refused}.",
            details={
                "held_quantity": str(held),
                "accepted_quantity": str(accepted),
                "rejected_quantity": str(refused),
            },
        )


async def _apply_verdict(
    session: AsyncSession,
    tenant: TenantContext,
    order: AssemblyOrder,
    *,
    accepted: Decimal,
    refused: Decimal,
    reason: str,
    repo: AssemblyOrderRepository,
    inspection_id: uuid.UUID | None,
    inspection_code: str | None,
) -> AssemblyOrder:
    """Write a verdict onto a QC_PENDING order and complete it. Moves no stock.

    Shared by both ways a verdict arrives - the quality inspection's callback and
    the owner-side endpoint for a hold no inspection covers - so the tallies are
    written one way only. The caller has already settled the stock.
    """
    _assert_verdict_covers_hold(order, accepted=accepted, refused=refused)
    before = snapshot(order, ["assembled_quantity", "rejected_quantity", "qc_inspection_id"])

    # The order's own tally moves with the verdict: units that failed
    # inspection were built but are not good output, and the header has to
    # agree with the stock the ledger now describes.
    order.assembled_quantity = accepted
    order.rejected_quantity = to_quantity(Decimal(order.rejected_quantity) + refused)
    order.qc_held_at = None
    if inspection_id is not None:
        order.qc_inspection_id = inspection_id
    repo.touch(order)
    await session.flush()

    await _transition(
        session,
        tenant,
        order,
        AssemblyOrderStatus.COMPLETED,
        reason=reason[:255],
        repo=repo,
        action=AuditAction.QC_DECISION,
    )
    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.QC_DECISION,
        entity_type=AUDIT_ASSEMBLY_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        summary=(
            f"{order.code} inspection verdict applied: {accepted} accepted, {refused} refused"
            + (f" by {inspection_code}" if inspection_code else "")
        ),
        user_id=tenant.user_id,
        old_values=before,
        new_values=snapshot(order, ["assembled_quantity", "rejected_quantity", "qc_inspection_id"]),
        context={
            "reason": reason,
            "qc_inspection_code": inspection_code,
            # Where the held stock was moved, so an auditor never has to infer
            # which of two paths released it.
            "stock_released_by": "qc_inspection" if inspection_code else "assembly_order",
        },
    )
    return order


# -----------------------------------------------------------------------------
# The quality verdict - called back by quality (one release path per hold)
# -----------------------------------------------------------------------------


class InspectionVerdict(Protocol):
    """What assembly reads off a decided inspection.

    A protocol rather than quality's own type, so this module does not import
    the module it reaches through a port.
    """

    @property
    def inspection_id(self) -> uuid.UUID: ...
    @property
    def inspection_code(self) -> str: ...
    @property
    def reference_id(self) -> uuid.UUID | None: ...
    @property
    def accepted_quantity(self) -> Decimal: ...
    @property
    def refused_quantity(self) -> Decimal: ...
    @property
    def is_settled(self) -> bool: ...
    @property
    def reason(self) -> str: ...


async def lock_for_quality_verdict(
    session: AsyncSession, tenant: TenantContext, reference_id: uuid.UUID
) -> None:
    """Take the order's row lock before quality moves any of its held output."""
    await AssemblyOrderRepository(session, tenant).lock_for_update(reference_id)


async def record_quality_verdict(
    session: AsyncSession, tenant: TenantContext, verdict: InspectionVerdict
) -> bool:
    """Complete a QC_PENDING order from the inspection its completion raised.

    Quality has already moved the held output - accepted to available, rejected
    to damaged, scrap to scrap - so this writes the tallies and moves the order
    QC_PENDING -> COMPLETED and nothing else (:func:`_apply_verdict`). Scrapped
    output counts as rejected: it was built and it is not good output.

    Idempotent, and deliberately narrow about what it acts on: only the
    inspection this order's completion raised, only while the order is
    QC_PENDING, and only once nothing is left in rework or hold. Anything else -
    a manual inspection quoting the order, a partial decision, a verdict already
    applied - changes nothing. Returns whether the order changed.
    """
    if verdict.reference_id is None:
        return False
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(verdict.reference_id)
        if order is None:
            return False
        if order.qc_inspection_id != verdict.inspection_id:
            log.info(
                "assembly.verdict_not_for_this_order",
                order_code=order.code,
                inspection_id=str(verdict.inspection_id),
            )
            return False
        if AssemblyOrderStatus(order.status) is not AssemblyOrderStatus.QC_PENDING:
            return False
        if not verdict.is_settled:
            # Rework or hold still sits in QC hold; the order completes when the
            # inspection is re-decided and nothing is left.
            return False

        await _apply_verdict(
            session,
            tenant,
            order,
            accepted=to_quantity(verdict.accepted_quantity),
            refused=to_quantity(verdict.refused_quantity),
            reason=f"Inspection {verdict.inspection_code}: {verdict.reason}",
            repo=repo,
            inspection_id=verdict.inspection_id,
            inspection_code=verdict.inspection_code,
        )
    return True


async def awaiting_quality_verdicts(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    after: uuid.UUID | None,
    limit: int,
) -> list[tuple[str, uuid.UUID]]:
    """Orders on QC hold behind an inspection - one keyset page for the
    reconciliation sweep (``quality.owners.reconcile_owners``)."""
    order_ids = await AssemblyOrderRepository(session, tenant).awaiting_quality_verdict(
        after=after, limit=limit
    )
    return [(MOVEMENT_REFERENCE_ASSEMBLY, order_id) for order_id in order_ids]


def _inspection_figures(inspection: Any) -> tuple[bool, Decimal, Decimal]:
    """(settled, accepted, refused) read off an inspection row without importing
    quality's types - the same reading procurement's return rules make."""

    def quantity(name: str) -> Decimal:
        return to_quantity(Decimal(getattr(inspection, name, 0) or 0))

    still_held = quantity("rework_quantity") + quantity("hold_quantity")
    decided = str(getattr(inspection, "result", "PENDING")) != "PENDING"
    refused = to_quantity(quantity("rejected_quantity") + quantity("scrap_quantity"))
    return decided and still_held <= ZERO, quantity("accepted_quantity"), refused


async def _settle_from_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    inspection_id: uuid.UUID,
    payload: QcResultRequest,
) -> AssemblyOrder:
    """The owner-side endpoint, for an order whose hold an inspection covers.

    ASSUMPTION: the inspection is the one release path for the hold it covers.
    While it is open the request is refused and pointed at it; once it is
    decided the stock has already moved, so the order is completed from the
    inspection's own figures - and only if the request repeats them, because a
    different verdict recorded here would contradict the ledger
    (docs/assumptions.md §E4-3). Nothing is moved either way.
    """
    repo = AssemblyOrderRepository(session, tenant)
    order = await repo.get_or_404(order_id)
    order_code = order.code

    reader = resolve_inspection_reader_port()
    rows = (
        []
        if reader is None
        else await reader(session, tenant, MOVEMENT_REFERENCE_ASSEMBLY, order.id)
    )
    inspection = next((row for row in rows if row.id == inspection_id), None)
    if inspection is None:
        raise BusinessRuleError(
            f"{order_code} names inspection {inspection_id}, which cannot be read; its "
            "verdict cannot be recorded here.",
            details={"qc_inspection_id": str(inspection_id)},
        )

    settled, accepted, refused = _inspection_figures(inspection)
    inspection_code = str(inspection.code)
    if not settled:
        raise BusinessRuleError(
            held_for_inspection(order_code, inspection_code),
            code=ErrorCode.INVALID_STATE_TRANSITION,
            details={"qc_inspection_id": str(inspection_id), "qc_inspection_code": inspection_code},
        )
    if (to_quantity(payload.accepted_quantity), to_quantity(payload.rejected_quantity)) != (
        accepted,
        refused,
    ):
        raise BusinessRuleError(
            verdict_differs_from_inspection(order_code, inspection_code),
            details={
                "qc_inspection_id": str(inspection_id),
                "qc_inspection_code": inspection_code,
                "inspection_accepted_quantity": str(accepted),
                "inspection_rejected_quantity": str(refused),
            },
        )

    async with transaction(session):
        locked = await repo.lock_for_update(order_id)
        if locked is None:  # pragma: no cover - read a moment ago
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(locked.status), AssemblyOrderStatus.COMPLETED
        )
        await _apply_verdict(
            session,
            tenant,
            locked,
            accepted=accepted,
            refused=refused,
            reason=f"Inspection {inspection_code}: {payload.reason}",
            repo=repo,
            inspection_id=inspection_id,
            inspection_code=inspection_code,
        )
    return locked


async def record_qc_result(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: QcResultRequest,
) -> AssemblyOrder:
    """Apply an inspection verdict to output this module put on QC hold.

    For a hold no quality inspection covers - quality absent at completion, or
    ``requires_qc`` set after the fact - this is the release path: accepted
    quantity goes back to the available pool, rejected to the damaged pool,
    and the two must account for everything held. Releasing part of a lot and
    leaving the rest in limbo is how stock becomes permanently unusable without
    anybody deciding that it should be.

    When the order names an inspection, that inspection is the one release path
    and this defers to it (:func:`_settle_from_inspection`): it never moves the
    same held stock a second time.
    """
    repo = AssemblyOrderRepository(session, tenant)

    # A plain read first, deliberately without the row lock: deferring to an
    # inspection must not hold the order while quality locks the inspection and
    # then the order, which is quality's lock order.
    current = await repo.get_or_404(order_id)
    inspection_id = current.qc_inspection_id
    if inspection_id is not None:
        return await _settle_from_inspection(session, tenant, order_id, inspection_id, payload)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(order.status), AssemblyOrderStatus.COMPLETED
        )
        if order.qc_inspection_id is not None:  # pragma: no cover - raised since the read
            raise BusinessRuleError(
                held_for_inspection(order.code, str(order.qc_inspection_id)),
                code=ErrorCode.INVALID_STATE_TRANSITION,
            )

        accepted = to_quantity(payload.accepted_quantity)
        rejected = to_quantity(payload.rejected_quantity)
        _assert_verdict_covers_hold(order, accepted=accepted, refused=rejected)

        reason = f"Assembly {order.code} inspection: {payload.reason}"[:255]
        if accepted > ZERO:
            await change_stock_state(
                session,
                tenant,
                item_id=order.item_id,
                warehouse_id=order.output_warehouse_id,
                quantity=accepted,
                from_state=StockState.QC_HOLD,
                to_state=StockState.AVAILABLE,
                reason=reason,
                reference_type=MOVEMENT_REFERENCE_ASSEMBLY,
                reference_id=order.id,
                reference_code=order.code,
            )
        if rejected > ZERO:
            await change_stock_state(
                session,
                tenant,
                item_id=order.item_id,
                warehouse_id=order.output_warehouse_id,
                quantity=rejected,
                from_state=StockState.QC_HOLD,
                to_state=StockState.DAMAGED,
                reason=reason,
                reference_type=MOVEMENT_REFERENCE_ASSEMBLY,
                reference_id=order.id,
                reference_code=order.code,
            )

        await _apply_verdict(
            session,
            tenant,
            order,
            accepted=accepted,
            refused=rejected,
            reason=payload.reason,
            repo=repo,
            inspection_id=payload.qc_inspection_id,
            inspection_code=None,
        )
    return order


async def close_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> AssemblyOrder:
    """Close a completed order. Terminal: nothing may be posted against it after."""
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        await _transition(
            session, tenant, order, AssemblyOrderStatus.CLOSED, reason=reason, repo=repo
        )
    return order


async def cancel_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    allocation: AllocationPort | None = None,
) -> AssemblyOrder:
    """Cancel an order and give every reserved component back.

    Releasing inside the same transaction as the status change is the point: a
    cancelled order still holding reservations makes parts invisible to every
    other document while belonging to nobody. Only reachable before the
    components have been issued - see ``ASSEMBLY_ORDER_LIFECYCLE``.
    """
    port = _allocation(allocation)
    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(order.status), AssemblyOrderStatus.CANCELLED
        )

        components = await component_repo.list_for_order(order.id)
        released = await _release_reservations(
            session,
            tenant,
            order,
            components,
            port=port,
            reason=f"{order.code} cancelled: {reason}"[:255],
        )

        order.cancellation_reason = reason
        await _transition(
            session,
            tenant,
            order,
            AssemblyOrderStatus.CANCELLED,
            reason=reason,
            repo=repo,
            action=AuditAction.CANCEL,
        )

        if released:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.RESERVATION_CHANGE,
                entity_type=AUDIT_ASSEMBLY_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Released {released} component reservation(s) held by {order.code}",
                user_id=tenant.user_id,
                new_values={"released_reservations": released},
                context={"reason": reason},
            )
    return order


async def mark_packed(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    packing_order_id: uuid.UUID,
    reason: str,
) -> AssemblyOrder:
    """Claim a completed assembly's output for a packing order.

    The other half of :func:`ready_for_packing`, and the reason that feed is
    stable: once a packing order has taken an assembly it stops being offered,
    so two packers cannot both plan around the same output. Idempotent only for
    the same packing order - a second, different claim is refused rather than
    silently overwriting the first.
    """
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        _assert_packable(order, packing_order_id=packing_order_id)

        order.packing_order_id = packing_order_id
        order.packed_at = order.packed_at or utcnow()
        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ASSEMBLY_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"{order.code} handed to packing order {packing_order_id}",
            user_id=tenant.user_id,
            new_values={"packing_order_id": packing_order_id, "packed_at": order.packed_at},
            context={"reason": reason},
        )
    return order


__all__ = [
    "ASSEMBLABLE_BOM_TYPES",
    "ASSEMBLY_ORDER_LIFECYCLE",
    "AUDIT_ASSEMBLY_ORDER",
    "CANCELLABLE_STATUSES",
    "CLEARABLE_UPDATE_FIELDS",
    "MOVEMENT_REFERENCE_ASSEMBLY",
    "AllocationPort",
    "AllocationService",
    "ComponentConsumption",
    "ComponentNeed",
    "InspectionReaderPort",
    "InspectionVerdict",
    "KitShortageError",
    "Lateness",
    "PackingHandoverFacts",
    "QualityPort",
    "approve_order",
    "awaiting_quality_verdicts",
    "board_lines",
    "board_orders",
    "build_movements",
    "cancel_order",
    "close_order",
    "complete_assembly",
    "component_availability",
    "component_shortages",
    "consumption_for",
    "create_order",
    "delete_draft_order",
    "get_order_detail",
    "is_whole_units",
    "issue_components",
    "lateness",
    "list_components",
    "list_orders",
    "list_ready_for_packing",
    "lock_for_quality_verdict",
    "mark_packed",
    "order_detail",
    "packing_handover_facts",
    "plan_consumption",
    "plan_issue",
    "ready_for_packing",
    "record_qc_result",
    "record_quality_verdict",
    "release_order",
    "requirement_for",
    "reservation_required_date",
    "resolve_inspection_reader_port",
    "resolve_quality_port",
    "start_assembly",
    "update_order",
]
