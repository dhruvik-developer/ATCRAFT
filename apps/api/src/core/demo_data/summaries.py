"""Make the seeded stock visible in reports without requiring a beat worker."""

from __future__ import annotations

from sqlalchemy import func, select

from src.core.database import session_scope
from src.core.deps import TenantContext
from src.modules.inventory.models import InventoryTransaction
from src.modules.reports import service as reports_service
from src.modules.reports.repository import ReportQueryRepository


async def refresh_if_stale(tenant: TenantContext) -> reports_service.SummaryRefreshResult | None:
    """Refresh this company's derived stock summary after demonstration writes.

    The ordinary reporting service remains responsible for aggregate values and
    their audit record. Checking the latest ledger insertion, rather than its
    backdated business date, also catches historical movements added by a rerun.
    An unchanged rerun preserves the summary and does not add another audit row.
    """
    async with session_scope() as session:
        latest_movement = await session.scalar(
            select(func.max(InventoryTransaction.created_at)).where(
                InventoryTransaction.company_id == tenant.company_id
            )
        )
        if latest_movement is None:
            return None
        refreshed_at = await ReportQueryRepository(session, tenant).summary_refreshed_at()
        if refreshed_at is not None and refreshed_at >= latest_movement:
            return None
        return await reports_service.refresh_inventory_summary(session, tenant)
