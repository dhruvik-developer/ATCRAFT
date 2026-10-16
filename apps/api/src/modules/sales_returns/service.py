from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.service import record_status_change
from src.modules.dispatch.models import Dispatch, DispatchLine, DispatchStatus, DispatchType
from src.modules.inventory.models import TransactionType
from src.modules.inventory.service import MovementRequest, post_movements
from src.modules.products.models import Item
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine, SalesReturnStatus
from src.modules.sales_returns.repository import SalesReturnRepository
from src.modules.sales_returns.schemas import (
    SalesReturnCreate,
    SalesReturnDetail,
    SalesReturnLineRead,
    SalesReturnSummary,
)
from src.modules.warehouses.models import Warehouse

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
            session.add(
                SalesReturnLine(
                    company_id=tenant.company_id,
                    created_by_id=tenant.user_id,
                    sales_return_id=row.id,
                    dispatch_line_id=dispatch_line.id,
                    item_id=dispatch_line.item_id,
                    quantity=line_in.quantity,
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
            movements = [
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=warehouse_id,
                    quantity=line.quantity,
                    transaction_type=TransactionType.CUSTOMER_RETURN,
                    reference_type="SALES_RETURN",
                    reference_id=row.id,
                    reference_code=row.code,
                    reason="Customer sales return received",
                    batch_id=dispatch_by_id[line.dispatch_line_id].batch_id,
                )
                for line in lines
            ]
            await post_movements(session, tenant, movements)
            for line in lines:
                source = dispatch_by_id[line.dispatch_line_id]
                if source.returned_quantity + line.quantity > source.quantity:
                    raise BusinessRuleError(
                        "Return quantity exceeds the remaining quantity for line "
                        f"{source.line_number}."
                    )
                source.returned_quantity += line.quantity
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
    return await get_return(session, tenant, row.id)
