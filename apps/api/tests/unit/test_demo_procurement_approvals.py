from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.demo_data import DemoContext
from src.core.demo_data import procurement as demo_procurement
from src.core.deps import Principal, TenantContext
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus


@dataclass
class _Order:
    id: uuid.UUID
    code: str
    status: PurchaseOrderStatus
    first_approved_by_id: uuid.UUID | None = None
    duplicate_acknowledged: bool = False


def _principal(user_id: uuid.UUID, company_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email=f"{user_id}@atcraft.example",
        full_name="Demo approver",
        is_superuser=True,
        permissions=frozenset(),
    )


def _context(principal: Principal) -> DemoContext:
    return DemoContext(
        session=cast(AsyncSession, object()),
        tenant=TenantContext(
            company_id=principal.company_id,
            user_id=principal.user_id,
            is_superuser=True,
        ),
        principal=principal,
        company_id=principal.company_id,
        user_id=principal.user_id,
    )


@pytest.mark.asyncio
async def test_demo_order_records_both_approvals_before_sending(monkeypatch):
    company_id = uuid.uuid4()
    first, second = _principal(uuid.uuid4(), company_id), _principal(uuid.uuid4(), company_id)
    order = _Order(uuid.uuid4(), "PO-DEMO", PurchaseOrderStatus.DRAFT)
    calls: list[tuple[uuid.UUID, PurchaseOrderStatus]] = []

    async def transition(
        _session: AsyncSession,
        _tenant: TenantContext,
        principal: Principal,
        _order_id: uuid.UUID,
        target: PurchaseOrderStatus,
        *,
        reason: str,
        confirm_duplicate: bool,
    ) -> _Order:
        calls.append((principal.user_id, target))
        if target is PurchaseOrderStatus.PENDING_APPROVAL:
            order.status = target
        elif target is PurchaseOrderStatus.APPROVED:
            if order.first_approved_by_id is None:
                order.first_approved_by_id = principal.user_id
                order.status = PurchaseOrderStatus.PENDING_APPROVAL
            else:
                assert order.first_approved_by_id != principal.user_id
                order.status = target
        elif target is PurchaseOrderStatus.SENT:
            assert order.status is PurchaseOrderStatus.APPROVED
            order.status = target
        return order

    monkeypatch.setattr(demo_procurement.procurement, "transition_purchase_order", transition)

    await demo_procurement._walk_order(
        _context(first), cast(PurchaseOrder, order), PurchaseOrderStatus.SENT, second
    )

    assert calls == [
        (first.user_id, PurchaseOrderStatus.PENDING_APPROVAL),
        (first.user_id, PurchaseOrderStatus.APPROVED),
        (second.user_id, PurchaseOrderStatus.APPROVED),
        (first.user_id, PurchaseOrderStatus.SENT),
    ]
    assert order.status is PurchaseOrderStatus.SENT


@pytest.mark.asyncio
async def test_demo_order_resumes_with_a_second_approver_after_first_signature(monkeypatch):
    company_id = uuid.uuid4()
    first, second = _principal(uuid.uuid4(), company_id), _principal(uuid.uuid4(), company_id)
    order = _Order(
        uuid.uuid4(),
        "PO-DEMO",
        PurchaseOrderStatus.PENDING_APPROVAL,
        first_approved_by_id=first.user_id,
    )
    calls: list[tuple[uuid.UUID, PurchaseOrderStatus]] = []

    async def transition(
        _session: AsyncSession,
        _tenant: TenantContext,
        principal: Principal,
        _order_id: uuid.UUID,
        target: PurchaseOrderStatus,
        *,
        reason: str,
        confirm_duplicate: bool,
    ) -> _Order:
        calls.append((principal.user_id, target))
        if target is PurchaseOrderStatus.APPROVED:
            assert order.first_approved_by_id != principal.user_id
            order.status = target
        elif target is PurchaseOrderStatus.SENT:
            assert order.status is PurchaseOrderStatus.APPROVED
            order.status = target
        return order

    monkeypatch.setattr(demo_procurement.procurement, "transition_purchase_order", transition)

    await demo_procurement._walk_order(
        _context(first), cast(PurchaseOrder, order), PurchaseOrderStatus.SENT, second
    )

    assert calls == [
        (second.user_id, PurchaseOrderStatus.APPROVED),
        (first.user_id, PurchaseOrderStatus.SENT),
    ]
    assert order.status is PurchaseOrderStatus.SENT
