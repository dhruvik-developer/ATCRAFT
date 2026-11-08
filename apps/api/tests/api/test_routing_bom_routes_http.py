"""Routing and BOM write routes over HTTP.

Service rules for work centres, machines, shifts, the roster, routings and
bills of materials are pinned in the integration suites listed in the
routing-bom campaign brief. These drive the twenty-four write routes no
backend test had reached through HTTP: the status and envelope each answers
with, the row and audit entry left behind, the 422 a payload or a record's
state can earn, and the 404 another company's record answers with - never a
403, which would confirm that it exists.

Permission gates on the route itself are covered by
``tests/unit/test_route_authorization.py``. The caller is the ``api_client``
principal - every permission, not a superuser - except where maker-checker
approval needs a second actor. Prerequisites are built through the routing
and BOM services (or ``seed_support`` for plain item / shift / centre rows),
so every fixture has the shape production gives it. Ids are held as plain
values: an expected failure rolls its savepoint back and expires what was
loaded before it.
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom import service as bom_service
from src.modules.bom.models import Bom, BomStatus
from src.modules.bom.schemas import BomCreate, BomLineIn
from src.modules.products.models import Item, ItemGroup, ProductionType
from src.modules.routing.models import (
    Machine,
    MachineStatus,
    Routing,
    RoutingStatus,
    Shift,
    ShiftAssignment,
    ShiftAssignmentStatus,
    WorkCenter,
)
from src.modules.routing.schemas import (
    MachineCreate,
    MachineStatusChange,
    RoutingCreate,
    RoutingOperationIn,
    ShiftAssignmentCreate,
    ShiftCreate,
    WorkCenterCreate,
)
from src.modules.routing.service import (
    MachineService,
    RoutingService,
    ShiftRosterService,
    ShiftService,
    WorkCenterService,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Checked against the drawing"
DAY = today() + timedelta(days=5)


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's masters, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


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


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read past the identity map."""
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def count_codes(session: AsyncSession, model: Any, company_id: uuid.UUID, code: str) -> int:
    stmt = (
        select(func.count())
        .select_from(model)
        .where(model.company_id == company_id, model.code == code)
    )
    return int((await session.execute(stmt)).scalar_one())


async def make_work_center(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> Any:
    return await WorkCenterService(session, tenant).create_work_center(
        WorkCenterCreate(code=code, name="Machining cell", **fields)
    )


async def make_machine(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    work_center_id: uuid.UUID,
    **fields: Any,
) -> Any:
    return await MachineService(session, tenant).create_machine(
        MachineCreate(code=code, name="CNC lathe", work_center_id=work_center_id, **fields)
    )


async def make_shift(session: AsyncSession, tenant: TenantContext, code: str, **fields: Any) -> Any:
    body: dict[str, Any] = {
        "code": code,
        "name": "Morning",
        "start_time": time(6),
        "end_time": time(14),
    }
    body.update(fields)
    return await ShiftService(session, tenant).create_shift(ShiftCreate(**body))


async def plan_assignment(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    shift_id: uuid.UUID,
    work_center_id: uuid.UUID,
    operator_user_id: uuid.UUID,
    day: date = DAY,
    machine_id: uuid.UUID | None = None,
    notes: str | None = None,
) -> Any:
    rows, _ = await ShiftRosterService(session, tenant).plan(
        [
            ShiftAssignmentCreate(
                roster_date=day,
                shift_id=shift_id,
                work_center_id=work_center_id,
                machine_id=machine_id,
                operator_user_id=operator_user_id,
                notes=notes,
            )
        ]
    )
    return rows[0]


async def make_parent_item(session: AsyncSession, tenant: TenantContext, code: str) -> Item:
    return await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=code,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        can_purchase=False,
    )


async def make_component_item(session: AsyncSession, tenant: TenantContext, code: str) -> Item:
    return await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=code,
        item_group=ItemGroup.COMPONENT,
        can_be_bom_component=True,
    )


def operation_body(work_center_id: uuid.UUID, *, sequence: int = 10) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "operation_code": f"OP{sequence}",
        "operation_name": f"Operation {sequence}",
        "work_center_id": str(work_center_id),
        "setup_time_minutes": "30",
        "run_time_per_unit_minutes": "1.5",
    }


async def make_routing(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    item_id: uuid.UUID,
    work_center_id: uuid.UUID,
    **fields: Any,
) -> Any:
    payload: dict[str, Any] = {
        "code": code,
        "item_id": item_id,
        "operations": [
            RoutingOperationIn(
                sequence=10,
                operation_code="OP10",
                operation_name="Machine",
                work_center_id=work_center_id,
                setup_time_minutes=Decimal("30"),
                run_time_per_unit_minutes=Decimal("1.5"),
            )
        ],
    }
    payload.update(fields)
    return await RoutingService(session, tenant).create_routing(RoutingCreate(**payload))


