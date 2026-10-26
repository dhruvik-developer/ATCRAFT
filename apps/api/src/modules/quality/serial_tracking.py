"""Inspect individually identified units in batches without losing their dispositions."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.types import ZERO
from src.modules.inventory.models import InventoryTransaction, StockSerial, StockState
from src.modules.inventory.repository import BalanceGrain, StockSerialRepository
from src.modules.inventory.service import (
    MovementEngine,
    MovementRequest,
    post_movements,
    resolve_pools,
    state_change_type,
)
from src.modules.quality.models import InspectionStatus, QcInspection
from src.modules.quality.schemas import DecideRequest

_FINAL = frozenset({"ACCEPTED", "REJECTED", "SCRAP"})
_TARGET = {
    "ACCEPTED": StockState.AVAILABLE,
    "REJECTED": StockState.DAMAGED,
    "SCRAP": StockState.SCRAP,
}


async def _locked_units(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    batch_id: uuid.UUID | None,
    serial_ids: list[uuid.UUID],
) -> tuple[dict[uuid.UUID, StockSerial], dict[uuid.UUID, StockState | None]]:
    return await _locked_positions(
        session,
        tenant,
        expected=dict.fromkeys(serial_ids, (item_id, warehouse_id, batch_id)),
    )


async def lock_inspection_units(
    session: AsyncSession, tenant: TenantContext, inspections: Sequence[QcInspection]
) -> dict[uuid.UUID, StockState | None]:
    """Lock all inspection grains before any serial, independent of document order."""
    expected = {
        serial_id: (inspection.item_id, inspection.warehouse_id, inspection.batch_id)
        for inspection in inspections
        for serial_id in inspection.serial_ids
    }
    if not expected:
        return {}
    _, pools = await _locked_positions(session, tenant, expected=expected)
    return pools


async def _locked_positions(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    expected: Mapping[uuid.UUID, tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]],
) -> tuple[dict[uuid.UUID, StockSerial], dict[uuid.UUID, StockState | None]]:
    """Take balance -> serial locks in the inventory engine's fixed order."""
    serial_ids = list(expected)
    initial = list(
        (
            await session.scalars(
                select(StockSerial).where(
                    StockSerial.company_id == tenant.company_id, StockSerial.id.in_(serial_ids)
                )
            )
        ).all()
    )
    if len(initial) != len(serial_ids):
        raise NotFoundError("Stock serial")
    positions = {row.id: (row.warehouse_id, row.location_id, row.batch_id) for row in initial}
    for row in initial:
        if (row.item_id, row.warehouse_id, row.batch_id) != expected[row.id]:
            raise BusinessRuleError(
                "Every inspected serial must match the inspected item, warehouse and batch."
            )
    engine = MovementEngine(session, tenant)
    grains = list(
        dict.fromkeys(
            BalanceGrain(
                item_id=expected[row.id][0],
                warehouse_id=expected[row.id][1],
                location_id=row.location_id,
                batch_id=expected[row.id][2],
            )
            for row in initial
        )
    )
    await engine.validate_positions(grains)
    await engine.lock(grains)
    units = await StockSerialRepository(session, tenant).lock_serials(serial_ids)
    if any(
        positions[row.id] != (row.warehouse_id, row.location_id, row.batch_id)
        for row in units.values()
    ):
        raise BusinessRuleError(
            "An inspected serial moved while preparing the inspection. Retry the operation."
        )
    rows = await session.scalars(
        select(InventoryTransaction)
        .where(
            InventoryTransaction.company_id == tenant.company_id,
            InventoryTransaction.serial_id.in_(serial_ids),
        )
        .distinct(InventoryTransaction.serial_id)
        .order_by(
            InventoryTransaction.serial_id,
            InventoryTransaction.created_at.desc(),
            InventoryTransaction.id.desc(),
        )
    )
    pools = {
        row.serial_id: resolve_pools(row.transaction_type, row.stock_state, row.source_state)[1]
        for row in rows
        if row.serial_id is not None
    }
    return units, pools


