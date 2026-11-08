"""Organisation masters over HTTP: branches, plants, departments, financial years.

The service rules - the department tree guard, the financial-year overlap
query, the retire guards - are pinned at service level in
``tests/integration/test_organization_service.py``. These drive the nine write
routes no test had reached through HTTP: the status code and envelope each
answers with, the row it leaves behind and the audit entry written with it, the
422 a payload or a record's state can earn, and the 404 another company's record
answers with - never a 403, which would confirm that it exists.

The caller is the ``api_client`` principal - every permission, not a superuser
- except where reopening a financial year needs a platform administrator.
Prerequisites are built through the organisation services, so every fixture row
has the shape production gives it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal, TenantContext, get_current_principal
from src.main import app
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.organization.models import (
    Branch,
    CostCentreType,
    Department,
    FinancialYear,
    Plant,
)
from src.modules.organization.schemas import (
    BranchCreate,
    BranchRead,
    CostCentreCreate,
    DepartmentCreate,
    DepartmentRead,
    FinancialYearCreate,
    FinancialYearRead,
    PlantCreate,
    PlantRead,
)
from src.modules.organization.service import (
    BranchService,
    CostCentreService,
    DepartmentService,
    FinancialYearService,
    PlantService,
)
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

#: A real GSTIN shape with a valid check digit, and the same with a wrong one.
GSTIN = "27AAPFU0939F1ZV"
GSTIN_BAD_CHECK_DIGIT = "27AAPFU0939F1ZX"

FY_START, FY_END = date(2031, 4, 1), date(2032, 3, 31)
NEXT_START, NEXT_END = date(2032, 4, 1), date(2033, 3, 31)


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


def platform_admin(base: Principal) -> Principal:
    """The same person with the platform-administrator flag the reopen route needs."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=True,
        permissions=base.permissions,
        role_codes=base.role_codes,
    )


def act_as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's masters, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


