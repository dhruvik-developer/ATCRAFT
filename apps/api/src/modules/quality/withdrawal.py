"""Withdraw unstarted packing checks without asserting a verdict or moving goods."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.core.types import ZERO
from src.modules.audit.models import AuditAction
from src.modules.inventory.models import StockState
from src.modules.quality.models import InspectionStatus
from src.modules.quality.repository import QcInspectionRepository
from src.modules.quality.serial_tracking import lock_inspection_units


async def withdraw_packing_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    line_ids: Sequence[uuid.UUID],
    *,
    reason: str,
) -> None:
    """The packing owner's cancellation port; intentionally no public QC endpoint.

    A packing FINAL factory never holds stock. Only its untouched checks can be
    withdrawn; a measured lot or a prior verdict must keep its normal QC flow.
    Inspection -> balance -> serial locks match deciding, while a new inspection
    waits on those same physical units before checking for overlapping QC work.
    """
    from src.modules.quality.service import _transition

    repo = QcInspectionRepository(session, tenant)
    async with transaction(session):
        inspections = await repo.lock_pending_packing(line_ids)
        recorded = await repo.inspection_ids_with_evidence([row.id for row in inspections])
        pools = await lock_inspection_units(session, tenant, inspections)
        for inspection in inspections:
            if (
                InspectionStatus(inspection.status) is not InspectionStatus.PENDING
                or inspection.accepted_quantity
                + inspection.rejected_quantity
                + inspection.rework_quantity
                + inspection.hold_quantity
                + inspection.scrap_quantity
                != ZERO
                or inspection.serial_dispositions
                or inspection.id in recorded
            ):
                raise BusinessRuleError(
                    "Finish the inspection already in progress before cancelling "
                    "this packing order.",
                    details={"inspection_id": str(inspection.id)},
                )
            if any(
                pools.get(serial_id) is not StockState.AVAILABLE
                for serial_id in inspection.serial_ids
            ):
                raise BusinessRuleError(
                    "A packing inspection can be withdrawn only while its units "
                    "remain in available stock.",
                    details={"inspection_id": str(inspection.id)},
                )
            await _transition(
                session,
                tenant,
                inspection,
                InspectionStatus.WITHDRAWN,
                reason=reason,
                repo=repo,
                action=AuditAction.CANCEL,
                flush=False,
            )
        # State changes and their audits flush together, independent of line count.
        await session.flush()
