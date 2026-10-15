"""Builders shared by the vendor-portal HTTP tests.

Orders are raised, approved and sent through the procurement service as the
buyer - the vendor's requests are what the tests put over HTTP. Rows are built
through the ORM (CLAUDE.md: a hand-written column list goes stale).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.procurement import service as procurement
from src.modules.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderCreate,
    PurchaseOrderLineIn,
)
from src.modules.products.models import ItemCategory, ProductionType
from src.modules.users.models import Role, User
from src.modules.vendors.models import Vendor, VendorType
from tests import seed_support


def code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def approved_vendor(session: AsyncSession, tenant: TenantContext, name: str) -> Vendor:
    vendor = Vendor(
        company_id=tenant.company_id,
        code=code("V"),
        name=name,
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER.value],
        payment_terms_days=30,
        lead_time_days=7,
        currency="INR",
        is_approved=True,
        # The database refuses an approved vendor with no approval timestamp.
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
    )
    session.add(vendor)
    await session.flush()
    return vendor


async def raise_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    *,
    vendor: Vendor,
    warehouse_id: uuid.UUID,
    uom_id: uuid.UUID,
    category: ItemCategory | None = None,
    quantity: Decimal = Decimal("10"),
    expected: date | None = None,
    notes: str | None = None,
) -> PurchaseOrder:
    """A DRAFT order for one fresh item (fresh, so the duplicate check stays quiet)."""
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=code("RM"),
        base_uom_id=uom_id,
        purchase_uom_id=uom_id,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("40"),
        production_type=ProductionType.BUY,
        can_purchase=True,
        category_id=category.id if category is not None else None,
    )
    order, _, _ = await procurement.create_purchase_order(
        session,
        tenant,
        principal,
        PurchaseOrderCreate(
            vendor_id=vendor.id,
            warehouse_id=warehouse_id,
            po_date=today() - timedelta(days=3),
            expected_delivery_date=expected or today() + timedelta(days=10),
            notes=notes,
            lines=[
                PurchaseOrderLineIn(
                    line_number=1, item_id=item.id, quantity=quantity, unit_price=Decimal("50")
                )
            ],
        ),
    )
    return order


def approver(base: Principal) -> Principal:
    """The buyer's own user holding SUPER_ADMIN - who may approve their own order."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=base.permissions,
        role_codes=("SUPER_ADMIN",),
    )


async def send(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    assigned_vendor_user_id: uuid.UUID | None = None,
) -> None:
    for target, actor in (
        (PurchaseOrderStatus.PENDING_APPROVAL, principal),
        (PurchaseOrderStatus.APPROVED, approver(principal)),
    ):
        await procurement.transition_purchase_order(
            session, tenant, actor, order_id, target, reason=str(target)
        )
    await procurement.transition_purchase_order(
        session,
        tenant,
        principal,
        order_id,
        PurchaseOrderStatus.SENT,
        reason="Sent to the vendor",
        assigned_vendor_user_id=assigned_vendor_user_id,
    )


def vendor_principal(
    base: Principal,
    vendor_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    preset: str = "VENDOR_OWNER",
    allowed_category_ids: tuple[uuid.UUID, ...] = (),
) -> Principal:
    """What a portal token resolves to: a vendor preset bound to one vendor."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS[preset]["permissions"])
    return Principal(
        user_id=user_id or base.user_id,
        company_id=base.company_id,
        email=f"{preset.lower()}@vendor.test",
        full_name=preset.replace("_", " ").title(),
        is_superuser=False,
        permissions=frozenset(str(p) for p in granted),
        role_codes=(preset,),
        vendor_id=vendor_id,
        allowed_category_ids=allowed_category_ids,
    )


def sign_in(principal: Principal) -> None:
    """Make every following request of the shared ``api_client`` run as ``principal``."""
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


async def role(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    role_code: str,
    permissions: Iterable[str],
    vendor_role: bool = False,
) -> Role:
    record = Role(
        company_id=tenant.company_id,
        code=role_code,
        name=role_code.replace("_", " ").title(),
        permissions=sorted({str(p) for p in permissions}),
        is_active=True,
        is_vendor_role=vendor_role,
    )
    session.add(record)
    await session.flush()
    return record


async def portal_user(
    session: AsyncSession,
    tenant: TenantContext,
    vendor: Vendor,
    *,
    preset: str = "VENDOR_OWNER",
    allowed_category_ids: Iterable[uuid.UUID] = (),
    with_role: Role | None = None,
) -> User:
    """A vendor account holding one vendor preset (a role of its own, per call)."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS[preset]["permissions"])
    record = with_role or await role(
        session, tenant, role_code=code(preset), permissions=granted, vendor_role=True
    )
    user = User(
        company_id=tenant.company_id,
        vendor_id=vendor.id,
        email=f"{uuid.uuid4().hex[:10]}@vendor.test",
        full_name=f"{vendor.name} {preset.title()}",
        password_hash="unused",
        is_active=True,
        allowed_category_ids=list(allowed_category_ids) or None,
        roles=[record],
    )
    session.add(user)
    await session.flush()
    return user


async def staff_user(
    session: AsyncSession, tenant: TenantContext, *, with_role: Role, name: str = "Buyer"
) -> User:
    user = User(
        company_id=tenant.company_id,
        email=f"{uuid.uuid4().hex[:10]}@staff.test",
        full_name=name,
        password_hash="unused",
        is_active=True,
        roles=[with_role],
    )
    session.add(user)
    await session.flush()
    return user


async def order_line(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrderLine:
    return (
        await session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order_id)
        )
    ).scalar_one()


async def receive(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    received: Decimal,
    rejected: Decimal = Decimal("0"),
    requires_qc: bool = False,
) -> GoodsReceipt:
    """Raise and post one goods receipt for the order's only line."""
    line = await order_line(session, order_id)
    receipt, _ = await procurement.create_goods_receipt(
        session,
        tenant,
        GoodsReceiptCreate(
            purchase_order_id=order_id,
            lines=[
                GoodsReceiptLineIn(
                    line_number=1,
                    purchase_order_line_id=line.id,
                    received_quantity=received,
                    accepted_quantity=received - rejected,
                    rejected_quantity=rejected,
                    rejection_reason="Dented casing" if rejected else None,
                    requires_qc=requires_qc,
                )
            ],
        ),
    )
    await procurement.post_goods_receipt(session, tenant, principal, receipt.id)
    return receipt
