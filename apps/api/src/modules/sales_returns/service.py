from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.dispatch.models import Dispatch, DispatchLine, DispatchStatus, DispatchType
from src.modules.inventory.models import (
    InventoryTransaction,
    SerialStatus,
    StockSerial,
    StockState,
    TransactionType,
)
from src.modules.inventory.service import (
    MovementRequest,
    change_stock_state,
    post_movements,
    state_change_type,
)
from src.modules.products.models import Item
from src.modules.quality.models import InspectionResult, QcInspection, QcStage
from src.modules.quality.schemas import InspectionCreate
from src.modules.quality.service import create_inspection
from src.modules.sales_returns.crediting import accepted_return_quantity
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine, SalesReturnStatus
from src.modules.sales_returns.repository import SalesReturnRepository
from src.modules.sales_returns.schemas import (
    SalesReturnCreate,
    SalesReturnDetail,
    SalesReturnLineRead,
    SalesReturnSummary,
    SalesReturnUnits,
)
from src.modules.warehouses.models import Warehouse

SALES_RETURN_REFERENCE = "sales_return"


class InspectionVerdict(Protocol):
    """Quality's settled verdict as consumed by the return owner."""

    inspection_id: uuid.UUID
    inspection_code: str
    reference_id: uuid.UUID | None
    is_settled: bool
    accepted_quantity: Decimal
    refused_quantity: Decimal
    reason: str