async def prepare_serial_inspection(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    batch_id: uuid.UUID | None,
    quantity: Decimal,
    serial_ids: list[uuid.UUID],
    hold_stock: bool,
    reason: str,
    inspection_id: uuid.UUID,
    inspection_code: str,
) -> None:
    if (
        not serial_ids
        or len(serial_ids) != len(set(serial_ids))
        or Decimal(len(serial_ids)) != quantity
    ):
        raise BusinessRuleError("Select one distinct serial unit for every inspected unit.")
    units, pools = await _locked_units(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        batch_id=batch_id,
        serial_ids=serial_ids,
    )
    open_inspections = await session.scalars(
        select(QcInspection).where(
            QcInspection.company_id == tenant.company_id,
            QcInspection.serial_ids.overlap(serial_ids),
            QcInspection.status.in_(
                [
                    InspectionStatus.PENDING,
                    InspectionStatus.IN_PROGRESS,
                    InspectionStatus.HOLD,
                    InspectionStatus.REWORK,
                ]
            ),
        )
    )
    wanted = set(serial_ids)
    for inspection in open_inspections:
        if any(
            inspection.serial_dispositions.get(str(serial_id)) not in _FINAL
            for serial_id in wanted.intersection(inspection.serial_ids)
        ):
            raise BusinessRuleError("An inspected serial is already awaiting another QC decision.")
    movements = []
    for serial_id, unit in units.items():
        source = pools.get(serial_id)
        if source not in (StockState.AVAILABLE, StockState.QC_HOLD):
            raise BusinessRuleError(
                "Only serial units in available stock or QC hold can be inspected."
            )
        if hold_stock:
            if source is not StockState.AVAILABLE:
                raise BusinessRuleError(
                    "A serial already held for inspection must not be held twice."
                )
            movements.append(
                MovementRequest(
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    location_id=unit.location_id,
                    batch_id=batch_id,
                    serial_id=serial_id,
                    quantity=Decimal("1"),
                    transaction_type=state_change_type(source, StockState.QC_HOLD),
                    stock_state=StockState.QC_HOLD,
                    from_state=source,
                    reason=reason,
                    reference_type="qc_inspection",
                    reference_id=inspection_id,
                    reference_code=inspection_code,
                )
            )
    if movements:
        await post_movements(session, tenant, movements)


def validate_serial_decision(inspection: QcInspection, payload: DecideRequest) -> dict[str, str]:
    proposed: dict[str, str] = {
        str(serial_id): value for serial_id, value in payload.serial_dispositions.items()
    }
    if not inspection.serial_ids:
        if proposed:
            raise BusinessRuleError("This inspection does not cover serial units.")
        return {}
    if set(proposed) != {str(serial_id) for serial_id in inspection.serial_ids}:
        raise BusinessRuleError("State a disposition for every serial unit on the inspection.")
    counts = Counter(proposed.values())
    for value, quantity in (
        ("ACCEPTED", payload.accepted_quantity),
        ("REJECTED", payload.rejected_quantity),
        ("REWORK", payload.rework_quantity),
        ("HOLD", payload.hold_quantity),
        ("SCRAP", payload.scrap_quantity),
    ):
        if Decimal(counts[value]) != quantity:
            raise BusinessRuleError("QC quantities must match the named serial dispositions.")
    previous = inspection.serial_dispositions or {}
    if any(
        value in _FINAL and proposed[serial_id] != value for serial_id, value in previous.items()
    ):
        raise BusinessRuleError(
            "A serial unit that has left QC hold cannot be dispositioned again."
        )
    return proposed


async def post_serial_decision(
    session: AsyncSession,
    tenant: TenantContext,
    inspection: QcInspection,
    proposed: dict[str, str],
    *,
    reason: str,
    occurred_at: datetime,
) -> dict[StockState, Decimal]:
    previous = inspection.serial_dispositions or {}
    changed = [
        uuid.UUID(serial_id)
        for serial_id, value in proposed.items()
        if value in _FINAL and previous.get(serial_id) not in _FINAL
    ]
    moved = dict.fromkeys((StockState.AVAILABLE, StockState.DAMAGED, StockState.SCRAP), ZERO)
    if not changed:
        return moved
    units, pools = await _locked_units(
        session,
        tenant,
        item_id=inspection.item_id,
        warehouse_id=inspection.warehouse_id,
        batch_id=inspection.batch_id,
        serial_ids=changed,
    )
    movements = []
    for serial_id in changed:
        target = _TARGET[proposed[str(serial_id)]]
        source = pools.get(serial_id)
        if source not in (StockState.QC_HOLD, StockState.AVAILABLE):
            raise BusinessRuleError(
                "An inspected serial is no longer in available stock or QC hold."
            )
        if source is target:
            continue
        unit = units[serial_id]
        movements.append(
            MovementRequest(
                item_id=inspection.item_id,
                warehouse_id=inspection.warehouse_id,
                location_id=unit.location_id,
                batch_id=inspection.batch_id,
                serial_id=serial_id,
                quantity=Decimal("1"),
                transaction_type=state_change_type(source, target),
                stock_state=target,
                from_state=source,
                reason=reason,
                occurred_at=occurred_at,
                reference_type="qc_inspection",
                reference_id=inspection.id,
                reference_code=inspection.code,
            )
        )
        moved[target] += Decimal("1")
    if movements:
        await post_movements(session, tenant, movements)
    return moved
