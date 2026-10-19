"""Queue and validate purchase-return debit-note PDF exports."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.types import ZERO, to_money
from src.modules.procurement.models import PurchaseReturnStatus
from src.modules.procurement.repository import PurchaseReturnRepository
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository


async def queue_purchase_return_debit_note_pdf(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> str:
    purchase_return = await PurchaseReturnRepository(session, tenant).get(return_id)
    if purchase_return is None:
        raise NotFoundError("Purchase return", return_id)
    if PurchaseReturnStatus(purchase_return.status) is not PurchaseReturnStatus.POSTED:
        raise BusinessRuleError(
            f"{purchase_return.code} is {purchase_return.status}; only a posted return can "
            "produce a vendor debit note.",
            details={"status": str(purchase_return.status)},
        )
    if to_money(purchase_return.total_amount) <= ZERO:
        raise BusinessRuleError(f"{purchase_return.code} has no debit-note value to print.")

    from src.modules.finance.repository import LedgerEntryRepository
    from src.modules.finance.service import REFERENCE_PURCHASE_RETURN

    debit_entries = await LedgerEntryRepository(session, tenant).entries_for_reference(
        REFERENCE_PURCHASE_RETURN, purchase_return.id
    )
    if not any(not entry.is_reversal for entry in debit_entries):
        raise BusinessRuleError(
            f"{purchase_return.code} has no posted vendor debit journal to print."
        )

    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.PURCHASE_RETURN_DEBIT_NOTE,
            {"purchase_return_id": str(purchase_return.id)},
            now=utcnow(),
        )
        job.expires_at = None
        ReportSnapshotRepository(session, tenant).add(job)
        await session.flush()
    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name="reports.generate_purchase_return_debit_note",
        queue="reports",
        task_kwargs={"purchase_return_id": str(purchase_return.id)},
        subject="The vendor debit note PDF",
    )
    return str(job.id)
