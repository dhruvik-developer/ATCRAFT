"""Production write routes over HTTP.

Service rules for releasing, issuing, booking and costing a job are pinned in
``tests/integration/test_production_service.py``. These drive the thirteen
production routes no backend test had reached through HTTP: the status and
envelope each answers with, the row, stock movement and audit entry it leaves
behind, the 422 a payload or a document's state earns, the 403 the
segregation rules own, and the 404 another company's record answers with -
never a 403, which would confirm that it exists.

Permission gates on the routes themselves are covered by
``tests/unit/test_route_authorization.py``. The caller is the ``api_client``
principal - every permission, not a superuser - and an order it is asked to
approve is raised by a second user of the same company, because nobody
approves their own. The plant is the integration suite's own ``stocked``
fixture: a kit of one body and six screws (5% scrap allowance), an approved
default structure and a one-operation routing, a thousand of each component on
the shelf at 40 and 5. Orders are walked forward through the production
service, so every prerequisite has the shape production gives it, and ids are
held as plain values: an expected failure rolls its savepoint back and expires
what was loaded before it.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.inventory.models import InventoryBalance, InventoryTransaction
from src.modules.production import service
from src.modules.production.models import (
    MaterialIssue,
    MaterialIssueType,
    ProductionEntry,
    ProductionOrder,
    ProductionOrderComponent,
    ProductionOrderStatus,
    WorkOrder,
    WorkOrderStatus,
)
from src.modules.production.schemas import (
    CancelRequest,
    CompleteRequest,
    MaterialIssueCreate,
    MaterialIssueLineIn,
    MaterialIssuePostRequest,
    ProductionOrderCreate,
    StartRequest,
    WorkOrderTransitionRequest,
)
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.integration.test_production_service import (
    approve,
    issue_everything,
    make_order,
    release,
)
from tests.integration.test_production_service import masters as masters
from tests.integration.test_production_service import stocked as stocked

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Planner decision recorded"

#: The order a job is walked through, as far as each test needs it to go.
STAGES = (
    ProductionOrderStatus.DRAFT,
    ProductionOrderStatus.APPROVED,
    ProductionOrderStatus.RELEASED,
    ProductionOrderStatus.MATERIAL_ISSUED,
    ProductionOrderStatus.IN_PROCESS,
    ProductionOrderStatus.COMPLETED,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Every task handed to Celery - approval mail and push stay in-process."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
def db(session: AsyncSession) -> AsyncSession:
    """The name the imported production fixtures ask their session by."""
    return session


@pytest.fixture
def plant(stocked) -> SimpleNamespace:
    """The stocked plant as plain ids, plus the rows the builders take."""
    return SimpleNamespace(
        masters=stocked,
        warehouse=stocked["warehouse"].id,
        kit=stocked["kit"].id,
        body=stocked["body"].id,
        screw=stocked["screw"].id,
        work_centre=stocked["work_centre"].id,
        bom=stocked["bom"].id,
    )


@pytest.fixture
async def maker(session: AsyncSession, tenant: TenantContext) -> TenantContext:
    """A second user of the same company: raises what the caller approves."""
    user = await seed_support.seed_user(session, company_id=tenant.company_id)
    return TenantContext(company_id=tenant.company_id, user_id=user.id)


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


class _Silent:
    """A logger that renders nothing."""

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: None


@pytest.fixture
async def lenient_client(api_client: AsyncClient, monkeypatch):
    """The same app and overrides, answering an unhandled error as the 500 a
    browser would see instead of re-raising it into the test.

    The two loggers an unhandled error passes through are silenced. The test
    app never runs its lifespan, so structlog keeps its default console
    renderer, which draws a rich traceback with every frame's locals - the
    whole FastAPI application among them - at minutes of CPU per error.
    """
    from httpx import ASGITransport

    from src.core import exception_handlers, middleware
    from src.main import app

    monkeypatch.setattr(middleware, "log", _Silent())
    monkeypatch.setattr(exception_handlers, "log", _Silent())
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
        yield client


# -----------------------------------------------------------------------------
# Envelope readers
# -----------------------------------------------------------------------------


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


def holding(base: Principal, *permissions: Permission) -> Principal:
    return dataclasses.replace(base, permissions=frozenset(str(p) for p in permissions))


def act_as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


# -----------------------------------------------------------------------------
# Builders and database readers
# -----------------------------------------------------------------------------


async def walked(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    plant: SimpleNamespace,
    to: ProductionOrderStatus,
    *,
    quantity: str = "10",
    by: TenantContext | None = None,
) -> uuid.UUID:
    """A job raised (by ``by``) and walked through the service as far as ``to``."""
    order = await make_order(session, by or tenant, plant.masters, quantity=quantity)
    order_id = order.id
    reach = STAGES.index(to)
    if reach >= 1:
        await approve(session, tenant, principal, order)
    if reach >= 2:
        await release(session, tenant, order)
    if reach >= 3:
        await issue_everything(session, tenant, principal, order)
    if reach >= 4:
        await service.start_order(session, tenant, order_id, StartRequest())
    if reach >= 5:
        await service.complete_order(
            session, tenant, order_id, CompleteRequest(produced_quantity=Decimal(quantity))
        )
    return order_id


async def components(session: AsyncSession, order_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(
            ProductionOrderComponent.id,
            ProductionOrderComponent.item_id,
            ProductionOrderComponent.required_quantity,
            ProductionOrderComponent.issued_quantity,
            ProductionOrderComponent.returned_quantity,
        )
        .where(ProductionOrderComponent.production_order_id == order_id)
        .order_by(ProductionOrderComponent.line_number)
    )
    return list((await session.execute(stmt)).all())


async def drafted_issue(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    quantities: dict[uuid.UUID, str],
    *,
    issue_type: MaterialIssueType = MaterialIssueType.ISSUE,
) -> uuid.UUID:
    """A draft issue (or return) naming components by id, through the service."""
    issue, _ = await service.create_material_issue(
        session,
        tenant,
        order_id,
        MaterialIssueCreate(
            reason="Kit picked for the line",
            lines=[
                MaterialIssueLineIn(
                    line_number=number, component_id=component_id, quantity=Decimal(quantity)
                )
                for number, (component_id, quantity) in enumerate(quantities.items(), start=1)
            ],
        ),
        issue_type=issue_type,
    )
    return issue.id


async def work_order_of(session: AsyncSession, order_id: uuid.UUID) -> uuid.UUID:
    found = await session.scalar(
        select(WorkOrder.id).where(WorkOrder.production_order_id == order_id)
    )
    assert found is not None
    return found


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read past the identity map."""
    return await session.scalar(select(attribute).where(attribute.class_.id == row_id))


