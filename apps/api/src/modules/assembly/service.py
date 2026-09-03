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
* A **batch-tracked** component or output, and a **serial-tracked output**, are
  refused at order creation. The inventory engine requires a batch on every
  movement of a batch-tracked item and a serial on every movement of a
  serialised one, and this module has no contract for choosing a batch or
  minting a serial. Refusing at creation fails before anything is committed to,
  rather than after the components have been issued.
* **Rejected output produces no stock.** Only ``assembled_quantity`` is
  received. A failed assembly is not a saleable unit, and receiving it would put
  scrap into the finished-goods balance.
* **Issued quantity may not exceed the requirement**, because the requirement is
  what was reserved; anything beyond it would be unreserved stock on the bench
  that another document could still be promised.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
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
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.allocation.models import DemandType, ReservationType
from src.modules.assembly.models import (
    CANCELLABLE_STATUSES,
    MAX_COMPONENTS_PER_ORDER,
    AssemblyOrder,
    AssemblyOrderComponent,
    AssemblyOrderStatus,
)
from src.modules.assembly.repository import (
    AssemblyComponentRepository,
    AssemblyItem,
    AssemblyOrderRepository,
    ItemLookupRepository,
    SerialLookupRepository,
    WarehouseLookupRepository,
)
from src.modules.assembly.schemas import (
    READY_FOR_PACKING_MAX,
    AssemblyComponentRead,
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
    change_stock_state,
    post_movements,
)

log = get_logger(__name__)

AUDIT_ASSEMBLY_ORDER = "assembly_order"

#: What the ledger rows this module posts point back at. A bare string, matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
MOVEMENT_REFERENCE_ASSEMBLY = "assembly_order"

#: Draft fields an explicit ``null`` in a PATCH body legitimately clears. Every
#: other column on the update payload backs a NOT NULL column, where a null is a
#: client mistake rather than an instruction - see :func:`update_order`.
CLEARABLE_UPDATE_FIELDS = frozenset(
    {"assembly_line", "planned_start", "planned_end", "notes"}
)

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


