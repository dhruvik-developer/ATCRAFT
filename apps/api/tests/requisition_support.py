"""A requisition ready to convert, and the vendor qualified to fill it.

``setting`` builds, over the conftest ``session``, ``tenant`` and ``principal``, a
component vendor qualified for a LOCKS category with a vendor-side manager
limited to it, a lock body in LOCKS and a box outside it - both linked to the
vendor as preferred - and an approved requisition for both. ``_as`` is a caller
holding only the rights a test names, and ``_code`` a fresh code.

The requisition-conversion and purchase-order change HTTP suites both start from
it; it lives here so neither is the other's library.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import RequisitionStatus
from src.modules.procurement.schemas import RequisitionCreate, RequisitionLineIn
from src.modules.products.models import ItemGroup, ItemVendorLink
from src.modules.users.models import Role
from src.modules.vendors.models import VendorCategory, VendorType
from tests import seed_support


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


def _as(base: Principal, *permissions: Permission, role: str = "BUYER") -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=(role,),
    )


@pytest.fixture
async def setting(session: AsyncSession, tenant: TenantContext, principal: Principal):
    """A vendor qualified for LOCKS, its LOCKS manager, a lock body and a box item,
    and an approved requisition for both."""
    company_id = tenant.company_id
    uom = await seed_support.seed_uom(session, company_id=company_id)
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=_code("WH"))
    locks = await seed_support.seed_category(session, company_id=company_id)
    packing = await seed_support.seed_category(session, company_id=company_id)
    vendor = await seed_support.seed_vendor(
        session,
        company_id=company_id,
        vendor_types=(VendorType.COMPONENT_SUPPLIER,),
        is_approved=True,
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
    )
    session.add(
        VendorCategory(company_id=company_id, vendor_id=vendor.id, item_category_id=locks.id)
    )
    vendor_role = Role(
        company_id=company_id,
        code=_code("VMGR"),
        name="Vendor Manager",
        permissions=[
            str(Permission.PURCHASE_ORDER_VIEW),
            str(Permission.PURCHASE_ORDER_UPDATE),
            # Vendor assignees act through the portal: lane C moved the
            # assignee right from PURCHASE_ORDER_UPDATE to VENDOR_PORTAL_RESPOND.
            str(Permission.VENDOR_PORTAL_RESPOND),
        ],
        is_system=False,
        is_vendor_role=True,
        is_active=True,
    )
    session.add(vendor_role)
    await session.flush()
    manager = await seed_support.seed_user(
        session,
        company_id=company_id,
        full_name="Manager One",
        vendor_id=vendor.id,
        allowed_category_ids=[locks.id],
        roles=[vendor_role],
    )

    async def item(category_id: uuid.UUID) -> Any:
        record = await seed_support.seed_item(
            session,
            company_id=company_id,
            code=_code("IT"),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            category_id=category_id,
            gst_rate=Decimal("18"),
            can_purchase=True,
        )
        session.add(
            ItemVendorLink(
                company_id=company_id,
                item_id=record.id,
                vendor_id=vendor.id,
                is_preferred=True,
                last_purchase_price=Decimal("50"),
            )
        )
        await session.flush()
        return record

    lock_body = await item(locks.id)
    box = await item(packing.id)

    requisition, _ = await procurement_service.create_requisition(
        session,
        tenant,
        principal,
        RequisitionCreate(
            required_date=today() + timedelta(days=14),
            warehouse_id=warehouse.id,
            lines=[
                RequisitionLineIn(line_number=1, item_id=lock_body.id, quantity=Decimal("100")),
                RequisitionLineIn(line_number=2, item_id=box.id, quantity=Decimal("500")),
            ],
        ),
    )
    approver = _as(principal, *Permission, role="SUPER_ADMIN")
    for target in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await procurement_service.transition_requisition(
            session, tenant, approver, requisition.id, target, reason=str(target)
        )
    return {
        "warehouse": warehouse,
        "vendor": vendor,
        "locks": locks,
        "packing": packing,
        "manager": manager,
        "lock_body": lock_body,
        "box": box,
        "requisition_id": requisition.id,
    }


__all__ = ["_as", "_code", "setting"]
