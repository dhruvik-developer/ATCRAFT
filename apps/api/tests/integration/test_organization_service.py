"""Service-level tests for the organisation module, against a real PostgreSQL.

These cover the behaviour only a database can prove:

* the tenant boundary on ``/company`` - a context pointing at another company
  cannot read this one, and there is no query shape in the module that could;
* the recursive ancestor walk that keeps the department tree a tree;
* the financial-year overlap query, including the inclusive endpoints;
* the delete guards, which read ``users``, ``warehouses``, ``plants`` and
  ``departments``, and the raw-SQL ``to_regclass`` probe behind the warehouse
  one;
* that every write lands an audit row in the same transaction.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``)
come from ``tests/conftest.py``. The session's outer transaction is rolled back
after every test, so nothing here survives into the next one.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocumentSequence
from src.core.deps import TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.organization.models import Branch, Department, FinancialYear, Plant
from src.modules.organization.schemas import (
    BranchCreate,
    BranchFilters,
    BranchUpdate,
    CompanyUpdate,
    DepartmentCreate,
    DepartmentFilters,
    DepartmentUpdate,
    FinancialYearCreate,
    FinancialYearFilters,
    FinancialYearUpdate,
    PlantCreate,
    PlantFilters,
)
from src.modules.organization.service import (
    AUDIT_ENTITY_BRANCH,
    AUDIT_ENTITY_COMPANY,
    AUDIT_ENTITY_FINANCIAL_YEAR,
    BranchService,
    CompanyService,
    DepartmentService,
    FinancialYearService,
    PlantService,
)
from src.modules.warehouses.models import Warehouse

pytestmark = pytest.mark.integration

PAGE = PageParams(page=1, limit=50)
SORT = SortParams()

FY_2026 = (date(2026, 4, 1), date(2027, 3, 31))
FY_2027 = (date(2027, 4, 1), date(2028, 3, 31))


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_type: str
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.company_id == company_id, AuditLog.entity_type == entity_type)
        .order_by(AuditLog.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _make_branch(session: AsyncSession, tenant: TenantContext, code: str, **kw) -> Branch:
    return await BranchService(session, tenant).create_branch(
        BranchCreate(code=code, name=f"Branch {code}", **kw)
    )


async def _make_year(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    span: tuple[date, date],
    **kw,
) -> FinancialYear:
    return await FinancialYearService(session, tenant).create_year(
        FinancialYearCreate(code=code, start_date=span[0], end_date=span[1], **kw)
    )


# -----------------------------------------------------------------------------
# Company - the tenant boundary
# -----------------------------------------------------------------------------


class TestCompany:
    async def test_reads_the_callers_own_company(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        read = await CompanyService(session, tenant).get_company()
        assert read.id == company.id
        assert read.code == company.code

    async def test_another_tenant_cannot_read_this_company(
        self, session: AsyncSession, other_company, user
    ) -> None:
        """The decisive tenant-isolation assertion for this module.

        The service resolves the company from the context, so a principal in
        another tenant does not get a filtered view of this company - it gets
        nothing at all, and the id it holds is never consulted.
        """
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        read = await CompanyService(session, foreign).get_company()
        assert read.id == other_company.id

    async def test_a_context_pointing_at_a_deleted_company_reports_not_found(
        self, session: AsyncSession, user
    ) -> None:
        stale = TenantContext(company_id=uuid.uuid4(), user_id=user.id)
        with pytest.raises(NotFoundError):
            await CompanyService(session, stale).get_company()

    async def test_update_persists_and_audits_only_what_changed(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        original_currency = company.currency
        await CompanyService(session, tenant).update_company(
            CompanyUpdate(legal_name="Atcraft Manufacturing Private Limited", pan="aaacc1206d")
        )

        await session.refresh(company)
        assert company.legal_name == "Atcraft Manufacturing Private Limited"
        assert company.pan == "AAACC1206D"
        assert company.currency == original_currency

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_COMPANY)
        assert len(rows) == 1
        entry = rows[0]
        # CONFIG_CHANGE, not UPDATE: these fields drive tax identity.
        assert entry.action == AuditAction.CONFIG_CHANGE
        assert set(entry.changed_fields or []) == {"legal_name", "pan"}
        assert entry.new_values["pan"] == "AAACC1206D"

    async def test_an_empty_patch_is_refused_rather_than_audited(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        with pytest.raises(BusinessRuleError, match="No fields"):
            await CompanyService(session, tenant).update_company(CompanyUpdate())
        assert await _audit_rows(session, company.id, AUDIT_ENTITY_COMPANY) == []


# -----------------------------------------------------------------------------
# Branch
# -----------------------------------------------------------------------------


class TestBranch:
    async def test_create_stamps_the_tenant_and_writes_an_audit_row(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        branch = await _make_branch(session, tenant, "HO", is_head_office=True)

        row = (
            await session.execute(select(Branch).where(Branch.id == branch.id))
        ).scalar_one()
        assert row.company_id == company.id
        assert row.created_by_id == tenant.user_id

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_BRANCH)
        assert [r.action for r in rows] == [AuditAction.CREATE]
        assert rows[0].entity_code == "HO"

    async def test_a_duplicate_code_is_refused_after_normalisation(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        await _make_branch(session, tenant, "HO")
        with pytest.raises(DuplicateError):
            await _make_branch(session, tenant, "  ho  ")

    async def test_a_retired_branch_still_holds_its_code(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Its code is quoted on historical documents; reuse would make two
        different branches read as one in the archive."""
        branch = await _make_branch(session, tenant, "OLD")
        await BranchService(session, tenant).delete_branch(branch.id, reason="Closed site")
        with pytest.raises(DuplicateError):
            await _make_branch(session, tenant, "OLD")

    async def test_a_second_head_office_is_refused_rather_than_demoting_the_first(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        first = await _make_branch(session, tenant, "HO", is_head_office=True)
        with pytest.raises(BusinessRuleError) as exc:
            await _make_branch(session, tenant, "HO2", is_head_office=True)
        assert exc.value.details["head_office_branch_id"] == str(first.id)

        # Still exactly one after the refusal.
        rows = (
            await session.execute(
                select(Branch).where(
                    Branch.company_id == tenant.company_id, Branch.is_head_office.is_(True)
                )
            )
        ).scalars().all()
        assert [r.id for r in rows] == [first.id]

    async def test_promoting_a_second_branch_is_refused_while_one_is_flagged(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        await _make_branch(session, tenant, "HO", is_head_office=True)
        other = await _make_branch(session, tenant, "BR1")
        with pytest.raises(BusinessRuleError, match="already has a head office"):
            await BranchService(session, tenant).update_branch(
                other.id, BranchUpdate(is_head_office=True)
            )

    async def test_deactivating_through_update_writes_a_status_change_row(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        await BranchService(session, tenant).update_branch(branch.id, BranchUpdate(is_active=False))

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_BRANCH)
        actions = [r.action for r in rows]
        assert AuditAction.STATUS_CHANGE in actions
        status_row = next(r for r in rows if r.action == AuditAction.STATUS_CHANGE)
        assert status_row.old_values == {"status": "ACTIVE"}
        assert status_row.new_values == {"status": "INACTIVE"}

    async def test_the_head_office_cannot_be_deactivated(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        branch = await _make_branch(session, tenant, "HO", is_head_office=True)
        with pytest.raises(BusinessRuleError, match="head office cannot be deactivated"):
            await BranchService(session, tenant).update_branch(
                branch.id, BranchUpdate(is_active=False)
            )

    async def test_retiring_is_refused_while_a_user_is_assigned(
        self, session: AsyncSession, tenant: TenantContext, user
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        user.branch_id = branch.id
        await session.flush()

        with pytest.raises(BusinessRuleError) as exc:
            await BranchService(session, tenant).delete_branch(branch.id, reason="Site closed")
        assert exc.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_retiring_is_refused_while_a_plant_points_at_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        await PlantService(session, tenant).create_plant(
            PlantCreate(code="PL1", name="Plant 1", branch_id=branch.id)
        )
        with pytest.raises(BusinessRuleError, match="still has plants"):
            await BranchService(session, tenant).delete_branch(branch.id, reason="Site closed")

    async def test_retiring_is_refused_while_a_warehouse_points_at_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Exercises the raw-SQL probe into another module's table."""
        branch = await _make_branch(session, tenant, "BR1")
        session.add(
            Warehouse(
                company_id=tenant.company_id,
                code="WH1",
                name="Store 1",
                warehouse_type="RAW_MATERIAL",
                branch_id=branch.id,
            )
        )
        await session.flush()

        with pytest.raises(BusinessRuleError, match="still has warehouses"):
            await BranchService(session, tenant).delete_branch(branch.id, reason="Site closed")

    async def test_retiring_soft_deletes_and_audits(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        await BranchService(session, tenant).delete_branch(branch.id, reason="Site closed")

        row = (
            await session.execute(select(Branch).where(Branch.id == branch.id))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.is_active is False

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_BRANCH)
        delete_row = next(r for r in rows if r.action == AuditAction.DELETE)
        assert delete_row.context["reason"] == "Site closed"

    async def test_a_retired_branch_is_hidden_from_the_default_list(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        live = await _make_branch(session, tenant, "BR1")
        retired = await _make_branch(session, tenant, "BR2")
        await BranchService(session, tenant).delete_branch(retired.id, reason="Site closed")

        service = BranchService(session, tenant)
        items, pagination = await service.list_branches(BranchFilters(), PAGE, SORT)
        assert [b.id for b in items] == [live.id]
        assert pagination.total == 1

        with_deleted, _ = await service.list_branches(
            BranchFilters(include_deleted=True), PAGE, SORT
        )
        assert {b.id for b in with_deleted} == {live.id, retired.id}

    async def test_the_list_never_leaks_another_tenants_branches(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        await _make_branch(session, tenant, "BR1")
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        items, pagination = await BranchService(session, foreign).list_branches(
            BranchFilters(), PAGE, SORT
        )
        assert items == []
        assert pagination.total == 0

    async def test_a_branch_from_another_tenant_is_not_fetchable_by_id(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        with pytest.raises(NotFoundError):
            await BranchService(session, foreign).get_branch(branch.id)


# -----------------------------------------------------------------------------
# Plant
# -----------------------------------------------------------------------------


class TestPlant:
    async def test_a_branch_in_another_tenant_is_rejected(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        """The foreign key would accept this; only the tenant-scoped check does not."""
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        foreign_branch = await _make_branch(session, foreign, "FBR")

        with pytest.raises(NotFoundError, match="Branch"):
            await PlantService(session, tenant).create_plant(
                PlantCreate(code="PL1", name="Plant 1", branch_id=foreign_branch.id)
            )

    async def test_retiring_is_refused_while_a_department_points_at_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        plant = await PlantService(session, tenant).create_plant(
            PlantCreate(code="PL1", name="Plant 1")
        )
        await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code="QC", name="Quality", plant_id=plant.id)
        )
        with pytest.raises(BusinessRuleError, match="still has departments"):
            await PlantService(session, tenant).delete_plant(plant.id, reason="Site closed")

    async def test_filtering_by_branch_narrows_the_page(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        branch = await _make_branch(session, tenant, "BR1")
        service = PlantService(session, tenant)
        attached = await service.create_plant(
            PlantCreate(code="PL1", name="Plant 1", branch_id=branch.id)
        )
        await service.create_plant(PlantCreate(code="PL2", name="Plant 2"))

        items, _ = await service.list_plants(PlantFilters(branch_id=branch.id), PAGE, SORT)
        assert [p.id for p in items] == [attached.id]

    async def test_retiring_an_unreferenced_plant_succeeds(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        plant = await PlantService(session, tenant).create_plant(
            PlantCreate(code="PL1", name="Plant 1")
        )
        await PlantService(session, tenant).delete_plant(plant.id, reason="Consolidated")

        row = (await session.execute(select(Plant).where(Plant.id == plant.id))).scalar_one()
        assert row.deleted_at is not None


# -----------------------------------------------------------------------------
# Department - the tree
# -----------------------------------------------------------------------------


class TestDepartment:
    async def _chain(
        self, session: AsyncSession, tenant: TenantContext, codes: list[str]
    ) -> list[Department]:
        """Create a parent chain, each department under the previous one."""
        service = DepartmentService(session, tenant)
        made: list[Department] = []
        parent_id: uuid.UUID | None = None
        for code in codes:
            record = await service.create_department(
                DepartmentCreate(code=code, name=f"Dept {code}", parent_id=parent_id)
            )
            made.append(record)
            parent_id = record.id
        return made

    async def test_a_direct_cycle_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        top, child = await self._chain(session, tenant, ["D1", "D2"])
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            await DepartmentService(session, tenant).update_department(
                top.id, DepartmentUpdate(parent_id=child.id)
            )

    async def test_an_indirect_cycle_is_refused_through_the_recursive_walk(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Three levels deep, so the CTE has to climb to find the conflict."""
        top, _middle, bottom = await self._chain(session, tenant, ["D1", "D2", "D3"])
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            await DepartmentService(session, tenant).update_department(
                top.id, DepartmentUpdate(parent_id=bottom.id)
            )

    async def test_self_parenting_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        (only,) = await self._chain(session, tenant, ["D1"])
        with pytest.raises(BusinessRuleError, match="cannot be its own parent"):
            await DepartmentService(session, tenant).update_department(
                only.id, DepartmentUpdate(parent_id=only.id)
            )

    async def test_reparenting_to_an_unrelated_department_is_allowed(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = DepartmentService(session, tenant)
        left = await service.create_department(DepartmentCreate(code="D1", name="Left"))
        right = await service.create_department(DepartmentCreate(code="D2", name="Right"))
        moved = await service.update_department(right.id, DepartmentUpdate(parent_id=left.id))
        assert moved.parent_id == left.id

    async def test_a_parent_in_another_tenant_is_not_found(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        foreign_parent = await DepartmentService(session, foreign).create_department(
            DepartmentCreate(code="FD", name="Foreign")
        )
        with pytest.raises(NotFoundError, match="Parent department"):
            await DepartmentService(session, tenant).create_department(
                DepartmentCreate(code="D1", name="Local", parent_id=foreign_parent.id)
            )

    async def test_retiring_is_refused_while_sub_departments_are_live(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        top, _child = await self._chain(session, tenant, ["D1", "D2"])
        with pytest.raises(BusinessRuleError, match="still has sub-departments"):
            await DepartmentService(session, tenant).delete_department(
                top.id, reason="Reorganised"
            )

    async def test_retiring_is_refused_while_a_user_is_assigned(
        self, session: AsyncSession, tenant: TenantContext, user
    ) -> None:
        (dept,) = await self._chain(session, tenant, ["D1"])
        user.department_id = dept.id
        await session.flush()

        with pytest.raises(BusinessRuleError) as exc:
            await DepartmentService(session, tenant).delete_department(
                dept.id, reason="Reorganised"
            )
        assert exc.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_a_head_user_from_another_tenant_is_rejected(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        foreign = TenantContext(company_id=other_company.id, user_id=user.id)
        # `user` belongs to `company`, so from the other tenant's point of view
        # it does not exist - which is exactly what must be refused.
        with pytest.raises(NotFoundError, match="User"):
            await DepartmentService(session, foreign).create_department(
                DepartmentCreate(code="D1", name="Quality", head_user_id=user.id)
            )

    async def test_filtering_by_parent_returns_the_direct_children_only(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        top, middle, _bottom = await self._chain(session, tenant, ["D1", "D2", "D3"])
        items, _ = await DepartmentService(session, tenant).list_departments(
            DepartmentFilters(parent_id=top.id), PAGE, SORT
        )
        assert [d.id for d in items] == [middle.id]


# -----------------------------------------------------------------------------
# Financial year
# -----------------------------------------------------------------------------


class TestFinancialYear:
    async def test_consecutive_years_are_both_allowed(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        await _make_year(session, tenant, "2026-27", FY_2026)
        second = await _make_year(session, tenant, "2027-28", FY_2027)
        assert second.is_active is True
        assert second.is_closed is False

    async def test_an_overlapping_active_year_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        first = await _make_year(session, tenant, "2026-27", FY_2026)
        with pytest.raises(BusinessRuleError) as exc:
            await _make_year(
                session, tenant, "2026-H2", (date(2026, 10, 1), date(2027, 9, 30))
            )
        assert exc.value.details["conflicting_financial_year_id"] == str(first.id)

    async def test_a_single_shared_day_counts_as_an_overlap(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The range endpoints are inclusive: 31 March belongs to the year."""
        await _make_year(session, tenant, "2026-27", FY_2026)
        with pytest.raises(BusinessRuleError, match="overlaps active financial year"):
            await _make_year(
                session, tenant, "2027-28", (date(2027, 3, 31), date(2028, 3, 30))
            )

    async def test_an_inactive_year_does_not_block_an_overlapping_one(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Only active years compete for a date range; a parked draft does not."""
        await _make_year(session, tenant, "2026-DRAFT", FY_2026, is_active=False)
        allowed = await _make_year(session, tenant, "2026-27", FY_2026)
        assert allowed.is_active is True

    async def test_reactivating_a_year_re_runs_the_overlap_check(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        parked = await _make_year(session, tenant, "2026-DRAFT", FY_2026, is_active=False)
        await _make_year(session, tenant, "2026-27", FY_2026)

        with pytest.raises(BusinessRuleError, match="overlaps active financial year"):
            await FinancialYearService(session, tenant).update_year(
                parked.id, FinancialYearUpdate(is_active=True)
            )

    async def test_a_backwards_period_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        with pytest.raises(BusinessRuleError, match="must start before it ends"):
            await _make_year(session, tenant, "BAD", (date(2027, 3, 31), date(2026, 4, 1)))

    async def test_the_overlap_check_ignores_this_year_when_editing_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        updated = await FinancialYearService(session, tenant).update_year(
            year.id, FinancialYearUpdate(end_date=date(2027, 3, 30))
        )
        assert updated.end_date == date(2027, 3, 30)

    async def test_closing_sets_both_flags_and_writes_a_status_change(
        self, session: AsyncSession, tenant: TenantContext, company
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        closed = await FinancialYearService(session, tenant).close_year(
            year.id, reason="Statutory filing complete"
        )

        assert closed.is_closed is True
        # A closed year stops competing for its range, so a re-cut period can be
        # opened over it.
        assert closed.is_active is False

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_FINANCIAL_YEAR)
        status_row = next(r for r in rows if r.action == AuditAction.STATUS_CHANGE)
        assert status_row.old_values == {"status": "OPEN"}
        assert status_row.new_values == {"status": "CLOSED"}
        assert status_row.context["reason"] == "Statutory filing complete"

    async def test_closing_twice_is_an_invalid_transition(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        service = FinancialYearService(session, tenant)
        await service.close_year(year.id, reason="Filed")
        with pytest.raises(InvalidStateTransitionError):
            await service.close_year(year.id, reason="Filed again")

    async def test_a_closed_year_cannot_be_edited(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        service = FinancialYearService(session, tenant)
        await service.close_year(year.id, reason="Filed")
        with pytest.raises(BusinessRuleError, match="closed and can no longer be edited"):
            await service.update_year(year.id, FinancialYearUpdate(notes="Reopened note"))

    async def test_an_ordinary_user_cannot_reopen_a_closed_year(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        service = FinancialYearService(session, tenant)
        await service.close_year(year.id, reason="Filed")

        with pytest.raises(AuthorizationError):
            await service.reopen_year(year.id, reason="Correction needed")

        refreshed = (
            await session.execute(select(FinancialYear).where(FinancialYear.id == year.id))
        ).scalar_one()
        assert refreshed.is_closed is True

    async def test_a_platform_administrator_can_reopen_and_it_is_audited(
        self, session: AsyncSession, tenant: TenantContext, company, user
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        await FinancialYearService(session, tenant).close_year(year.id, reason="Filed")

        admin = TenantContext(
            company_id=company.id, user_id=user.id, is_superuser=True
        )
        reopened = await FinancialYearService(session, admin).reopen_year(
            year.id, reason="Audit adjustment"
        )
        assert reopened.is_closed is False
        assert reopened.is_active is True

        rows = await _audit_rows(session, company.id, AUDIT_ENTITY_FINANCIAL_YEAR)
        assert [r.new_values["status"] for r in rows if r.action == AuditAction.STATUS_CHANGE] == [
            "CLOSED",
            "OPEN",
        ]

    async def test_reopening_is_refused_when_another_year_took_the_range(
        self, session: AsyncSession, tenant: TenantContext, company, user
    ) -> None:
        """Closing frees the range, so reopening has to re-check it."""
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        service = FinancialYearService(session, tenant)
        await service.close_year(year.id, reason="Filed")
        await _make_year(session, tenant, "2026-27R", FY_2026)

        admin = TenantContext(company_id=company.id, user_id=user.id, is_superuser=True)
        with pytest.raises(BusinessRuleError, match="overlaps active financial year"):
            await FinancialYearService(session, admin).reopen_year(
                year.id, reason="Audit adjustment"
            )

    async def test_a_closed_year_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        service = FinancialYearService(session, tenant)
        await service.close_year(year.id, reason="Filed")
        with pytest.raises(BusinessRuleError) as exc:
            await service.delete_year(year.id, reason="Created in error")
        assert exc.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_a_year_that_has_numbered_documents_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        session.add(
            DocumentSequence(
                company_id=tenant.company_id,
                doc_type="purchase_order",
                period_key="2026-27",
                prefix="PO",
                last_number=1,
                padding=5,
            )
        )
        await session.flush()

        with pytest.raises(BusinessRuleError, match="already been numbered"):
            await FinancialYearService(session, tenant).delete_year(
                year.id, reason="Created in error"
            )

    async def test_an_unused_open_year_can_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        await FinancialYearService(session, tenant).delete_year(year.id, reason="Wrong dates")

        row = (
            await session.execute(select(FinancialYear).where(FinancialYear.id == year.id))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.is_active is False

    async def test_contains_date_finds_the_year_a_posting_belongs_to(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        first = await _make_year(session, tenant, "2026-27", FY_2026)
        await _make_year(session, tenant, "2027-28", FY_2027)

        items, _ = await FinancialYearService(session, tenant).list_years(
            FinancialYearFilters(contains_date=date(2027, 3, 31)), PAGE, SORT
        )
        assert [y.id for y in items] == [first.id]

    async def test_a_retired_year_no_longer_blocks_its_range(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        year = await _make_year(session, tenant, "2026-27", FY_2026)
        await FinancialYearService(session, tenant).delete_year(year.id, reason="Wrong dates")
        replacement = await _make_year(session, tenant, "2026-27B", FY_2026)
        assert replacement.is_active is True
