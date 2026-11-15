"""Sales returns: goods a customer sends back against a SALES dispatch.

A return is raised as DRAFT against a dispatch that has left, received
(DRAFT -> RECEIVED), then closed (RECEIVED -> CLOSED), or cancelled while still
a draft. Receiving takes the goods back at the cost they left at. An ordinary
line that needs QC goes on hold with a RETURN inspection that quality owns
(``quality.owners``): quality moves the stock and calls
:func:`record_quality_verdict`, which closes the return once every inspection it
raised is settled. Serial units are held without an inspection and settled one
by one on the return itself (:func:`dispose_return_units`). Whenever accepted
goods are settled, :func:`_issue_return_credit_note` credits what was accepted
and not yet credited, against the live invoice issued before the goods came
back - exactly once per quantity.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.common.repository import labels_by_id, labels_in_order
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.messages import not_tracked
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.customers.models import Customer
from src.modules.dispatch.models import Dispatch, DispatchStatus, DispatchType
from src.modules.inventory.models import (
    SerialStatus,
    StockSerial,
    StockState,
    TransactionType,
)
from src.modules.inventory.repository import StockSerialRepository
from src.modules.inventory.service import (
    MovementRequest,
    post_movements,
    state_change_request,
    state_change_type,
)
from src.modules.products.models import Item
from src.modules.quality.models import QcStage
from src.modules.quality.repository import QcInspectionRepository
from src.modules.quality.schemas import InspectionCreate
from src.modules.quality.service import create_inspection
from src.modules.sales_returns.costing import return_cost_plans
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


async def describe_returns(
    session: AsyncSession, tenant: TenantContext, rows: Sequence[SalesReturn]
) -> list[SalesReturnSummary]:
    """Project returns, each naming its dispatch and its customer.

    One statement per referenced table for the whole page, never one per row.
    """
    dispatches = await labels_by_id(
        session, tenant, (row.dispatch_id for row in rows), Dispatch.code
    )
    customers = await labels_by_id(
        session, tenant, (row.customer_id for row in rows), Customer.code, Customer.name
    )
    summaries: list[SalesReturnSummary] = []
    for row in rows:
        summary = SalesReturnSummary.model_validate(row)
        dispatch = dispatches.get(row.dispatch_id)
        summary.dispatch_code = dispatch.code if dispatch else None
        customer = customers.get(row.customer_id)
        summary.customer_code = customer.code if customer else None
        summary.customer_name = customer.name if customer else None
        summaries.append(summary)
    return summaries


async def _detail(
    session: AsyncSession,
    tenant: TenantContext,
    row: SalesReturn,
    lines: Sequence[SalesReturnLine],
) -> SalesReturnDetail:
    """The return with every line naming its item and its returned units."""
    (summary,) = await describe_returns(session, tenant, [row])
    items = await labels_by_id(
        session, tenant, (line.item_id for line in lines), Item.code, Item.name
    )
    serials = await labels_by_id(
        session,
        tenant,
        (serial_id for line in lines for serial_id in line.serial_ids),
        StockSerial.serial_number,
    )
    reads: list[SalesReturnLineRead] = []
    for line in lines:
        read = SalesReturnLineRead.model_validate(line)
        item = items.get(line.item_id)
        read.item_code = item.code if item else None
        read.item_name = item.name if item else None
        read.serial_numbers = labels_in_order(line.serial_ids, serials, "serial_number")
        reads.append(read)
    return SalesReturnDetail(**summary.model_dump(), lines=reads)


async def _lock_source_dispatch(repo: SalesReturnRepository, dispatch_id: uuid.UUID) -> Dispatch:
    """Row-lock the dispatch a return comes back from, or answer 404 Dispatch."""
    dispatch = await repo.lock_dispatch(dispatch_id)
    if dispatch is None:
        raise NotFoundError("Dispatch", dispatch_id)
    return dispatch


async def list_returns(
    session: AsyncSession, tenant: TenantContext, params: PageParams
) -> tuple[list[SalesReturnSummary], object]:
    rows, page = await SalesReturnRepository(session, tenant).list_page_for_tenant(params)
    return await describe_returns(session, tenant, rows), page


async def get_return(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> SalesReturnDetail:
    repo = SalesReturnRepository(session, tenant)
    row = await repo.get_or_404(return_id)
    return await _detail(session, tenant, row, await repo.lines(row.id))


async def create_return(
    session: AsyncSession, tenant: TenantContext, payload: SalesReturnCreate
) -> SalesReturnDetail:
    """Raise a DRAFT return of goods from one SALES dispatch that has left.

    The dispatch and its named lines are locked, so two returns cannot claim the
    same returnable quantity. A return warehouse, when named, must be an active
    one of this company. Each dispatch line may appear once, must belong to the
    dispatch and carry a returnable item, and may claim no more than it shipped
    less what open (not cancelled) returns already claim. A serial-tracked line
    must go to QC hold and name one unit per quantity, each shipped on that
    line, of that item, still DISPATCHED and claimed by no open return; any
    other line names no serials. Audited.
    """
    returns = SalesReturnRepository(session, tenant)
    dispatch = await _lock_source_dispatch(returns, payload.dispatch_id)
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
    if payload.return_warehouse_id is not None and not await returns.active_warehouse_exists(
        payload.return_warehouse_id
    ):
        raise NotFoundError("Warehouse", payload.return_warehouse_id)
    line_ids = [line.dispatch_line_id for line in payload.lines]
    if len(set(line_ids)) != len(line_ids):
        # The table allows one line per dispatch line in a return
        # (uq_sales_return_lines_dispatch); refused here in words rather than
        # by the constraint as a bare 409 once every line has been checked.
        raise BusinessRuleError(
            "Each dispatch line can appear only once on a sales return; enter its whole "
            "quantity on one line."
        )
    dispatch_lines = await returns.lock_dispatch_lines(dispatch.id, line_ids)
    by_id = {line.id: line for line in dispatch_lines}
    if len(by_id) != len(set(line_ids)):
        raise BusinessRuleError("Every return line must belong to the selected dispatch.")
    items = await returns.items(line.item_id for line in dispatch_lines)
    all_serial_ids = [serial_id for line in payload.lines for serial_id in line.serial_ids]
    if len(all_serial_ids) != len(set(all_serial_ids)):
        raise BusinessRuleError("A serial can appear only once on a sales return.")
    serials = await StockSerialRepository(session, tenant).lock_serials(all_serial_ids)
    # What earlier open returns already claim, for every named dispatch line in
    # two statements rather than two per line. Read under the dispatch-line
    # locks above, so a concurrent request for the same lines waits for this
    # one rather than reading the same pool.
    claimed = await returns.claimed_quantities(list(by_id))
    serial_line_ids = [
        line.id
        for line in dispatch_lines
        if line.item_id in items and items[line.item_id].is_serial_tracked
    ]
    claimed_serials = await returns.claimed_serials(serial_line_ids)
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
            # Open DRAFT/RECEIVED requests consume the returnable pool too.
            # ``returned_quantity`` only grows when a return is received, so a
            # second DRAFT could otherwise over-claim the dispatched quantity.
            available = dispatch_line.quantity - claimed.get(dispatch_line.id, ZERO)
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
                already_returned = claimed_serials.get(dispatch_line.id, set())
                if already_returned.intersection(serial_ids):
                    raise BusinessRuleError(
                        f"A serial on line {dispatch_line.line_number} has already been returned."
                    )
            elif serial_ids:
                raise BusinessRuleError(not_tracked(f"Item {item.code}", "serial", "serials"))
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
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="sales_return",
            entity_id=row.id,
            entity_code=row.code,
            summary=f"Created sales return {row.code}",
            user_id=tenant.user_id,
            new_values={
                "dispatch_id": str(dispatch.id),
                "source": str(payload.source),
                "line_count": len(payload.lines),
            },
        )
    return await get_return(session, tenant, row.id)


async def transition_return(
    session: AsyncSession,
    tenant: TenantContext,
    return_id: uuid.UUID,
    target: SalesReturnStatus,
    *,
    reason: str | None = None,
) -> SalesReturnDetail:
    """Move a return through its lifecycle: receive, close or cancel it.

    The header and lines are locked and the transition re-checked under the
    lock. Cancelling a draft needs a reason. Receiving posts the goods back
    into the return warehouse (or the dispatch's) at the cost they left at;
    serial units come back one by one straight onto QC hold, and the ordinary
    lines that need QC go on hold in one posting with a RETURN inspection each.
    Each dispatch line's returned quantity grows, never past what it shipped;
    the dealer is told, and anything already accepted is credited. Closing
    needs every serial unit dispositioned and every inspection settled, and
    credits what was accepted. Every change is audited as a status change.
    """
    repo = SalesReturnRepository(session, tenant)
    row = await repo.get_or_404(return_id)
    SALES_RETURN_LIFECYCLE.assert_transition(row.status, target)
    if target == SalesReturnStatus.CANCELLED and not reason:
        raise BusinessRuleError("A cancellation reason is required.")
    async with transaction(session):
        locked_row = await repo.lock_for_update(return_id)
        if locked_row is None:
            raise NotFoundError("Sales return", return_id)
        row = locked_row
        SALES_RETURN_LIFECYCLE.assert_transition(row.status, target)
        lines = await repo.lock_lines(row.id)
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
            if inspection_ids and not await QcInspectionRepository(session, tenant).all_settled(
                inspection_ids
            ):
                raise BusinessRuleError("A return with pending QC inspections cannot be closed.")
            await _issue_return_credit_note(session, tenant, row.id, lines)
        if target == SalesReturnStatus.RECEIVED:
            dispatch = await _lock_source_dispatch(repo, row.dispatch_id)
            warehouse_id = row.return_warehouse_id or dispatch.warehouse_id
            dispatch_lines = await repo.lock_dispatch_lines(
                dispatch.id, [line.dispatch_line_id for line in lines]
            )
            dispatch_by_id = {line.id: line for line in dispatch_lines}
            ordinary_costs, serial_costs = await return_cost_plans(
                session, tenant, dispatch.id, lines
            )
            movements: list[MovementRequest] = []
            ordinary_receipt_owners: dict[int, SalesReturnLine] = {}
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
                            cost_layers=serial_costs[serial_id],
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
                    ordinary_receipt_owners[len(movements)] = line
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
                            cost_layers=ordinary_costs[line.id],
                        )
                    )
            receipt_movements = await post_movements(session, tenant, movements)
            for position, ordinary_line in ordinary_receipt_owners.items():
                ordinary_line.receipt_transaction_id = receipt_movements[position].id
            if serial_hold_movements:
                await post_movements(session, tenant, serial_hold_movements)
            inspected = [line for line in lines if line.requires_qc and not line.serial_ids]
            for line in lines:
                source = dispatch_by_id[line.dispatch_line_id]
                if source.returned_quantity + line.quantity > source.quantity:
                    raise BusinessRuleError(
                        "Return quantity exceeds the remaining quantity for line "
                        f"{source.line_number}."
                    )
                source.returned_quantity += line.quantity
            # Every inspected line goes on hold in one posting - the balances
            # locked once, in key order - rather than one full posting a line.
            if inspected:
                await post_movements(
                    session,
                    tenant,
                    [
                        state_change_request(
                            item_id=line.item_id,
                            warehouse_id=warehouse_id,
                            quantity=line.quantity,
                            from_state=StockState.AVAILABLE,
                            to_state=StockState.QC_HOLD,
                            reason=f"{row.code}: customer return inspection",
                            batch_id=dispatch_by_id[line.dispatch_line_id].batch_id,
                            reference_type=SALES_RETURN_REFERENCE,
                            reference_id=row.id,
                            reference_code=row.code,
                        )
                        for line in inspected
                    ],
                )
            for line in inspected:
                inspection = await create_inspection(
                    session,
                    tenant,
                    InspectionCreate(
                        item_id=line.item_id,
                        warehouse_id=warehouse_id,
                        qc_stage=QcStage.RETURN,
                        inspected_quantity=line.quantity,
                        batch_id=dispatch_by_id[line.dispatch_line_id].batch_id,
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
            from src.core.permissions import Permission
            from src.modules.notifications.models import NotificationType
            from src.modules.notifications.service import notify_customer

            await notify_customer(
                session,
                company_id=tenant.company_id,
                customer_id=row.customer_id,
                permission=Permission.DEALER_PORTAL_RETURN,
                notification_type=NotificationType.DEALER_RETURN_RECEIVED,
                title=f"Return received: {row.code}",
                body=f"Your return {row.code} has been received and is awaiting quality review.",
                action_url="/dealer/returns",
                entity_type="sales_return",
                entity_id=row.id,
                actor_id=tenant.user_id,
            )
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
    repo = SalesReturnRepository(session, tenant)
    async with transaction(session):
        row = await repo.lock_for_update(return_id)
        if row is None:
            raise NotFoundError("Sales return", return_id)
        if SalesReturnStatus(row.status) is not SalesReturnStatus.RECEIVED:
            raise BusinessRuleError("Serial units can be dispositioned only on a received return.")

        lines = await repo.lock_lines(row.id)
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

        dispatch = await _lock_source_dispatch(repo, row.dispatch_id)
        dispatch_lines = await repo.lock_dispatch_lines(
            dispatch.id, (line.dispatch_line_id for line in lines)
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
    await SalesReturnRepository(session, tenant).lock_for_update(reference_id)


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

    repo = SalesReturnRepository(session, tenant)
    async with transaction(session):
        return_row = await repo.lock_for_update(verdict.reference_id)
        if return_row is None or (
            SalesReturnStatus(return_row.status) is not SalesReturnStatus.RECEIVED
        ):
            return False

        lines = await repo.lock_lines(return_row.id)
        inspection_ids = [line.qc_inspection_id for line in lines if line.qc_inspection_id]
        if not inspection_ids or verdict.inspection_id not in inspection_ids:
            return False
        if not await QcInspectionRepository(session, tenant).all_settled(inspection_ids):
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
    from src.modules.finance.credit_note_repository import CreditNoteRepository
    from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
    from src.modules.finance.credit_note_service import issue_credit_note
    from src.modules.finance.models import CreditNoteReason

    # QC and serial disposition update their rows before this owner callback.
    # Flush that accepted evidence before finance re-locks and validates it.
    await session.flush()
    credit_notes = CreditNoteRepository(session, tenant)
    invoice_lines = await credit_notes.creditable_invoice_lines(
        return_id, [line.dispatch_line_id for line in lines]
    )
    by_dispatch_line = {line.dispatch_line_id: line for line in invoice_lines}
    credited_by_invoice_line = await credit_notes.return_credited_by_invoice_line(return_id)
    inspections_by_id = {
        inspection.id: inspection
        for inspection in await QcInspectionRepository(session, tenant).get_many(
            [line.qc_inspection_id for line in lines if line.qc_inspection_id is not None]
        )
    }
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
    ids = await SalesReturnRepository(session, tenant).awaiting_quality_verdict_ids(
        after=after, limit=limit
    )
    return [(SALES_RETURN_REFERENCE, return_id) for return_id in ids]
