"""Cost centres against a real PostgreSQL: the master, its carriers, the ledger.

What only a database can prove, in the order the rules were written:

* the master itself - create, list, the recursive walks behind the tree rules,
  and the duplicate check that counts retired codes;
* the retire guards, which reach tables owned by modules *above* organisation
  through raw SQL behind a ``to_regclass`` probe - so they are exercised with
  real work centres, production orders and vendor invoices, not stand-ins;
* every carrier refusing another company's cost centre, because a foreign key to
  a UUID is satisfied by another tenant's row;
* the ledger: a centre only on income and expense legs, a reversal that copies
  its centre even after retirement, and an invoice approval that posts one
  expense leg per centre;
* the cost-centre statement summing what was posted;
* the extended department and plant retire guards, and the company's bank
  details with the account number masked in the audit trail.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``,
``principal``) come from ``tests/conftest.py``; the shared schema lives in
``public``, which is where the ``to_regclass`` probes look. Every test's writes
are rolled back. Masters for other modules are built through the ORM builders
in ``tests/seed_support`` - never a hand-written INSERT.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError, DuplicateError, ErrorCode, NotFoundError
from src.core.errors import ValidationError as AppValidationError
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.finance import service as finance
from src.modules.finance.models import (
    LedgerAccountType,
    LedgerEntry,
    PartyType,
    SystemAccount,
    VendorInvoiceStatus,
)
from src.modules.finance.schemas import (
    InvoiceApproval,
    LedgerAccountCreate,
    VendorInvoiceCreate,
    VendorInvoiceFilters,
    VendorInvoiceLineIn,
    VendorInvoiceUpdate,
)
from src.modules.inventory.models import AdjustmentReason
from src.modules.inventory.schemas import StockAdjustmentCreate, StockAdjustmentLineCreate
from src.modules.inventory.service import StockAdjustmentService
from src.modules.job_work import service as job_work_service
from src.modules.job_work.models import JobWorkProcess
from src.modules.job_work.schemas import JobWorkOrderCreate, JobWorkOrderLineCreate
from src.modules.organization.models import CostCentreType, Plant
from src.modules.organization.schemas import (
    CompanyUpdate,
    CostCentreCreate,
    CostCentreFilters,
    CostCentreRead,
    CostCentreUpdate,
    DepartmentCreate,
    DepartmentUpdate,
    PlantCreate,
)
from src.modules.organization.service import (
    AUDIT_ENTITY_COMPANY,
    AUDIT_ENTITY_COST_CENTRE,
    MAX_COST_CENTRE_DEPTH,
    CompanyService,
    CostCentreService,
    DepartmentService,
    PlantService,
    active_cost_centre_ids,
    require_cost_centre,
    require_cost_centres,
)
from src.modules.procurement.models import PurchaseRequisition
from src.modules.production import service as production_service
from src.modules.production.schemas import ProductionOrderCreate, ProductionOrderUpdate
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.routing.schemas import WorkCenterCreate
from src.modules.routing.service import WorkCenterService
from src.modules.vendors.models import VendorType
from tests import seed_support

pytestmark = pytest.mark.integration

PAGE = PageParams(page=1, limit=50)
SORT = SortParams()
D = Decimal


# -----------------------------------------------------------------------------
# Builders
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def _centre(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    code: str | None = None,
    parent_id: uuid.UUID | None = None,
    is_active: bool = True,
    cost_centre_type: CostCentreType = CostCentreType.PRODUCTION,
    name: str = "Centre",
) -> CostCentreRead:
    return await CostCentreService(session, tenant).create_cost_centre(
        CostCentreCreate(
            code=code or _code("CC"),
            name=name,
            cost_centre_type=cost_centre_type,
            parent_id=parent_id,
            is_active=is_active,
        )
    )


async def _deactivate(session: AsyncSession, tenant: TenantContext, centre_id: uuid.UUID) -> None:
    await CostCentreService(session, tenant).update_cost_centre(
        centre_id, CostCentreUpdate(is_active=False)
    )


async def _archive(session: AsyncSession, tenant: TenantContext, centre_id: uuid.UUID) -> None:
    await CostCentreService(session, tenant).delete_cost_centre(
        centre_id, reason="Created in error during the test"
    )


async def _work_centre(session: AsyncSession, tenant: TenantContext, **kw: object) -> uuid.UUID:
    created = await WorkCenterService(session, tenant).create_work_center(
        WorkCenterCreate(code=_code("WC"), name="Machine shop", **kw)  # type: ignore[arg-type]
    )
    return created.id


async def _production_order(
    session: AsyncSession, tenant: TenantContext, *, cost_centre_id: uuid.UUID | None
) -> uuid.UUID:
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=_code("FG"), can_manufacture=True
    )
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    order = await production_service.create_order(
        session,
        tenant,
        ProductionOrderCreate(
            item_id=item.id,
            planned_quantity=D("10"),
            warehouse_id=warehouse.id,
            planned_start_date=today(),
            planned_end_date=today() + timedelta(days=5),
            cost_centre_id=cost_centre_id,
        ),
    )
    return order.id


async def _vendor(session: AsyncSession, tenant: TenantContext) -> uuid.UUID:
    vendor = await seed_support.seed_vendor(
        session,
        company_id=tenant.company_id,
        vendor_types=(VendorType.JOB_WORK, VendorType.SERVICE),
        is_approved=True,
        approved_at=utcnow(),
    )
    return vendor.id


def _service_line(
    amount: str, *, cost_centre_id: uuid.UUID | None = None, tax_rate: str = "18"
) -> VendorInvoiceLineIn:
    return VendorInvoiceLineIn(
        description=f"Service worth {amount}",
        quantity=D("1"),
        unit_price=D(amount),
        tax_rate=D(tax_rate),
        cost_centre_id=cost_centre_id,
    )


async def _invoice(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    lines: list[VendorInvoiceLineIn],
    cost_centre_id: uuid.UUID | None = None,
    invoice_date: date | None = None,
) -> uuid.UUID:
    detail = await finance.create_invoice(
        session,
        tenant,
        VendorInvoiceCreate(
            vendor_id=await _vendor(session, tenant),
            vendor_invoice_number=_code("BILL"),
            invoice_date=invoice_date or today(),
            cost_centre_id=cost_centre_id,
            lines=lines,
        ),
    )
    return detail.id


async def _approve(
    session: AsyncSession, tenant: TenantContext, principal: Principal, invoice_id: uuid.UUID
) -> None:
    await finance.submit_invoice(session, tenant, invoice_id, reason=None)
    # A service bill has no order to match: approving it is a named decision.
    await finance.approve_invoice(
        session,
        tenant,
        principal,
        invoice_id,
        InvoiceApproval(override_reason="Service bill with no purchase order behind it."),
    )


async def _expense_account(session: AsyncSession, tenant: TenantContext) -> uuid.UUID:
    accounts = await finance.ensure_system_accounts(session, tenant)
    return accounts[str(SystemAccount.PURCHASE_EXPENSE)].id


async def _account(
    session: AsyncSession, tenant: TenantContext, account_type: LedgerAccountType
) -> uuid.UUID:
    created = await finance.create_account(
        session,
        tenant,
        LedgerAccountCreate(
            code=_code("A")[:32], name=f"{account_type} account", account_type=account_type
        ),
    )
    return created.id


async def _audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> list[AuditLog]:
    # (created_at, id): every row of one transaction shares created_at, and the
    # UUIDv7 id is what recovers the order they were written in (CLAUDE.md).
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
        )
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _legs(session: AsyncSession, reference_id: uuid.UUID) -> list[LedgerEntry]:
    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.reference_id == reference_id)
        .order_by(LedgerEntry.journal_id.asc(), LedgerEntry.line_number.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _foreign(other_company: object, user: object) -> TenantContext:
    return TenantContext(company_id=other_company.id, user_id=user.id)  # type: ignore[attr-defined]


# -----------------------------------------------------------------------------
# The master
# -----------------------------------------------------------------------------


class TestMaster:
    async def test_create_list_and_fetch(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        tag = uuid.uuid4().hex[:6].upper()
        parent = await _centre(session, tenant, code=f"PROD-{tag}", name="Production")
        child = await _centre(
            session, tenant, code=f"PROD-{tag}-DIE", name="Die-casting", parent_id=parent.id
        )
        sales = await _centre(
            session,
            tenant,
            code=f"SALES-{tag}",
            name="Sales",
            cost_centre_type=CostCentreType.SELLING,
        )

        service = CostCentreService(session, tenant)
        rows, pagination = await service.list_cost_centres(
            CostCentreFilters(search=tag), PAGE, SORT
        )
        assert [row.code for row in rows] == [parent.code, child.code, sales.code]
        assert pagination.total == 3

        children, _ = await service.list_cost_centres(
            CostCentreFilters(parent_id=parent.id), PAGE, SORT
        )
        assert [row.id for row in children] == [child.id]

        selling, _ = await service.list_cost_centres(
            CostCentreFilters(search=tag, cost_centre_type=CostCentreType.SELLING), PAGE, SORT
        )
        assert [row.id for row in selling] == [sales.id]

        fetched = await service.get_cost_centre(child.id)
        assert fetched.parent_id == parent.id
        assert fetched.cost_centre_type is CostCentreType.PRODUCTION

        created = await _audit_rows(session, tenant.company_id, AUDIT_ENTITY_COST_CENTRE, child.id)
        assert [row.action for row in created] == [AuditAction.CREATE]

    async def test_a_code_is_never_reused_not_even_a_retired_one(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        code = _code("CC")
        first = await _centre(session, tenant, code=code)
        await _archive(session, tenant, first.id)

        with pytest.raises(DuplicateError):
            await _centre(session, tenant, code=code)

    async def test_another_tenant_cannot_read_or_adopt_a_centre(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        mine = await _centre(session, tenant)
        foreign = _foreign(other_company, user)

        with pytest.raises(NotFoundError):
            await CostCentreService(session, foreign).get_cost_centre(mine.id)
        with pytest.raises(NotFoundError, match="Parent cost centre"):
            await _centre(session, foreign, parent_id=mine.id)

    async def test_a_cycle_is_refused(self, session: AsyncSession, tenant: TenantContext) -> None:
        top = await _centre(session, tenant)
        middle = await _centre(session, tenant, parent_id=top.id)
        bottom = await _centre(session, tenant, parent_id=middle.id)

        service = CostCentreService(session, tenant)
        with pytest.raises(BusinessRuleError, match="must stay a tree"):
            await service.update_cost_centre(top.id, CostCentreUpdate(parent_id=bottom.id))
        with pytest.raises(BusinessRuleError, match="cannot be its own parent"):
            await service.update_cost_centre(top.id, CostCentreUpdate(parent_id=top.id))

    async def test_moving_a_subtree_counts_its_deepest_descendant(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        chain = [await _centre(session, tenant)]
        for _ in range(MAX_COST_CENTRE_DEPTH - 2):
            chain.append(await _centre(session, tenant, parent_id=chain[-1].id))
        # chain[-1] sits at depth MAX - 1: a leaf fits under it, a parent does not.
        mover = await _centre(session, tenant)
        await _centre(session, tenant, parent_id=mover.id)

        service = CostCentreService(session, tenant)
        with pytest.raises(BusinessRuleError, match="may not be deeper"):
            await service.update_cost_centre(mover.id, CostCentreUpdate(parent_id=chain[-1].id))

        moved = await service.update_cost_centre(mover.id, CostCentreUpdate(parent_id=chain[-2].id))
        assert moved.parent_id == chain[-2].id

    async def test_an_active_centre_cannot_hang_under_an_inactive_one(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        parent = await _centre(session, tenant)
        await _deactivate(session, tenant, parent.id)

        with pytest.raises(BusinessRuleError, match="is inactive"):
            await _centre(session, tenant, parent_id=parent.id)

        dormant = await _centre(session, tenant, parent_id=parent.id, is_active=False)
        with pytest.raises(BusinessRuleError, match="is inactive"):
            await CostCentreService(session, tenant).update_cost_centre(
                dormant.id, CostCentreUpdate(is_active=True)
            )

    async def test_a_null_for_a_required_field_is_ignored_not_written(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant, name="Assembly")
        updated = await CostCentreService(session, tenant).update_cost_centre(
            centre.id, CostCentreUpdate.model_validate({"name": None, "description": "Bench"})
        )
        assert updated.name == "Assembly"
        assert updated.description == "Bench"

    async def test_deactivation_is_a_recorded_status_change(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await _deactivate(session, tenant, centre.id)

        rows = await _audit_rows(session, tenant.company_id, AUDIT_ENTITY_COST_CENTRE, centre.id)
        assert [row.action for row in rows] == [
            AuditAction.CREATE,
            AuditAction.UPDATE,
            AuditAction.STATUS_CHANGE,
        ]


# -----------------------------------------------------------------------------
# Deactivation and retirement
# -----------------------------------------------------------------------------


class TestDeactivation:
    async def test_refused_while_an_active_child_remains(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        parent = await _centre(session, tenant)
        child = await _centre(session, tenant, parent_id=parent.id)
        parent_id = parent.id

        with pytest.raises(BusinessRuleError, match="active sub-centres") as raised:
            await _deactivate(session, tenant, parent_id)
        assert raised.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

        await _deactivate(session, tenant, child.id)
        await _deactivate(session, tenant, parent_id)

    async def test_refused_while_a_department_defaults_to_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code=_code("D"), name="Production", cost_centre_id=centre.id)
        )
        with pytest.raises(BusinessRuleError, match="default of one or more departments"):
            await _deactivate(session, tenant, centre.id)

    async def test_refused_while_a_work_centre_is_charged_to_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await _work_centre(session, tenant, cost_centre_id=centre.id)
        with pytest.raises(BusinessRuleError, match="assigned to work centres"):
            await _deactivate(session, tenant, centre.id)

    async def test_a_document_keeps_its_reference_but_a_new_one_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Follow the row: deactivating must not strand the draft that names it."""
        centre = await _centre(session, tenant)
        order_id = await _production_order(session, tenant, cost_centre_id=centre.id)

        await _deactivate(session, tenant, centre.id)

        # The draft is still editable, and re-saving it is not a new allocation.
        edited = await production_service.update_order(
            session,
            tenant,
            order_id,
            ProductionOrderUpdate(cost_centre_id=centre.id, priority=3),
        )
        assert edited.cost_centre_id == centre.id

        # A fresh assignment is, and is refused against the control that sent it.
        with pytest.raises(AppValidationError) as raised:
            await _production_order(session, tenant, cost_centre_id=centre.id)
        assert "is inactive" in raised.value.message
        assert "cost_centre_id" in raised.value.details["fields"]