SALES_RETURN_LIFECYCLE: StateMachine[SalesReturnStatus] = StateMachine(
    entity="Sales return",
    initial=SalesReturnStatus.DRAFT,
    transitions=(
        Transition(
            SalesReturnStatus.DRAFT,
            SalesReturnStatus.RECEIVED,
            permission=Permission.SALES_RETURN_CREATE,
            label="Receive",
        ),
        Transition(
            SalesReturnStatus.RECEIVED,
            SalesReturnStatus.CLOSED,
            permission=Permission.SALES_RETURN_CREATE,
            label="Close",
        ),
        Transition(
            SalesReturnStatus.DRAFT,
            SalesReturnStatus.CANCELLED,
            permission=Permission.SALES_RETURN_CREATE,
            label="Cancel",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({SalesReturnStatus.CLOSED, SalesReturnStatus.CANCELLED}),
)


def _detail(row: SalesReturn, lines: list[SalesReturnLine]) -> SalesReturnDetail:
    return SalesReturnDetail(
        **SalesReturnSummary.model_validate(row).model_dump(),
        lines=[SalesReturnLineRead.model_validate(line) for line in lines],
    )


async def list_returns(
    session: AsyncSession, tenant: TenantContext, params: PageParams
) -> tuple[list[SalesReturnSummary], object]:
    rows, page = await SalesReturnRepository(session, tenant).list_page_for_tenant(params)
    return [SalesReturnSummary.model_validate(row) for row in rows], page


async def get_return(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> SalesReturnDetail:
    repo = SalesReturnRepository(session, tenant)
    row = await repo.get_or_404(return_id)
    return _detail(row, await repo.lines(row.id))


async def create_return(
    session: AsyncSession, tenant: TenantContext, payload: SalesReturnCreate
) -> SalesReturnDetail:
    dispatch = (
        await session.execute(
            select(Dispatch)
            .where(
                Dispatch.company_id == tenant.company_id,
                Dispatch.id == payload.dispatch_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dispatch is None:
        raise NotFoundError("Dispatch", payload.dispatch_id)
    if dispatch.dispatch_type != DispatchType.SALES:
        raise BusinessRuleError("Only sales dispatches can be returned.")
    allowed = {
        DispatchStatus.DISPATCHED,
        DispatchStatus.IN_TRANSIT,
        DispatchStatus.DELIVERED,
        DispatchStatus.RECEIVED,
        DispatchStatus.QC_PENDING,
        DispatchStatus.QC_COMPLETED,
        DispatchStatus.GRN_COMPLETED,
    }
    if dispatch.status not in allowed:
        raise BusinessRuleError("A return is allowed only after the dispatch has left.")
    if payload.return_warehouse_id is not None:
        warehouse_exists = await session.scalar(
            select(Warehouse.id).where(
                Warehouse.company_id == tenant.company_id,
                Warehouse.id == payload.return_warehouse_id,
                Warehouse.is_active.is_(True),
            )
        )
        if warehouse_exists is None:
            raise NotFoundError("Warehouse", payload.return_warehouse_id)
    line_ids = [line.dispatch_line_id for line in payload.lines]
    dispatch_lines = list(
        (
            await session.execute(
                select(DispatchLine)
                .where(
                    DispatchLine.company_id == tenant.company_id,
                    DispatchLine.dispatch_id == dispatch.id,
                    DispatchLine.id.in_(line_ids),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {line.id: line for line in dispatch_lines}
    if len(by_id) != len(set(line_ids)):
        raise BusinessRuleError("Every return line must belong to the selected dispatch.")
    items = {
        item.id: item
        for item in (
            await session.execute(
                select(Item).where(
                    Item.company_id == tenant.company_id,
                    Item.id.in_([line.item_id for line in dispatch_lines]),
                )
            )
        ).scalars()
    }
    all_serial_ids = [serial_id for line in payload.lines for serial_id in line.serial_ids]
    if len(all_serial_ids) != len(set(all_serial_ids)):
        raise BusinessRuleError("A serial can appear only once on a sales return.")
    serials = (
        {
            serial.id: serial
            for serial in (
                await session.execute(
                    select(StockSerial)
                    .where(
                        StockSerial.company_id == tenant.company_id,
                        StockSerial.id.in_(all_serial_ids),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        }
        if all_serial_ids
        else {}
    )
    async with transaction(session):
        row = SalesReturn(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            code=await next_document_number(session, tenant.company_id, DocTypes.SALES_RETURN),
            dispatch_id=dispatch.id,
            customer_id=dispatch.customer_id,
            return_warehouse_id=payload.return_warehouse_id,
            source=payload.source,
            requested_by_user_id=tenant.user_id,
            notes=payload.notes,
            status=SalesReturnStatus.DRAFT,
        )
        session.add(row)
        await session.flush()
        for line_in in payload.lines:
            dispatch_line = by_id[line_in.dispatch_line_id]
            item = items[dispatch_line.item_id]
            if not item.can_return:
                raise BusinessRuleError(f"Item {item.code} cannot be returned.")
            available = dispatch_line.quantity - dispatch_line.returned_quantity
            if line_in.quantity > available:
                raise BusinessRuleError(
                    "Return quantity exceeds the remaining quantity for line "
                    f"{dispatch_line.line_number}."
                )
            serial_ids = list(dict.fromkeys(line_in.serial_ids))
            if len(serial_ids) != len(line_in.serial_ids):
                raise BusinessRuleError(
                    f"Return line {dispatch_line.line_number} contains duplicate serials."
                )
            if item.is_serial_tracked:
                if not line_in.requires_qc:
                    raise BusinessRuleError(
                        f"Serial-tracked item {item.code} must be received into QC hold."
                    )
                if len(serial_ids) != line_in.quantity:
                    raise BusinessRuleError(
                        f"Serial-tracked item {item.code} needs one serial per returned unit."
                    )
                shipped_serials = set(dispatch_line.serial_ids)
                if not set(serial_ids).issubset(shipped_serials):
                    raise BusinessRuleError(
                        f"Every returned serial for line {dispatch_line.line_number} must have "
                        "been shipped on that dispatch line."
                    )
                if any(
                    serial_id not in serials
                    or serials[serial_id].item_id != dispatch_line.item_id
                    or SerialStatus(serials[serial_id].status) is not SerialStatus.DISPATCHED
                    for serial_id in serial_ids
                ):
                    raise BusinessRuleError(
                        f"Every serial on line {dispatch_line.line_number} must belong to "
                        "the item and currently be dispatched."
                    )
                prior_lines = list(
                    (
                        await session.execute(
                            select(SalesReturnLine)
                            .join(
                                SalesReturn,
                                SalesReturn.id == SalesReturnLine.sales_return_id,
                            )
                            .where(
                                SalesReturnLine.company_id == tenant.company_id,
                                SalesReturnLine.dispatch_line_id == dispatch_line.id,
                                SalesReturnLine.sales_return_id != row.id,
                                SalesReturn.status != SalesReturnStatus.CANCELLED,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                already_returned = {
                    serial_id for prior in prior_lines for serial_id in prior.serial_ids
                }
                if already_returned.intersection(serial_ids):
                    raise BusinessRuleError(
                        f"A serial on line {dispatch_line.line_number} has already been returned."
                    )
            elif serial_ids:
                raise BusinessRuleError(f"Item {item.code} is not serial tracked.")
            session.add(
                SalesReturnLine(
                    company_id=tenant.company_id,
                    created_by_id=tenant.user_id,
                    sales_return_id=row.id,
                    dispatch_line_id=dispatch_line.id,
                    item_id=dispatch_line.item_id,
                    quantity=line_in.quantity,
                    serial_ids=serial_ids,
                    reason_code=line_in.reason_code,
                    note=line_in.note,
                    requires_qc=line_in.requires_qc,
                )
            )
        await session.flush()
    return await get_return(session, tenant, row.id)


async def transition_return(
    session: AsyncSession,
    tenant: TenantContext,
    return_id: uuid.UUID,
    target: SalesReturnStatus,
    *,
    reason: str | None = None,
) -> SalesReturnDetail:
    repo = SalesReturnRepository(session, tenant)
    row = await repo.get_or_404(return_id)
    SALES_RETURN_LIFECYCLE.assert_transition(row.status, target)
    if target == SalesReturnStatus.CANCELLED and not reason:
        raise BusinessRuleError("A cancellation reason is required.")
    async with transaction(session):
        locked_row = (
            await session.execute(
                select(SalesReturn)
                .where(
                    SalesReturn.company_id == tenant.company_id,
                    SalesReturn.id == return_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_row is None:
            raise NotFoundError("Sales return", return_id)
        row = locked_row
        SALES_RETURN_LIFECYCLE.assert_transition(row.status, target)
        lines = list(
            (
                await session.execute(
                    select(SalesReturnLine)
                    .where(
                        SalesReturnLine.company_id == tenant.company_id,
                        SalesReturnLine.sales_return_id == row.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if target == SalesReturnStatus.CLOSED:
            for line in lines:
                if line.serial_ids:
                    disposed = (
                        set(line.accepted_serial_ids)
                        | set(line.damaged_serial_ids)
                        | set(line.scrapped_serial_ids)
                    )
                    if not set(line.serial_ids).issubset(disposed):
                        raise BusinessRuleError(
                            "A return with QC-held serials cannot be closed until every unit "
                            "has a disposition."
                        )
            inspection_ids = [line.qc_inspection_id for line in lines if line.qc_inspection_id]
            if inspection_ids:
                settled_count = await session.scalar(
                    select(func.count(QcInspection.id)).where(
                        QcInspection.company_id == tenant.company_id,
                        QcInspection.id.in_(inspection_ids),
                        QcInspection.result != InspectionResult.PENDING,
                        QcInspection.rework_quantity == 0,
                        QcInspection.hold_quantity == 0,
                    )
                )
                if settled_count != len(inspection_ids):
                    raise BusinessRuleError(
                        "A return with pending QC inspections cannot be closed."
                    )
            await _issue_return_credit_note(session, tenant, row.id, lines)
        if target == SalesReturnStatus.RECEIVED:
            dispatch = (
                await session.execute(
                    select(Dispatch)
                    .where(
                        Dispatch.company_id == tenant.company_id,
                        Dispatch.id == row.dispatch_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            warehouse_id = row.return_warehouse_id or dispatch.warehouse_id
            dispatch_lines = list(
                (
                    await session.execute(
                        select(DispatchLine)
                        .where(
                            DispatchLine.company_id == tenant.company_id,
                            DispatchLine.id.in_([line.dispatch_line_id for line in lines]),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            dispatch_by_id = {line.id: line for line in dispatch_lines}
            return_serial_ids = [serial_id for line in lines for serial_id in line.serial_ids]
            serial_costs: dict[uuid.UUID, Decimal] = {}
            if return_serial_ids:
                dispatch_serial_rows = list(
                    (
                        await session.execute(
                            select(InventoryTransaction).where(
                                InventoryTransaction.company_id == tenant.company_id,
                                InventoryTransaction.reference_type == "dispatch",
                                InventoryTransaction.reference_id == dispatch.id,
                                InventoryTransaction.transaction_type == TransactionType.DISPATCH,
                                InventoryTransaction.serial_id.in_(return_serial_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                serial_costs = {
                    transaction.serial_id: Decimal(transaction.unit_cost)
                    for transaction in dispatch_serial_rows
                    if transaction.serial_id is not None
                }
                if set(serial_costs) != set(return_serial_ids):
                    raise BusinessRuleError(
                        "The original dispatch cost for every returned serial could not be "
                        "verified."
                    )
            movements: list[MovementRequest] = []
            serial_hold_movements: list[MovementRequest] = []
            for line in lines:
                source = dispatch_by_id[line.dispatch_line_id]
                if line.serial_ids:
                    line.requires_qc = True
                    movements.extend(
                        MovementRequest(
                            item_id=line.item_id,
                            warehouse_id=warehouse_id,
                            quantity=Decimal("1"),
                            transaction_type=TransactionType.CUSTOMER_RETURN,
                            reference_type=SALES_RETURN_REFERENCE,
                            reference_id=row.id,
                            reference_code=row.code,
                            reason="Customer sales return received",
                            batch_id=source.batch_id,
                            serial_id=serial_id,
                            unit_cost=serial_costs[serial_id],
                        )
                        for serial_id in line.serial_ids
                    )
                    serial_hold_movements.extend(
                        MovementRequest(
                            item_id=line.item_id,
                            warehouse_id=warehouse_id,
                            quantity=Decimal("1"),
                            transaction_type=TransactionType.QC_HOLD,
                            reference_type=SALES_RETURN_REFERENCE,
                            reference_id=row.id,
                            reference_code=row.code,
                            reason="Customer-return serial awaiting QC disposition",
                            batch_id=source.batch_id,
                            serial_id=serial_id,
                            stock_state=StockState.QC_HOLD,
                            from_state=StockState.AVAILABLE,
                        )
                        for serial_id in line.serial_ids
                    )
                else:
                    movements.append(
                        MovementRequest(
                            item_id=line.item_id,
                            warehouse_id=warehouse_id,
                            quantity=line.quantity,
                            transaction_type=TransactionType.CUSTOMER_RETURN,
                            reference_type=SALES_RETURN_REFERENCE,
                            reference_id=row.id,
                            reference_code=row.code,
                            reason="Customer sales return received",
                            batch_id=source.batch_id,
                        )
                    )
            await post_movements(session, tenant, movements)
            if serial_hold_movements:
                await post_movements(session, tenant, serial_hold_movements)
            for line in lines:
                source = dispatch_by_id[line.dispatch_line_id]
                if source.returned_quantity + line.quantity > source.quantity:
                    raise BusinessRuleError(
                        "Return quantity exceeds the remaining quantity for line "
                        f"{source.line_number}."
                    )
                source.returned_quantity += line.quantity
                if line.requires_qc and not line.serial_ids:
                    await change_stock_state(
                        session,
                        tenant,
                        item_id=line.item_id,
                        warehouse_id=warehouse_id,
                        quantity=line.quantity,
                        from_state=StockState.AVAILABLE,
                        to_state=StockState.QC_HOLD,
                        reason=f"{row.code}: customer return inspection",
                        batch_id=source.batch_id,
                        reference_type=SALES_RETURN_REFERENCE,
                        reference_id=row.id,
                        reference_code=row.code,
                    )
                    inspection = await create_inspection(
                        session,
                        tenant,
                        InspectionCreate(
                            item_id=line.item_id,
                            warehouse_id=warehouse_id,
                            qc_stage=QcStage.RETURN,
                            inspected_quantity=line.quantity,
                            batch_id=source.batch_id,
                            reference_type=SALES_RETURN_REFERENCE,
                            reference_id=row.id,
                            reference_code=row.code,
                            hold_stock=False,
                            reason=f"Inspection for {row.code}",
                            remarks=line.note,
                        ),
                    )
                    line.qc_inspection_id = inspection.id
        previous_status = row.status
        row.status = target
        row.updated_by_id = tenant.user_id
        if target == SalesReturnStatus.RECEIVED:
            row.received_at = utcnow()
        if target == SalesReturnStatus.CANCELLED:
            row.cancelled_at = utcnow()
            row.cancellation_reason = reason
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="sales_return",
            entity_id=row.id,
            entity_code=row.code,
            from_status=str(previous_status),
            to_status=str(target),
            user_id=tenant.user_id,
            reason=reason,
        )
        await session.flush()
        if target == SalesReturnStatus.RECEIVED:
            await _issue_return_credit_note(session, tenant, row.id, lines)
            await session.flush()
    return await get_return(session, tenant, row.id)


async def dispose_return_units(
    session: AsyncSession,
    tenant: TenantContext,
    return_id: uuid.UUID,
    payload: SalesReturnUnits,
) -> SalesReturnDetail:
    """Settle named serials on a received return and post each stock-state move."""
    async with transaction(session):
        row = (
            await session.execute(
                select(SalesReturn)
                .where(
                    SalesReturn.company_id == tenant.company_id,
                    SalesReturn.id == return_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Sales return", return_id)
        if SalesReturnStatus(row.status) is not SalesReturnStatus.RECEIVED:
            raise BusinessRuleError("Serial units can be dispositioned only on a received return.")

        lines = list(
            (
                await session.execute(
                    select(SalesReturnLine)
                    .where(
                        SalesReturnLine.company_id == tenant.company_id,
                        SalesReturnLine.sales_return_id == row.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        lines_by_serial: dict[uuid.UUID, SalesReturnLine] = {}
        disposed_serials: set[uuid.UUID] = set()
        for line in lines:
            for serial_id in line.serial_ids:
                lines_by_serial[serial_id] = line
            disposed_serials.update(line.accepted_serial_ids)
            disposed_serials.update(line.damaged_serial_ids)
            disposed_serials.update(line.scrapped_serial_ids)

        requested = {
            "accepted": set(payload.accepted),
            "damaged": set(payload.damaged),
            "scrapped": set(payload.scrapped),
        }
        requested_ids = set().union(*requested.values())
        if not requested_ids.issubset(lines_by_serial):
            raise BusinessRuleError("Every dispositioned serial must belong to this return.")
        if requested_ids.intersection(disposed_serials):
            raise BusinessRuleError("A serial unit on this return has already been dispositioned.")

        dispatch = (
            await session.execute(
                select(Dispatch)
                .where(
                    Dispatch.company_id == tenant.company_id,
                    Dispatch.id == row.dispatch_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        dispatch_line_ids = {line.dispatch_line_id for line in lines}
        dispatch_lines = list(
            (
                await session.execute(
                    select(DispatchLine)
                    .where(
                        DispatchLine.company_id == tenant.company_id,
                        DispatchLine.dispatch_id == dispatch.id,
                        DispatchLine.id.in_(dispatch_line_ids),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        dispatch_lines_by_id = {line.id: line for line in dispatch_lines}
        warehouse_id = row.return_warehouse_id or dispatch.warehouse_id
        target_by_disposition = {
            "accepted": StockState.AVAILABLE,
            "damaged": StockState.DAMAGED,
            "scrapped": StockState.SCRAP,
        }
        movements: list[MovementRequest] = []
        for disposition, serial_ids in requested.items():
            target_state = target_by_disposition[disposition]
            for serial_id in serial_ids:
                line = lines_by_serial[serial_id]
                source = dispatch_lines_by_id[line.dispatch_line_id]
                movements.append(
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=warehouse_id,
                        quantity=Decimal("1"),
                        transaction_type=state_change_type(StockState.QC_HOLD, target_state),
                        reference_type=SALES_RETURN_REFERENCE,
                        reference_id=row.id,
                        reference_code=row.code,
                        reason=f"Sales return serial disposition: {disposition}",
                        batch_id=source.batch_id,
                        serial_id=serial_id,
                        stock_state=target_state,
                        from_state=StockState.QC_HOLD,
                    )
                )

        await post_movements(session, tenant, movements)
        for line in lines:
            line.accepted_serial_ids = [
                *line.accepted_serial_ids,
                *(serial_id for serial_id in payload.accepted if serial_id in line.serial_ids),
            ]
            line.damaged_serial_ids = [
                *line.damaged_serial_ids,
                *(serial_id for serial_id in payload.damaged if serial_id in line.serial_ids),
            ]
            line.scrapped_serial_ids = [
                *line.scrapped_serial_ids,
                *(serial_id for serial_id in payload.scrapped if serial_id in line.serial_ids),
            ]

        await _issue_return_credit_note(session, tenant, row.id, lines)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="sales_return",
            entity_id=row.id,
            entity_code=row.code,
            summary=f"Dispositioned serial units on sales return {row.code}",
            user_id=tenant.user_id,
            context={
                disposition: [str(serial_id) for serial_id in serial_ids]
                for disposition, serial_ids in requested.items()
                if serial_ids
            },
        )
        await session.flush()

    return await get_return(session, tenant, return_id)


async def lock_for_quality_verdict(
    session: AsyncSession, tenant: TenantContext, reference_id: uuid.UUID
) -> None:
    """Lock the return before quality decides the stock it owns."""
    await session.execute(
        select(SalesReturn.id)
        .where(
            SalesReturn.company_id == tenant.company_id,
            SalesReturn.id == reference_id,
        )
        .with_for_update()
    )


async def record_quality_verdict(
    session: AsyncSession, tenant: TenantContext, verdict: InspectionVerdict
) -> bool:
    """Close a received return once every linked QC inspection is settled.

    Quality has already moved the stock. This callback only completes the
    document lifecycle, and is idempotent so reconciliation can safely repeat
    it for decisions made before the callback existed.
    """
    if verdict.reference_id is None or not verdict.is_settled:
        return False

    async with transaction(session):
        return_row = (
            await session.execute(
                select(SalesReturn)
                .where(
                    SalesReturn.company_id == tenant.company_id,
                    SalesReturn.id == verdict.reference_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if return_row is None or (
            SalesReturnStatus(return_row.status) is not SalesReturnStatus.RECEIVED
        ):
            return False

        lines = list(
            (
                await session.execute(
                    select(SalesReturnLine)
                    .where(
                        SalesReturnLine.company_id == tenant.company_id,
                        SalesReturnLine.sales_return_id == return_row.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        inspection_ids = [line.qc_inspection_id for line in lines if line.qc_inspection_id]
        if not inspection_ids or verdict.inspection_id not in inspection_ids:
            return False

        settled_count = await session.scalar(
            select(func.count(QcInspection.id)).where(
                QcInspection.company_id == tenant.company_id,
                QcInspection.id.in_(inspection_ids),
                QcInspection.result != InspectionResult.PENDING,
                QcInspection.rework_quantity == 0,
                QcInspection.hold_quantity == 0,
            )
        )
        if settled_count != len(inspection_ids):
            return False

        await _issue_return_credit_note(session, tenant, return_row.id, lines)
        previous_status = SalesReturnStatus(return_row.status)
        SALES_RETURN_LIFECYCLE.assert_transition(previous_status, SalesReturnStatus.CLOSED)
        return_row.status = SalesReturnStatus.CLOSED
        return_row.updated_by_id = tenant.user_id
        reason = f"Inspection {verdict.inspection_code}: {verdict.reason}"
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="sales_return",
            entity_id=return_row.id,
            entity_code=return_row.code,
            from_status=str(previous_status),
            to_status=str(SalesReturnStatus.CLOSED),
            user_id=tenant.user_id,
            reason=reason,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="sales_return",
            entity_id=return_row.id,
            entity_code=return_row.code,
            summary=f"Closed sales return {return_row.code} after QC settlement",
            user_id=tenant.user_id,
            context={
                "inspection_id": str(verdict.inspection_id),
                "accepted_quantity": str(verdict.accepted_quantity),
                "refused_quantity": str(verdict.refused_quantity),
                "reason": reason,
            },
        )
        await session.flush()
    return True


async def _issue_return_credit_note(
    session: AsyncSession,
    tenant: TenantContext,
    return_id: uuid.UUID,
    lines: list[SalesReturnLine],
) -> None:
    """Credit the accepted part of a settled return exactly once.

    Returns without an invoice have no receivable to reverse and remain
    operationally complete without creating a financial document.
    """
    from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
    from src.modules.finance.credit_note_service import issue_credit_note
    from src.modules.finance.models import (
        CreditNote,
        CreditNoteLine,
        CreditNoteReason,
        SalesInvoiceLine,
    )

    # QC and serial disposition update their rows before this owner callback.
    # Flush that accepted evidence before finance re-locks and validates it.
    await session.flush()
    dispatch_line_ids = [line.dispatch_line_id for line in lines]
    invoice_lines = list(
        (
            await session.execute(
                select(SalesInvoiceLine).where(
                    SalesInvoiceLine.company_id == tenant.company_id,
                    SalesInvoiceLine.dispatch_line_id.in_(dispatch_line_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_dispatch_line = {line.dispatch_line_id: line for line in invoice_lines}
    existing_lines = list(
        (
            await session.execute(
                select(CreditNoteLine)
                .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
                .where(
                    CreditNote.company_id == tenant.company_id,
                    CreditNote.sales_return_id == return_id,
                    CreditNote.status != "CANCELLED",
                )
            )
        )
        .scalars()
        .all()
    )
    credited_by_invoice_line: dict[uuid.UUID, Decimal] = {}
    for existing_line in existing_lines:
        credited_by_invoice_line[existing_line.sales_invoice_line_id] = (
            credited_by_invoice_line.get(existing_line.sales_invoice_line_id, ZERO)
            + Decimal(existing_line.quantity or ZERO)
        )
    inspection_ids = {line.qc_inspection_id for line in lines if line.qc_inspection_id is not None}
    inspections_by_id = {}
    if inspection_ids:
        inspections = (
            await session.execute(
                select(QcInspection).where(
                    QcInspection.company_id == tenant.company_id,
                    QcInspection.id.in_(inspection_ids),
                )
            )
        ).scalars()
        inspections_by_id = {inspection.id: inspection for inspection in inspections}
    credit_lines = []
    for return_line in lines:
        invoice_line = by_dispatch_line.get(return_line.dispatch_line_id)
        if invoice_line is None:
            continue
        eligible_quantity = accepted_return_quantity(
            return_line,
            inspections_by_id.get(return_line.qc_inspection_id)
            if return_line.qc_inspection_id is not None
            else None,
        )
        if eligible_quantity <= ZERO:
            continue
        eligible_quantity -= credited_by_invoice_line.get(invoice_line.id, ZERO)
        if eligible_quantity <= ZERO:
            continue
        credit_lines.append(
            CreditNoteLineIn(
                sales_invoice_line_id=invoice_line.id,
                quantity=to_quantity(eligible_quantity),
            )
        )
    if not credit_lines:
        return
    invoice_ids = {line.sales_invoice_id for line in invoice_lines}
    if len(invoice_ids) != 1:
        raise BusinessRuleError(
            "A return spanning multiple sales invoices cannot be credited automatically."
        )
    await issue_credit_note(
        session,
        tenant,
        CreditNoteCreate(
            sales_invoice_id=next(iter(invoice_ids)),
            sales_return_id=return_id,
            note_date=utcnow().date(),
            reason_type=CreditNoteReason.RETURN,
            lines=credit_lines,
        ),
    )


async def awaiting_quality_verdicts(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    after: uuid.UUID | None,
    limit: int,
) -> list[tuple[str, uuid.UUID]]:
    """Return received returns that still await a quality owner callback."""
    stmt = (
        select(SalesReturn.id)
        .join(SalesReturnLine, SalesReturnLine.sales_return_id == SalesReturn.id)
        .where(
            SalesReturn.company_id == tenant.company_id,
            SalesReturn.status == SalesReturnStatus.RECEIVED,
            SalesReturnLine.qc_inspection_id.is_not(None),
        )
        .distinct()
        .order_by(SalesReturn.id)
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(SalesReturn.id > after)
    ids = list((await session.execute(stmt)).scalars().all())
    return [(SALES_RETURN_REFERENCE, return_id) for return_id in ids]