async def make_approved_routing(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    code: str,
    item_id: uuid.UUID,
    work_center_id: uuid.UUID,
) -> Any:
    """Create under a second user, approve as ``principal`` - maker-checker."""
    maker = await seed_support.seed_user(session, company_id=tenant.company_id)
    maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
    draft = await make_routing(session, maker_tenant, code, item_id, work_center_id)
    return await RoutingService(session, tenant).approve_routing(principal, draft.id, reason=REASON)


def bom_line(
    component_id: uuid.UUID, *, line_number: int = 1, quantity: str = "4"
) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "component_item_id": str(component_id),
        "quantity_per": quantity,
    }


async def make_bom(
    session: AsyncSession,
    tenant: TenantContext,
    parent: Item,
    component: Item,
    **fields: Any,
) -> Bom:
    payload = BomCreate(
        item_id=parent.id,
        lines=[
            BomLineIn(
                line_number=1,
                component_item_id=component.id,
                quantity_per=Decimal("4"),
            )
        ],
        **fields,
    )
    bom, _, _ = await bom_service.create_bom(session, tenant, payload)
    return bom


async def make_submitted_bom(
    session: AsyncSession,
    tenant: TenantContext,
    parent: Item,
    component: Item,
) -> Bom:
    bom = await make_bom(session, tenant, parent, component)
    return await bom_service.submit_bom(session, tenant, bom.id, reason="Ready for review")


async def make_approved_bom(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    parent: Item,
    component: Item,
) -> Bom:
    maker = await seed_support.seed_user(session, company_id=tenant.company_id)
    maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
    submitted = await make_submitted_bom(session, maker_tenant, parent, component)
    return await bom_service.approve_bom(session, tenant, principal, submitted.id, reason=REASON)


# -----------------------------------------------------------------------------
# POST /routing/work-centers
# -----------------------------------------------------------------------------


