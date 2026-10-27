"""Durable requisition allocations behind physical purchase-order lines.

Callers lock requisition headers/lines before the vendor/warehouse duplicate
key and PO header. Allocation rows are mutated only while that header is locked
(or while creating an unpublished order in the same transaction).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.warehouse_scope import assert_warehouse_scope
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from src.core.types import Quantity, to_quantity
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseRequisitionLine,
    RequisitionLineStatus,
)
from src.modules.procurement.purchase_order_changes_repository import SourceAllocationRepository
from src.modules.procurement.repository import PurchaseOrderRepository, RequisitionLineRepository
from src.modules.procurement.schemas import PurchaseOrderLineIn, PurchaseOrderSourceAllocationIn
from src.modules.procurement.service_rules import requisition_line_status_for


async def lock_order_sources(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    incoming_ids: Sequence[uuid.UUID] = (),
    warehouse_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> PurchaseOrder:
    """Use PR headers/lines -> sorted duplicate keys -> refreshed PO everywhere.

    The initial read discovers the lock set. A concurrent edit which changes
    that set must be retried rather than taking new source locks after the PO.
    Receipt creation takes the same PO lock and cannot race an amendment.
    """
    from src.core.database import advisory_xact_lock
    from src.modules.procurement.service import _lock_requisition_sources

    repo = PurchaseOrderRepository(session, tenant)
    observed = await repo.get_or_404(order_id)
    assert_warehouse_scope(tenant, observed.warehouse_id, warehouse_id or observed.warehouse_id)
    version, vendor_id, old_warehouse_id = (
        observed.version,
        observed.vendor_id,
        observed.warehouse_id,
    )
    known_ids = await SourceAllocationRepository(session, tenant).source_ids_for_order(order_id)
    await _lock_requisition_sources(session, tenant, sorted(known_ids | set(incoming_ids)))
    for target_warehouse in sorted({old_warehouse_id, warehouse_id or old_warehouse_id}):
        await advisory_xact_lock(
            session, tenant.company_id, "po-duplicate", vendor_id, target_warehouse
        )
    order = await repo.lock_for_update(order_id)
    if order is None:
        raise NotFoundError("Purchase order", order_id)
    if order.version != version or (
        expected_version is not None and order.version != expected_version
    ):
        raise ConflictError()
    if (
        await SourceAllocationRepository(session, tenant).source_ids_for_order(order_id)
        != known_ids
    ):
        raise ConflictError()
    return order


def allocation_inputs(line: PurchaseOrderLineIn) -> list[PurchaseOrderSourceAllocationIn]:
    if line.source_allocations is not None:
        return line.source_allocations
    if line.requisition_line_id is None:
        return []
    return [
        PurchaseOrderSourceAllocationIn(
            requisition_line_id=line.requisition_line_id, quantity=line.quantity
        )
    ]


def source_ids(lines: Sequence[PurchaseOrderLineIn]) -> list[uuid.UUID]:
    return sorted(
        {allocation.requisition_line_id for line in lines for allocation in allocation_inputs(line)}
    )


def flattened_source_lines(lines: Sequence[PurchaseOrderLineIn]) -> list[PurchaseOrderLineIn]:
    """Reuse the existing approved-source and item checks for every allocation."""
    return [
        line.model_copy(
            update={
                "requisition_line_id": allocation.requisition_line_id,
                "quantity": allocation.quantity,
                "source_allocations": None,
            }
        )
        for line in lines
        for allocation in allocation_inputs(line)
    ]


def assert_source_units(
    payloads: Sequence[PurchaseOrderLineIn],
    source_lines: Mapping[uuid.UUID, PurchaseRequisitionLine],
    resolved_units: Mapping[uuid.UUID, uuid.UUID],
) -> None:
    """The allocation quantity is expressed in the requisition's unit.

    ASSUMPTION: a linked PO cannot silently convert units. Explicit conversion
    factors are not part of this document contract; incompatible units require
    a separate request in the same purchasing unit.
    """
    for payload in payloads:
        unit_id = payload.uom_id or resolved_units[payload.item_id]
        for allocation in allocation_inputs(payload):
            source = source_lines.get(allocation.requisition_line_id)
            if source is None:
                raise NotFoundError("Purchase requisition line", allocation.requisition_line_id)
            if source.uom_id != unit_id:
                raise ValidationError(
                    "A source allocation must use the requisition line's unit of measure.",
                    details={"line_number": payload.line_number},
                )


async def effective_allocations(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[PurchaseOrderLine]
) -> dict[uuid.UUID, dict[uuid.UUID, Decimal]]:
    rows = await SourceAllocationRepository(session, tenant).list_for_lines(
        [line.id for line in lines]
    )
    mapped = {row.purchase_order_line_id for row in rows}
    result: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {line.id: {} for line in lines}
    for row in rows:
        if row.is_active:
            result[row.purchase_order_line_id][row.requisition_line_id] = Decimal(row.quantity)
    for line in lines:
        if line.id not in mapped and line.requisition_line_id is not None:
            result[line.id][line.requisition_line_id] = Decimal(line.quantity)
    return result


async def _refresh_counters(
    session: AsyncSession, tenant: TenantContext, requisition_line_ids: Sequence[uuid.UUID]
) -> None:
    if not requisition_line_ids:
        return
    await session.flush()
    quantities = await SourceAllocationRepository(session, tenant).ordered_quantities(
        requisition_line_ids
    )
    # Each physical line can fit while their pooled source total exceeds the
    # counter's column. Validate the whole batch before mutating any source.
    precision, scale = Quantity.precision, Quantity.scale
    assert precision is not None and scale is not None
    bound = Decimal(10) ** (precision - scale)
    for quantity in quantities.values():
        if (
            not quantity.is_finite()
            or abs(quantity) >= bound
            or abs(to_quantity(quantity)) >= bound
        ):
            raise ValidationError(
                "The total ordered quantity is too large. Reduce the source allocations."
            )
    sources = await RequisitionLineRepository(session, tenant).lock_many(requisition_line_ids)
    for source in sources:
        RequisitionLineRepository(session, tenant).touch(source)
        source.ordered_quantity = to_quantity(quantities.get(source.id, Decimal("0")))
        # Cancellation is a decision about outstanding demand. Releasing a PO
        # changes its historical ordered quantity, never reopens that decision.
        if RequisitionLineStatus(source.status) is not RequisitionLineStatus.CANCELLED:
            source.status = requisition_line_status_for(
                quantity=source.quantity, ordered_quantity=source.ordered_quantity
            )
    await session.flush()


async def sync_allocations(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[PurchaseOrderLine],
    desired: Mapping[uuid.UUID, Mapping[uuid.UUID, Decimal]],
) -> None:
    repo = SourceAllocationRepository(session, tenant)
    rows = await repo.list_for_lines([line.id for line in lines])
    existing = {(row.purchase_order_line_id, row.requisition_line_id): row for row in rows}
    # Unchanged inactive history contributes no ordered quantity. Refreshing it
    # would acquire source locks not present in the active pre-lock set.
    touched = {row.requisition_line_id for row in rows if row.is_active}
    prior = await effective_allocations(session, tenant, lines)
    selected_ids = {source_id for selected in desired.values() for source_id in selected}
    sources = {
        source.id: source
        for source in await RequisitionLineRepository(session, tenant).lock_many(
            sorted(selected_ids)
        )
    }
    for line in lines:
        for source_id, quantity in desired.get(line.id, {}).items():
            source = sources.get(source_id)
            if source is None:
                raise NotFoundError("Purchase requisition line", source_id)
            if RequisitionLineStatus(
                source.status
            ) is RequisitionLineStatus.CANCELLED and quantity > prior.get(line.id, {}).get(
                source_id, Decimal("0")
            ):
                raise BusinessRuleError(
                    "A cancelled requisition line cannot receive new allocation."
                )
    for line in lines:
        selected = desired.get(line.id, {})
        if sum(selected.values(), Decimal("0")) > Decimal(line.quantity):
            raise ValidationError("Source allocations cannot exceed the physical PO line quantity.")
        for source_id, quantity in selected.items():
            if quantity <= 0:
                raise ValidationError("A source allocation quantity must be positive.")
            touched.add(source_id)
            key = (line.id, source_id)
            row = existing.get(key)
            if row is None:
                row = repo.add(
                    PurchaseOrderSourceAllocation(
                        purchase_order_line_id=line.id,
                        requisition_line_id=source_id,
                        quantity=to_quantity(quantity),
                    )
                )
                existing[key] = row
            row.quantity = to_quantity(quantity)
            row.is_active = True
            repo.touch(row)
        for (line_id, source_id), row in existing.items():
            if line_id == line.id and source_id not in selected and row.is_active:
                row.is_active = False
                repo.touch(row)
        # Compatibility only: a partial or pooled physical line has no honest
        # scalar source. The allocation table remains authoritative.
        line.requisition_line_id = (
            next(iter(selected))
            if len(selected) == 1 and next(iter(selected.values())) == Decimal(line.quantity)
            else None
        )
    await _refresh_counters(session, tenant, sorted(touched))


async def release_allocations(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[PurchaseOrderLine]
) -> None:
    # Materialise legacy scalar links first so released history cannot fall
    # back to an active scalar after cancellation or rejection.
    current = await effective_allocations(session, tenant, lines)
    await sync_allocations(session, tenant, lines, current)
    await sync_allocations(session, tenant, lines, {line.id: {} for line in lines})