async def stock_at(
    session: AsyncSession, company_id: uuid.UUID, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Row[Any] | None:
    stmt = select(
        InventoryBalance.on_hand_quantity,
        InventoryBalance.reserved_quantity,
        InventoryBalance.available_quantity,
    ).where(
        InventoryBalance.company_id == company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None),
    )
    return (await session.execute(stmt)).one_or_none()


async def reservations_for(session: AsyncSession, order_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(StockReservation.item_id, StockReservation.quantity, StockReservation.status)
        .where(StockReservation.demand_id == order_id)
        .order_by(StockReservation.created_at, StockReservation.id)
    )
    return list((await session.execute(stmt)).all())


async def audit_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_id: uuid.UUID,
    action: AuditAction,
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.changed_fields,
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


def moves(rows: list[Row[Any]]) -> list[tuple[str, str]]:
    return [(row.old_values["status"], row.new_values["status"]) for row in rows]


def issue_body(*lines: tuple[uuid.UUID, str], reason: str = "Kit picked for the line") -> dict:
    return {
        "reason": reason,
        "lines": [
            {"line_number": number, "component_id": str(component_id), "quantity": quantity}
            for number, (component_id, quantity) in enumerate(lines, start=1)
        ],
    }


# -----------------------------------------------------------------------------
# PATCH /production/{order_id}
# -----------------------------------------------------------------------------