async def make_branch(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> BranchRead:
    return await BranchService(session, tenant).create_branch(
        BranchCreate(code=code, name="Branch", **fields)
    )


async def make_plant(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> PlantRead:
    return await PlantService(session, tenant).create_plant(
        PlantCreate(code=code, name="Plant", **fields)
    )


async def make_department(
    session: AsyncSession, tenant: TenantContext, code: str, **fields: Any
) -> DepartmentRead:
    return await DepartmentService(session, tenant).create_department(
        DepartmentCreate(code=code, name="Department", **fields)
    )


async def make_year(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    start: date = FY_START,
    end: date = FY_END,
    **fields: Any,
) -> FinancialYearRead:
    return await FinancialYearService(session, tenant).create_year(
        FinancialYearCreate(code=code, start_date=start, end_date=end, **fields)
    )


def data_of(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def error_of(response: Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["message"], body
    return body["error"]


async def audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_id: uuid.UUID, action: AuditAction
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


# -----------------------------------------------------------------------------
# POST /organization/branches
# -----------------------------------------------------------------------------


class TestCreateBranch:
    async def test_a_branch_is_created_normalised_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("br").lower()

        response = await api_client.post(
            "/organization/branches",
            json={
                "code": f"  {raw} ",
                "name": " North   Depot ",
                "gstin": GSTIN.lower(),
                "city": " Pune ",
                "email": "north.depot@atcraft-qa.com",
            },
        )

        data = data_of(response, 201)
        assert data["code"] == raw.upper()
        assert data["name"] == "North Depot"
        assert data["gstin"] == GSTIN
        assert data["city"] == "Pune"
        assert (data["is_active"], data["is_head_office"]) == (True, False)
        branch_id = uuid.UUID(data["id"])
        assert await column(session, Branch.company_id, branch_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, branch_id, AuditAction.CREATE)
        assert created.entity_type == "branch"
        assert created.created_by_id == tenant.user_id
        assert created.new_values["gstin"] == GSTIN
        read = data_of(await api_client.get(f"/organization/branches/{branch_id}"))
        assert read["code"] == data["code"]

    async def test_a_code_already_in_use_is_a_conflict_after_normalisation(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_branch(session, tenant, unique_code("BR"))

        response = await api_client.post(
            "/organization/branches", json={"code": existing.code.lower(), "name": "Twin"}
        )

        assert error_of(response, 409)["code"] == "DUPLICATE_DOCUMENT"
        assert await count_codes(session, Branch, tenant.company_id, existing.code) == 1

    async def test_a_gstin_with_a_wrong_check_digit_is_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        code = unique_code("BR")

        response = await api_client.post(
            "/organization/branches",
            json={"code": code, "name": "Typo", "gstin": GSTIN_BAD_CHECK_DIGIT},
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "gstin" in error["details"]["fields"]
        assert await count_codes(session, Branch, tenant.company_id, code) == 0

    async def test_a_second_head_office_is_refused_rather_than_demoting_the_first(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        head_office = await make_branch(session, tenant, unique_code("HO"), is_head_office=True)

        response = await api_client.post(
            "/organization/branches",
            json={"code": unique_code("HO"), "name": "Usurper", "is_head_office": True},
        )

        error = error_of(response, 422)
        assert error["code"] == "BUSINESS_RULE_VIOLATION"
        assert error["details"]["head_office_branch_id"] == str(head_office.id)
        assert await column(session, Branch.is_head_office, head_office.id) is True


# -----------------------------------------------------------------------------
# PATCH /organization/branches/{branch_id}
# -----------------------------------------------------------------------------


class TestUpdateBranch:
    async def test_an_edit_applies_only_what_was_sent_and_audits_the_difference(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        branch = await make_branch(
            session, tenant, unique_code("BR"), city="Pune", state="Maharashtra"
        )

        response = await api_client.patch(
            f"/organization/branches/{branch.id}",
            json={"name": "North Depot (Main)", "city": " Nashik "},
        )

        data = data_of(response)
        assert data["name"] == "North Depot (Main)"
        assert data["city"] == "Nashik"
        assert data["state"] == "Maharashtra"
        assert await column(session, Branch.city, branch.id) == "Nashik"
        (update,) = await audit_rows(session, tenant.company_id, branch.id, AuditAction.UPDATE)
        assert update.changed_fields == ["city", "name"]
        assert update.old_values == {"city": "Pune", "name": "Branch"}
        assert not await audit_rows(
            session, tenant.company_id, branch.id, AuditAction.STATUS_CHANGE
        )

    async def test_switching_a_branch_off_through_an_edit_is_a_status_change(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        branch = await make_branch(session, tenant, unique_code("BR"))

        response = await api_client.patch(
            f"/organization/branches/{branch.id}", json={"is_active": False}
        )

        assert data_of(response)["is_active"] is False
        assert await column(session, Branch.is_active, branch.id) is False
        (change,) = await audit_rows(
            session, tenant.company_id, branch.id, AuditAction.STATUS_CHANGE
        )
        assert change.old_values == {"status": "ACTIVE"}
        assert change.new_values == {"status": "INACTIVE"}

    async def test_the_head_office_cannot_be_switched_off(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        head_office = await make_branch(session, tenant, unique_code("HO"), is_head_office=True)

        response = await api_client.patch(
            f"/organization/branches/{head_office.id}", json={"is_active": False}
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert await column(session, Branch.is_active, head_office.id) is True

    async def test_an_empty_patch_and_a_code_change_are_both_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        branch = await make_branch(session, tenant, unique_code("BR"))

        empty = await api_client.patch(f"/organization/branches/{branch.id}", json={})
        assert error_of(empty, 422)["code"] == "BUSINESS_RULE_VIOLATION"

        recoded = await api_client.patch(
            f"/organization/branches/{branch.id}", json={"code": "RENAMED"}
        )
        error = error_of(recoded, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "code" in error["details"]["fields"]
        assert await column(session, Branch.code, branch.id) == branch.code

    async def test_another_companys_branch_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_branch(session, foreign, unique_code("BR"))

        response = await api_client.patch(
            f"/organization/branches/{theirs.id}", json={"name": "Taken over"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, Branch.name, theirs.id) == "Branch"


# -----------------------------------------------------------------------------
# POST /organization/plants and PATCH /organization/plants/{plant_id}
# -----------------------------------------------------------------------------


class TestCreatePlant:
    async def test_a_plant_is_created_under_a_branch_of_this_company(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        branch = await make_branch(session, tenant, unique_code("BR"))
        code = unique_code("PL")

        response = await api_client.post(
            "/organization/plants",
            json={
                "code": code,
                "name": " Casting   Shop ",
                "branch_id": str(branch.id),
                "city": "Rajkot",
            },
        )

        data = data_of(response, 201)
        assert data["code"] == code
        assert data["name"] == "Casting Shop"
        assert data["branch_id"] == str(branch.id)
        plant_id = uuid.UUID(data["id"])
        assert await column(session, Plant.branch_id, plant_id) == branch.id
        (created,) = await audit_rows(session, tenant.company_id, plant_id, AuditAction.CREATE)
        assert created.entity_type == "plant"
        assert created.new_values["branch_id"] == str(branch.id)
        assert data_of(await api_client.get(f"/organization/plants/{plant_id}"))["code"] == code

    async def test_a_branch_of_another_company_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_branch(session, foreign, unique_code("BR"))
        code = unique_code("PL")

        response = await api_client.post(
            "/organization/plants",
            json={"code": code, "name": "Borrowed", "branch_id": str(theirs.id)},
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await count_codes(session, Plant, tenant.company_id, code) == 0

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_plant(session, tenant, unique_code("PL"))

        response = await api_client.post(
            "/organization/plants", json={"code": existing.code.lower(), "name": "Twin"}
        )

        assert error_of(response, 409)["code"] == "DUPLICATE_DOCUMENT"

    async def test_a_plant_needs_a_code_and_a_name(self, api_client: AsyncClient) -> None:
        response = await api_client.post("/organization/plants", json={"city": "Rajkot"})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"code", "name"} <= set(error["details"]["fields"])


class TestUpdatePlant:
    async def test_a_plant_can_move_branch_and_be_switched_off_and_on(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        first = await make_branch(session, tenant, unique_code("BR"))
        second = await make_branch(session, tenant, unique_code("BR"))
        plant = await make_plant(session, tenant, unique_code("PL"), branch_id=first.id)
        path = f"/organization/plants/{plant.id}"

        moved = data_of(
            await api_client.patch(path, json={"branch_id": str(second.id), "name": "Moved"})
        )
        assert (moved["branch_id"], moved["name"]) == (str(second.id), "Moved")
        assert (
            data_of(await api_client.patch(path, json={"is_active": False}))["is_active"] is False
        )
        assert data_of(await api_client.patch(path, json={"is_active": True}))["is_active"] is True

        assert await column(session, Plant.branch_id, plant.id) == second.id
        assert await column(session, Plant.is_active, plant.id) is True
        changes = await audit_rows(session, tenant.company_id, plant.id, AuditAction.STATUS_CHANGE)
        assert [(c.old_values["status"], c.new_values["status"]) for c in changes] == [
            ("ACTIVE", "INACTIVE"),
            ("INACTIVE", "ACTIVE"),
        ]
        edits = await audit_rows(session, tenant.company_id, plant.id, AuditAction.UPDATE)
        assert edits[0].changed_fields == ["branch_id", "name"]

    async def test_moving_a_plant_to_another_companys_branch_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        ours = await make_branch(session, tenant, unique_code("BR"))
        theirs = await make_branch(session, foreign, unique_code("BR"))
        plant = await make_plant(session, tenant, unique_code("PL"), branch_id=ours.id)

        response = await api_client.patch(
            f"/organization/plants/{plant.id}", json={"branch_id": str(theirs.id)}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, Plant.branch_id, plant.id) == ours.id

    async def test_another_companys_plant_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_plant(session, foreign, unique_code("PL"))

        response = await api_client.patch(
            f"/organization/plants/{theirs.id}", json={"is_active": False}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, Plant.is_active, theirs.id) is True

    async def test_unknown_fields_and_empty_patches_are_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        plant = await make_plant(session, tenant, unique_code("PL"))

        recoded = await api_client.patch(
            f"/organization/plants/{plant.id}", json={"code": "RENAMED"}
        )
        assert error_of(recoded, 422)["code"] == "VALIDATION_ERROR"

        empty = await api_client.patch(f"/organization/plants/{plant.id}", json={})
        assert error_of(empty, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert not await audit_rows(session, tenant.company_id, plant.id, AuditAction.UPDATE)


# -----------------------------------------------------------------------------
# PATCH /organization/departments/{department_id}
# -----------------------------------------------------------------------------


class TestUpdateDepartment:
    async def test_a_department_is_placed_under_a_parent_plant_head_and_cost_centre(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        plant = await make_plant(session, tenant, unique_code("PL"))
        parent = await make_department(session, tenant, unique_code("DP"))
        child = await make_department(session, tenant, unique_code("DP"))
        centre = await CostCentreService(session, tenant).create_cost_centre(
            CostCentreCreate(
                code=unique_code("CC"),
                name="Assembly",
                cost_centre_type=CostCentreType.PRODUCTION,
            )
        )

        response = await api_client.patch(
            f"/organization/departments/{child.id}",
            json={
                "name": " Final   Assembly ",
                "parent_id": str(parent.id),
                "plant_id": str(plant.id),
                "head_user_id": str(tenant.user_id),
                "cost_centre_id": str(centre.id),
            },
        )

        data = data_of(response)
        assert data["name"] == "Final Assembly"
        assert (data["parent_id"], data["plant_id"]) == (str(parent.id), str(plant.id))
        assert (data["head_user_id"], data["cost_centre_id"]) == (
            str(tenant.user_id),
            str(centre.id),
        )
        assert await column(session, Department.parent_id, child.id) == parent.id
        (update,) = await audit_rows(session, tenant.company_id, child.id, AuditAction.UPDATE)
        assert update.changed_fields == [
            "cost_centre_id",
            "head_user_id",
            "name",
            "parent_id",
            "plant_id",
        ]
        read = data_of(await api_client.get(f"/organization/departments/{child.id}"))
        assert read["cost_centre_id"] == str(centre.id)

    async def test_switching_a_department_off_is_a_status_change(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        department = await make_department(session, tenant, unique_code("DP"))

        response = await api_client.patch(
            f"/organization/departments/{department.id}", json={"is_active": False}
        )

        assert data_of(response)["is_active"] is False
        (change,) = await audit_rows(
            session, tenant.company_id, department.id, AuditAction.STATUS_CHANGE
        )
        assert (change.old_values, change.new_values) == (
            {"status": "ACTIVE"},
            {"status": "INACTIVE"},
        )

    async def test_a_cycle_or_a_self_parent_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        top = await make_department(session, tenant, unique_code("DP"))
        below = await make_department(session, tenant, unique_code("DP"), parent_id=top.id)

        cycle = await api_client.patch(
            f"/organization/departments/{top.id}", json={"parent_id": str(below.id)}
        )
        assert error_of(cycle, 422)["code"] == "BUSINESS_RULE_VIOLATION"

        itself = await api_client.patch(
            f"/organization/departments/{top.id}", json={"parent_id": str(top.id)}
        )
        assert error_of(itself, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert await column(session, Department.parent_id, top.id) is None

    @pytest.mark.parametrize("field", ["plant_id", "parent_id", "head_user_id", "cost_centre_id"])
    async def test_no_reference_may_point_into_another_company(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: TenantContext,
        unique_code,
        field: str,
    ) -> None:
        """A UUID foreign key is satisfied by another tenant's row; the route must not be."""
        department = await make_department(session, tenant, unique_code("DP"))
        theirs: dict[str, uuid.UUID] = {
            "plant_id": (await make_plant(session, foreign, unique_code("PL"))).id,
            "parent_id": (await make_department(session, foreign, unique_code("DP"))).id,
            "head_user_id": foreign.user_id,
            "cost_centre_id": (
                await CostCentreService(session, foreign).create_cost_centre(
                    CostCentreCreate(
                        code=unique_code("CC"),
                        name="Theirs",
                        cost_centre_type=CostCentreType.SELLING,
                    )
                )
            ).id,
        }

        response = await api_client.patch(
            f"/organization/departments/{department.id}", json={field: str(theirs[field])}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, getattr(Department, field), department.id) is None
        assert not await audit_rows(session, tenant.company_id, department.id, AuditAction.UPDATE)

    async def test_another_companys_department_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_department(session, foreign, unique_code("DP"))

        response = await api_client.patch(
            f"/organization/departments/{theirs.id}", json={"name": "Taken over"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, Department.name, theirs.id) == "Department"

    async def test_the_code_cannot_be_patched(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        department = await make_department(session, tenant, unique_code("DP"))

        response = await api_client.patch(
            f"/organization/departments/{department.id}", json={"code": "RENAMED"}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "code" in error["details"]["fields"]


# -----------------------------------------------------------------------------
# POST /organization/financial-years and PATCH .../{year_id}
# -----------------------------------------------------------------------------


class TestCreateFinancialYear:
    async def test_a_year_is_opened_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("fy").lower()

        response = await api_client.post(
            "/organization/financial-years",
            json={
                "code": raw,
                "start_date": FY_START.isoformat(),
                "end_date": FY_END.isoformat(),
                "notes": "  First year on the ERP  ",
            },
        )

        data = data_of(response, 201)
        assert data["code"] == raw.upper()
        assert (data["start_date"], data["end_date"]) == (FY_START.isoformat(), FY_END.isoformat())
        assert (data["is_active"], data["is_closed"]) == (True, False)
        assert data["notes"] == "First year on the ERP"
        year_id = uuid.UUID(data["id"])
        assert await column(session, FinancialYear.company_id, year_id) == tenant.company_id
        (created,) = await audit_rows(session, tenant.company_id, year_id, AuditAction.CREATE)
        assert created.entity_type == "financial_year"
        assert created.created_by_id == tenant.user_id
        read = data_of(await api_client.get(f"/organization/financial-years/{year_id}"))
        assert read["code"] == data["code"]

    async def test_a_backwards_or_overlong_period_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        code = unique_code("FY")
        backwards = await api_client.post(
            "/organization/financial-years",
            json={"code": code, "start_date": "2032-03-31", "end_date": "2031-04-01"},
        )
        assert error_of(backwards, 422)["code"] == "BUSINESS_RULE_VIOLATION"

        overlong = await api_client.post(
            "/organization/financial-years",
            json={"code": code, "start_date": "2031-04-01", "end_date": "2033-03-31"},
        )
        assert error_of(overlong, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert await count_codes(session, FinancialYear, tenant.company_id, code) == 0

    async def test_a_period_sharing_a_day_with_an_active_year_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.post(
            "/organization/financial-years",
            json={
                "code": unique_code("FY"),
                "start_date": FY_END.isoformat(),
                "end_date": NEXT_END.isoformat(),
            },
        )

        error = error_of(response, 422)
        assert error["code"] == "BUSINESS_RULE_VIOLATION"
        assert error["details"]["conflicting_financial_year_id"] == str(existing.id)

    async def test_a_duplicate_code_is_a_conflict(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        existing = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.post(
            "/organization/financial-years",
            json={
                "code": existing.code.lower(),
                "start_date": NEXT_START.isoformat(),
                "end_date": NEXT_END.isoformat(),
            },
        )

        assert error_of(response, 409)["code"] == "DUPLICATE_DOCUMENT"

    async def test_missing_and_malformed_dates_are_field_errors(
        self, api_client: AsyncClient, unique_code
    ) -> None:
        response = await api_client.post(
            "/organization/financial-years",
            json={"code": unique_code("FY"), "start_date": "31-04-2031"},
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"start_date", "end_date"} <= set(error["details"]["fields"])


class TestUpdateFinancialYear:
    async def test_an_open_year_can_be_adjusted(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.patch(
            f"/organization/financial-years/{year.id}",
            json={"end_date": "2032-06-30", "notes": " Extended first period "},
        )

        data = data_of(response)
        assert data["end_date"] == "2032-06-30"
        assert data["notes"] == "Extended first period"
        assert await column(session, FinancialYear.end_date, year.id) == date(2032, 6, 30)
        (update,) = await audit_rows(session, tenant.company_id, year.id, AuditAction.UPDATE)
        assert update.changed_fields == ["end_date", "notes"]

    async def test_stretching_into_another_active_year_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        first = await make_year(session, tenant, unique_code("FY"))
        await make_year(session, tenant, unique_code("FY"), NEXT_START, NEXT_END)

        response = await api_client.patch(
            f"/organization/financial-years/{first.id}", json={"end_date": "2032-04-30"}
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert await column(session, FinancialYear.end_date, first.id) == FY_END

    async def test_a_closed_year_cannot_be_edited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))
        await FinancialYearService(session, tenant).close_year(year.id, reason="Books filed")

        response = await api_client.patch(
            f"/organization/financial-years/{year.id}", json={"notes": "Late correction"}
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert await column(session, FinancialYear.notes, year.id) is None

    async def test_closing_is_not_an_editable_field(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.patch(
            f"/organization/financial-years/{year.id}", json={"is_closed": True}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "is_closed" in error["details"]["fields"]
        assert await column(session, FinancialYear.is_closed, year.id) is False

    async def test_another_companys_year_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_year(session, foreign, unique_code("FY"))

        response = await api_client.patch(
            f"/organization/financial-years/{theirs.id}", json={"notes": "Not ours"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, FinancialYear.notes, theirs.id) is None

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DEFECT: an explicit null start_date/end_date reaches assert_period_valid "
            "and raises TypeError - a 500, not a field error"
        ),
    )
    @pytest.mark.parametrize("field", ["start_date", "end_date"])
    async def test_an_explicit_null_date_is_a_field_error(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        unique_code,
        field: str,
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.patch(
            f"/organization/financial-years/{year.id}", json={field: None}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert field in error["details"]["fields"]
        assert await column(session, FinancialYear.start_date, year.id) == FY_START
        assert await column(session, FinancialYear.end_date, year.id) == FY_END


class TestExplicitNullsOnRequiredColumns:
    """An explicit ``null`` for a column that cannot hold one is a payload error.

    Every update schema types its fields ``X | None`` so a PATCH may omit one.
    A client that *sends* null for a required column has made a mistake the API
    contract reports as a field error (docs/api.md: validation failures carry
    field paths so a form can map them onto controls) - which is what
    ``PATCH /roles/{id}`` does for ``name``. Here the null reaches the
    database instead, and its NOT NULL constraint answers with a 422 that
    names no field.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DEFECT: an explicit null name/is_active is written to a NOT NULL column; "
            "the IntegrityError comes back as a 422 with no details.fields"
        ),
    )
    @pytest.mark.parametrize(
        ("route", "field"),
        [
            ("branches", "name"),
            ("branches", "is_active"),
            ("plants", "name"),
            ("plants", "is_active"),
            ("departments", "name"),
            ("departments", "is_active"),
            ("financial-years", "is_active"),
        ],
    )
    async def test_a_null_for_a_required_column_names_the_field(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        unique_code,
        route: str,
        field: str,
    ) -> None:
        builders = {
            "branches": make_branch,
            "plants": make_plant,
            "departments": make_department,
            "financial-years": make_year,
        }
        record = await builders[route](session, tenant, unique_code("NN"))

        response = await api_client.patch(f"/organization/{route}/{record.id}", json={field: None})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert field in (error["details"] or {}).get("fields", {})


# -----------------------------------------------------------------------------
# POST /organization/financial-years/{year_id}/close and /reopen
# -----------------------------------------------------------------------------


class TestCloseFinancialYear:
    async def test_closing_freezes_the_year_and_records_why(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/close",
            json={"reason": "Books filed with the auditor"},
        )

        data = data_of(response)
        # Closed is history: it stops competing for its date range too.
        assert (data["is_closed"], data["is_active"]) == (True, False)
        assert await column(session, FinancialYear.is_closed, year.id) is True
        (change,) = await audit_rows(session, tenant.company_id, year.id, AuditAction.STATUS_CHANGE)
        assert (change.old_values, change.new_values) == (
            {"status": "OPEN"},
            {"status": "CLOSED"},
        )
        assert change.context == {"reason": "Books filed with the auditor"}
        assert change.created_by_id == tenant.user_id

    async def test_closing_twice_is_an_invalid_transition(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))
        await FinancialYearService(session, tenant).close_year(year.id, reason="Books filed")

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/close", json={"reason": "Again"}
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_STATE_TRANSITION"
        assert error["details"]["current_status"] == "CLOSED"

    async def test_a_close_needs_a_reason(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/close", json={"reason": "ok"}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "reason" in error["details"]["fields"]
        assert await column(session, FinancialYear.is_closed, year.id) is False

    async def test_another_companys_year_cannot_be_closed(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext, unique_code
    ) -> None:
        theirs = await make_year(session, foreign, unique_code("FY"))

        response = await api_client.post(
            f"/organization/financial-years/{theirs.id}/close", json={"reason": "Not ours"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, FinancialYear.is_closed, theirs.id) is False


class TestReopenFinancialYear:
    async def test_a_platform_administrator_reopens_a_closed_year(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))
        await FinancialYearService(session, tenant).close_year(year.id, reason="Books filed")
        act_as(platform_admin(principal))

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/reopen",
            json={"reason": "Audit adjustment"},
        )

        data = data_of(response)
        assert (data["is_closed"], data["is_active"]) == (False, True)
        assert await column(session, FinancialYear.is_closed, year.id) is False
        reopened = (
            await audit_rows(session, tenant.company_id, year.id, AuditAction.STATUS_CHANGE)
        )[-1]
        assert (reopened.old_values, reopened.new_values) == (
            {"status": "CLOSED"},
            {"status": "OPEN"},
        )
        assert reopened.context == {"reason": "Audit adjustment"}
        assert reopened.created_by_id == principal.user_id

    async def test_reopening_an_open_year_is_an_invalid_transition(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))
        act_as(platform_admin(principal))

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/reopen", json={"reason": "Nothing to undo"}
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_STATE_TRANSITION"
        assert error["details"]["current_status"] == "OPEN"

    async def test_reopening_is_refused_once_another_year_holds_the_range(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        year = await make_year(session, tenant, unique_code("FY"))
        await FinancialYearService(session, tenant).close_year(year.id, reason="Books filed")
        replacement = await make_year(session, tenant, unique_code("FY"))
        act_as(platform_admin(principal))

        response = await api_client.post(
            f"/organization/financial-years/{year.id}/reopen", json={"reason": "Audit adjustment"}
        )

        error = error_of(response, 422)
        assert error["code"] == "BUSINESS_RULE_VIOLATION"
        assert error["details"]["conflicting_financial_year_id"] == str(replacement.id)
        assert await column(session, FinancialYear.is_closed, year.id) is True

    async def test_another_companys_year_is_not_found_even_for_an_administrator(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        principal: Principal,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        theirs = await make_year(session, foreign, unique_code("FY"))
        await FinancialYearService(session, foreign).close_year(theirs.id, reason="Books filed")
        act_as(platform_admin(principal))

        response = await api_client.post(
            f"/organization/financial-years/{theirs.id}/reopen", json={"reason": "Not ours"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await column(session, FinancialYear.is_closed, theirs.id) is True
