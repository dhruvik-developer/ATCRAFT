"""Shifts and the shift roster over HTTP: who may do what, and what is refused at the door.

The service rules are covered in ``tests/integration/test_shift_roster_service``.
These assert the boundary:

* the shift register is readable with any right that fills a shift picker - the
  existing production roles must not need a new grant to choose a shift - and
  writable only with ``shift.manage``;
* the roster is ``shift_roster.view`` / ``shift_roster.manage`` exactly, and its
  operator directory answers the people who fill in assignments and bookings,
  none of whom hold ``user.view``;
* a roster read without a bounded range is a 422 against the query parameter;
* another company's shift answers 404 - never 403, which would confirm it exists.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal
from src.core.messages import SHIFT_ASSIGNMENT_OVERLAP
from src.core.permissions import Permission
from src.modules.routing.schemas import MAX_ROSTER_RANGE_DAYS
from tests.seed_support import seed_company, seed_shift, seed_user, seed_work_center

pytestmark = [pytest.mark.api, pytest.mark.integration]

DAY = today() + timedelta(days=4)


def _holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person, a superuser no longer, holding exactly these rights."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


def _range(day_from: date, day_to: date) -> dict[str, str]:
    return {"date_from": day_from.isoformat(), "date_to": day_to.isoformat()}


async def test_existing_production_rights_read_the_shift_picker(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    await seed_shift(session, company_id=principal.company_id, code="A")
    for right in (
        Permission.SHIFT_VIEW,
        Permission.SHIFT_ROSTER_VIEW,
        Permission.WORK_ORDER_VIEW,
        Permission.PRODUCTION_ENTRY_CREATE,
    ):
        _as(_holding(principal, right))
        response = await api_client.get("/routing/shifts", params={"is_active": True})
        assert response.status_code == 200, right
        assert [row["code"] for row in response.json()["data"]] == ["A"]

    _as(_holding(principal, Permission.ITEM_VIEW))
    assert (await api_client.get("/routing/shifts")).status_code == 403


async def test_only_shift_manage_writes_the_register(api_client, principal: Principal) -> None:
    body = {"code": "c", "name": "Night", "start_time": "22:00", "end_time": "06:00"}

    _as(_holding(principal, Permission.SHIFT_VIEW, Permission.WORK_ORDER_MANAGE))
    refused = await api_client.post("/routing/shifts", json=body)
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["required_permission"] == "shift.manage"

    _as(_holding(principal, Permission.SHIFT_VIEW, Permission.SHIFT_MANAGE))
    created = await api_client.post("/routing/shifts", json=body)
    assert created.status_code == 201
    shift = created.json()["data"]
    assert (shift["code"], shift["crosses_midnight"]) == ("C", True)
    assert created.json()["message"] == "Shift created"

    # The code is fixed, and the midnight flag is never the client's to state.
    assert (
        await api_client.patch(f"/routing/shifts/{shift['id']}", json={"code": "N"})
    ).status_code == 422
    assert (
        await api_client.post(
            "/routing/shifts", json={**body, "code": "D", "crosses_midnight": True}
        )
    ).status_code == 422
    too_long_break = await api_client.patch(
        f"/routing/shifts/{shift['id']}", json={"break_minutes": 480}
    )
    assert too_long_break.status_code == 422
    assert "break_minutes" in too_long_break.json()["error"]["details"]["fields"]

    retired = await api_client.delete(
        f"/routing/shifts/{shift['id']}", params={"reason": "Night shift dropped"}
    )
    assert retired.status_code == 200


async def test_the_roster_is_gated_on_its_own_rights(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    company = principal.company_id
    shift = await seed_shift(session, company_id=company, code="A")
    centre = await seed_work_center(session, company_id=company)
    operator = await seed_user(session, company_id=company, full_name="Amit Shah")
    assignment = {
        "roster_date": DAY.isoformat(),
        "shift_id": str(shift.id),
        "work_center_id": str(centre.id),
        "operator_user_id": str(operator.id),
    }

    _as(_holding(principal, Permission.WORK_ORDER_MANAGE))
    assert (
        await api_client.get("/routing/roster/assignments", params=_range(DAY, DAY))
    ).status_code == 403

    _as(_holding(principal, Permission.SHIFT_ROSTER_VIEW))
    refused = await api_client.post("/routing/roster/assignments", json=assignment)
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["required_permission"] == "shift_roster.manage"

    _as(_holding(principal, Permission.SHIFT_ROSTER_VIEW, Permission.SHIFT_ROSTER_MANAGE))
    created = await api_client.post("/routing/roster/assignments", json=assignment)
    assert created.status_code == 201
    row = created.json()["data"]
    assert (row["shift_code"], row["operator_name"], row["status"]) == ("A", "Amit Shah", "PLANNED")

    listed = await api_client.get("/routing/roster/assignments", params=_range(DAY, DAY))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [row["id"]]

    clash = await api_client.post("/routing/roster/assignments", json=assignment)
    assert clash.status_code == 422
    error = clash.json()["error"]
    assert error["message"] == SHIFT_ASSIGNMENT_OVERLAP
    assert [item["resource"] for item in error["details"]["conflicts"]] == ["operator"]

    cancelled = await api_client.post(
        f"/routing/roster/assignments/{row['id']}/cancel", json={"reason": "Plan changed"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "CANCELLED"


async def test_a_roster_read_needs_a_bounded_range(api_client, principal: Principal) -> None:
    _as(_holding(principal, Permission.SHIFT_ROSTER_VIEW))
    assert (await api_client.get("/routing/roster/assignments")).status_code == 422

    too_long = await api_client.get(
        "/routing/roster/assignments",
        params=_range(DAY, DAY + timedelta(days=MAX_ROSTER_RANGE_DAYS)),
    )
    assert too_long.status_code == 422
    assert "query.date_to" in too_long.json()["error"]["details"]["fields"]

    backwards = await api_client.get(
        "/routing/roster/assignments", params=_range(DAY, DAY - timedelta(days=1))
    )
    assert backwards.status_code == 422

    month = await api_client.get(
        "/routing/roster/assignments",
        params=_range(DAY, DAY + timedelta(days=MAX_ROSTER_RANGE_DAYS - 1)),
    )
    assert month.status_code == 200


async def test_the_operator_directory_serves_the_people_who_staff_the_floor(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    await seed_user(
        session, company_id=principal.company_id, full_name="Neha Patel", employee_code="OP-2"
    )
    for right in (
        Permission.SHIFT_ROSTER_VIEW,
        Permission.WORK_ORDER_MANAGE,
        Permission.PRODUCTION_ENTRY_CREATE,
    ):
        _as(_holding(principal, right))
        response = await api_client.get("/routing/roster/operators", params={"search": "op-2"})
        assert response.status_code == 200, right
        (person,) = response.json()["data"]
        # A picker row, not a user record: no e-mail, no roles.
        assert set(person) == {"id", "full_name", "employee_code", "plant_id", "department_id"}

    _as(_holding(principal, Permission.WORK_ORDER_VIEW))
    assert (await api_client.get("/routing/roster/operators")).status_code == 403


async def test_another_tenants_shift_is_not_found(
    api_client, principal: Principal, session: AsyncSession
) -> None:
    other = await seed_company(session)
    foreign = await seed_shift(
        session, company_id=other.id, code="X", start_time=time(7), end_time=time(15)
    )
    _as(_holding(principal, Permission.SHIFT_VIEW, Permission.SHIFT_MANAGE))
    assert (await api_client.get(f"/routing/shifts/{foreign.id}")).status_code == 404
    assert (
        await api_client.patch(f"/routing/shifts/{foreign.id}", json={"name": "Taken over"})
    ).status_code == 404