def plan_consumption(
    components: Sequence[AssemblyOrderComponent],
    *,
    planned_quantity: Decimal,
    produced_quantity: Decimal,
    serials_by_line: dict[int, list[uuid.UUID]],
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

        plan.append(
            ComponentConsumption(component=component, quantity=quantity, serial_ids=serials)
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
        movements.append(
            MovementRequest(
                item_id=order.item_id,
                warehouse_id=order.output_warehouse_id,
                quantity=assembled,
                transaction_type=TransactionType.ASSEMBLY_RECEIPT,
                reference_type=MOVEMENT_REFERENCE_ASSEMBLY,
                reference_id=order.id,
                reference_code=order.code,
                reason=reason,
                # The whole QC behaviour, in one argument: a held receipt raises
                # qc_hold_quantity instead of the available pool, so the output
                # exists and is countable but nothing can be promised from it.
                stock_state=(
                    StockState.QC_HOLD if order.requires_qc else StockState.AVAILABLE
                ),
                occurred_at=occurred_at,
            )
        )

    return movements


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
            reason=reason,
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
    """The approved ``ASSEMBLY`` structure this order will consume against.

    An explicit id is honoured but still checked: an order built from another
    item's recipe, or from a structure an engineer is still editing, would
    consume the wrong parts and could never be reconciled against engineering's
    view of the product (database.md section 5).
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
    if BomType(bom.bom_type) is not BomType.ASSEMBLY:
        raise BusinessRuleError(
            f"BOM {bom.code} is a {bom.bom_type} structure; an assembly order needs an "
            "ASSEMBLY one.",
            details={"bom_id": str(bom.id), "bom_type": str(bom.bom_type)},
        )
    return bom


def _assert_trackable(item: AssemblyItem, *, role: str) -> None:
    """Refuse tracking modes this module has no contract to satisfy.

    See the module's Assumptions. Checked at creation so the order never reaches
    a bench it cannot be completed from.
    """
    if item.is_batch_tracked:
        raise BusinessRuleError(
            f"{item.code} is batch tracked and cannot be used as an assembly {role}: "
            "there is no contract for choosing a batch on this path.",
            code=ErrorCode.BATCH_REQUIRED,
            details={"item_code": item.code, "role": role},
        )
    if role == "output" and item.is_serial_tracked:
        raise BusinessRuleError(
            f"{item.code} is serial tracked and cannot be an assembly output: there is no "
            "contract for minting a serial number on this path.",
            code=ErrorCode.SERIAL_REQUIRED,
            details={"item_code": item.code},
        )


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
        _assert_trackable(item, role="component")

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
                consumed_serial_ids=[],
                notes=line.notes,
            )
        )
        for line, item, quantity in exploded
    ]
    await session.flush()
    return components


async def _assert_warehouses(
    session: AsyncSession, tenant: TenantContext, *ids: uuid.UUID
) -> None:
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
    return await AssemblyOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )


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
        _assert_trackable(output_item, role="output")

        output_warehouse_id = payload.output_warehouse_id or payload.warehouse_id
        await _assert_warehouses(session, tenant, payload.warehouse_id, output_warehouse_id)

        bom = await _resolve_bom(
            session, tenant, item_id=payload.item_id, bom_id=payload.bom_id
        )
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
        # Reserving in a fixed order across every caller of this path is what
        # stops two releases that share components from taking the same balance
        # locks in opposite orders and deadlocking.
        for component in sorted(components, key=lambda row: (row.item_id, row.line_number)):
            if component.reservation_id is not None:
                continue
            reservation = await port.reserve(
                session,
                tenant,
                item_id=component.item_id,
                warehouse_id=order.warehouse_id,
                quantity=Decimal(component.required_quantity),
                demand_type=DemandType.ASSEMBLY_ORDER,
                demand_id=order.id,
                demand_line_id=component.id,
                demand_code=order.code,
                reservation_type=ReservationType.HARD,
                reason=f"{order.code} released to the assembly line: {reason}"[:255],
            )
            component.reservation_id = reservation.id
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
    """
    repo = AssemblyOrderRepository(session, tenant)
    component_repo = AssemblyComponentRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(order.status), AssemblyOrderStatus.COMPONENTS_ISSUED
        )

        components = await component_repo.list_for_order(order.id)
        by_line = {component.line_number: component for component in components}

        overrides: dict[int, Decimal] = {}
        for line in payload.lines:
            component = by_line.get(line.line_number)
            if component is None:
                raise NotFoundError("Assembly order component", line.line_number)
            issued = to_quantity(line.issued_quantity)
            # More than the requirement was never reserved, so it would be stock
            # on the bench that another document could still be promised.
            if issued > to_quantity(Decimal(component.required_quantity)):
                raise BusinessRuleError(
                    f"Component line {line.line_number} may be issued at most "
                    f"{component.required_quantity}; only that much is reserved for it.",
                    details={
                        "line_number": line.line_number,
                        "required": str(component.required_quantity),
                        "requested": str(issued),
                    },
                )
            overrides[line.line_number] = issued

        for component in components:
            component.issued_quantity = overrides.get(
                component.line_number, to_quantity(Decimal(component.required_quantity))
            )
            component_repo.touch(component)
        await session.flush()

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
            summary=f"Issued {len(components)} component line(s) to {order.code}",
            user_id=tenant.user_id,
            new_values={
                "issued": {
                    str(component.line_number): component.issued_quantity
                    for component in components
                }
            },
            context={"reason": payload.reason, "short_issued_lines": len(overrides)},
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
    components: Sequence[AssemblyOrderComponent],
    *,
    port: AllocationPort,
    reason: str,
) -> int:
    """Hand every held reservation back before the movements are posted.

    The order matters and is not a style choice. ``reserved_quantity`` has to
    come down before ``on_hand_quantity`` does, or the movement engine would see
    a position whose reservation exceeds the stock it is about to reduce and
    would correctly refuse it. Both happen inside the completing transaction, so
    no other document can slip into the gap.

    The iteration order is fixed - by item, then line - across every caller, so
    two completions sharing a component cannot take the same balance locks in
    opposite orders.
    """
    released = 0
    for component in sorted(components, key=lambda row: (row.item_id, row.line_number)):
        if component.reservation_id is None:
            continue
        await port.release(
            session, tenant, reservation_id=component.reservation_id, reason=reason
        )
        component.reservation_id = None
        released += 1
    await session.flush()
    return released


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

        components = await component_repo.list_for_order(order.id)
        plan = plan_consumption(
            components,
            planned_quantity=planned,
            produced_quantity=produced,
            serials_by_line={line.line_number: line.serial_ids for line in payload.components},
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
            )
            order.qc_inspection_id = getattr(inspection, "id", None)
            inspection_created = order.qc_inspection_id is not None
            await session.flush()

        await _transition(
            session, tenant, order, target, reason=payload.reason, repo=repo
        )

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
                "consumed": {
                    str(entry.component.line_number): entry.quantity for entry in plan
                },
                "consumed_serials": {
                    str(entry.component.line_number): list(entry.serial_ids)
                    for entry in plan
                    if entry.serial_ids
                },
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
        consume_movement_count=len(movements) - (1 if assembled > ZERO else 0),
        output_quantity=assembled,
        output_held_for_qc=held_for_qc,
        qc_inspection_created=inspection_created,
    )


