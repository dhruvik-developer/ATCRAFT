"""Builders shared by the dealer-portal HTTP tests.

Dealers, dealer users and dealer tenants. Rows are built through the ORM
(CLAUDE.md: a hand-written column list goes stale).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.customers.models import CustomerType
from src.modules.products.models import PriceType
from src.modules.users.models import Role, User
from tests import seed_support


def code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def dealer_customer(
    session: AsyncSession,
    tenant: TenantContext,
    name: str,
    *,
    customer_type: CustomerType = CustomerType.DEALER,
    price_type: PriceType = PriceType.DEALER,
) -> Any:

    record = await seed_support.seed_customer(
        session,
        company_id=tenant.company_id,
        code=code("DLR"),
        name=name,
        customer_type=customer_type,
        price_list_type=price_type,
    )
    return record


def dealer_principal(
    base: Principal,
    customer_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    preset: str = "DEALER_OWNER",
) -> Principal:
    """What a portal token resolves to: a dealer preset bound to one customer."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS[preset]["permissions"])
    return Principal(
        user_id=user_id or base.user_id,
        company_id=base.company_id,
        email=f"{preset.lower()}@dealer.example",
        full_name=preset.replace("_", " ").title(),
        is_superuser=False,
        permissions=frozenset(str(p) for p in granted),
        role_codes=(preset,),
        customer_id=customer_id,
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
) -> Role:
    record = Role(
        company_id=tenant.company_id,
        code=role_code,
        name=role_code.replace("_", " ").title(),
        permissions=sorted({str(p) for p in permissions}),
        is_system=True,
        is_dealer_role=True,
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


async def portal_user(
    session: AsyncSession,
    tenant: TenantContext,
    customer_id: uuid.UUID,
    *,
    preset: str = "DEALER_OWNER",
    with_role: Role | None = None,
) -> User:
    """A dealer account holding one dealer preset (a role of its own, per call)."""
    granted = cast("frozenset[Permission]", ROLE_PRESETS[preset]["permissions"])
    record = with_role or await role(session, tenant, role_code=code(preset), permissions=granted)
    user = User(
        company_id=tenant.company_id,
        customer_id=customer_id,
        email=f"{uuid.uuid4().hex[:10]}@dealer.example",
        full_name=f"Dealer {preset.title()}",
        password_hash="unused",
        is_active=True,
        roles=[record],
    )
    session.add(user)
    await session.flush()
    return user


async def sellable_item(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    dealer_price: Decimal = Decimal("100"),
) -> Any:
    return await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=code("FG"),
        can_sell=True,
        is_active=True,
        sale_price=dealer_price + Decimal("20"),
        dealer_price=dealer_price,
        gst_rate=Decimal("18"),
    )
