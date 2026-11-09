"""Reservation and reallocation-decision routes over HTTP.

The engine's rules - the balance row lock, promisable stock excluding QC hold,
release-never-clamps, the separate-approver control - are pinned at service
level in ``tests/integration/test_allocation_service.py``, and raising and
approving a reallocation over HTTP is ``test_allocation_reallocation_http.py``'s.
These drive the three routes no backend test had reached through HTTP -
reserving stock, releasing a reservation and rejecting a reallocation request -
for the status and envelope each answers with, the balance, the row and the
audit entry it leaves behind, the 422 a payload or a record's state earns, and
the 404 another company's record answers with.

Permission gates are ``tests/unit/test_route_authorization.py``'s. The caller is
the ``api_client`` principal - every permission, not a superuser. A request the
principal is to reject is raised through the service by ``maker``, a colleague,
because the requester may not decide their own. Stock arrives through
``post_movement`` and reservations through ``allocation.service.reserve``. Ids
are held as plain values: an expected failure rolls its savepoint back and
expires whatever was loaded before it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import TenantContext
from src.modules.allocation import service as alloc
from src.modules.allocation.models import (
    DemandType,
    ReallocationRequest,
    StockReservation,
)
from src.modules.allocation.schemas import ReallocationRequestCreate
from src.modules.allocation.service import AllocationService
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.inventory.models import InventoryBalance, InventoryTransaction, TransactionType
from src.modules.inventory.service import post_movement
from src.modules.notifications.models import Notification, NotificationType
from src.workers import celery_app as celery_module
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Held for the Friday assembly run"


# -----------------------------------------------------------------------------
# Callers, envelopes and readers
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Approval decisions fan out notifications; nothing reaches a broker."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's rows, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


@pytest.fixture
async def maker(session: AsyncSession, tenant: TenantContext) -> TenantContext:
    """A planner who raises the requests the principal is asked to decide."""
    colleague = await seed_support.seed_user(
        session, company_id=tenant.company_id, full_name="Planner"
    )
    return TenantContext(company_id=tenant.company_id, user_id=colleague.id)


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_id: uuid.UUID, action: AuditAction
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.context,
        )
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_id == entity_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).all())


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read from the row rather than from the identity map."""
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def held(session: AsyncSession, item_id: uuid.UUID, warehouse_id: uuid.UUID) -> Row[Any]:
    """The unbinned, unbatched position's on-hand, reserved and available figures."""
    stmt = select(
        InventoryBalance.on_hand_quantity,
        InventoryBalance.reserved_quantity,
        InventoryBalance.available_quantity,
    ).where(
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None),
    )
    return (await session.execute(stmt)).one()


async def reservations_for(session: AsyncSession, item_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(StockReservation)
        .where(StockReservation.item_id == item_id)
    )
    return int(await session.scalar(stmt) or 0)


# -----------------------------------------------------------------------------
# Builders
# -----------------------------------------------------------------------------


async def stocked(
    session: AsyncSession,
    tenant: TenantContext,
    unique_code,
    *,
    quantity: str = "100",
    **item_fields: Any,
) -> tuple[uuid.UUID, uuid.UUID]:
    """``quantity`` units at the unbinned position of a new warehouse."""
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("RES"), **item_fields
    )
    item_id = item.id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("WH")
    )
    warehouse_id = warehouse.id
    await post_movement(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="test",
        reference_id=None,
        reference_code=None,
        reason="Opening test stock",
        unit_cost=Decimal("10"),
    )
    return item_id, warehouse_id


async def reserved(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str = "30",
) -> uuid.UUID:
    reservation = await alloc.reserve(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal(quantity),
        demand_type=DemandType.PRODUCTION_ORDER,
        demand_id=uuid.uuid4(),
        demand_code="MO-0042",
        reason=REASON,
    )
    return reservation.id