class TestUpdateOrder:
    async def test_a_draft_takes_only_the_fields_sent_and_audits_the_diff(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        data = ok(
            await api_client.patch(
                f"/production/{order_id}",
                json={"planned_quantity": "25", "priority": 3, "notes": "Rush for the expo"},
            )
        )

        assert data["status"] == "DRAFT"
        assert Decimal(data["planned_quantity"]) == Decimal("25")
        assert data["priority"] == 3
        assert data["notes"] == "Rush for the expo"
        assert data["warehouse_id"] == str(plant.warehouse)
        assert await column(session, ProductionOrder.planned_quantity, order_id) == Decimal("25")
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert update.entity_type == "production_order"
        assert update.created_by_id == tenant.user_id
        assert set(update.changed_fields) == {"planned_quantity", "priority", "notes"}
        assert update.old_values["planned_quantity"] == "10.0000"

    async def test_an_approved_order_is_no_longer_editable(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.APPROVED)

        response = await api_client.patch(f"/production/{order_id}", json={"priority": 1})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ProductionOrder.priority, order_id) != 1

    async def test_a_finish_before_the_start_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        response = await api_client.patch(
            f"/production/{order_id}",
            json={"planned_end_date": (today() - timedelta(days=1)).isoformat()},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ProductionOrder.planned_end_date, order_id) > today()

    async def test_another_companys_warehouse_is_not_found(
        self, api_client: AsyncClient, session, tenant, principal, plant, foreign
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)
        theirs = await seed_support.seed_warehouse(
            session, company_id=foreign.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
        )
        theirs_id = theirs.id

        response = await api_client.patch(
            f"/production/{order_id}", json={"warehouse_id": str(theirs_id)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, ProductionOrder.warehouse_id, order_id) == plant.warehouse

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"planned_quantity": "0"}, "planned_quantity"),
            ({"priority": -1}, "priority"),
            ({"item_id": str(uuid.uuid4())}, "item_id"),
        ],
        ids=["zero-quantity", "negative-priority", "item-is-not-editable"],
    )
    async def test_an_invalid_patch_names_the_field(
        self, api_client: AsyncClient, session, tenant, principal, plant, body, field
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        response = await api_client.patch(f"/production/{order_id}", json=body)

        assert field in field_errors(response)

    @pytest.mark.parametrize(
        "field",
        ["planned_quantity", "priority", "planned_start_date", "planned_end_date", "warehouse_id"],
    )
    async def test_an_explicit_null_on_a_required_field_is_refused_or_ignored(
        self, lenient_client: AsyncClient, session, tenant, principal, plant, field
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)
        attribute = getattr(ProductionOrder, field)
        before = await column(session, attribute, order_id)

        response = await lenient_client.patch(f"/production/{order_id}", json={field: None})

        # Refused with the field named - the house rule for a required value, see
        # test_required_patch_identity_http.py - or ignored, as assembly and job
        # work do; never a 500, and never "not found" for a record nobody named.
        if response.status_code != 200:
            error = refused(response, 422, "VALIDATION_ERROR")
            assert field in ((error.get("details") or {}).get("fields") or {}), error
        assert await column(session, attribute, order_id) == before

    async def test_a_null_hold_flag_asks_for_the_default_again(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        # Not one of the refused nulls: on ``requires_qc`` it means "decide it
        # from the item and the routing", exactly as omitting it on create does.
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)
        default = await column(session, ProductionOrder.requires_qc, order_id)
        ok(await api_client.patch(f"/production/{order_id}", json={"requires_qc": not default}))

        data = ok(await api_client.patch(f"/production/{order_id}", json={"requires_qc": None}))

        assert data["requires_qc"] is default
        assert await column(session, ProductionOrder.requires_qc, order_id) is default


# -----------------------------------------------------------------------------
# Another company's order, on every order route in the batch
# -----------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("PATCH", "", {"priority": 1}),
            ("POST", "/approve", {"note": "Looks fine"}),
            ("POST", "/release", {}),
            ("POST", "/start", {}),
            ("POST", "/close", {}),
            ("POST", "/cancel", {"reason": "Taken over"}),
            ("POST", "/material-issues", issue_body((uuid.uuid4(), "1"))),
            ("POST", "/material-returns", issue_body((uuid.uuid4(), "1"))),
            ("POST", "/entries", {"produced_quantity": "1"}),
        ],
        ids=[
            "update",
            "approve",
            "release",
            "start",
            "close",
            "cancel",
            "material-issue",
            "material-return",
            "entry",
        ],
    )
    async def test_another_companys_order_is_not_found(
        self, api_client: AsyncClient, session, foreign, method, suffix, body
    ) -> None:
        item = await seed_support.seed_item(
            session,
            company_id=foreign.company_id,
            code=f"FG-{uuid.uuid4().hex[:8]}",
            can_manufacture=True,
        )
        store = await seed_support.seed_warehouse(
            session, company_id=foreign.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
        )
        theirs = await service.create_order(
            session,
            foreign,
            ProductionOrderCreate(
                item_id=item.id,
                planned_quantity=Decimal("5"),
                warehouse_id=store.id,
                planned_start_date=today(),
                planned_end_date=today(),
                priority=50,
            ),
        )
        theirs_id = theirs.id

        response = await api_client.request(method, f"/production/{theirs_id}{suffix}", json=body)

        refused(response, 404, "NOT_FOUND")
        assert await column(session, ProductionOrder.status, theirs_id) == "DRAFT"
        assert await column(session, ProductionOrder.priority, theirs_id) == 50


# -----------------------------------------------------------------------------
# POST /production/{order_id}/approve
# -----------------------------------------------------------------------------


