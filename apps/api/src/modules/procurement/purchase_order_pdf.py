"""Queue a document-only purchase order job through the shared report lifecycle."""

from __future__ import annotations

import uuid
from typing import Any

from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.responses import AcceptedJobResponse, api_path
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.reports import service as reports
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository


async def queue_purchase_order_pdf(
    session: Any, tenant: TenantContext, order_id: uuid.UUID, *, portal: bool = False
) -> AcceptedJobResponse:
    order = await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
    async with transaction(session):
        snapshot = reports.new_job_snapshot(
            ReportKey.PURCHASE_ORDER, {"purchase_order_id": str(order_id)}, now=utcnow()
        )
        ReportSnapshotRepository(session, tenant).add(snapshot)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type="purchase_order",
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Queued PDF for {order.code}",
            user_id=tenant.user_id,
        )
    await reports.dispatch_job(
        session,
        tenant,
        snapshot,
        task_name="reports.generate_purchase_order",
        queue="reports",
        task_kwargs={"purchase_order_id": str(order_id)},
    )
    return AcceptedJobResponse.create(
        str(snapshot.id), api_path(f"{'vendor-portal' if portal else 'reports'}/jobs/{snapshot.id}")
    )
