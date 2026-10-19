"""Restricted-role coverage for the reallocation HTTP surface (WP E2).

The reallocation workflow has three distinct rights - raise, view the queue,
decide - and the whole control is that the last one belongs to somebody other
than the person who asked. Service tests already prove the separate-approver
rule; these tests prove the *boundary* carries it too, because a screen that
hides a button is not a permission check.

They also pin the requester notification: an approval inbox is pull-based, and
the person waiting on an urgent request must be told the outcome without having
to refresh it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.modules.allocation import service as alloc
from src.modules.allocation.models import DemandType, ReallocationStatus
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.notifications.models import Notification, NotificationType
from src.modules.products.models import ItemGroup
from src.modules.warehouses.models import WarehouseType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _principal_without(base: Principal, *missing: Permission) -> Principal:
    """The same person holding every right except ``missing``."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission if p not in missing),
        role_codes=base.role_codes,
    )


def _payload(item_id: uuid.UUID, warehouse_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_id": str(item_id),
        "warehouse_id": str(warehouse_id),
        "quantity": "40",
        "to_demand_type": "PRODUCTION_ORDER",
        "to_demand_id": str(uuid.uuid4()),
        "to_demand_code": "MO-URGENT",
        "priority": 10,
        "required_date": (today() + timedelta(days=5)).isoformat(),
        "reason": "The line stops on Friday without this stock",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def stocked(
    session: AsyncSession, tenant: TenantContext, company: Any
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """100 units on hand with a low-priority production demand holding all of it."""
    suffix = uuid.uuid4().hex[:8].upper()
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"RA-{suffix}",
        base_uom_id=uom.id,
        item_group=ItemGroup.RAW_MATERIAL,
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=f"WH-{suffix}",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    await inventory.post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("100"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code="OPENING",
        reason="Reallocation permission fixture",
        unit_cost=Decimal("10"),
    )
    donor = await alloc.reserve(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("100"),
        demand_type=DemandType.PRODUCTION_ORDER,
        demand_id=uuid.uuid4(),
        demand_code="MO-LOW",
        priority=100,
        required_date=today() + timedelta(days=30),
        reason="The low-priority order holds it",
    )
    return item.id, warehouse.id, donor.id


async def test_a_requester_with_reallocate_can_raise_a_request(
    api_client: AsyncClient, stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    item_id, warehouse_id, donor_id = stocked

    response = await api_client.post(
        "/allocation/reallocation-requests", json=_payload(item_id, warehouse_id)
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["status"] == ReallocationStatus.PENDING
    assert body["from_reservation_id"] == str(donor_id)
    assert body["to_demand_code"] == "MO-URGENT"


async def test_raising_a_request_needs_the_reallocate_right(
    api_client: AsyncClient,
    principal: Principal,
    stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    from src.main import app

    item_id, warehouse_id, _ = stocked
    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.ALLOCATION_REALLOCATE
    )

    refused = await api_client.post(
        "/allocation/reallocation-requests", json=_payload(item_id, warehouse_id)
    )

    assert refused.status_code == 403, refused.text


async def test_the_approval_queue_needs_the_allocation_view_right(
    api_client: AsyncClient,
    principal: Principal,
    stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.ALLOCATION_VIEW
    )

    refused = await api_client.get("/allocation/reallocation-requests")

    assert refused.status_code == 403, refused.text


async def test_approving_needs_the_approve_right(
    api_client: AsyncClient,
    principal: Principal,
    stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    from src.main import app

    item_id, warehouse_id, _ = stocked
    created = await api_client.post(
        "/allocation/reallocation-requests", json=_payload(item_id, warehouse_id)
    )
    request_id = created.json()["data"]["id"]
    app.dependency_overrides[get_current_principal] = lambda: _principal_without(
        principal, Permission.ALLOCATION_APPROVE
    )

    refused = await api_client.post(
        f"/allocation/reallocation-requests/{request_id}/approve", json={}
    )

    assert refused.status_code == 403, refused.text


async def test_the_requester_cannot_approve_their_own_request(
    api_client: AsyncClient, stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    item_id, warehouse_id, _ = stocked
    created = await api_client.post(
        "/allocation/reallocation-requests", json=_payload(item_id, warehouse_id)
    )
    request_id = created.json()["data"]["id"]

    refused = await api_client.post(
        f"/allocation/reallocation-requests/{request_id}/approve", json={}
    )

    assert refused.status_code == 422, refused.text
    assert "someone other than" in refused.json()["error"]["message"]


async def test_a_separate_approver_executes_and_both_people_are_notified(
    api_client: AsyncClient,
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    company: Any,
    stocked: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    from src.main import app

    item_id, warehouse_id, donor_id = stocked
    created = await api_client.post(
        "/allocation/reallocation-requests",
        json=_payload(item_id, warehouse_id, reason="Customer escalation, ships Friday"),
    )
    request_id = created.json()["data"]["id"]

    approver = await seed_support.seed_user(
        session, company_id=company.id, full_name="Shift Supervisor"
    )
    supervisor = Principal(
        user_id=approver.id,
        company_id=company.id,
        email=approver.email,
        full_name=approver.full_name,
        is_superuser=False,
        permissions=frozenset(
            str(p) for p in (Permission.ALLOCATION_VIEW, Permission.ALLOCATION_APPROVE)
        ),
        role_codes=("SHIFT_SUPERVISOR",),
    )
    app.dependency_overrides[get_current_principal] = lambda: supervisor

    approved = await api_client.post(
        f"/allocation/reallocation-requests/{request_id}/approve",
        json={"decision_note": "Approved - urgent order first"},
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == ReallocationStatus.EXECUTED

    rows = (
        (
            await session.execute(
                select(Notification).where(Notification.entity_id == uuid.UUID(request_id))
            )
        )
        .scalars()
        .all()
    )
    by_type = {row.notification_type: row for row in rows}
    # The requester learns the decision...
    assert by_type[NotificationType.APPROVAL_GRANTED].user_id == tenant.user_id
    assert "Approved - urgent order first" in by_type[NotificationType.APPROVAL_GRANTED].body
    # ...and the donor's owner learns the stock left, which is the existing
    # ALLOCATION_CHANGED notification addressed to the same person here.
    donor_rows = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.entity_id == donor_id,
                    Notification.notification_type == NotificationType.ALLOCATION_CHANGED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(donor_rows) == 1