class TestApproveOrder:
    async def test_a_second_person_approves_and_the_queue_row_closes(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.DRAFT, by=maker
        )

        data = ok(
            await api_client.post(
                f"/production/{order_id}/approve", json={"note": "Capacity confirmed"}
            )
        )

        assert data["status"] == "APPROVED"
        assert data["approved_by_id"] == str(principal.user_id)
        assert data["approved_at"] is not None
        assert await column(session, ProductionOrder.status, order_id) == "APPROVED"
        (approval,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.APPROVE)
        assert approval.created_by_id == principal.user_id
        assert moves([approval]) == [("DRAFT", "APPROVED")]
        assert approval.context["reason"] == "Capacity confirmed"
        queue = (
            await session.execute(
                select(ApprovalRequest.status, ApprovalRequest.decided_by_id).where(
                    ApprovalRequest.entity_id == order_id
                )
            )
        ).one()
        assert (queue.status, queue.decided_by_id) == ("APPROVED", principal.user_id)

    async def test_the_author_cannot_approve_their_own_order(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        response = await api_client.post(f"/production/{order_id}/approve", json={})

        refused(response, 403, "APPROVAL_REQUIRED")
        assert await column(session, ProductionOrder.status, order_id) == "DRAFT"
        assert await audit_rows(session, tenant.company_id, order_id, AuditAction.APPROVE) == []

    async def test_an_approved_order_cannot_be_approved_again(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.APPROVED, by=maker
        )

        response = await api_client.post(f"/production/{order_id}/approve", json={})

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_an_overlong_note_is_a_field_error(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.DRAFT, by=maker
        )

        response = await api_client.post(
            f"/production/{order_id}/approve", json={"note": "x" * 1001}
        )

        assert "note" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/{order_id}/release
# -----------------------------------------------------------------------------


class TestReleaseOrder:
    async def test_release_freezes_the_kit_issues_the_traveller_and_holds_the_stock(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.APPROVED)

        data = ok(await api_client.post(f"/production/{order_id}/release", json={"note": "Staged"}))

        assert data["order"]["status"] == "RELEASED"
        assert data["bom_id"] == str(plant.bom)
        assert (data["component_count"], data["work_order_count"]) == (2, 1)
        assert data["fully_reserved"] is True
        assert data["shortages"] == []
        frozen = await components(session, order_id)
        assert [(row.item_id, row.required_quantity) for row in frozen] == [
            (plant.body, Decimal("10.0000")),
            (plant.screw, Decimal("63.0000")),
        ]
        held = await reservations_for(session, order_id)
        assert sorted((row.item_id, row.quantity, row.status) for row in held) == sorted(
            [
                (plant.body, Decimal("10.0000"), ReservationStatus.ACTIVE),
                (plant.screw, Decimal("63.0000"), ReservationStatus.ACTIVE),
            ]
        )
        screw = await stock_at(session, tenant.company_id, plant.screw, plant.warehouse)
        assert screw is not None
        assert tuple(screw) == (Decimal("1000"), Decimal("63"), Decimal("937"))
        work_order_id = await work_order_of(session, order_id)
        assert await column(session, WorkOrder.status, work_order_id) == "PENDING"
        (released,) = await audit_rows(
            session, tenant.company_id, order_id, AuditAction.PRODUCTION_STATUS_CHANGE
        )
        assert moves([released]) == [("APPROVED", "RELEASED")]
        assert released.context["reason"] == "Staged"

    async def test_a_short_component_is_reported_and_what_exists_is_held(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        # 200 kits need 1260 screws; the shelf has 1000.
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.APPROVED, quantity="200"
        )

        data = ok(await api_client.post(f"/production/{order_id}/release", json={}))

        assert data["order"]["status"] == "RELEASED"
        assert data["fully_reserved"] is False
        (short,) = data["shortages"]
        assert short["item_id"] == str(plant.screw)
        assert Decimal(short["required_quantity"]) == Decimal("1260")
        assert Decimal(short["reserved_quantity"]) == Decimal("1000")
        assert Decimal(short["shortage_quantity"]) == Decimal("260")
        screw = await stock_at(session, tenant.company_id, plant.screw, plant.warehouse)
        assert screw is not None
        assert screw.available_quantity == Decimal("0")

    async def test_a_component_store_from_another_company_is_not_found(
        self, api_client: AsyncClient, session, tenant, principal, plant, foreign
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.APPROVED)
        theirs = await seed_support.seed_warehouse(
            session, company_id=foreign.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
        )
        theirs_id = theirs.id

        response = await api_client.post(
            f"/production/{order_id}/release", json={"component_warehouse_id": str(theirs_id)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, ProductionOrder.status, order_id) == "APPROVED"
        assert await components(session, order_id) == []

    async def test_a_draft_cannot_be_released(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        response = await api_client.post(f"/production/{order_id}/release", json={})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await components(session, order_id) == []
        assert await reservations_for(session, order_id) == []

    async def test_a_malformed_as_of_is_a_field_error(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.APPROVED)

        response = await api_client.post(
            f"/production/{order_id}/release", json={"as_of": "next week"}
        )

        assert "as_of" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/{order_id}/start
# -----------------------------------------------------------------------------


class TestStartOrder:
    async def test_an_issued_job_goes_into_process_on_the_stated_day(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        started = today() - timedelta(days=1)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/start",
                json={"actual_start_date": started.isoformat(), "note": "Line 2 picked it up"},
            )
        )

        assert data["status"] == "IN_PROCESS"
        assert data["actual_start_date"] == started.isoformat()
        assert await column(session, ProductionOrder.actual_start_date, order_id) == started
        changes = await audit_rows(
            session, tenant.company_id, order_id, AuditAction.PRODUCTION_STATUS_CHANGE
        )
        assert moves(changes)[-1] == ("MATERIAL_ISSUED", "IN_PROCESS")
        assert changes[-1].context["reason"] == "Line 2 picked it up"

    async def test_a_job_without_its_material_cannot_start(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)

        response = await api_client.post(f"/production/{order_id}/start", json={})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, ProductionOrder.status, order_id) == "RELEASED"
        assert await column(session, ProductionOrder.actual_start_date, order_id) is None


# -----------------------------------------------------------------------------
# POST /production/{order_id}/close
# -----------------------------------------------------------------------------


class TestCloseOrder:
    async def test_a_completed_job_is_costed_and_the_cost_is_stored(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.COMPLETED)

        data = ok(
            await api_client.post(f"/production/{order_id}/close", json={"closure_note": "Clean"})
        )

        assert data["order"]["status"] == "CLOSED"
        assert data["order"]["closure_note"] == "Clean"
        # Ten bodies at 40 and sixty-three screws at 5, at the value they left at.
        assert Decimal(data["cost"]["material_cost"]) == Decimal("715.00")
        assert data["cost"]["is_final"] is True
        assert await column(session, ProductionOrder.status, order_id) == "CLOSED"
        assert await column(session, ProductionOrder.total_material_cost, order_id) == Decimal(
            "715.00"
        )
        assert await column(session, ProductionOrder.total_cost, order_id) == Decimal(
            data["cost"]["total_cost"]
        )
        changes = await audit_rows(
            session, tenant.company_id, order_id, AuditAction.PRODUCTION_STATUS_CHANGE
        )
        assert moves(changes)[-1] == ("COMPLETED", "CLOSED")

    async def test_a_job_still_in_process_cannot_close(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.IN_PROCESS)

        response = await api_client.post(f"/production/{order_id}/close", json={})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, ProductionOrder.closure_note, order_id) is None

    async def test_an_overlong_closure_note_is_a_field_error(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.COMPLETED)

        response = await api_client.post(
            f"/production/{order_id}/close", json={"closure_note": "x" * 4001}
        )

        assert "closure_note" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/{order_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelOrder:
    async def test_a_released_job_gives_its_stock_and_operations_back(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/cancel", json={"reason": "Customer withdrew"}
            )
        )

        assert data["status"] == "CANCELLED"
        assert await column(session, ProductionOrder.status, order_id) == "CANCELLED"
        assert {row.status for row in await reservations_for(session, order_id)} == {
            ReservationStatus.RELEASED
        }
        body = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert body is not None
        assert body.reserved_quantity == Decimal("0")
        assert await column(session, WorkOrder.status, work_order_id) == "CANCELLED"
        cancels = await audit_rows(session, tenant.company_id, order_id, AuditAction.CANCEL)
        (moved,) = [row for row in cancels if (row.old_values or {}).get("status")]
        assert moves([moved]) == [("RELEASED", "CANCELLED")]
        assert moved.context["reason"] == "Customer withdrew"

    async def test_cancelling_a_draft_rejects_its_pending_approval(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/cancel", json={"reason": "Raised in error"}
            )
        )

        assert data["status"] == "CANCELLED"
        queue = (
            await session.execute(
                select(ApprovalRequest.status, ApprovalRequest.decision_note).where(
                    ApprovalRequest.entity_id == order_id
                )
            )
        ).one()
        assert tuple(queue) == ("REJECTED", "Raised in error")

    async def test_a_job_still_holding_material_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )

        response = await api_client.post(
            f"/production/{order_id}/cancel", json={"reason": "Changed our mind"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, ProductionOrder.status, order_id) == "MATERIAL_ISSUED"

    async def test_a_job_under_way_cannot_be_cancelled(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.IN_PROCESS)

        response = await api_client.post(
            f"/production/{order_id}/cancel", json={"reason": "Changed our mind"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_a_cancellation_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.DRAFT)

        response = await api_client.post(f"/production/{order_id}/cancel", json={"reason": "no"})

        assert "reason" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/{order_id}/material-issues and /material-returns
# -----------------------------------------------------------------------------


class TestDraftMaterialDocuments:
    async def test_an_issue_is_drafted_and_nothing_moves(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, screw = await components(session, order_id)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/material-issues",
                json=issue_body((body.id, "10"), (screw.id, "63")),
            ),
            201,
        )

        assert (data["issue_type"], data["status"]) == ("ISSUE", "DRAFT")
        assert data["production_order_id"] == str(order_id)
        assert data["line_count"] == 2
        assert Decimal(data["total_quantity"]) == Decimal("73")
        assert [line["component_id"] for line in data["lines"]] == [str(body.id), str(screw.id)]
        issue_id = uuid.UUID(data["id"])
        assert await column(session, MaterialIssue.status, issue_id) == "DRAFT"
        moved = await session.scalar(
            select(InventoryTransaction.id).where(InventoryTransaction.reference_id == issue_id)
        )
        assert moved is None
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert shelf.on_hand_quantity == Decimal("1000.0000")
        (created,) = await audit_rows(session, tenant.company_id, issue_id, AuditAction.CREATE)
        assert created.entity_type == "material_issue"

    async def test_a_return_is_drafted_against_what_the_job_holds(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        body, _ = await components(session, order_id)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/material-returns", json=issue_body((body.id, "4"))
            ),
            201,
        )

        assert (data["issue_type"], data["status"]) == ("RETURN", "DRAFT")
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert shelf.on_hand_quantity == Decimal("990.0000")

    async def test_returning_more_than_the_job_holds_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        body, _ = await components(session, order_id)

        response = await api_client.post(
            f"/production/{order_id}/material-returns", json=issue_body((body.id, "11"))
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_material_moves_only_against_a_released_job(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.APPROVED)

        response = await api_client.post(
            f"/production/{order_id}/material-issues", json=issue_body((uuid.uuid4(), "1"))
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_a_component_that_is_not_on_the_job_is_not_found(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)

        response = await api_client.post(
            f"/production/{order_id}/material-issues", json=issue_body((uuid.uuid4(), "1"))
        )

        refused(response, 404, "NOT_FOUND")

    async def test_issuing_past_the_tolerance_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)

        # Ten required, eleven allowed at the 10% tolerance.
        response = await api_client.post(
            f"/production/{order_id}/material-issues", json=issue_body((body.id, "12"))
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    @pytest.mark.parametrize(
        ("shape", "field"),
        [
            ("no-lines", "lines"),
            ("one-component-twice", "_root"),
            ("zero-quantity", "lines.0.quantity"),
            ("no-reason", "reason"),
        ],
    )
    async def test_an_invalid_document_names_the_field(
        self, api_client: AsyncClient, session, tenant, principal, plant, shape, field
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        payload = {
            "no-lines": {"reason": REASON, "lines": []},
            "one-component-twice": issue_body((body.id, "1"), (body.id, "2")),
            "zero-quantity": issue_body((body.id, "0")),
            "no-reason": {"lines": issue_body((body.id, "1"))["lines"]},
        }[shape]

        response = await api_client.post(f"/production/{order_id}/material-issues", json=payload)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/material-issues/{issue_id}/post
# -----------------------------------------------------------------------------


class TestPostMaterialIssue:
    async def test_a_full_kit_draws_the_reservations_and_advances_the_job(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, screw = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10", screw.id: "63"})

        data = ok(await api_client.post(f"/production/material-issues/{issue_id}/post", json={}))

        assert data["issue"]["status"] == "POSTED"
        assert data["order_status"] == "MATERIAL_ISSUED"
        assert data["outstanding_components"] == []
        assert sorted((m["item_id"], m["transaction_type"]) for m in data["movements"]) == sorted(
            [(str(plant.body), "MATERIAL_ISSUE"), (str(plant.screw), "MATERIAL_ISSUE")]
        )
        assert Decimal(data["issue"]["total_cost"]) == Decimal("715.00")
        assert await column(session, MaterialIssue.posted_by_id, issue_id) == principal.user_id
        assert await column(session, ProductionOrder.status, order_id) == "MATERIAL_ISSUED"
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert tuple(shelf) == (Decimal("990"), Decimal("0"), Decimal("990"))
        assert {row.status for row in await reservations_for(session, order_id)} == {
            ReservationStatus.CONSUMED
        }
        posted = await audit_rows(
            session, tenant.company_id, issue_id, AuditAction.INVENTORY_MOVEMENT
        )
        assert posted and {row.entity_type for row in posted} == {"material_issue"}

    async def test_a_return_puts_the_material_back_on_the_shelf(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        body, _ = await components(session, order_id)
        return_id = await drafted_issue(
            session, tenant, order_id, {body.id: "4"}, issue_type=MaterialIssueType.RETURN
        )

        data = ok(await api_client.post(f"/production/material-issues/{return_id}/post", json={}))

        assert data["issue"]["status"] == "POSTED"
        assert data["order_status"] == "MATERIAL_ISSUED"
        assert [m["transaction_type"] for m in data["movements"]] == ["MATERIAL_RETURN"]
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert shelf.on_hand_quantity == Decimal("994.0000")
        (returned,) = [row for row in await components(session, order_id) if row.id == body.id]
        assert returned.returned_quantity == Decimal("4.0000")

    @pytest.mark.parametrize("forced", [False, True], ids=["short", "forced"])
    async def test_a_short_kit_leaves_the_job_released_unless_forced(
        self, api_client: AsyncClient, session, tenant, principal, plant, forced
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, screw = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})
        payload = {"allow_partial_issue": True, "override_reason": "Screws at the line"}

        data = ok(
            await api_client.post(
                f"/production/material-issues/{issue_id}/post", json=payload if forced else {}
            )
        )

        expected = "MATERIAL_ISSUED" if forced else "RELEASED"
        assert data["order_status"] == expected
        (short,) = data["outstanding_components"]
        assert short["component_id"] == str(screw.id)
        assert Decimal(short["shortage_quantity"]) == Decimal("63")
        assert await column(session, ProductionOrder.status, order_id) == expected

    async def test_a_posted_document_cannot_be_posted_again(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})
        ok(await api_client.post(f"/production/material-issues/{issue_id}/post", json={}))

        response = await api_client.post(f"/production/material-issues/{issue_id}/post", json={})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert shelf.on_hand_quantity == Decimal("990.0000")

    async def test_posting_a_return_needs_the_return_right(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        body, _ = await components(session, order_id)
        return_id = await drafted_issue(
            session, tenant, order_id, {body.id: "4"}, issue_type=MaterialIssueType.RETURN
        )
        act_as(holding(principal, Permission.MATERIAL_ISSUE_CREATE))

        response = await api_client.post(f"/production/material-issues/{return_id}/post", json={})

        error = refused(response, 403, "PERMISSION_DENIED")
        assert error["details"]["required_permission"] == "material_return.create"
        assert await column(session, MaterialIssue.status, return_id) == "DRAFT"
        shelf = await stock_at(session, tenant.company_id, plant.body, plant.warehouse)
        assert shelf is not None
        assert shelf.on_hand_quantity == Decimal("990.0000")

    async def test_forcing_a_partial_issue_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})

        response = await api_client.post(
            f"/production/material-issues/{issue_id}/post", json={"allow_partial_issue": True}
        )

        assert "_root" in field_errors(response)
        assert await column(session, MaterialIssue.status, issue_id) == "DRAFT"


# -----------------------------------------------------------------------------
# POST /production/material-issues/{issue_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelMaterialIssue:
    async def test_a_draft_is_cancelled_with_its_reason(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})

        data = ok(
            await api_client.post(
                f"/production/material-issues/{issue_id}/cancel",
                json={"reason": "Picked the wrong kit"},
            )
        )

        assert data["status"] == "CANCELLED"
        assert await column(session, MaterialIssue.status, issue_id) == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, issue_id, AuditAction.CANCEL)
        assert moves([cancel]) == [("DRAFT", "CANCELLED")]
        assert cancel.context["reason"] == "Picked the wrong kit"

    async def test_a_posted_document_is_reversed_by_a_return_not_cancelled(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})
        await service.post_material_issue(
            session, tenant, principal, issue_id, MaterialIssuePostRequest()
        )

        response = await api_client.post(
            f"/production/material-issues/{issue_id}/cancel", json={"reason": "Too late"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, MaterialIssue.status, issue_id) == "POSTED"

    async def test_a_cancellation_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        body, _ = await components(session, order_id)
        issue_id = await drafted_issue(session, tenant, order_id, {body.id: "10"})

        response = await api_client.post(f"/production/material-issues/{issue_id}/cancel", json={})

        assert "reason" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/{order_id}/entries
# -----------------------------------------------------------------------------


class TestBookProduction:
    async def test_a_booking_on_the_job_receives_the_output_into_stock(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )

        data = ok(
            await api_client.post(
                f"/production/{order_id}/entries",
                json={"produced_quantity": "4", "notes": "First shift"},
            ),
            201,
        )

        assert Decimal(data["entry"]["produced_quantity"]) == Decimal("4")
        assert data["entry"]["work_order_id"] is None
        assert data["output_movement"]["transaction_type"] == "PRODUCTION_RECEIPT"
        assert Decimal(data["output_movement"]["quantity"]) == Decimal("4")
        assert Decimal(data["order"]["produced_quantity"]) == Decimal("4")
        entry_id = uuid.UUID(data["entry"]["id"])
        assert await column(session, ProductionEntry.produced_quantity, entry_id) == Decimal("4")
        output = await stock_at(session, tenant.company_id, plant.kit, plant.warehouse)
        assert output is not None
        assert output.on_hand_quantity == Decimal("4.0000")
        (created,) = await audit_rows(session, tenant.company_id, entry_id, AuditAction.CREATE)
        assert created.entity_type == "production_entry"
        assert created.new_values["posted_output"] is True

    async def test_a_booking_at_the_last_operation_starts_it_and_counts_on_it(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )
        work_order_id = await work_order_of(session, order_id)

        data = ok(
            await api_client.post(
                f"/production/{order_id}/entries",
                json={"work_order_id": str(work_order_id), "produced_quantity": "3"},
            ),
            201,
        )

        assert data["entry"]["work_order_id"] == str(work_order_id)
        assert data["output_movement"] is not None
        assert await column(session, WorkOrder.status, work_order_id) == "IN_PROGRESS"
        assert await column(session, WorkOrder.completed_quantity, work_order_id) == Decimal("3")

    async def test_a_job_without_its_material_cannot_book_output(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)

        response = await api_client.post(
            f"/production/{order_id}/entries", json={"produced_quantity": "1"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await stock_at(session, tenant.company_id, plant.kit, plant.warehouse) is None

    async def test_output_past_the_tolerance_is_refused_and_nothing_is_received(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )

        # Ten planned, eleven allowed at the 10% tolerance.
        response = await api_client.post(
            f"/production/{order_id}/entries", json={"produced_quantity": "12"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await stock_at(session, tenant.company_id, plant.kit, plant.warehouse) is None
        assert (
            await session.scalar(
                select(ProductionEntry.id).where(ProductionEntry.production_order_id == order_id)
            )
            is None
        )

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({}, "_root"),
            ({"rejected_quantity": "2"}, "_root"),
            ({"produced_quantity": "-1"}, "produced_quantity"),
            ({"produced_quantity": "2", "output_serial_numbers": ["SN-1"]}, "_root"),
        ],
        ids=["records-nothing", "rejection-without-reason", "negative", "serial-count"],
    )
    async def test_an_invalid_booking_names_the_field(
        self, api_client: AsyncClient, session, tenant, principal, plant, body, field
    ) -> None:
        order_id = await walked(
            session, tenant, principal, plant, ProductionOrderStatus.MATERIAL_ISSUED
        )

        response = await api_client.post(f"/production/{order_id}/entries", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# PATCH /production/work-orders/{work_order_id}
# -----------------------------------------------------------------------------


class TestAssignWorkOrder:
    async def test_an_operation_is_staffed_and_scheduled_and_the_change_audited(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        machine = await seed_support.seed_machine(
            session, company_id=tenant.company_id, work_center_id=plant.work_centre
        )
        shift = await seed_support.seed_shift(session, company_id=tenant.company_id)
        machine_id, shift_id, shift_code = machine.id, shift.id, shift.code
        start = (utcnow() + timedelta(days=3)).replace(microsecond=0)

        data = ok(
            await api_client.patch(
                f"/production/work-orders/{work_order_id}",
                json={
                    "operator_user_id": str(maker.user_id),
                    "machine_id": str(machine_id),
                    "shift_id": str(shift_id),
                    "planned_start_at": start.isoformat(),
                    "planned_end_at": (start + timedelta(hours=2)).isoformat(),
                    "notes": "Second lathe",
                },
            )
        )

        assert data["operator_user_id"] == str(maker.user_id)
        assert data["machine_id"] == str(machine_id)
        assert (data["shift_id"], data["shift"]) == (str(shift_id), shift_code)
        assert datetime.fromisoformat(data["planned_start_at"]) == start
        assert data["status"] == "PENDING"
        assert await column(session, WorkOrder.operator_user_id, work_order_id) == maker.user_id
        (update,) = await audit_rows(session, tenant.company_id, work_order_id, AuditAction.UPDATE)
        assert update.entity_type == "work_order"
        assert {"operator_user_id", "machine_id", "shift_id", "shift", "notes"} <= set(
            update.changed_fields
        )

    async def test_a_finish_before_the_start_is_refused_and_nothing_changes(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        before = await column(session, WorkOrder.planned_start_at, work_order_id)
        start = (utcnow() + timedelta(days=3)).replace(microsecond=0)

        response = await api_client.patch(
            f"/production/work-orders/{work_order_id}",
            json={
                "planned_start_at": start.isoformat(),
                "planned_end_at": (start - timedelta(hours=1)).isoformat(),
            },
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, WorkOrder.planned_start_at, work_order_id) == before

    async def test_a_machine_from_another_work_centre_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        elsewhere = await seed_support.seed_work_center(session, company_id=tenant.company_id)
        machine = await seed_support.seed_machine(
            session, company_id=tenant.company_id, work_center_id=elsewhere.id
        )
        machine_id = machine.id

        response = await api_client.patch(
            f"/production/work-orders/{work_order_id}", json={"machine_id": str(machine_id)}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, WorkOrder.machine_id, work_order_id) is None

    @pytest.mark.parametrize("reference", ["operator_user_id", "machine_id", "shift_id"])
    async def test_another_companys_reference_is_not_found(
        self, api_client: AsyncClient, session, tenant, principal, plant, foreign, reference
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        if reference == "operator_user_id":
            theirs = foreign.user_id
        elif reference == "machine_id":
            centre = await seed_support.seed_work_center(session, company_id=foreign.company_id)
            theirs = (
                await seed_support.seed_machine(
                    session, company_id=foreign.company_id, work_center_id=centre.id
                )
            ).id
        else:
            theirs = (await seed_support.seed_shift(session, company_id=foreign.company_id)).id

        response = await api_client.patch(
            f"/production/work-orders/{work_order_id}", json={reference: str(theirs)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, getattr(WorkOrder, reference), work_order_id) is None

    async def test_a_completed_operation_can_no_longer_be_changed(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        for target in (WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.COMPLETED):
            await service.transition_work_order(
                session, tenant, work_order_id, WorkOrderTransitionRequest(status=target)
            )

        response = await api_client.patch(
            f"/production/work-orders/{work_order_id}",
            json={"operator_user_id": str(maker.user_id)},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_an_operation_of_a_cancelled_job_can_no_longer_be_changed(
        self, api_client: AsyncClient, session, tenant, principal, plant, maker
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        await service.cancel_order(
            session, tenant, order_id, CancelRequest(reason="Customer withdrew")
        )

        response = await api_client.patch(
            f"/production/work-orders/{work_order_id}",
            json={"operator_user_id": str(maker.user_id)},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, WorkOrder.operator_user_id, work_order_id) is None

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"planned_start_at": "after lunch"}, "planned_start_at"),
            ({"completed_quantity": "5"}, "completed_quantity"),
        ],
        ids=["malformed-time", "quantities-move-only-through-bookings"],
    )
    async def test_an_invalid_patch_names_the_field(
        self, api_client: AsyncClient, session, tenant, principal, plant, body, field
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)

        response = await api_client.patch(f"/production/work-orders/{work_order_id}", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /production/work-orders/{work_order_id}/status
# -----------------------------------------------------------------------------


class TestTransitionWorkOrder:
    async def test_an_operation_is_started_and_finished_at_the_stated_moments(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        began = (utcnow() - timedelta(hours=3)).replace(microsecond=0)
        path = f"/production/work-orders/{work_order_id}/status"

        started = ok(
            await api_client.post(
                path,
                json={
                    "status": "IN_PROGRESS",
                    "occurred_at": began.isoformat(),
                    "reason": "Setter on shift",
                },
            )
        )
        finished = ok(
            await api_client.post(
                path,
                json={
                    "status": "COMPLETED",
                    "occurred_at": (began + timedelta(minutes=90)).isoformat(),
                },
            )
        )

        assert started["status"] == "IN_PROGRESS"
        assert datetime.fromisoformat(started["actual_start_at"]) == began
        assert finished["status"] == "COMPLETED"
        assert datetime.fromisoformat(finished["actual_end_at"]) == began + timedelta(minutes=90)
        assert await column(session, WorkOrder.status, work_order_id) == "COMPLETED"
        changes = await audit_rows(
            session, tenant.company_id, work_order_id, AuditAction.PRODUCTION_STATUS_CHANGE
        )
        assert moves(changes) == [("PENDING", "IN_PROGRESS"), ("IN_PROGRESS", "COMPLETED")]
        assert changes[0].context["reason"] == "Setter on shift"

    async def test_a_completed_operation_cannot_be_reopened(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        for target in (WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.COMPLETED):
            await service.transition_work_order(
                session, tenant, work_order_id, WorkOrderTransitionRequest(status=target)
            )

        response = await api_client.post(
            f"/production/work-orders/{work_order_id}/status", json={"status": "IN_PROGRESS"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, WorkOrder.status, work_order_id) == "COMPLETED"

    async def test_an_operation_cannot_finish_before_it_started(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)
        began = (utcnow() - timedelta(hours=1)).replace(microsecond=0)
        await service.transition_work_order(
            session,
            tenant,
            work_order_id,
            WorkOrderTransitionRequest(status=WorkOrderStatus.IN_PROGRESS, occurred_at=began),
        )

        response = await api_client.post(
            f"/production/work-orders/{work_order_id}/status",
            json={"status": "COMPLETED", "occurred_at": (began - timedelta(hours=1)).isoformat()},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, WorkOrder.status, work_order_id) == "IN_PROGRESS"

    async def test_an_unknown_status_is_a_field_error(
        self, api_client: AsyncClient, session, tenant, principal, plant
    ) -> None:
        order_id = await walked(session, tenant, principal, plant, ProductionOrderStatus.RELEASED)
        work_order_id = await work_order_of(session, order_id)

        response = await api_client.post(
            f"/production/work-orders/{work_order_id}/status", json={"status": "PAUSED"}
        )

        assert "status" in field_errors(response)
