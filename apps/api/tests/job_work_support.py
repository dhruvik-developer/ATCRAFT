"""The worked job-work example: raw handles out for PVD, coated handles back.

``PvdJob`` holds the unit, the raw and coated items, the store and the plating
vendor, and builds the order payload; the ``seed_*`` builders make each of those
rows through the ORM and return its id, ``stock_in`` posts opening stock through
the real engine, and ``issued_order`` raises, approves and issues an order so its
material sits at the vendor. ``issued_order`` takes the ``(session, tenant)``
pair the job-work suite's ``ctx`` fixture yields.

The job-work service suite and the job-work and vendor-portal HTTP suites all
use them; they live here so none of those files is another's library.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.job_work import service
from src.modules.job_work.models import JobWorkProcess, RateBasis
from src.modules.job_work.schemas import IssueRequest, JobWorkOrderCreate, JobWorkOrderLineCreate
from src.modules.products.models import Item, ItemGroup, UnitOfMeasure, UomCategory
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def seed_uom(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    uom = UnitOfMeasure(
        company_id=company_id,
        code=_code("NOS")[:16],
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=False,
    )
    session.add(uom)
    await session.flush()
    return uom.id


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    uom_id: uuid.UUID,
    *,
    code: str,
    can_job_work: bool = True,
    standard_cost: str = "100",
    batch_tracked: bool = False,
) -> uuid.UUID:
    item = Item(
        company_id=company_id,
        code=code,
        name=f"Item {code}",
        item_group=ItemGroup.COMPONENT,
        base_uom_id=uom_id,
        standard_cost=Decimal(standard_cost),
        can_job_work=can_job_work,
        can_scrap=True,
        is_batch_tracked=batch_tracked,
    )
    session.add(item)
    await session.flush()
    return item.id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    code = _code("WH")
    warehouse = Warehouse(
        company_id=company_id,
        code=code,
        name=f"Store {code}",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    session.add(warehouse)
    await session.flush()
    return warehouse.id


async def seed_vendor(
    session: AsyncSession, company_id: uuid.UUID, *, approved: bool = True, active: bool = True
) -> uuid.UUID:
    vendor = Vendor(
        company_id=company_id,
        code=_code("V")[:32],
        name="Chrome Plating Works",
        vendor_types=[VendorType.JOB_WORK.value],
        is_approved=approved,
        # The vendors table refuses an approval with no timestamp behind it.
        approved_at=utcnow() if approved else None,
        is_active=active,
    )
    session.add(vendor)
    await session.flush()
    return vendor.id


async def stock_in(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    unit_cost: str = "100",
    batch_id: uuid.UUID | None = None,
) -> None:
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="job work fixture opening stock",
            unit_cost=Decimal(unit_cost),
            batch_id=batch_id,
        )


class PvdJob:
    """The worked example: raw handles out for PVD, coated handles back."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        raw_item_id: uuid.UUID,
        coated_item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        vendor_id: uuid.UUID,
    ) -> None:
        self.uom_id = uom_id
        self.raw_item_id = raw_item_id
        self.coated_item_id = coated_item_id
        self.warehouse_id = warehouse_id
        self.vendor_id = vendor_id

    def payload(
        self,
        *,
        quantity: str = "100",
        expected_output: str | None = None,
        loss_percentage: str = "0",
        rate: str = "12.50",
        rate_basis: RateBasis = RateBasis.PER_PIECE,
        requires_qc: bool = False,
    ) -> JobWorkOrderCreate:
        return JobWorkOrderCreate(
            vendor_id=self.vendor_id,
            process_type=JobWorkProcess.PVD,
            process_description="PVD, rose gold, 3 micron",
            expected_return_date=today() + timedelta(days=14),
            warehouse_id=self.warehouse_id,
            rate_per_unit=Decimal(rate),
            rate_basis=rate_basis,
            requires_qc=requires_qc,
            lines=[
                JobWorkOrderLineCreate(
                    line_number=1,
                    item_id=self.raw_item_id,
                    quantity_to_issue=Decimal(quantity),
                    expected_output_item_id=self.coated_item_id,
                    expected_output_quantity=(
                        Decimal(expected_output) if expected_output else None
                    ),
                    expected_loss_percentage=Decimal(loss_percentage),
                )
            ],
        )


async def issued_order(ctx, job: PvdJob, *, stock: str = "100", **payload_kwargs):
    """Raise, approve and issue an order, leaving its material at the vendor."""
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, stock)
    order, _ = await service.create_order(session, tenant, job.payload(**payload_kwargs))
    await service.approve_order(session, tenant, order.id, reason="Rate agreed with vendor")
    result = await service.issue_order(
        session,
        tenant,
        order.id,
        IssueRequest(reason="Handed to the vendor's vehicle", vehicle_number="GJ01AB1234"),
    )
    return order, result


__all__ = [
    "PvdJob",
    "_code",
    "issued_order",
    "seed_item",
    "seed_uom",
    "seed_vendor",
    "seed_warehouse",
    "stock_in",
]
