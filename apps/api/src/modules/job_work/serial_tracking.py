"""Named-unit validation and movement shapes for vendor processing."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.repository import StockSerialRepository
from src.modules.inventory.service import MovementRequest, TraceabilityService
from src.modules.job_work.models import JobWorkOrder, JobWorkReceiptLine, RejectionAction

if TYPE_CHECKING:
    from src.modules.job_work.service import PlannedLine


async def validate_receipt_units(
    session: AsyncSession, tenant: TenantContext, planned: Sequence[PlannedLine]
) -> None:
    serial_lines = [row for row in planned if row.order_line.is_serial_tracked]
    previous = (
        (
            await session.scalars(
                select(JobWorkReceiptLine).where(
                    JobWorkReceiptLine.company_id == tenant.company_id,
                    JobWorkReceiptLine.job_work_order_line_id.in_(
                        [row.order_line.id for row in serial_lines]
                    ),
                )
            )
        ).all()
        if serial_lines
        else []
    )
    settled: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in previous:
        identities = set(
            row.output_serial_ids + row.returned_unprocessed_serial_ids + row.scrapped_serial_ids
        )
        if row.rejection_action != RejectionAction.REWORK_AT_VENDOR:
            identities.update(row.rejected_serial_ids)
        settled.setdefault(row.job_work_order_line_id, set()).update(identities)
    for planned_line in planned:
        line, entry, plan = planned_line.order_line, planned_line.entry, planned_line.plan
        identities = set(
            entry.output_serial_ids
            + entry.rejected_serial_ids
            + entry.returned_unprocessed_serial_ids
            + entry.scrapped_serial_ids
        )
        if not line.is_serial_tracked:
            if identities:
                raise BusinessRuleError(
                    "Remove serial dispositions from a non-serial job-work line."
                )
            continue
        if not identities.issubset(set(line.serial_ids)):
            raise BusinessRuleError(
                "Every dispositioned serial must have been issued on this job-work line."
            )
        if identities & settled.get(line.id, set()):
            raise BusinessRuleError(
                "A serial unit on this job-work line has already been received or scrapped."
            )
        for serial_ids, quantity in (
            (entry.output_serial_ids, plan.output_quantity),
            (entry.rejected_serial_ids, entry.rejected_quantity),
            (entry.returned_unprocessed_serial_ids, plan.returned_unprocessed_quantity),
            (entry.scrapped_serial_ids, plan.scrap_quantity),
        ):
            if Decimal(len(serial_ids)) != quantity:
                raise BusinessRuleError(
                    "Name every serial unit in each job-work receipt disposition."
                )


def receipt_movements(
    row: PlannedLine,
    *,
    order: JobWorkOrder,
    reason: str,
    occurred_at: datetime | None,
    capitalised_cost: Decimal | None,
    rejected_cost: Decimal | None,
    converted_serial_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> tuple[list[MovementRequest], int | None]:
    """Consume each named vendor input before receiving its processed identity.

    Separate ledger legs retain processing-cost capitalization and allow the
    processed unit to return to a different warehouse without losing its trace.
    """
    line, entry = row.order_line, row.entry
    movements: list[MovementRequest] = []
    common: dict[str, Any] = {
        "reference_type": "job_work_order",
        "reference_id": order.id,
        "reference_code": order.code,
        "reason": reason,
        "batch_id": row.batch_id,
        "occurred_at": occurred_at,
    }
    for serial_ids, state, cost in (
        (
            entry.output_serial_ids,
            StockState.QC_HOLD if order.requires_qc else StockState.AVAILABLE,
            capitalised_cost,
        ),
        (
            entry.rejected_serial_ids
            if entry.rejection_action is RejectionAction.RETURN_TO_STOCK
            else [],
            StockState.DAMAGED,
            rejected_cost,
        ),
    ):
        for serial_id in serial_ids:
            movements.extend(
                [
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=order.warehouse_id,
                        quantity=Decimal("1"),
                        transaction_type=TransactionType.MATERIAL_ISSUE,
                        stock_state=StockState.JOB_WORK,
                        serial_id=serial_id,
                        **common,
                    ),
                    MovementRequest(
                        item_id=line.expected_output_item_id,
                        warehouse_id=order.return_warehouse_id,
                        quantity=Decimal("1"),
                        transaction_type=TransactionType.JOB_WORK_RECEIPT,
                        stock_state=state,
                        unit_cost=cost,
                        serial_id=(converted_serial_ids or {}).get(serial_id, serial_id),
                        **common,
                    ),
                ]
            )
    if entry.rejection_action is RejectionAction.SCRAP_AT_VENDOR:
        for serial_id in entry.rejected_serial_ids:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=order.warehouse_id,
                    quantity=Decimal("1"),
                    transaction_type=TransactionType.SCRAP,
                    from_state=StockState.JOB_WORK,
                    stock_state=StockState.SCRAP,
                    serial_id=serial_id,
                    **common,
                )
            )
    for serial_id in entry.returned_unprocessed_serial_ids:
        movements.append(
            MovementRequest(
                item_id=line.item_id,
                warehouse_id=order.warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.JOB_WORK_RECEIPT,
                from_state=StockState.JOB_WORK,
                stock_state=StockState.AVAILABLE,
                serial_id=serial_id,
                **common,
            )
        )
    scrap_index = None
    for serial_id in entry.scrapped_serial_ids:
        if scrap_index is None:
            scrap_index = len(movements)
        movements.append(
            MovementRequest(
                item_id=line.item_id,
                warehouse_id=order.warehouse_id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.SCRAP,
                from_state=StockState.JOB_WORK,
                stock_state=StockState.SCRAP,
                serial_id=serial_id,
                **common,
            )
        )
    return movements, scrap_index


async def prepare_conversions(
    session: AsyncSession,
    tenant: TenantContext,
    planned: Sequence[PlannedLine],
    *,
    order: JobWorkOrder,
    occurred_at: datetime,
) -> dict[uuid.UUID, uuid.UUID]:
    """Keep the physical label while creating the output SKU's separate identity.

    The original serial row and its ledger keep their original item. A later
    immutable conversion link connects the two histories instead of rewriting
    what was purchased or issued.
    """
    groups: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in planned:
        line, entry = row.order_line, row.entry
        if not line.is_serial_tracked or line.item_id == line.expected_output_item_id:
            continue
        identities = [*entry.output_serial_ids]
        if entry.rejection_action is RejectionAction.RETURN_TO_STOCK:
            identities.extend(entry.rejected_serial_ids)
        groups.setdefault(line.expected_output_item_id, []).extend(identities)
    wanted = [serial_id for identities in groups.values() for serial_id in identities]
    if not wanted:
        return {}
    sources = await StockSerialRepository(session, tenant).by_ids(wanted)
    if len(sources) != len(set(wanted)):
        raise NotFoundError("Stock serial")
    converted: dict[uuid.UUID, uuid.UUID] = {}
    for output_item_id, source_ids in groups.items():
        if not source_ids:
            continue
        outputs = await TraceabilityService(session, tenant).resolve_serial_ids(
            output_item_id,
            [sources[serial_id].serial_number for serial_id in source_ids],
            batch_id=None,
            warehouse_id=order.return_warehouse_id,
            location_id=None,
            received_on=occurred_at.date(),
            source=order.code,
        )
        converted.update(zip(source_ids, outputs, strict=True))
    return converted