async def record_qc_result(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: QcResultRequest,
) -> AssemblyOrder:
    """Apply an inspection verdict to output this module put on QC hold.

    Accepted quantity is released back into the available pool; rejected
    quantity is moved to the damaged pool, where it stays on hand and countable
    but cannot be promised. The two must account for everything held: releasing
    part of a lot and leaving the rest in limbo is how stock becomes permanently
    unusable without anybody deciding that it should be.
    """
    repo = AssemblyOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Assembly order", order_id)
        ASSEMBLY_ORDER_LIFECYCLE.assert_transition(
            AssemblyOrderStatus(order.status), AssemblyOrderStatus.COMPLETED
        )

        held = to_quantity(Decimal(order.assembled_quantity))
        accepted = to_quantity(payload.accepted_quantity)
        rejected = to_quantity(payload.rejected_quantity)
        if accepted + rejected != held:
            raise BusinessRuleError(
                f"{order.code} is holding {held} unit(s) for inspection; the verdict accounts "
                f"for {accepted + rejected}.",
                details={
                    "held_quantity": str(held),
                    "accepted_quantity": str(accepted),
                    "rejected_quantity": str(rejected),
                },
            )

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

        # The order's own tally moves with the verdict: units that failed
        # inspection were built but are not good output, and the header has to
        # agree with the stock the ledger now describes.
        order.assembled_quantity = accepted
        order.rejected_quantity = to_quantity(Decimal(order.rejected_quantity) + rejected)
        order.qc_held_at = None
        if payload.qc_inspection_id is not None:
            order.qc_inspection_id = payload.qc_inspection_id
        repo.touch(order)
        await session.flush()

        await _transition(
            session,
            tenant,
            order,
            AssemblyOrderStatus.COMPLETED,
            reason=payload.reason,
            repo=repo,
            action=AuditAction.QC_DECISION,
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
    "ASSEMBLY_ORDER_LIFECYCLE",
    "AUDIT_ASSEMBLY_ORDER",
    "CANCELLABLE_STATUSES",
    "CLEARABLE_UPDATE_FIELDS",
    "MOVEMENT_REFERENCE_ASSEMBLY",
    "AllocationPort",
    "AllocationService",
    "ComponentConsumption",
    "QualityPort",
    "approve_order",
    "build_movements",
    "cancel_order",
    "close_order",
    "complete_assembly",
    "consumption_for",
    "create_order",
    "delete_draft_order",
    "get_order_detail",
    "is_whole_units",
    "issue_components",
    "list_components",
    "list_orders",
    "list_ready_for_packing",
    "mark_packed",
    "order_detail",
    "plan_consumption",
    "ready_for_packing",
    "record_qc_result",
    "release_order",
    "requirement_for",
    "resolve_quality_port",
    "start_assembly",
    "update_order",
]
