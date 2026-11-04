"""Collections HTTP permissions and tenant boundaries."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal
from src.core.permissions import Permission
from src.modules.collections.models import FollowUpStatus, PaymentFollowUp
from tests.seed_support import seed_customer

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(
    base: Principal,
    *permissions: Permission,
    vendor_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(permission) for permission in permissions),
        role_codes=base.role_codes,
        vendor_id=vendor_id,
        customer_id=customer_id,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


async def _follow_up(
    session: AsyncSession, *, company_id: uuid.UUID, customer_id: uuid.UUID
) -> PaymentFollowUp:
    follow = PaymentFollowUp(
        company_id=company_id,
        code=f"HTTP-FU-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer_id,
        currency="INR",
        status=FollowUpStatus.OPEN,
        opened_overdue_amount=Decimal("250"),
        current_overdue_amount=Decimal("250"),
        oldest_due_date=today() - timedelta(days=10),
    )
    session.add(follow)
    await session.flush()
    return follow


async def test_collection_view_and_manage_are_separate_rights(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    customer = await seed_customer(
        session, company_id=principal.company_id, code=f"CU-{uuid.uuid4().hex[:8]}"
    )
    follow = await _follow_up(session, company_id=principal.company_id, customer_id=customer.id)
    path = f"/collections/follow-ups/{follow.id}"

    _as(_holding(principal, Permission.COLLECTION_VIEW))
    assert (await api_client.get(path)).status_code == 200
    refused = await api_client.post(f"{path}/notes", json={"channel": "CALL", "note": "Attempt"})
    assert refused.status_code == 403

    _as(_holding(principal, Permission.COLLECTION_MANAGE))
    assert (await api_client.get(path)).status_code == 403
    created = await api_client.post(f"{path}/notes", json={"channel": "CALL", "note": "Attempt"})
    assert created.status_code == 201
    assert created.json()["data"]["channel"] == "CALL"


async def test_portal_principals_are_refused_from_collections(
    api_client, principal: Principal
) -> None:
    all_rights = tuple(Permission)
    vendor = _holding(principal, *all_rights, vendor_id=uuid.uuid4())
    _as(vendor)
    vendor_result = await api_client.get("/collections/worklist")
    assert vendor_result.status_code == 403
    assert vendor_result.json()["error"]["code"] == "SCOPE_DENIED"

    dealer = _holding(principal, *all_rights, customer_id=uuid.uuid4())
    _as(dealer)
    dealer_result = await api_client.get("/collections/worklist")
    assert dealer_result.status_code == 403
    assert dealer_result.json()["error"]["code"] == "SCOPE_DENIED"


async def test_foreign_tenant_follow_up_is_not_found(
    api_client, session: AsyncSession, other_company
) -> None:
    customer = await seed_customer(
        session, company_id=other_company.id, code=f"CU-{uuid.uuid4().hex[:8]}"
    )
    follow = await _follow_up(session, company_id=other_company.id, customer_id=customer.id)

    response = await api_client.get(f"/collections/follow-ups/{follow.id}")
    assert response.status_code == 404


async def test_blocking_customer_during_escalation_requires_customer_update(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    customer = await seed_customer(
        session, company_id=principal.company_id, code=f"CU-{uuid.uuid4().hex[:8]}"
    )
    follow = await _follow_up(session, company_id=principal.company_id, customer_id=customer.id)
    _as(_holding(principal, Permission.COLLECTION_MANAGE))

    response = await api_client.post(
        f"/collections/follow-ups/{follow.id}/escalate",
        json={"reason": "Repeated missed commitments", "block_customer": True},
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required_permission"] == "customer.update"
    await session.refresh(follow)
    assert follow.status == FollowUpStatus.OPEN