async def pending_request(
    session: AsyncSession, requester: TenantContext, unique_code
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A PENDING request for 40 of the 100 a low-priority order holds."""
    item_id, warehouse_id = await stocked(session, requester, unique_code)
    donor = await alloc.reserve(
        session,
        requester,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal("100"),
        demand_type=DemandType.PRODUCTION_ORDER,
        demand_id=uuid.uuid4(),
        demand_code="MO-LOW",
        priority=100,
        required_date=today() + timedelta(days=30),
        reason="The low-priority order holds it",
    )
    donor_id = donor.id
    request = await AllocationService(session, requester).create_reallocation_request(
        ReallocationRequestCreate(
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal("40"),
            to_demand_type=DemandType.PRODUCTION_ORDER,
            to_demand_id=uuid.uuid4(),
            to_demand_code="MO-URGENT",
            priority=10,
            required_date=today() + timedelta(days=5),
            reason="The line stops on Friday without this stock",
        )
    )
    return request.id, donor_id, item_id, warehouse_id


def reservation_body(
    item_id: uuid.UUID | str, warehouse_id: uuid.UUID | str, **fields: Any
) -> dict[str, Any]:
    return {
        "item_id": str(item_id),
        "warehouse_id": str(warehouse_id),
        "quantity": "30",
        "demand_type": "PRODUCTION_ORDER",
        "demand_id": str(uuid.uuid4()),
        "reason": REASON,
        **fields,
    }


NIL = "019a0000-0000-7000-8000-000000000001"


# -----------------------------------------------------------------------------
# POST /allocation/reservations
# -----------------------------------------------------------------------------


class TestCreateReservation:
    async def test_stock_is_promised_without_moving_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        required = (today() + timedelta(days=4)).isoformat()

        data = ok(
            await api_client.post(
                "/allocation/reservations",
                json=reservation_body(
                    item_id, warehouse_id, demand_code="  MO-   0042 ", required_date=required
                ),
            ),
            201,
        )

        assert (data["status"], data["reservation_type"], data["priority"]) == (
            "ACTIVE",
            "HARD",
            100,
        )
        assert data["demand_code"] == "MO- 0042"
        assert (Decimal(data["quantity"]), Decimal(data["remaining_quantity"])) == (30, 30)
        assert (Decimal(data["consumed_quantity"]), Decimal(data["released_quantity"])) == (0, 0)
        assert (data["required_date"], data["item_id"]) == (required, str(item_id))
        assert data["item_code"] and data["warehouse_code"]
        reservation_id = uuid.UUID(data["id"])
        assert await column(session, StockReservation.status, reservation_id) == "ACTIVE"
        # A promise, not a movement: on-hand is untouched and no ledger row is written.
        assert tuple(await held(session, item_id, warehouse_id)) == (100, 30, 70)
        movements = await session.scalar(
            select(func.count())
            .select_from(InventoryTransaction)
            .where(InventoryTransaction.item_id == item_id)
        )
        assert movements == 1
        (change,) = await audit_rows(
            session, tenant.company_id, reservation_id, AuditAction.RESERVATION_CHANGE
        )
        assert (change.entity_type, change.created_by_id) == (
            "stock_reservation",
            principal.user_id,
        )
        assert (
            Decimal(change.context["promisable_before"]),
            Decimal(change.context["promisable_after"]),
        ) == (100, 70)

    async def test_more_than_is_promisable_is_refused(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        await reserved(session, tenant, item_id, warehouse_id, "30")

        error = refused(
            await api_client.post(
                "/allocation/reservations",
                json=reservation_body(item_id, warehouse_id, quantity="71"),
            ),
            422,
            "INSUFFICIENT_AVAILABLE_STOCK",
        )
        assert (Decimal(error["details"]["requested"]), Decimal(error["details"]["available"])) == (
            71,
            70,
        )
        assert tuple(await held(session, item_id, warehouse_id)) == (100, 30, 70)
        assert await reservations_for(session, item_id) == 1

    async def test_a_spare_reservation_needs_an_item_that_can_be_a_spare(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)

        refused(
            await api_client.post(
                "/allocation/reservations",
                json=reservation_body(item_id, warehouse_id, demand_type="SPARE"),
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await reservations_for(session, item_id) == 0

    @pytest.mark.parametrize("whose", ["item", "warehouse"])
    async def test_another_companys_item_or_warehouse_is_not_found(
        self, api_client, session, tenant, foreign, unique_code, whose
    ) -> None:
        mine = await stocked(session, tenant, unique_code)
        theirs = await stocked(session, foreign, unique_code)
        item_id, warehouse_id = (theirs[0], mine[1]) if whose == "item" else (mine[0], theirs[1])

        refused(
            await api_client.post(
                "/allocation/reservations", json=reservation_body(item_id, warehouse_id)
            ),
            404,
            "NOT_FOUND",
        )
        assert await reservations_for(session, item_id) == 0
        assert tuple(await held(session, *theirs))[1] == 0

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            pytest.param({"quantity": "0"}, "quantity", id="zero-quantity"),
            pytest.param({"reason": "no"}, "reason", id="reason-short"),
            pytest.param({"priority": 10000}, "priority", id="priority-out-of-range"),
            pytest.param({"demand_type": "INVOICE"}, "demand_type", id="unknown-demand-type"),
            pytest.param({"demand_code": "M" * 65}, "demand_code", id="demand-code-too-long"),
        ],
    )
    async def test_a_malformed_request_is_refused_with_its_field_named(
        self, api_client, change, field
    ) -> None:
        response = await api_client.post(
            "/allocation/reservations", json=reservation_body(NIL, NIL, **change)
        )
        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /allocation/reservations/{reservation_id}/release
# -----------------------------------------------------------------------------


class TestReleaseReservation:
    async def test_part_is_given_back_and_the_rest_still_stands(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        reservation_id = await reserved(session, tenant, item_id, warehouse_id, "30")

        data = ok(
            await api_client.post(
                f"/allocation/reservations/{reservation_id}/release",
                json={"reason": "Order cut to twenty", "quantity": "10"},
            )
        )

        assert data["status"] == "ACTIVE"
        assert (Decimal(data["released_quantity"]), Decimal(data["remaining_quantity"])) == (10, 20)
        assert (data["release_reason"], data["released_by_id"]) == (
            "Order cut to twenty",
            str(principal.user_id),
        )
        assert data["released_at"] is not None
        assert tuple(await held(session, item_id, warehouse_id)) == (100, 20, 80)
        changes = await audit_rows(
            session, tenant.company_id, reservation_id, AuditAction.RESERVATION_CHANGE
        )
        # The reservation's own entry, then this release - and no status change.
        assert len(changes) == 2
        release = changes[-1]
        assert release.created_by_id == principal.user_id
        assert Decimal(release.context["released_now"]) == 10
        assert release.context["release_reason"] == "Order cut to twenty"

    async def test_omitting_the_quantity_releases_the_remainder(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        reservation_id = await reserved(session, tenant, item_id, warehouse_id, "30")

        data = ok(
            await api_client.post(
                f"/allocation/reservations/{reservation_id}/release",
                json={"reason": "Production order cancelled"},
            )
        )

        assert data["status"] == "RELEASED"
        assert (Decimal(data["released_quantity"]), Decimal(data["remaining_quantity"])) == (30, 0)
        assert await column(session, StockReservation.status, reservation_id) == "RELEASED"
        assert tuple(await held(session, item_id, warehouse_id)) == (100, 0, 100)
        changes = await audit_rows(
            session, tenant.company_id, reservation_id, AuditAction.RESERVATION_CHANGE
        )
        assert (changes[-1].old_values, changes[-1].new_values) == (
            {"status": "ACTIVE"},
            {"status": "RELEASED"},
        )
        assert changes[-1].context == {"reason": "Production order cancelled"}

    @pytest.mark.parametrize(
        ("history", "quantity"),
        [
            pytest.param((), "31", id="more-than-remains"),
            pytest.param(("release",), "1", id="already-released"),
        ],
    )
    async def test_a_release_that_does_not_fit_is_refused_not_clamped(
        self, api_client, session, tenant, unique_code, history, quantity
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        reservation_id = await reserved(session, tenant, item_id, warehouse_id, "30")
        for _ in history:
            await alloc.release(session, tenant, reservation_id=reservation_id, reason="Earlier")
        before = tuple(await held(session, item_id, warehouse_id))

        refused(
            await api_client.post(
                f"/allocation/reservations/{reservation_id}/release",
                json={"reason": "Give it back", "quantity": quantity},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert tuple(await held(session, item_id, warehouse_id)) == before

    async def test_another_companys_reservation_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, foreign, unique_code)
        theirs = await reserved(session, foreign, item_id, warehouse_id, "30")

        refused(
            await api_client.post(
                f"/allocation/reservations/{theirs}/release", json={"reason": "Scope probe"}
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, StockReservation.status, theirs) == "ACTIVE"
        assert tuple(await held(session, item_id, warehouse_id))[1] == 30

    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            pytest.param({}, "reason", id="reason-missing"),
            pytest.param({"reason": "Give it back", "quantity": "-5"}, "quantity", id="negative"),
        ],
    )
    async def test_a_malformed_release_is_refused_with_its_field_named(
        self, api_client, payload, field
    ) -> None:
        response = await api_client.post(f"/allocation/reservations/{NIL}/release", json=payload)
        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /allocation/reallocation-requests/{request_id}/reject
# -----------------------------------------------------------------------------


class TestRejectReallocationRequest:
    async def test_a_second_person_rejects_and_the_donor_keeps_its_stock(
        self, api_client, session, tenant, principal, maker, unique_code
    ) -> None:
        request_id, donor_id, item_id, warehouse_id = await pending_request(
            session, maker, unique_code
        )

        data = ok(
            await api_client.post(
                f"/allocation/reallocation-requests/{request_id}/reject",
                json={"decision_note": "  The customer order   ships first "},
            )
        )

        assert data["status"] == "REJECTED"
        assert (data["approved_by_id"], data["decision_note"]) == (
            str(principal.user_id),
            "The customer order ships first",
        )
        assert (data["from_reservation_id"], data["from_demand_code"]) == (str(donor_id), "MO-LOW")
        assert data["to_reservation_id"] is None
        assert await column(session, ReallocationRequest.status, request_id) == "REJECTED"
        # Nothing moved: the donor still holds every unit it held.
        assert await column(session, StockReservation.released_quantity, donor_id) == 0
        assert tuple(await held(session, item_id, warehouse_id)) == (100, 100, 0)
        (rejection,) = await audit_rows(session, tenant.company_id, request_id, AuditAction.REJECT)
        assert (rejection.entity_type, rejection.created_by_id) == (
            "reallocation_request",
            principal.user_id,
        )
        assert (rejection.old_values, rejection.new_values) == (
            {"status": "PENDING"},
            {"status": "REJECTED"},
        )
        assert rejection.context == {"reason": "The customer order ships first"}
        queue = await session.scalars(
            select(ApprovalRequest.status).where(ApprovalRequest.entity_id == request_id)
        )
        assert list(queue.all()) == ["REJECTED"]
        told = await session.scalars(
            select(Notification.user_id).where(
                Notification.entity_id == request_id,
                Notification.notification_type == NotificationType.APPROVAL_REJECTED,
            )
        )
        assert list(told.all()) == [maker.user_id]

    async def test_the_requester_cannot_reject_their_own_request(
        self, api_client, session, tenant, unique_code
    ) -> None:
        request_id, _, _, _ = await pending_request(session, tenant, unique_code)

        # Segregation of duties answers with the one shared refusal (SelfApprovalError).
        refused(
            await api_client.post(
                f"/allocation/reallocation-requests/{request_id}/reject", json={}
            ),
            403,
            "APPROVAL_REQUIRED",
        )
        assert await column(session, ReallocationRequest.status, request_id) == "PENDING"

    async def test_a_decided_request_cannot_be_rejected_again(
        self, api_client, session, tenant, maker, unique_code
    ) -> None:
        request_id, _, _, _ = await pending_request(session, maker, unique_code)
        await AllocationService(session, tenant).reject_reallocation_request(
            request_id, decision_note="First decision"
        )

        refused(
            await api_client.post(
                f"/allocation/reallocation-requests/{request_id}/reject",
                json={"decision_note": "Second thoughts"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )
        assert await column(session, ReallocationRequest.decision_note, request_id) == (
            "First decision"
        )

    async def test_another_companys_request_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        theirs, _, _, _ = await pending_request(session, foreign, unique_code)

        refused(
            await api_client.post(f"/allocation/reallocation-requests/{theirs}/reject", json={}),
            404,
            "NOT_FOUND",
        )
        assert await column(session, ReallocationRequest.status, theirs) == "PENDING"

    async def test_an_overlong_note_is_refused_with_its_field_named(self, api_client) -> None:
        response = await api_client.post(
            f"/allocation/reallocation-requests/{NIL}/reject", json={"decision_note": "x" * 1001}
        )
        assert "decision_note" in field_errors(response)