class TestArchive:
    async def test_allowed_when_nothing_names_it_and_audited_with_the_reason(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await _archive(session, tenant, centre.id)

        service = CostCentreService(session, tenant)
        with pytest.raises(NotFoundError):
            await service.get_cost_centre(centre.id)
        retired, _ = await service.list_cost_centres(
            CostCentreFilters(search=centre.code, include_deleted=True), PAGE, SORT
        )
        assert [row.id for row in retired] == [centre.id]
        assert retired[0].deleted_at is not None

        rows = await _audit_rows(session, tenant.company_id, AUDIT_ENTITY_COST_CENTRE, centre.id)
        assert rows[-1].action == AuditAction.DELETE
        assert rows[-1].context == {"reason": "Created in error during the test"}

    async def test_refused_while_a_live_child_remains(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        parent = await _centre(session, tenant)
        await _centre(session, tenant, parent_id=parent.id, is_active=False)
        with pytest.raises(BusinessRuleError, match="sub-centres"):
            await _archive(session, tenant, parent.id)

    async def test_refused_while_a_department_defaults_to_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code=_code("D"), name="Stores", cost_centre_id=centre.id)
        )
        with pytest.raises(BusinessRuleError, match="departments"):
            await _archive(session, tenant, centre.id)

    async def test_refused_while_a_work_centre_names_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await _work_centre(session, tenant, cost_centre_id=centre.id)
        with pytest.raises(BusinessRuleError, match="work centres") as raised:
            await _archive(session, tenant, centre.id)
        assert raised.value.details["referenced_by"] == ["work centres"]

    async def test_refused_while_any_document_names_it_and_the_documents_are_listed(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        await _production_order(session, tenant, cost_centre_id=centre.id)
        await _invoice(session, tenant, lines=[_service_line("100", cost_centre_id=centre.id)])

        with pytest.raises(BusinessRuleError) as raised:
            await _archive(session, tenant, centre.id)
        assert raised.value.code == ErrorCode.REFERENCED_RECORD_IN_USE
        assert raised.value.details["referenced_by"] == [
            "production orders",
            "vendor invoice lines",
        ]

    async def test_posted_history_does_not_block_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        expense = await _expense_account(session, tenant)
        bank = await _account(session, tenant, LedgerAccountType.ASSET)
        await finance.post_journal(
            session,
            tenant,
            [
                finance.JournalLine(account_id=expense, debit=D("50"), cost_centre_id=centre.id),
                finance.JournalLine(account_id=bank, credit=D("50")),
            ],
            entry_date=today(),
            reference_type="test_journal",
        )
        await _archive(session, tenant, centre.id)


# -----------------------------------------------------------------------------
# The seam, and every carrier
# -----------------------------------------------------------------------------


class TestSeam:
    async def test_a_foreign_or_unknown_id_is_not_found(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        foreign = await _centre(session, _foreign(other_company, user))
        with pytest.raises(NotFoundError):
            await require_cost_centre(session, tenant, foreign.id)
        with pytest.raises(NotFoundError):
            await require_cost_centre(session, tenant, uuid.uuid4())

    async def test_active_ids_are_exactly_the_live_active_ones(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        live = await _centre(session, tenant)
        dormant = await _centre(session, tenant, is_active=False)
        retired = await _centre(session, tenant)
        await _archive(session, tenant, retired.id)
        foreign = await _centre(session, _foreign(other_company, user))

        found = await active_cost_centre_ids(
            session, tenant, [live.id, dormant.id, retired.id, foreign.id]
        )
        assert found == {live.id}

    async def test_the_batch_form_reports_every_inactive_field_at_once(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        live = await _centre(session, tenant)
        dormant = await _centre(session, tenant, is_active=False)
        with pytest.raises(AppValidationError) as raised:
            await require_cost_centres(
                session,
                tenant,
                [("cost_centre_id", live.id), ("lines.1.cost_centre_id", dormant.id)],
            )
        assert list(raised.value.details["fields"]) == ["lines.1.cost_centre_id"]


class TestCarriersRefuseAnotherTenantsCentre:
    """Every module that accepts a cost centre asks the seam - none trusts the FK."""

    @pytest.fixture
    async def foreign_centre(self, session: AsyncSession, other_company, user) -> uuid.UUID:
        created = await _centre(session, _foreign(other_company, user))
        return created.id

    async def test_department(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        service = DepartmentService(session, tenant)
        with pytest.raises(NotFoundError):
            await service.create_department(
                DepartmentCreate(code=_code("D"), name="Dept", cost_centre_id=foreign_centre)
            )
        department = await service.create_department(DepartmentCreate(code=_code("D"), name="Dept"))
        with pytest.raises(NotFoundError):
            await service.update_department(
                department.id, DepartmentUpdate(cost_centre_id=foreign_centre)
            )

    async def test_work_centre(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        with pytest.raises(NotFoundError):
            await _work_centre(session, tenant, cost_centre_id=foreign_centre)

    async def test_production_order(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        with pytest.raises(NotFoundError):
            await _production_order(session, tenant, cost_centre_id=foreign_centre)

    async def test_job_work_order(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=_code("CMP"), can_job_work=True
        )
        warehouse = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WH")
        )
        payload = JobWorkOrderCreate(
            vendor_id=await _vendor(session, tenant),
            process_type=JobWorkProcess.COATING,
            expected_return_date=today() + timedelta(days=10),
            warehouse_id=warehouse.id,
            cost_centre_id=foreign_centre,
            lines=[
                JobWorkOrderLineCreate(line_number=1, item_id=item.id, quantity_to_issue=D("5"))
            ],
        )
        with pytest.raises(NotFoundError):
            await job_work_service.create_order(session, tenant, payload)

        mine = await _centre(session, tenant)
        order, _ = await job_work_service.create_order(
            session, tenant, payload.model_copy(update={"cost_centre_id": mine.id})
        )
        assert order.cost_centre_id == mine.id

    async def test_stock_adjustment(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=_code("RM"), standard_cost=D("10")
        )
        warehouse = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WH")
        )
        payload = StockAdjustmentCreate(
            warehouse_id=warehouse.id,
            reason_code=AdjustmentReason.DAMAGE,
            reason="Water damage in the rack",
            cost_centre_id=foreign_centre,
            lines=[StockAdjustmentLineCreate(item_id=item.id, quantity=D("2"), unit_cost=D("10"))],
        )
        service = StockAdjustmentService(session, tenant)
        with pytest.raises(NotFoundError):
            await service.create(payload)

        mine = await _centre(session, tenant)
        created = await service.create(payload.model_copy(update={"cost_centre_id": mine.id}))
        assert created.cost_centre_id == mine.id

    async def test_vendor_invoice_header_and_line(
        self, session: AsyncSession, tenant: TenantContext, foreign_centre: uuid.UUID
    ) -> None:
        with pytest.raises(NotFoundError):
            await _invoice(
                session, tenant, lines=[_service_line("10")], cost_centre_id=foreign_centre
            )
        with pytest.raises(NotFoundError):
            await _invoice(
                session, tenant, lines=[_service_line("10", cost_centre_id=foreign_centre)]
            )


class TestVendorInvoiceCarrier:
    async def test_an_inactive_line_centre_is_refused_against_its_own_field(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        live = await _centre(session, tenant)
        dormant = await _centre(session, tenant, is_active=False)
        with pytest.raises(AppValidationError) as raised:
            await _invoice(
                session,
                tenant,
                cost_centre_id=live.id,
                lines=[_service_line("10"), _service_line("20", cost_centre_id=dormant.id)],
            )
        assert list(raised.value.details["fields"]) == ["lines.1.cost_centre_id"]

    async def test_the_header_and_lines_are_stored_and_read_back(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        header = await _centre(session, tenant)
        own = await _centre(session, tenant)
        invoice_id = await _invoice(
            session,
            tenant,
            cost_centre_id=header.id,
            lines=[_service_line("10"), _service_line("20", cost_centre_id=own.id)],
        )
        detail = await finance.get_invoice_detail(session, tenant, invoice_id)
        assert detail.cost_centre_id == header.id
        assert [line.cost_centre_id for line in detail.lines] == [None, own.id]

        rows, _ = await finance.list_invoices(
            session,
            tenant,
            filters=VendorInvoiceFilters(search=detail.code),
            params=PAGE,
            sort=SORT,
        )
        assert [row.cost_centre_id for row in rows] == [header.id]

    async def test_a_held_centre_survives_an_edit_after_deactivation(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        invoice_id = await _invoice(
            session,
            tenant,
            cost_centre_id=centre.id,
            lines=[_service_line("10", cost_centre_id=centre.id)],
        )
        await _deactivate(session, tenant, centre.id)

        updated = await finance.update_invoice(
            session,
            tenant,
            invoice_id,
            VendorInvoiceUpdate(
                notes="Re-keyed from the original",
                lines=[_service_line("12", cost_centre_id=centre.id)],
            ),
        )
        assert updated.cost_centre_id == centre.id
        assert updated.lines[0].cost_centre_id == centre.id


# -----------------------------------------------------------------------------
# The ledger
# -----------------------------------------------------------------------------


class TestLedger:
    async def test_a_centre_is_refused_on_a_balance_sheet_leg(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        expense = await _expense_account(session, tenant)
        payable = await _account(session, tenant, LedgerAccountType.LIABILITY)

        with pytest.raises(AppValidationError, match="Leg 2") as raised:
            await finance.post_journal(
                session,
                tenant,
                [
                    finance.JournalLine(account_id=expense, debit=D("10")),
                    finance.JournalLine(
                        account_id=payable, credit=D("10"), cost_centre_id=centre.id
                    ),
                ],
                entry_date=today(),
                reference_type="test_journal",
            )
        assert raised.value.details["account_type"] == str(LedgerAccountType.LIABILITY)

    async def test_a_centre_on_an_expense_leg_is_posted(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        centre = await _centre(session, tenant)
        expense = await _expense_account(session, tenant)
        bank = await _account(session, tenant, LedgerAccountType.ASSET)

        entries = await finance.post_journal(
            session,
            tenant,
            [
                finance.JournalLine(account_id=expense, debit=D("75"), cost_centre_id=centre.id),
                finance.JournalLine(account_id=bank, credit=D("75")),
            ],
            entry_date=today(),
            reference_type="test_journal",
        )
        assert [entry.cost_centre_id for entry in entries] == [centre.id, None]

    async def test_an_inactive_or_foreign_centre_takes_no_new_posting(
        self, session: AsyncSession, tenant: TenantContext, other_company, user
    ) -> None:
        dormant = await _centre(session, tenant, is_active=False)
        foreign = await _centre(session, _foreign(other_company, user))
        expense = await _expense_account(session, tenant)
        bank = await _account(session, tenant, LedgerAccountType.ASSET)

        def journal(centre_id: uuid.UUID) -> list[finance.JournalLine]:
            return [
                finance.JournalLine(account_id=expense, debit=D("5"), cost_centre_id=centre_id),
                finance.JournalLine(account_id=bank, credit=D("5")),
            ]

        with pytest.raises(AppValidationError, match="cannot take new postings"):
            await finance.post_journal(
                session, tenant, journal(dormant.id), entry_date=today(), reference_type="t"
            )
        with pytest.raises(NotFoundError):
            await finance.post_journal(
                session, tenant, journal(foreign.id), entry_date=today(), reference_type="t"
            )

    async def test_a_reversal_copies_the_centre_even_after_it_was_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Retiring a centre must not make the journals posted to it irreversible."""
        centre = await _centre(session, tenant)
        expense = await _expense_account(session, tenant)
        bank = await _account(session, tenant, LedgerAccountType.ASSET)
        original = await finance.post_journal(
            session,
            tenant,
            [
                finance.JournalLine(account_id=expense, debit=D("40"), cost_centre_id=centre.id),
                finance.JournalLine(account_id=bank, credit=D("40")),
            ],
            entry_date=today(),
            reference_type="test_journal",
        )
        journal_id = original[0].journal_id
        await _deactivate(session, tenant, centre.id)
        await _archive(session, tenant, centre.id)

        reversal = await finance.reverse_journal(
            session, tenant, journal_id, reason="Posted to the wrong period"
        )
        assert [(leg.cost_centre_id, leg.credit_amount) for leg in reversal] == [
            (centre.id, D("40")),
            (None, D("0")),
        ]
        assert all(leg.is_reversal for leg in reversal)

    async def test_approval_posts_one_expense_leg_per_centre(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        die = await _centre(session, tenant, name="Die-casting")
        maint = await _centre(session, tenant, name="Maintenance")
        invoice_id = await _invoice(
            session,
            tenant,
            cost_centre_id=maint.id,
            lines=[
                _service_line("12500", cost_centre_id=die.id),
                _service_line("18400"),
                _service_line("100", cost_centre_id=die.id),
            ],
        )
        await _approve(session, tenant, principal, invoice_id)

        detail = await finance.get_invoice_detail(session, tenant, invoice_id)
        assert detail.status is VendorInvoiceStatus.APPROVED
        expense = await _expense_account(session, tenant)
        legs = await _legs(session, invoice_id)

        purchases = [
            (leg.cost_centre_id, leg.debit_amount) for leg in legs if leg.account_id == expense
        ]
        assert purchases == [(die.id, D("12600.0000")), (maint.id, D("18400.0000"))]
        assert sum((amount for _, amount in purchases), start=D("0")) == detail.subtotal
        # Tax and the payable are balance-sheet legs: no centre on either.
        assert {leg.cost_centre_id for leg in legs if leg.account_id != expense} == {None}
        debit = sum((leg.debit_amount for leg in legs), start=D("0"))
        credit = sum((leg.credit_amount for leg in legs), start=D("0"))
        assert debit == credit == detail.total_amount

    async def test_an_invoice_naming_no_centre_posts_the_single_unallocated_leg(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        invoice_id = await _invoice(
            session, tenant, lines=[_service_line("300"), _service_line("200")]
        )
        await _approve(session, tenant, principal, invoice_id)

        expense = await _expense_account(session, tenant)
        legs = [leg for leg in await _legs(session, invoice_id) if leg.account_id == expense]
        assert [(leg.cost_centre_id, leg.debit_amount) for leg in legs] == [(None, D("500.0000"))]

    async def test_a_centre_switched_off_after_entry_blocks_approval_until_retagged(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        """Follow the row: the bill is not stranded - it stays editable."""
        old = await _centre(session, tenant, name="Old line")
        invoice_id = await _invoice(
            session, tenant, cost_centre_id=old.id, lines=[_service_line("90")]
        )
        await finance.submit_invoice(session, tenant, invoice_id, reason=None)
        await _deactivate(session, tenant, old.id)

        with pytest.raises(AppValidationError, match="cannot take new postings"):
            await finance.approve_invoice(
                session,
                tenant,
                principal,
                invoice_id,
                InvoiceApproval(override_reason="Service bill with no purchase order."),
            )

        new = await _centre(session, tenant, name="New line")
        await finance.update_invoice(
            session, tenant, invoice_id, VendorInvoiceUpdate(cost_centre_id=new.id)
        )
        await finance.approve_invoice(
            session,
            tenant,
            principal,
            invoice_id,
            InvoiceApproval(override_reason="Service bill with no purchase order."),
        )
        expense = await _expense_account(session, tenant)
        legs = [leg for leg in await _legs(session, invoice_id) if leg.account_id == expense]
        assert [leg.cost_centre_id for leg in legs] == [new.id]


# -----------------------------------------------------------------------------
# The cost-centre statement
# -----------------------------------------------------------------------------


class TestStatement:
    async def test_it_sums_posted_income_and_expense_per_centre_and_account(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        tag = uuid.uuid4().hex[:6].upper()
        press = await _centre(session, tenant, code=f"ST-{tag}-PRESS", name="Press shop")
        scrap = await _centre(session, tenant, code=f"ST-{tag}-SCRAP", name="Scrap yard")
        expense = await _expense_account(session, tenant)
        income = await _account(session, tenant, LedgerAccountType.INCOME)
        bank = await _account(session, tenant, LedgerAccountType.ASSET)
        in_period = date(2026, 5, 10)

        async def post(lines: list[finance.JournalLine], on: date) -> None:
            await finance.post_journal(
                session, tenant, lines, entry_date=on, reference_type="test_journal"
            )

        await post(
            [
                finance.JournalLine(account_id=expense, debit=D("1000"), cost_centre_id=press.id),
                finance.JournalLine(account_id=bank, credit=D("1000")),
            ],
            in_period,
        )
        await post(
            [
                finance.JournalLine(account_id=expense, credit=D("150"), cost_centre_id=press.id),
                finance.JournalLine(account_id=bank, debit=D("150")),
            ],
            in_period,
        )
        await post(
            [
                finance.JournalLine(account_id=bank, debit=D("400")),
                finance.JournalLine(account_id=income, credit=D("400"), cost_centre_id=scrap.id),
            ],
            in_period,
        )
        # Outside the period, and an unallocated leg: neither is on the statement.
        await post(
            [
                finance.JournalLine(account_id=expense, debit=D("999"), cost_centre_id=press.id),
                finance.JournalLine(account_id=bank, credit=D("999")),
            ],
            date(2026, 7, 1),
        )
        await post(
            [
                finance.JournalLine(account_id=expense, debit=D("55")),
                finance.JournalLine(account_id=bank, credit=D("55")),
            ],
            in_period,
        )
        # Retired since: posted history keeps it on the statement.
        await _archive(session, tenant, scrap.id)

        result = await reports_service.run_report(
            session,
            tenant,
            key=ReportKey.COST_CENTRE_EXPENSE,
            period=ReportPeriod(date_from=date(2026, 5, 1), date_to=date(2026, 5, 31)),
            filters=ReportFilters(search=f"ST-{tag}"),
            params=PAGE,
            may_export=False,
        )
        assert isinstance(result, reports_service.InlineReport)
        rows = [
            (
                row.cost_centre_code,  # type: ignore[attr-defined]
                row.account_type,  # type: ignore[attr-defined]
                row.debit_amount,  # type: ignore[attr-defined]
                row.credit_amount,  # type: ignore[attr-defined]
                row.net_amount,  # type: ignore[attr-defined]
            )
            for row in result.rows
        ]
        assert rows == [
            (press.code, "EXPENSE", D("1000.0000"), D("150.0000"), D("850.0000")),
            (scrap.code, "INCOME", D("0.0000"), D("400.0000"), D("-400.0000")),
        ]

        narrowed = await reports_service.run_report(
            session,
            tenant,
            key=ReportKey.COST_CENTRE_EXPENSE,
            period=ReportPeriod(date_from=date(2026, 5, 1), date_to=date(2026, 5, 31)),
            filters=ReportFilters(search="press shop"),
            params=PAGE,
            may_export=False,
        )
        assert isinstance(narrowed, reports_service.InlineReport)
        assert [row.cost_centre_id for row in narrowed.rows] == [press.id]  # type: ignore[attr-defined]


# -----------------------------------------------------------------------------
# The extended department and plant retire guards
# -----------------------------------------------------------------------------


class TestDepartmentAndPlantRetireGuards:
    async def test_a_department_named_on_a_work_centre_is_not_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        department = await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code=_code("D"), name="Machining")
        )
        await _work_centre(session, tenant, department_id=department.id)
        with pytest.raises(BusinessRuleError, match="work centres") as raised:
            await DepartmentService(session, tenant).delete_department(
                department.id, reason="No longer used"
            )
        assert raised.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_a_department_named_on_a_requisition_is_not_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        department = await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code=_code("D"), name="Purchase")
        )
        warehouse = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WH")
        )
        session.add(
            PurchaseRequisition(
                company_id=tenant.company_id,
                code=_code("PR"),
                requisition_date=today(),
                required_date=today() + timedelta(days=7),
                warehouse_id=warehouse.id,
                department_id=department.id,
            )
        )
        await session.flush()

        with pytest.raises(BusinessRuleError, match="purchase requisitions"):
            await DepartmentService(session, tenant).delete_department(
                department.id, reason="No longer used"
            )

    async def test_a_free_department_is_still_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        department = await DepartmentService(session, tenant).create_department(
            DepartmentCreate(code=_code("D"), name="Temporary")
        )
        await DepartmentService(session, tenant).delete_department(
            department.id, reason="Created in error"
        )

    async def test_a_plant_named_on_a_work_centre_or_a_production_order_is_not_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = PlantService(session, tenant)
        plant = await service.create_plant(PlantCreate(code=_code("P"), name="Works"))
        await _work_centre(session, tenant, plant_id=plant.id)
        with pytest.raises(BusinessRuleError, match="work centres"):
            await service.delete_plant(plant.id, reason="Closed")

        other = await service.create_plant(PlantCreate(code=_code("P"), name="Annexe"))
        order_id = await _production_order(session, tenant, cost_centre_id=None)
        await production_service.update_order(
            session, tenant, order_id, ProductionOrderUpdate(plant_id=other.id)
        )
        with pytest.raises(BusinessRuleError, match="production orders"):
            await service.delete_plant(other.id, reason="Closed")

    async def test_a_plant_with_cost_centres_is_not_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = PlantService(session, tenant)
        plant = await service.create_plant(PlantCreate(code=_code("P"), name="Works"))
        await CostCentreService(session, tenant).create_cost_centre(
            CostCentreCreate(
                code=_code("CC"),
                name="Works production",
                cost_centre_type=CostCentreType.PRODUCTION,
                plant_id=plant.id,
            )
        )
        with pytest.raises(BusinessRuleError, match="cost centres"):
            await service.delete_plant(plant.id, reason="Closed")
        assert (await session.get(Plant, plant.id)) is not None


# -----------------------------------------------------------------------------
# The company's bank details
# -----------------------------------------------------------------------------


class TestCompanyBankDetails:
    async def test_they_are_stored_and_audited_with_the_account_number_masked(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = CompanyService(session, tenant)
        updated = await service.update_company(
            CompanyUpdate(
                bank_name="HDFC Bank",
                bank_account_name="Atcraft Hardware Pvt Ltd",
                bank_account_number="5010 0123 4567 89",
                bank_ifsc="hdfc0001204",
                bank_branch="Rajkot - Kalawad Road",
                invoice_terms="Payment within 30 days of invoice.",
            )
        )
        assert updated.bank_ifsc == "HDFC0001204"
        assert updated.bank_account_number == "50100123456789"

        rows = await _audit_rows(
            session, tenant.company_id, AUDIT_ENTITY_COMPANY, tenant.company_id
        )
        assert rows[-1].action == AuditAction.CONFIG_CHANGE
        assert rows[-1].new_values["bank_account_number"] == "**********6789"
        assert "50100123456789" not in str(rows[-1].new_values)
        assert rows[-1].context == {"bank_account_number_changed": True}

    async def test_a_change_that_masks_alike_is_still_flagged(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Two numbers ending in the same four digits mask the same; the diff alone
        would not show that the payee account moved."""
        service = CompanyService(session, tenant)
        await service.update_company(CompanyUpdate(bank_account_number="111122223333"))
        await service.update_company(CompanyUpdate(bank_account_number="999988883333"))

        rows = await _audit_rows(
            session, tenant.company_id, AUDIT_ENTITY_COMPANY, tenant.company_id
        )
        assert rows[-1].context == {"bank_account_number_changed": True}
        assert rows[-1].changed_fields is None

    async def test_a_party_leg_is_untouched_by_any_of_this(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        """A sanity check on the invoice journal's payable leg after the split."""
        centre = await _centre(session, tenant)
        invoice_id = await _invoice(
            session, tenant, cost_centre_id=centre.id, lines=[_service_line("100")]
        )
        await _approve(session, tenant, principal, invoice_id)
        payable = [
            leg for leg in await _legs(session, invoice_id) if leg.party_type == PartyType.VENDOR
        ]
        assert len(payable) == 1
        assert payable[0].cost_centre_id is None