class TestCreateWorkCenter:
    async def test_a_work_centre_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("wc").lower()

        response = await api_client.post(
            "/routing/work-centers",
            json={
                "code": f"  {raw} ",
                "name": "Plating cell",
                "cost_per_hour": "450.00",
                "working_hours_per_day": "8",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "Plating cell"
        assert data["is_active"] is True
        assert Decimal(data["cost_per_hour"]) == Decimal("450.00")
        centre_id = uuid.UUID(data["id"])
        assert await column(session, WorkCenter.company_id, centre_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, centre_id, AuditAction.CREATE)
        assert created.entity_type == "work_center"
        assert created.created_by_id == tenant.user_id
        read = ok(await api_client.get(f"/routing/work-centers/{centre_id}"))
        assert read["code"] == data["code"]

    async def test_a_code_already_in_use_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            "/routing/work-centers",
            json={"code": existing.code.lower(), "name": "Twin"},
        )

        refused(response, 409, "DUPLICATE_DOCUMENT")
        assert await count_codes(session, WorkCenter, tenant.company_id, existing.code) == 1

    async def test_a_work_centre_needs_a_code_and_a_name(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/routing/work-centers", json={"cost_per_hour": "10"})

        assert {"code", "name"} <= set(field_errors(response))


# -----------------------------------------------------------------------------
# PATCH /routing/work-centers/{work_center_id}
# -----------------------------------------------------------------------------


class TestUpdateWorkCenter:
    async def test_an_edit_applies_only_what_was_sent_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(
            session, tenant, unique_code("WC"), cost_per_hour=Decimal("400")
        )

        response = await api_client.patch(
            f"/routing/work-centers/{centre.id}",
            json={"name": "Plating cell (main)", "cost_per_hour": "525.50"},
        )

        data = ok(response)
        assert data["name"] == "Plating cell (main)"
        assert Decimal(data["cost_per_hour"]) == Decimal("525.50")
        assert await column(session, WorkCenter.name, centre.id) == "Plating cell (main)"
        (update,) = await audit_rows(session, tenant.company_id, centre.id, AuditAction.UPDATE)
        assert update.entity_type == "work_center"
        assert set(update.changed_fields or []) >= {"name", "cost_per_hour"}

    async def test_an_empty_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.patch(f"/routing/work-centers/{centre.id}", json={})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_work_centre_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_work_center(session, foreign, unique_code("WC"))

        response = await api_client.patch(
            f"/routing/work-centers/{theirs.id}", json={"name": "Taken over"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, WorkCenter.name, theirs.id) == "Machining cell"


# -----------------------------------------------------------------------------
# POST /routing/work-centers/{id}/activate|deactivate
# -----------------------------------------------------------------------------


class TestWorkCenterLifecycle:
    async def test_deactivate_and_activate_are_status_changes(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        path = f"/routing/work-centers/{centre.id}"

        off = ok(await api_client.post(f"{path}/deactivate", json={"reason": "Line remodel"}))
        assert off["is_active"] is False
        assert await column(session, WorkCenter.is_active, centre.id) is False

        on = ok(await api_client.post(f"{path}/activate", json={"reason": "Remodel complete"}))
        assert on["is_active"] is True
        changes = await audit_rows(session, tenant.company_id, centre.id, AuditAction.STATUS_CHANGE)
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]
        assert changes[0].context["reason"] == "Line remodel"

    async def test_activating_an_already_active_centre_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            f"/routing/work-centers/{centre.id}/activate",
            json={"reason": "Already live"},
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_a_short_reason_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            f"/routing/work-centers/{centre.id}/deactivate", json={"reason": "no"}
        )

        assert "reason" in field_errors(response)

    async def test_another_companys_lifecycle_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_work_center(session, foreign, unique_code("WC"))

        response = await api_client.post(
            f"/routing/work-centers/{theirs.id}/deactivate",
            json={"reason": "Taken over"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, WorkCenter.is_active, theirs.id) is True


# -----------------------------------------------------------------------------
# POST /routing/machines and PATCH /routing/machines/{machine_id}
# -----------------------------------------------------------------------------


class TestCreateMachine:
    async def test_a_machine_is_created_available_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        raw = unique_code("mc").lower()

        response = await api_client.post(
            "/routing/machines",
            json={
                "code": f"  {raw} ",
                "name": "CNC lathe 2",
                "work_center_id": str(centre.id),
                "machine_type": "CNC",
            },
        )

        data = ok(response, 201)
        assert data["code"] == raw.upper()
        assert data["status"] == "AVAILABLE"
        assert data["is_active"] is True
        assert data["work_center_id"] == str(centre.id)
        machine_id = uuid.UUID(data["id"])
        assert await column(session, Machine.work_center_id, machine_id) == centre.id
        (created,) = await audit_rows(session, tenant.company_id, machine_id, AuditAction.CREATE)
        assert created.entity_type == "machine"

    async def test_a_work_centre_of_another_company_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_work_center(session, foreign, unique_code("WC"))
        code = unique_code("MC")

        response = await api_client.post(
            "/routing/machines",
            json={"code": code, "name": "Borrowed", "work_center_id": str(theirs.id)},
        )

        refused(response, 404, "NOT_FOUND")
        assert await count_codes(session, Machine, tenant.company_id, code) == 0

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        existing = await make_machine(session, tenant, unique_code("MC"), centre.id)

        response = await api_client.post(
            "/routing/machines",
            json={
                "code": existing.code.lower(),
                "name": "Twin",
                "work_center_id": str(centre.id),
            },
        )

        refused(response, 409, "DUPLICATE_DOCUMENT")

    async def test_next_service_before_last_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            "/routing/machines",
            json={
                "code": unique_code("MC"),
                "name": "Lathe",
                "work_center_id": str(centre.id),
                "last_maintenance_date": "2026-06-01",
                "next_maintenance_date": "2026-05-01",
            },
        )

        assert field_errors(response)  # model-level ordering message


class TestUpdateMachine:
    async def test_an_edit_moves_the_machine_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        first = await make_work_center(session, tenant, unique_code("WC"))
        second = await make_work_center(session, tenant, unique_code("WC"))
        machine = await make_machine(session, tenant, unique_code("MC"), first.id)

        response = await api_client.patch(
            f"/routing/machines/{machine.id}",
            json={"name": "CNC lathe (moved)", "work_center_id": str(second.id)},
        )

        data = ok(response)
        assert data["name"] == "CNC lathe (moved)"
        assert data["work_center_id"] == str(second.id)
        assert await column(session, Machine.work_center_id, machine.id) == second.id
        (update,) = await audit_rows(session, tenant.company_id, machine.id, AuditAction.UPDATE)
        assert update.entity_type == "machine"

    async def test_another_companys_machine_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        centre = await make_work_center(session, foreign, unique_code("WC"))
        theirs = await make_machine(session, foreign, unique_code("MC"), centre.id)

        response = await api_client.patch(
            f"/routing/machines/{theirs.id}", json={"name": "Taken over"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Machine.name, theirs.id) == "CNC lathe"


# -----------------------------------------------------------------------------
# POST /routing/machines/{machine_id}/status
# -----------------------------------------------------------------------------


class TestChangeMachineStatus:
    async def test_available_to_maintenance_is_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        machine = await make_machine(session, tenant, unique_code("MC"), centre.id)
        next_service = (today() + timedelta(days=90)).isoformat()

        response = await api_client.post(
            f"/routing/machines/{machine.id}/status",
            json={
                "status": "MAINTENANCE",
                "reason": "Quarterly service",
                "next_maintenance_date": next_service,
            },
        )

        data = ok(response)
        assert data["status"] == "MAINTENANCE"
        assert await column(session, Machine.status, machine.id) == MachineStatus.MAINTENANCE
        (change,) = await audit_rows(
            session, tenant.company_id, machine.id, AuditAction.STATUS_CHANGE
        )
        assert change.old_values["status"] == "AVAILABLE"
        assert change.new_values["status"] == "MAINTENANCE"
        assert change.context["reason"] == "Quarterly service"

    async def test_an_illegal_transition_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        machine = await make_machine(session, tenant, unique_code("MC"), centre.id)

        # AVAILABLE has no direct edge to RUNNING without going through booking;
        # RUNNING is reached from AVAILABLE, but RETIRED from RUNNING is illegal.
        await MachineService(session, tenant).change_status(
            machine.id,
            MachineStatusChange(status=MachineStatus.RUNNING, reason="Job started"),
        )

        response = await api_client.post(
            f"/routing/machines/{machine.id}/status",
            json={"status": "MAINTENANCE", "reason": "Cannot service mid-job"},
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, Machine.status, machine.id) == MachineStatus.RUNNING

    async def test_another_companys_machine_status_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        centre = await make_work_center(session, foreign, unique_code("WC"))
        theirs = await make_machine(session, foreign, unique_code("MC"), centre.id)

        response = await api_client.post(
            f"/routing/machines/{theirs.id}/status",
            json={"status": "MAINTENANCE", "reason": "Taken over"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Machine.status, theirs.id) == MachineStatus.AVAILABLE


# -----------------------------------------------------------------------------
# POST /routing/shifts/{shift_id}/activate|deactivate
# -----------------------------------------------------------------------------


class TestShiftLifecycle:
    async def test_deactivate_and_activate_are_status_changes(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        shift = await make_shift(session, tenant, unique_code("S")[:8])
        path = f"/routing/shifts/{shift.id}"

        off = ok(await api_client.post(f"{path}/deactivate", json={"reason": "Seasonal"}))
        assert off["is_active"] is False
        assert await column(session, Shift.is_active, shift.id) is False

        on = ok(await api_client.post(f"{path}/activate", json={"reason": "Season starts"}))
        assert on["is_active"] is True
        changes = await audit_rows(session, tenant.company_id, shift.id, AuditAction.STATUS_CHANGE)
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]

    async def test_activating_an_already_active_shift_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        shift = await make_shift(session, tenant, unique_code("S")[:8])

        response = await api_client.post(
            f"/routing/shifts/{shift.id}/activate", json={"reason": "Already offered"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_another_companys_shift_lifecycle_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_shift(session, foreign, unique_code("S")[:8])

        response = await api_client.post(
            f"/routing/shifts/{theirs.id}/deactivate", json={"reason": "Taken over"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, Shift.is_active, theirs.id) is True


# -----------------------------------------------------------------------------
# POST /routing/roster/assignments/bulk|copy and PATCH .../{assignment_id}
# -----------------------------------------------------------------------------


class TestRosterBulkAndCopy:
    async def test_bulk_plans_several_rows_and_audits_each(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        shift_a = await make_shift(session, tenant, unique_code("A")[:8])
        shift_b = await make_shift(
            session,
            tenant,
            unique_code("B")[:8],
            start_time=time(14),
            end_time=time(22),
        )
        amit = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Amit Shah"
        )
        neha = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Neha Patel"
        )

        response = await api_client.post(
            "/routing/roster/assignments/bulk",
            json={
                "assignments": [
                    {
                        "roster_date": DAY.isoformat(),
                        "shift_id": str(shift_a.id),
                        "work_center_id": str(centre.id),
                        "operator_user_id": str(amit.id),
                    },
                    {
                        "roster_date": DAY.isoformat(),
                        "shift_id": str(shift_b.id),
                        "work_center_id": str(centre.id),
                        "operator_user_id": str(neha.id),
                    },
                ]
            },
        )

        data = ok(response, 201)
        assert len(data) == 2
        assert {row["operator_name"] for row in data} == {"Amit Shah", "Neha Patel"}
        for row in data:
            assignment_id = uuid.UUID(row["id"])
            assert (
                await column(session, ShiftAssignment.status, assignment_id)
                == ShiftAssignmentStatus.PLANNED
            )
            (created,) = await audit_rows(
                session, tenant.company_id, assignment_id, AuditAction.CREATE
            )
            assert created.entity_type == "shift_assignment"

    async def test_an_empty_bulk_payload_is_a_field_error(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            "/routing/roster/assignments/bulk", json={"assignments": []}
        )

        assert "assignments" in field_errors(response)

    async def test_copy_replays_one_day_onto_a_range(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        shift = await make_shift(session, tenant, unique_code("A")[:8])
        operator = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Amit Shah"
        )
        source = await plan_assignment(
            session,
            tenant,
            shift_id=shift.id,
            work_center_id=centre.id,
            operator_user_id=operator.id,
            day=DAY,
        )
        target_from = DAY + timedelta(days=1)
        target_to = DAY + timedelta(days=2)

        response = await api_client.post(
            "/routing/roster/assignments/copy",
            json={
                "source_date": DAY.isoformat(),
                "target_from": target_from.isoformat(),
                "target_to": target_to.isoformat(),
                "work_center_id": str(centre.id),
            },
        )

        data = ok(response, 201)
        assert len(data) == 2
        assert {row["roster_date"] for row in data} == {
            target_from.isoformat(),
            target_to.isoformat(),
        }
        assert all(row["operator_name"] == "Amit Shah" for row in data)
        assert source.id  # source row kept

    async def test_copying_onto_the_source_day_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            "/routing/roster/assignments/copy",
            json={
                "source_date": DAY.isoformat(),
                "target_from": DAY.isoformat(),
                "target_to": (DAY + timedelta(days=1)).isoformat(),
                "work_center_id": str(centre.id),
            },
        )

        assert "target_to" in field_errors(response)

    async def test_copying_an_empty_day_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            "/routing/roster/assignments/copy",
            json={
                "source_date": DAY.isoformat(),
                "target_from": (DAY + timedelta(days=1)).isoformat(),
                "target_to": (DAY + timedelta(days=1)).isoformat(),
                "work_center_id": str(centre.id),
            },
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")


class TestUpdateRosterAssignment:
    async def test_restaffing_changes_the_operator_and_audits(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        shift = await make_shift(session, tenant, unique_code("A")[:8])
        amit = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Amit Shah"
        )
        neha = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Neha Patel"
        )
        row = await plan_assignment(
            session,
            tenant,
            shift_id=shift.id,
            work_center_id=centre.id,
            operator_user_id=amit.id,
        )

        response = await api_client.patch(
            f"/routing/roster/assignments/{row.id}",
            json={"operator_user_id": str(neha.id), "notes": "Cover for leave"},
        )

        data = ok(response)
        assert data["operator_name"] == "Neha Patel"
        assert data["notes"] == "Cover for leave"
        assert await column(session, ShiftAssignment.operator_user_id, row.id) == neha.id
        (update,) = await audit_rows(session, tenant.company_id, row.id, AuditAction.UPDATE)
        assert update.entity_type == "shift_assignment"

    async def test_an_empty_patch_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        centre = await make_work_center(session, tenant, unique_code("WC"))
        shift = await make_shift(session, tenant, unique_code("A")[:8])
        operator = await seed_support.seed_user(session, company_id=tenant.company_id)
        row = await plan_assignment(
            session,
            tenant,
            shift_id=shift.id,
            work_center_id=centre.id,
            operator_user_id=operator.id,
        )

        response = await api_client.patch(f"/routing/roster/assignments/{row.id}", json={})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_assignment_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        centre = await make_work_center(session, foreign, unique_code("WC"))
        shift = await make_shift(session, foreign, unique_code("A")[:8])
        operator = await seed_support.seed_user(session, company_id=foreign.company_id)
        theirs = await plan_assignment(
            session,
            foreign,
            shift_id=shift.id,
            work_center_id=centre.id,
            operator_user_id=operator.id,
        )
        replacement = await seed_support.seed_user(session, company_id=foreign.company_id)

        response = await api_client.patch(
            f"/routing/roster/assignments/{theirs.id}",
            json={"operator_user_id": str(replacement.id)},
        )

        refused(response, 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /routing and PATCH /routing/{routing_id}
# -----------------------------------------------------------------------------


class TestCreateRouting:
    async def test_a_draft_routing_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        code = unique_code("RT")

        response = await api_client.post(
            "/routing",
            json={
                "code": code.lower(),
                "item_id": str(parent.id),
                "is_default": True,
                "operations": [operation_body(centre.id)],
            },
        )

        data = ok(response, 201)
        assert data["code"] == code
        assert data["status"] == "DRAFT"
        assert data["version"] == 1
        assert len(data["operations"]) == 1
        routing_id = uuid.UUID(data["id"])
        assert await column(session, Routing.item_id, routing_id) == parent.id
        (created,) = await audit_rows(session, tenant.company_id, routing_id, AuditAction.CREATE)
        assert created.entity_type == "routing"
        assert created.new_values["operation_count"] == 1

    async def test_a_bought_in_item_cannot_have_a_routing(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        bought = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("BUY"),
            can_manufacture=False,
            can_purchase=True,
        )
        centre = await make_work_center(session, tenant, unique_code("WC"))
        code = unique_code("RT")

        response = await api_client.post(
            "/routing",
            json={
                "code": code,
                "item_id": str(bought.id),
                "operations": [operation_body(centre.id)],
            },
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await count_codes(session, Routing, tenant.company_id, code) == 0

    async def test_duplicate_operation_sequences_are_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))

        response = await api_client.post(
            "/routing",
            json={
                "code": unique_code("RT"),
                "item_id": str(parent.id),
                "operations": [
                    operation_body(centre.id, sequence=10),
                    {
                        **operation_body(centre.id, sequence=10),
                        "operation_code": "OP20",
                        "operation_name": "Duplicate",
                    },
                ],
            },
        )

        assert field_errors(response)  # operations set validator


class TestUpdateRouting:
    async def test_a_draft_can_be_renamed_and_have_its_steps_replaced(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        routing = await make_routing(session, tenant, unique_code("RT"), parent.id, centre.id)

        response = await api_client.patch(
            f"/routing/{routing.id}",
            json={
                "name": "Handle machining v1",
                "operations": [
                    operation_body(centre.id, sequence=10),
                    operation_body(centre.id, sequence=20),
                ],
            },
        )

        data = ok(response)
        assert data["name"] == "Handle machining v1"
        assert [op["sequence"] for op in data["operations"]] == [10, 20]
        (update,) = await audit_rows(session, tenant.company_id, routing.id, AuditAction.UPDATE)
        assert update.entity_type == "routing"

    async def test_an_approved_routing_cannot_be_edited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        routing = await make_approved_routing(
            session, tenant, principal, unique_code("RT"), parent.id, centre.id
        )

        response = await api_client.patch(
            f"/routing/{routing.id}", json={"name": "Should not stick"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_routing_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, foreign, unique_code("HANDLE"))
        centre = await make_work_center(session, foreign, unique_code("WC"))
        theirs = await make_routing(session, foreign, unique_code("RT"), parent.id, centre.id)

        response = await api_client.patch(f"/routing/{theirs.id}", json={"name": "Taken over"})

        refused(response, 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /routing/{routing_id}/approve|obsolete|copy-as-new-version
# -----------------------------------------------------------------------------


class TestRoutingLifecycle:
    async def test_a_second_person_can_approve_a_draft(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        maker = await seed_support.seed_user(session, company_id=tenant.company_id)
        maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
        draft = await make_routing(session, maker_tenant, unique_code("RT"), parent.id, centre.id)
        routing_id = draft.id

        response = await api_client.post(f"/routing/{routing_id}/approve", json={"reason": REASON})

        data = ok(response)
        assert data["status"] == "APPROVED"
        assert data["approved_by_id"] == str(principal.user_id)
        assert await column(session, Routing.status, routing_id) == RoutingStatus.APPROVED
        (approve,) = await audit_rows(session, tenant.company_id, routing_id, AuditAction.APPROVE)
        assert approve.created_by_id == tenant.user_id

    async def test_the_creator_cannot_approve_their_own_routing(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        draft = await make_routing(session, tenant, unique_code("RT"), parent.id, centre.id)

        response = await api_client.post(f"/routing/{draft.id}/approve", json={"reason": REASON})

        refused(response, 403, "FORBIDDEN")
        assert await column(session, Routing.status, draft.id) == RoutingStatus.DRAFT

    async def test_an_approved_routing_can_be_obsoleted(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        routing = await make_approved_routing(
            session, tenant, principal, unique_code("RT"), parent.id, centre.id
        )

        response = await api_client.post(
            f"/routing/{routing.id}/obsolete",
            json={"reason": "Superseded by a faster process"},
        )

        data = ok(response)
        assert data["status"] == "OBSOLETE"
        assert await column(session, Routing.status, routing.id) == RoutingStatus.OBSOLETE
        (cancel,) = await audit_rows(session, tenant.company_id, routing.id, AuditAction.CANCEL)
        assert cancel.entity_type == "routing"

    async def test_copy_as_new_version_creates_the_next_draft(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("HANDLE"))
        centre = await make_work_center(session, tenant, unique_code("WC"))
        routing = await make_approved_routing(
            session, tenant, principal, unique_code("RT"), parent.id, centre.id
        )
        new_code = unique_code("RT")

        response = await api_client.post(
            f"/routing/{routing.id}/copy-as-new-version",
            json={"code": new_code, "name": "Handle machining v2"},
        )

        data = ok(response, 201)
        assert data["code"] == new_code
        assert data["version"] == 2
        assert data["status"] == "DRAFT"
        assert len(data["operations"]) == 1
        copy_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, copy_id, AuditAction.CREATE)
        assert created.new_values["copied_from_routing_id"] == str(routing.id)

    async def test_another_companys_lifecycle_routes_are_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, foreign, unique_code("HANDLE"))
        centre = await make_work_center(session, foreign, unique_code("WC"))
        theirs = await make_routing(session, foreign, unique_code("RT"), parent.id, centre.id)

        for path, body in (
            (f"/routing/{theirs.id}/approve", {"reason": REASON}),
            (f"/routing/{theirs.id}/obsolete", {"reason": REASON}),
            (
                f"/routing/{theirs.id}/copy-as-new-version",
                {"code": unique_code("RT")},
            ),
        ):
            refused(await api_client.post(path, json=body), 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /bom and PATCH /bom/{bom_id}
# -----------------------------------------------------------------------------


class TestCreateBom:
    async def test_a_draft_bom_is_created_with_a_code_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))

        response = await api_client.post(
            "/bom",
            json={
                "item_id": str(parent.id),
                "revision": "A",
                "lines": [bom_line(screw.id)],
            },
        )

        data = ok(response, 201)
        assert data["code"].startswith("BOM/")
        assert data["status"] == "DRAFT"
        assert data["version"] == 1
        assert len(data["lines"]) == 1
        assert data["lines"][0]["component_item_id"] == str(screw.id)
        bom_id = uuid.UUID(data["id"])
        assert await column(session, Bom.item_id, bom_id) == parent.id
        (created,) = await audit_rows(session, tenant.company_id, bom_id, AuditAction.CREATE)
        assert created.entity_type == "bom"
        assert created.new_values["line_count"] == 1

    async def test_a_bought_in_item_cannot_have_a_structure(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        bought = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("BUY"),
            can_manufacture=False,
        )
        screw = await make_component_item(session, tenant, unique_code("SCREW"))

        response = await api_client.post(
            "/bom",
            json={"item_id": str(bought.id), "lines": [bom_line(screw.id)]},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_lines_are_required(self, api_client: AsyncClient, unique_code) -> None:
        response = await api_client.post(
            "/bom",
            json={"item_id": str(uuid.uuid4()), "lines": []},
        )

        assert "lines" in field_errors(response)


class TestUpdateBom:
    async def test_a_draft_can_change_revision_and_lines(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        washer = await make_component_item(session, tenant, unique_code("WASH"))
        bom = await make_bom(session, tenant, parent, screw)

        response = await api_client.patch(
            f"/bom/{bom.id}",
            json={
                "revision": "B",
                "lines": [
                    bom_line(screw.id, quantity="2"),
                    bom_line(washer.id, line_number=2, quantity="1"),
                ],
            },
        )

        data = ok(response)
        assert data["revision"] == "B"
        assert len(data["lines"]) == 2
        (update,) = await audit_rows(session, tenant.company_id, bom.id, AuditAction.UPDATE)
        assert update.entity_type == "bom"

    async def test_an_approved_bom_cannot_be_edited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        bom = await make_approved_bom(session, tenant, principal, parent, screw)

        response = await api_client.patch(f"/bom/{bom.id}", json={"revision": "Z"})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")

    async def test_another_companys_bom_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, foreign, unique_code("KIT"))
        screw = await make_component_item(session, foreign, unique_code("SCREW"))
        theirs = await make_bom(session, foreign, parent, screw)

        response = await api_client.patch(f"/bom/{theirs.id}", json={"revision": "X"})

        refused(response, 404, "NOT_FOUND")


# -----------------------------------------------------------------------------
# POST /bom/{bom_id}/submit|approve|reject|obsolete|copy-as-new-version
# -----------------------------------------------------------------------------


class TestBomLifecycle:
    async def test_submit_moves_a_draft_to_pending_approval(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        bom = await make_bom(session, tenant, parent, screw)

        response = await api_client.post(
            f"/bom/{bom.id}/submit", json={"reason": "Ready for review"}
        )

        data = ok(response)
        assert data["status"] == "PENDING_APPROVAL"
        assert await column(session, Bom.status, bom.id) == BomStatus.PENDING_APPROVAL
        changes = await audit_rows(session, tenant.company_id, bom.id, AuditAction.STATUS_CHANGE)
        assert changes[-1].new_values["status"] == "PENDING_APPROVAL"

    async def test_a_second_person_can_approve_a_submitted_bom(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        maker = await seed_support.seed_user(session, company_id=tenant.company_id)
        maker_tenant = TenantContext(company_id=tenant.company_id, user_id=maker.id)
        submitted = await make_submitted_bom(session, maker_tenant, parent, screw)
        bom_id = submitted.id

        response = await api_client.post(f"/bom/{bom_id}/approve", json={"reason": REASON})

        data = ok(response)
        assert data["status"] == "APPROVED"
        assert data["approved_by_id"] == str(principal.user_id)
        assert await column(session, Bom.status, bom_id) == BomStatus.APPROVED
        (approve,) = await audit_rows(session, tenant.company_id, bom_id, AuditAction.APPROVE)
        assert approve.created_by_id == tenant.user_id

    async def test_the_creator_cannot_approve_their_own_bom(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        submitted = await make_submitted_bom(session, tenant, parent, screw)

        response = await api_client.post(f"/bom/{submitted.id}/approve", json={"reason": REASON})

        refused(response, 403, "FORBIDDEN")
        assert await column(session, Bom.status, submitted.id) == BomStatus.PENDING_APPROVAL

    async def test_reject_returns_a_submission_to_draft(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        submitted = await make_submitted_bom(session, tenant, parent, screw)

        response = await api_client.post(
            f"/bom/{submitted.id}/reject",
            json={"reason": "Scrap percentage looks wrong"},
        )

        data = ok(response)
        assert data["status"] == "DRAFT"
        assert await column(session, Bom.status, submitted.id) == BomStatus.DRAFT
        (reject,) = await audit_rows(session, tenant.company_id, submitted.id, AuditAction.REJECT)
        assert reject.entity_type == "bom"

    async def test_an_approved_bom_can_be_obsoleted(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        bom = await make_approved_bom(session, tenant, principal, parent, screw)

        response = await api_client.post(
            f"/bom/{bom.id}/obsolete",
            json={"reason": "Replaced by revision B"},
        )

        data = ok(response)
        assert data["status"] == "OBSOLETE"
        assert data["is_default"] is False
        assert await column(session, Bom.status, bom.id) == BomStatus.OBSOLETE
        (cancel,) = await audit_rows(session, tenant.company_id, bom.id, AuditAction.CANCEL)
        assert cancel.entity_type == "bom"

    async def test_copy_as_new_version_creates_the_next_draft(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        bom = await make_approved_bom(session, tenant, principal, parent, screw)

        response = await api_client.post(
            f"/bom/{bom.id}/copy-as-new-version",
            json={"revision": "B", "reason": "Tighten scrap", "copy_operations": True},
        )

        data = ok(response, 201)
        assert data["version"] == 2
        assert data["status"] == "DRAFT"
        assert data["revision"] == "B"
        assert len(data["lines"]) == 1
        copy_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, copy_id, AuditAction.CREATE)
        assert created.entity_type == "bom"

    async def test_a_short_reason_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        parent = await make_parent_item(session, tenant, unique_code("KIT"))
        screw = await make_component_item(session, tenant, unique_code("SCREW"))
        bom = await make_bom(session, tenant, parent, screw)

        response = await api_client.post(f"/bom/{bom.id}/submit", json={"reason": "no"})

        assert "reason" in field_errors(response)

    async def test_another_companys_lifecycle_routes_are_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        parent = await make_parent_item(session, foreign, unique_code("KIT"))
        screw = await make_component_item(session, foreign, unique_code("SCREW"))
        theirs = await make_bom(session, foreign, parent, screw)

        for path, body in (
            (f"/bom/{theirs.id}/submit", {"reason": REASON}),
            (f"/bom/{theirs.id}/approve", {"reason": REASON}),
            (f"/bom/{theirs.id}/reject", {"reason": REASON}),
            (f"/bom/{theirs.id}/obsolete", {"reason": REASON}),
            (
                f"/bom/{theirs.id}/copy-as-new-version",
                {"reason": REASON},
            ),
        ):
            refused(await api_client.post(path, json=body), 404, "NOT_FOUND")
