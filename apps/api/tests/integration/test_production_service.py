"""Service-level production tests against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible
without it: the generated ``available_quantity`` column that decides what a
release can hold, the CHECK constraints that bound issued and returned
quantities against each other, the row locks that serialise two storekeepers
picking one kit, the partial unique index behind "one default approved BOM per
item", and the audit rows written inside the same transaction as the movement
they describe.

The inventory, allocation and BOM engines are exercised for real, not doubled.
The point of the production path is that it moves stock correctly and freezes
what it consumed; a double would assert only that this module calls something.

Isolation follows the module-scoped schema pattern from
``test_procurement_service.py``: one throwaway schema, built once, with each
test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.allocation.models import DemandType, ReservationStatus, StockReservation
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom.models import Bom, BomLine, BomOperation, BomStatus, BomType
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    TransactionType,
)
from src.modules.inventory.service import post_movement
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkSourceType,
    RateBasis,
)
from src.modules.organization.models import Company
from src.modules.production import service
from src.modules.production.models import (
    MaterialIssueStatus,
    MaterialIssueType,
    ProductionOrderComponent,
    ProductionOrderStatus,
    WorkOrderStatus,
)
from src.modules.production.schemas import (
    ApproveRequest,
    CancelRequest,
    CloseRequest,
    CompleteRequest,
    MaterialIssueCreate,
    MaterialIssueLineIn,
    MaterialIssuePostRequest,
    ProductionEntryCreate,
    ProductionOrderCreate,
    ProductionOrderFilters,
    ReleaseRequest,
    StartRequest,
    WorkOrderTransitionRequest,
    WorkOrderUpdate,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.routing.models import (
    MachineStatus,
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from src.modules.routing.schemas import ShiftAssignmentCreate, ShiftUpdate
from src.modules.routing.service import ShiftRosterService, ShiftService, local_date
from src.modules.users.models import User
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure
from tests.seed_support import seed_machine, seed_shift, seed_user, seed_vendor

pytestmark = pytest.mark.integration


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and two users, built once."""
    prepare_metadata()

    schema = f"it_prod_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    planner_id = uuid.uuid4()
    approver_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        eng = _schema_engine(schema)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
            async with factory() as setup_session, setup_session.begin():
                for cid in (company_id, other_company_id):
                    setup_session.add(
                        Company(
                            id=cid,
                            code=f"ATC{uuid.uuid4().hex[:6].upper()}",
                            name="Atcraft Manufacturing",
                            country="India",
                            currency="INR",
                            state_code="27",
                            is_active=True,
                        )
                    )
                for uid, handle in ((planner_id, "planner"), (approver_id, "approver")):
                    setup_session.add(
                        User(
                            id=uid,
                            company_id=company_id,
                            email=f"{handle}-{uuid.uuid4().hex[:6]}@atcraft.test",
                            full_name="Tester",
                            password_hash="x",
                            is_active=True,
                        )
                    )
        finally:
            await eng.dispose()

    async def teardown() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, other_company_id, planner_id, approver_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def db(_schema) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back at the end of the test."""
    schema, *_ = _schema
    eng = _schema_engine(schema)
    async with eng.connect() as connection:
        outer = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as session:
            yield session
        await outer.rollback()
    await eng.dispose()


@pytest.fixture
def tenant(_schema) -> TenantContext:
    _, company_id, _, planner_id, _ = _schema
    return TenantContext(company_id=company_id, user_id=planner_id)


@pytest.fixture
def other_tenant(_schema) -> TenantContext:
    _, _, other_company_id, planner_id, _ = _schema
    return TenantContext(company_id=other_company_id, user_id=planner_id)


def _principal(company_id: uuid.UUID, user_id: uuid.UUID, *permissions: Permission) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email="tester@atcraft.test",
        full_name="Tester",
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions)
        if permissions
        else frozenset(str(p) for p in Permission),
        role_codes=("PRODUCTION_MANAGER",),
    )


@pytest.fixture
def planner(_schema) -> Principal:
    _, company_id, _, planner_id, _ = _schema
    return _principal(company_id, planner_id)


@pytest.fixture
def approver(_schema) -> Principal:
    _, company_id, _, _, approver_id = _schema
    return _principal(company_id, approver_id)


@pytest.fixture
def storekeeper(_schema) -> Principal:
    """Holds every picking right but not the production approval right."""
    _, company_id, _, planner_id, _ = _schema
    return _principal(
        company_id,
        planner_id,
        Permission.PRODUCTION_ORDER_VIEW,
        Permission.MATERIAL_ISSUE_CREATE,
        Permission.MATERIAL_RETURN_CREATE,
    )


# -----------------------------------------------------------------------------
# Master data
# -----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def masters(db: AsyncSession, tenant: TenantContext):
    """A kit, its two components, an approved structure and an approved process.

    Built through the ORM deliberately: many NOT NULL columns default in Python
    rather than in the database, and a hand-written INSERT column list goes stale
    the moment another module adds one.
    """
    suffix = uuid.uuid4().hex[:6].upper()

    warehouse = Warehouse(
        company_id=tenant.company_id,
        code=f"WH{suffix}",
        name="Main Store",
        warehouse_type=WarehouseType.RAW_MATERIAL,
        state_code="27",
        is_active=True,
    )
    uom = UnitOfMeasure(
        company_id=tenant.company_id,
        code=f"NOS{suffix}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=True,
    )
    work_centre = WorkCenter(
        company_id=tenant.company_id,
        code=f"WC{suffix}",
        name="Assembly cell",
        capacity_per_hour=Decimal("60"),
        cost_per_hour=Decimal("600"),
        setup_cost=Decimal("0"),
        working_hours_per_day=Decimal("8"),
        is_active=True,
    )
    db.add_all([warehouse, uom, work_centre])
    await db.flush()

    def make_item(code: str, *, cost: str, **kwargs) -> Item:
        defaults = {
            "item_group": ItemGroup.COMPONENT,
            "production_type": ProductionType.BUY,
            "can_purchase": True,
            "can_be_bom_component": True,
        }
        return Item(
            company_id=tenant.company_id,
            code=code,
            name=f"Item {code}",
            base_uom_id=uom.id,
            gst_rate=Decimal("18"),
            standard_cost=Decimal(cost),
            is_active=True,
            **{**defaults, **kwargs},
        )

    kit = make_item(
        f"KIT-{suffix}",
        cost="100",
        can_manufacture=True,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        default_warehouse_id=warehouse.id,
    )
    body = make_item(f"BODY-{suffix}", cost="40", default_warehouse_id=warehouse.id)
    screw = make_item(f"SCREW-{suffix}", cost="5", default_warehouse_id=warehouse.id)
    db.add_all([kit, body, screw])
    await db.flush()

    bom = Bom(
        company_id=tenant.company_id,
        code=f"BOM-{suffix}",
        item_id=kit.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.PRODUCTION,
        is_default=True,
        output_quantity=Decimal("1"),
        base_uom_id=uom.id,
        effective_from=today() - timedelta(days=30),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    db.add(bom)
    await db.flush()
    db.add_all(
        [
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=kit.id,
                line_number=1,
                component_item_id=body.id,
                quantity_per=Decimal("1"),
                uom_id=uom.id,
            ),
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=kit.id,
                line_number=2,
                component_item_id=screw.id,
                quantity_per=Decimal("6"),
                uom_id=uom.id,
                # 5% process loss: 600 screws needed becomes 630 to pick.
                scrap_percentage=Decimal("5"),
            ),
        ]
    )

    routing = Routing(
        company_id=tenant.company_id,
        code=f"RT-{suffix}",
        item_id=kit.id,
        version=1,
        status=RoutingStatus.APPROVED,
        is_default=True,
        effective_from=today() - timedelta(days=30),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    db.add(routing)
    await db.flush()
    db.add(
        RoutingOperation(
            company_id=tenant.company_id,
            routing_id=routing.id,
            sequence=10,
            operation_code="ASM",
            operation_name="Assemble",
            work_center_id=work_centre.id,
            setup_time_minutes=Decimal("30"),
            run_time_per_unit_minutes=Decimal("1"),
            process_cost=Decimal("0"),
        )
    )
    await db.flush()

    return {
        "warehouse": warehouse,
        "uom": uom,
        "work_centre": work_centre,
        "kit": kit,
        "body": body,
        "screw": screw,
        "bom": bom,
        "routing": routing,
    }


async def stock(
    db: AsyncSession,
    tenant: TenantContext,
    item: Item,
    warehouse: Warehouse,
    quantity: str,
    unit_cost: str,
) -> None:
    """Seed a position through the real engine, so the average cost is real."""
    async with transaction(db):
        await post_movement(
            db,
            tenant,
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening_balance",
            reference_id=None,
            reference_code=None,
            reason="Opening stock for the production test",
            unit_cost=Decimal(unit_cost),
        )


@pytest_asyncio.fixture
async def stocked(db: AsyncSession, tenant: TenantContext, masters):
    """Plenty of both components on the shelf."""
    await stock(db, tenant, masters["body"], masters["warehouse"], "1000", "40")
    await stock(db, tenant, masters["screw"], masters["warehouse"], "1000", "5")
    return masters


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def make_order(db, tenant, masters, *, quantity: str = "100", **kwargs):
    payload = ProductionOrderCreate(
        item_id=masters["kit"].id,
        planned_quantity=Decimal(quantity),
        warehouse_id=masters["warehouse"].id,
        planned_start_date=today(),
        planned_end_date=today() + timedelta(days=5),
        **kwargs,
    )
    return await service.create_order(db, tenant, payload)


async def approve(db, tenant, principal, order):
    """Approve as somebody other than the author - segregation has its own test."""
    second_person = dataclasses.replace(principal, is_superuser=True)
    return await service.approve_order(
        db, tenant, second_person, order.id, ApproveRequest(note="approved")
    )


async def release(db, tenant, order, **kwargs):
    return await service.release_order(db, tenant, order.id, ReleaseRequest(**kwargs))


async def components_of(db, tenant, order) -> list[ProductionOrderComponent]:
    stmt = (
        select(ProductionOrderComponent)
        .where(
            ProductionOrderComponent.company_id == tenant.company_id,
            ProductionOrderComponent.production_order_id == order.id,
        )
        .order_by(ProductionOrderComponent.line_number)
    )
    return list((await db.execute(stmt)).scalars().all())


async def issue_everything(db, tenant, principal, order, *, factor: str = "1"):
    """Draft and post one issue covering every component of the job."""
    rows = await components_of(db, tenant, order)
    payload = MaterialIssueCreate(
        reason="Kit issued to the line",
        lines=[
            MaterialIssueLineIn(
                line_number=index,
                component_id=component.id,
                quantity=(Decimal(component.required_quantity) * Decimal(factor)).quantize(
                    Decimal("0.0001")
                ),
            )
            for index, component in enumerate(rows, start=1)
        ],
    )
    issue, _ = await service.create_material_issue(
        db, tenant, order.id, payload, issue_type=MaterialIssueType.ISSUE
    )
    return await service.post_material_issue(
        db, tenant, principal, issue.id, MaterialIssuePostRequest()
    )


async def balance_of(db, tenant, item, warehouse) -> InventoryBalance:
    stmt = select(InventoryBalance).where(
        InventoryBalance.company_id == tenant.company_id,
        InventoryBalance.item_id == item.id,
        InventoryBalance.warehouse_id == warehouse.id,
    )
    return (await db.execute(stmt)).scalars().one()


async def movements_of(db, tenant, *, reference_id: uuid.UUID) -> list[InventoryTransaction]:
    stmt = (
        select(InventoryTransaction)
        .where(
            InventoryTransaction.company_id == tenant.company_id,
            InventoryTransaction.reference_id == reference_id,
        )
        .order_by(InventoryTransaction.id)
    )
    return list((await db.execute(stmt)).scalars().all())


# -----------------------------------------------------------------------------
# Creation and approval
# -----------------------------------------------------------------------------


async def test_a_job_can_only_be_raised_for_a_manufacturable_item(db, tenant, masters):
    """Master rules section 1: the capability flag decides, never the item group."""
    with pytest.raises(BusinessRuleError, match="not flagged as manufacturable"):
        await service.create_order(
            db,
            tenant,
            ProductionOrderCreate(
                item_id=masters["body"].id,
                planned_quantity=Decimal("10"),
                warehouse_id=masters["warehouse"].id,
                planned_start_date=today(),
                planned_end_date=today(),
            ),
        )


async def test_an_order_cannot_be_approved_by_the_person_who_raised_it(
    db, tenant, masters, planner
):
    order = await make_order(db, tenant, masters)
    with pytest.raises(AuthorizationError) as exc:
        await service.approve_order(db, tenant, planner, order.id, ApproveRequest())
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED


async def test_approval_records_who_and_when_and_writes_an_audit_row(db, tenant, masters, approver):
    order = await make_order(db, tenant, masters)
    approved = await service.approve_order(db, tenant, approver, order.id, ApproveRequest())

    assert approved.status is ProductionOrderStatus.APPROVED
    assert approved.approved_by_id == approver.user_id
    assert approved.approved_at is not None

    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.company_id == tenant.company_id,
            AuditLog.entity_id == order.id,
            AuditLog.action == AuditAction.APPROVE,
        )
    )
    assert (await db.execute(stmt)).scalar_one() == 1


async def test_an_approved_order_can_no_longer_be_edited(db, tenant, masters, approver):
    from src.modules.production.schemas import ProductionOrderUpdate

    order = await make_order(db, tenant, masters)
    await service.approve_order(db, tenant, approver, order.id, ApproveRequest())
    with pytest.raises(BusinessRuleError, match="can no longer be edited"):
        await service.update_order(
            db, tenant, order.id, ProductionOrderUpdate(planned_quantity=Decimal("5"))
        )


# -----------------------------------------------------------------------------
# Release
# -----------------------------------------------------------------------------


async def test_release_snapshots_the_exploded_structure_and_holds_the_material(
    db, tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    result = await release(db, tenant, order)

    assert result.component_count == 2
    assert result.work_order_count == 1
    assert result.fully_reserved is True
    assert result.shortages == []

    rows = await components_of(db, tenant, order)
    by_code = {row.item_code: row for row in rows}
    assert by_code[stocked["body"].code].required_quantity == Decimal("100.0000")
    # 6 per unit with a 5% process loss: 600 becomes 630 to pick.
    assert by_code[stocked["screw"].code].required_quantity == Decimal("630.0000")

    # The reservation moved reserved_quantity, not on_hand.
    screw_balance = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert screw_balance.on_hand_quantity == Decimal("1000.0000")
    assert screw_balance.reserved_quantity == Decimal("630.0000")
    assert screw_balance.available_quantity == Decimal("370.0000")


async def test_release_reports_a_shortage_per_component_instead_of_failing(
    db, tenant, masters, approver
):
    """A planner needs the whole list of what is missing, not the first failure."""
    await stock(db, tenant, masters["body"], masters["warehouse"], "40", "40")
    # No screws on the shelf at all.
    order = await make_order(db, tenant, masters, quantity="100")
    await approve(db, tenant, approver, order)
    result = await release(db, tenant, order)

    assert result.order.status is ProductionOrderStatus.RELEASED
    assert result.fully_reserved is False
    shortages = {s.item_code: s for s in result.shortages}
    assert set(shortages) == {masters["body"].code, masters["screw"].code}
    # What was there is still held, so nobody else can take it.
    assert shortages[masters["body"].code].reserved_quantity == Decimal("40.0000")
    assert shortages[masters["body"].code].shortage_quantity == Decimal("60.0000")
    assert shortages[masters["screw"].code].reserved_quantity == Decimal("0")
    assert shortages[masters["screw"].code].shortage_quantity == Decimal("630.0000")


async def test_release_refuses_a_structure_that_is_not_approved(db, tenant, masters, approver):
    masters["bom"].status = BomStatus.DRAFT
    masters["bom"].is_default = False
    masters["bom"].approved_by_id = None
    masters["bom"].approved_at = None
    await db.flush()

    order = await make_order(db, tenant, masters)
    await approve(db, tenant, approver, order)
    with pytest.raises(BusinessRuleError) as exc:
        await release(db, tenant, order)
    assert exc.value.code is ErrorCode.BOM_NOT_FOUND


async def test_release_issues_one_work_order_per_routing_operation(db, tenant, stocked, approver):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    detail = await service.get_order_detail(db, tenant, order.id)
    assert [wo.sequence for wo in detail.work_orders] == [10]
    work_order = detail.work_orders[0]
    assert work_order.operation_code == "ASM"
    assert work_order.planned_setup_minutes == Decimal("30.0000")
    assert work_order.planned_run_minutes == Decimal("100.0000")
    assert work_order.status is WorkOrderStatus.PENDING


# -----------------------------------------------------------------------------
# Shifts on operations and bookings (WP B2)
# -----------------------------------------------------------------------------


async def _released_operation(db, tenant, stocked, approver):
    """A released job and the id of its one operation, held as a plain value."""
    order = await make_order(db, tenant, stocked)
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    detail = await service.get_order_detail(db, tenant, order.id)
    return order, detail.work_orders[0].id


async def test_assigning_a_shift_snapshots_its_code_and_refuses_a_foreign_or_inactive_one(
    db, tenant, other_tenant, stocked, approver
):
    _, work_order_id = await _released_operation(db, tenant, stocked, approver)
    early = await seed_shift(db, company_id=tenant.company_id, code="A")
    early_id = early.id

    assigned = await service.update_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(shift_id=early_id)
    )
    assert (assigned.shift_id, assigned.shift) == (early_id, "A")
    audit = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == work_order_id, AuditLog.action == AuditAction.UPDATE)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert audit is not None
    assert audit.new_values == {"shift_id": str(early_id), "shift": "A"}

    # Renaming the shift is free and does not rewrite the snapshot.
    await ShiftService(db, tenant).update_shift(early_id, ShiftUpdate(name="Early"))
    reread = await service.update_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(notes="Renamed shift")
    )
    assert reread.shift == "A"

    foreign = await seed_shift(db, company_id=other_tenant.company_id, code="F")
    with pytest.raises(NotFoundError):
        await service.update_work_order(
            db, tenant, work_order_id, WorkOrderUpdate(shift_id=foreign.id)
        )
    retired = await seed_shift(db, company_id=tenant.company_id, code="OLD", is_active=False)
    with pytest.raises(ValidationError) as caught:
        await service.update_work_order(
            db, tenant, work_order_id, WorkOrderUpdate(shift_id=retired.id)
        )
    assert set(caught.value.details["fields"]) == {"shift_id"}

    cleared = await service.update_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(shift_id=None)
    )
    assert (cleared.shift_id, cleared.shift) == (None, None)


async def test_an_operation_refuses_inactive_or_vendor_operators_and_retired_machines(
    db, tenant, stocked, approver
):
    _, work_order_id = await _released_operation(db, tenant, stocked, approver)
    centre_id = stocked["work_centre"].id
    gone = await seed_user(db, company_id=tenant.company_id, full_name="Left", is_active=False)
    vendor = await seed_vendor(db, company_id=tenant.company_id)
    portal = await seed_user(
        db, company_id=tenant.company_id, full_name="Portal", vendor_id=vendor.id
    )
    retired = await seed_machine(
        db,
        company_id=tenant.company_id,
        work_center_id=centre_id,
        status=MachineStatus.RETIRED,
        is_active=False,
    )
    serviced = await seed_machine(
        db, company_id=tenant.company_id, work_center_id=centre_id, status=MachineStatus.MAINTENANCE
    )
    serviced_id = serviced.id

    with pytest.raises(BusinessRuleError, match="inactive"):
        await service.update_work_order(
            db, tenant, work_order_id, WorkOrderUpdate(operator_user_id=gone.id)
        )
    with pytest.raises(BusinessRuleError, match="vendor-portal"):
        await service.update_work_order(
            db, tenant, work_order_id, WorkOrderUpdate(operator_user_id=portal.id)
        )
    with pytest.raises(BusinessRuleError, match="retired"):
        await service.update_work_order(
            db, tenant, work_order_id, WorkOrderUpdate(machine_id=retired.id)
        )
    # Maintenance is not retirement: a job may be lined up for after the repair.
    lined_up = await service.update_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(machine_id=serviced_id)
    )
    assert lined_up.machine_id == serviced_id


async def test_the_assignment_warns_when_the_roster_does_not_plan_the_operator(
    db, tenant, stocked, approver
):
    _, work_order_id = await _released_operation(db, tenant, stocked, approver)
    shift = await seed_shift(db, company_id=tenant.company_id, code="A")
    shift_id = shift.id
    operator = await seed_user(db, company_id=tenant.company_id, full_name="Amit Shah")
    operator_id = operator.id

    staffed, message = await service.assign_work_order(
        db,
        tenant,
        work_order_id,
        WorkOrderUpdate(operator_user_id=operator_id, shift_id=shift_id),
    )
    assert message.endswith(service.ROSTER_MISMATCH_WARNING)
    assert staffed.operator_user_id == operator_id

    # Plan him on that shift, at that cell, on the planned start's local date.
    roster_date = local_date(staffed.planned_start_at, ZoneInfo("Asia/Kolkata"))
    await ShiftRosterService(db, tenant).plan(
        [
            ShiftAssignmentCreate(
                roster_date=roster_date,
                shift_id=shift_id,
                work_center_id=stocked["work_centre"].id,
                operator_user_id=operator_id,
            )
        ]
    )
    code = staffed.code
    _, message = await service.assign_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(shift_id=shift_id)
    )
    assert message == f"{code} updated."

    # A notes-only edit is no staffing decision and asks the roster nothing.
    _, message = await service.assign_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(notes="Bring the setter")
    )
    assert message == f"{code} updated."


async def test_a_booking_snapshots_its_shift_and_downtime_alone_is_a_booking(
    db, tenant, other_tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked)
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, approver, order)
    await service.start_order(db, tenant, order.id, StartRequest())
    order_id = order.id
    shift = await seed_shift(
        db, company_id=tenant.company_id, code="B", start_time=time(14), end_time=time(22)
    )
    shift_id = shift.id
    operator = await seed_user(db, company_id=tenant.company_id, full_name="Neha Patel")

    result = await service.create_production_entry(
        db,
        tenant,
        order_id,
        ProductionEntryCreate(
            shift_id=shift_id,
            operator_user_id=operator.id,
            downtime_minutes=Decimal("25"),
            notes="Waiting for the setter",
        ),
    )
    assert (result.entry.shift_id, result.entry.shift) == (shift_id, "B")
    assert result.output_movement is None
    assert result.message.startswith("Booked 25")
    assert "downtime" in result.message
    assert result.message.endswith(service.ROSTER_MISMATCH_WARNING)

    foreign = await seed_shift(db, company_id=other_tenant.company_id, code="F")
    with pytest.raises(NotFoundError):
        await service.create_production_entry(
            db,
            tenant,
            order_id,
            ProductionEntryCreate(shift_id=foreign.id, downtime_minutes=Decimal("5")),
        )


async def test_daily_production_by_shift_sums_each_shift_cell_and_operator(
    db, tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked)
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, approver, order)
    await service.start_order(db, tenant, order.id, StartRequest())
    order_id = order.id
    work_order_id = (await service.get_order_detail(db, tenant, order_id)).work_orders[0].id
    shift_a = await seed_shift(db, company_id=tenant.company_id, code="A")
    shift_b = await seed_shift(
        db, company_id=tenant.company_id, code="B", start_time=time(14), end_time=time(22)
    )
    operator = await seed_user(db, company_id=tenant.company_id, full_name="Amit Shah")
    shift_a_id, shift_b_id, operator_id = shift_a.id, shift_b.id, operator.id

    for shift_id, produced, rejected, downtime in (
        (shift_a_id, "30", "2", "0"),
        (shift_a_id, "20", "0", "10"),
        (shift_b_id, "10", "0", "15"),
    ):
        await service.create_production_entry(
            db,
            tenant,
            order_id,
            ProductionEntryCreate(
                work_order_id=work_order_id,
                shift_id=shift_id,
                operator_user_id=operator_id,
                produced_quantity=Decimal(produced),
                rejected_quantity=Decimal(rejected),
                rejection_reason="Scored face" if rejected != "0" else None,
                downtime_minutes=Decimal(downtime),
            ),
        )

    result = await reports_service.run_report(
        db,
        tenant,
        key=ReportKey.SHIFT_PRODUCTION,
        period=ReportPeriod(date_from=today(), date_to=today()),
        filters=ReportFilters(),
        params=PageParams(),
        may_export=False,
    )
    assert isinstance(result, reports_service.InlineReport)
    rows = {row.shift: row for row in result.rows}  # type: ignore[attr-defined]
    assert set(rows) == {"A", "B"}
    morning = rows["A"]
    assert (morning.produced_quantity, morning.rejected_quantity) == (Decimal("50"), Decimal("2"))
    assert (morning.downtime_minutes, morning.bookings) == (Decimal("10"), 2)
    assert morning.work_center_code == stocked["work_centre"].code
    assert morning.operator_name == "Amit Shah"
    # 50 good of 52 attempted.
    assert morning.yield_percent == Decimal("96.15")
    assert (rows["B"].bookings, rows["B"].downtime_minutes) == (1, Decimal("15"))


async def test_machine_utilisation_counts_rostered_minutes_beside_calendar_capacity(
    db, tenant, stocked, approver
):
    _, work_order_id = await _released_operation(db, tenant, stocked, approver)
    centre_id = stocked["work_centre"].id
    machine = await seed_machine(db, company_id=tenant.company_id, work_center_id=centre_id)
    machine_id = machine.id
    general = await seed_shift(
        db,
        company_id=tenant.company_id,
        code="GEN",
        start_time=time(9),
        end_time=time(17, 30),
        break_minutes=30,
    )
    operator = await seed_user(db, company_id=tenant.company_id, full_name="Ravi Desai")

    await service.update_work_order(
        db, tenant, work_order_id, WorkOrderUpdate(machine_id=machine_id)
    )
    started = utcnow() - timedelta(hours=3)
    for status, moment in (
        (WorkOrderStatus.IN_PROGRESS, started),
        (WorkOrderStatus.COMPLETED, started + timedelta(minutes=120)),
    ):
        await service.transition_work_order(
            db,
            tenant,
            work_order_id,
            WorkOrderTransitionRequest(status=status, occurred_at=moment),
        )
    await ShiftRosterService(db, tenant).plan(
        [
            ShiftAssignmentCreate(
                roster_date=today(),
                shift_id=general.id,
                work_center_id=centre_id,
                machine_id=machine_id,
                operator_user_id=operator.id,
            )
        ]
    )

    period = ReportPeriod(date_from=today() - timedelta(days=1), date_to=today())
    definition = reports_service.definition_for(ReportKey.MACHINE_UTILISATION)
    context = reports_service.build_context(definition, period, ReportFilters())
    stmt = definition.build(ReportQueryRepository(db, tenant), context)
    (row,) = [dict(found) for found in (await db.execute(stmt)).mappings()]
    assert definition.derive is not None
    shaped = definition.derive(row, context)
    # 09:00-17:30 less a 30-minute break: 480 rostered minutes; 120 busy.
    assert Decimal(shaped["rostered_minutes"]) == Decimal("480")
    assert shaped["rostered_utilisation_percent"] == Decimal("25.00")
    # Calendar capacity is untouched: 8 hours a day over the two days.
    assert shaped["available_minutes"] == Decimal("960.00")


async def test_a_bom_revised_after_release_does_not_change_the_released_job(
    db, tenant, stocked, approver
):
    """The whole reason the components are a snapshot.

    An engineering change belongs to the next order, not to the kit that has
    already been picked against the drawing as it stood.
    """
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    before = {c.item_code: c.required_quantity for c in await components_of(db, tenant, order)}

    stmt = select(BomLine).where(
        BomLine.company_id == tenant.company_id,
        BomLine.bom_id == stocked["bom"].id,
        BomLine.line_number == 2,
    )
    screw_line = (await db.execute(stmt)).scalars().one()
    screw_line.quantity_per = Decimal("12")
    await db.flush()

    after = {c.item_code: c.required_quantity for c in await components_of(db, tenant, order)}
    assert after == before
    assert after[stocked["screw"].code] == Decimal("630.0000")


async def test_the_component_view_reports_the_live_shortage_against_the_snapshot(
    db, tenant, masters, approver
):
    await stock(db, tenant, masters["body"], masters["warehouse"], "40", "40")
    order = await make_order(db, tenant, masters, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    rows, _ = await service.list_components(
        db, tenant, order.id, PageParams(), SortParams(), shortage_only=True
    )
    by_code = {row.item_code: row for row in rows}
    body = by_code[masters["body"].code]
    assert body.required_quantity == Decimal("100.0000")
    assert body.reserved_quantity == Decimal("40.0000")
    assert body.shortage_quantity == Decimal("60.0000")
    # Everything free has been promised to this job, so nothing is left over.
    assert body.available_quantity == Decimal("0.0000")


# -----------------------------------------------------------------------------
# Release -> issue -> complete, end to end
# -----------------------------------------------------------------------------


async def test_release_issue_complete_moves_stock_end_to_end(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    # --- issue -------------------------------------------------------------
    posted = await issue_everything(db, tenant, planner, order)
    assert posted.order_status is ProductionOrderStatus.MATERIAL_ISSUED
    assert posted.outstanding_components == []
    assert {m.transaction_type for m in posted.movements} == {"MATERIAL_ISSUE"}

    body_balance = await balance_of(db, tenant, stocked["body"], stocked["warehouse"])
    screw_balance = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert body_balance.on_hand_quantity == Decimal("900.0000")
    assert screw_balance.on_hand_quantity == Decimal("370.0000")
    # The reservations were consumed, not left standing beside the issue.
    assert body_balance.reserved_quantity == Decimal("0.0000")
    assert screw_balance.reserved_quantity == Decimal("0.0000")

    rows = {c.item_code: c for c in await components_of(db, tenant, order)}
    assert rows[stocked["body"].code].issued_quantity == Decimal("100.0000")
    assert rows[stocked["screw"].code].issued_quantity == Decimal("630.0000")

    # --- start and book ----------------------------------------------------
    await service.start_order(db, tenant, order.id, StartRequest())
    detail = await service.get_order_detail(db, tenant, order.id)
    work_order_id = detail.work_orders[0].id

    started = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    await service.transition_work_order(
        db,
        tenant,
        work_order_id,
        WorkOrderTransitionRequest(status=WorkOrderStatus.IN_PROGRESS, occurred_at=started),
    )
    morning = await seed_shift(db, company_id=tenant.company_id, code="A")
    booking = await service.create_production_entry(
        db,
        tenant,
        order.id,
        ProductionEntryCreate(
            work_order_id=work_order_id,
            produced_quantity=Decimal("100"),
            shift_id=morning.id,
            downtime_minutes=Decimal("15"),
        ),
    )
    assert booking.output_movement is not None
    assert booking.output_movement.transaction_type == "PRODUCTION_RECEIPT"

    kit_balance = await balance_of(db, tenant, stocked["kit"], stocked["warehouse"])
    assert kit_balance.on_hand_quantity == Decimal("100.0000")

    await service.transition_work_order(
        db,
        tenant,
        work_order_id,
        WorkOrderTransitionRequest(
            status=WorkOrderStatus.COMPLETED, occurred_at=started + timedelta(hours=2)
        ),
    )

    # --- complete ----------------------------------------------------------
    completed = await service.complete_order(db, tenant, order.id, CompleteRequest())
    assert completed.order.status is ProductionOrderStatus.COMPLETED
    assert completed.order.produced_quantity == Decimal("100.0000")
    assert completed.consumed_component_count == 2

    # Consumption is a WIP fact, not a second movement: the stock left the store
    # when it was issued, and completing must not remove it twice.
    rows = {c.item_code: c for c in await components_of(db, tenant, order)}
    assert rows[stocked["screw"].code].consumed_quantity == Decimal("630.0000")
    screw_balance = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert screw_balance.on_hand_quantity == Decimal("370.0000")

    ledger = await movements_of(db, tenant, reference_id=order.id)
    assert [t.transaction_type for t in ledger] == [TransactionType.PRODUCTION_RECEIPT]
    assert ledger[0].quantity == Decimal("100.0000")


async def test_closing_costs_the_job_from_real_movements_and_real_time(
    db, tenant, stocked, planner, approver
):
    db.add(
        BomOperation(
            company_id=tenant.company_id,
            bom_id=stocked["bom"].id,
            operation_sequence=10,
            name="PVD",
            setup_time_minutes=Decimal("0"),
            run_time_minutes_per_unit=Decimal("0"),
            cost_per_hour=Decimal("0"),
            fixed_cost=Decimal("9"),
            is_outsourced=True,
        )
    )
    await db.flush()
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await service.start_order(db, tenant, order.id, StartRequest())

    detail = await service.get_order_detail(db, tenant, order.id)
    work_order_id = detail.work_orders[0].id
    started = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    await service.transition_work_order(
        db,
        tenant,
        work_order_id,
        WorkOrderTransitionRequest(status=WorkOrderStatus.IN_PROGRESS, occurred_at=started),
    )
    await service.create_production_entry(
        db,
        tenant,
        order.id,
        ProductionEntryCreate(work_order_id=work_order_id, produced_quantity=Decimal("100")),
    )
    await service.transition_work_order(
        db,
        tenant,
        work_order_id,
        WorkOrderTransitionRequest(
            status=WorkOrderStatus.COMPLETED, occurred_at=started + timedelta(hours=2)
        ),
    )
    await service.complete_order(db, tenant, order.id, CompleteRequest())

    vendor = await seed_vendor(db, company_id=tenant.company_id)
    db.add(
        JobWorkOrder(
            company_id=tenant.company_id,
            code=f"JW-{uuid.uuid4().hex[:8]}",
            vendor_id=vendor.id,
            process_type=JobWorkProcess.PVD,
            process_description="Production costing test",
            expected_return_date=today(),
            warehouse_id=stocked["warehouse"].id,
            return_warehouse_id=stocked["warehouse"].id,
            rate_per_unit=Decimal("9"),
            rate_basis=RateBasis.PER_PIECE,
            total_charges=Decimal("900"),
            status=JobWorkOrderStatus.COMPLETED,
            source_type=JobWorkSourceType.PRODUCTION_ORDER,
            source_id=order.id,
        )
    )
    await db.flush()

    closed = await service.close_order(db, tenant, order.id, CloseRequest(closure_note="Ran clean"))
    # 100 bodies at 40 plus 630 screws at 5, and the accrued vendor charge.
    assert closed.cost.material_cost == Decimal("7150.00")
    # Two clocked hours at 600 an hour.
    assert closed.cost.operation_cost == Decimal("1200.00")
    assert closed.cost.subcontract_cost == Decimal("900.00")
    assert closed.cost.total_cost == Decimal("9250.00")
    assert closed.cost.cost_per_unit == Decimal("92.50")
    assert closed.cost.standard_unit_cost == Decimal("80.50")
    assert closed.cost.actual_unit_cost == Decimal("92.50")
    assert closed.cost.material_variance == Decimal("0.00")
    assert closed.cost.operation_variance == Decimal("1200.00")
    assert closed.cost.subcontract_variance == Decimal("0.00")
    assert closed.cost.total_variance == Decimal("1200.00")
    assert closed.order.status is ProductionOrderStatus.CLOSED
    assert closed.order.actual_subcontract_cost == Decimal("900.0000")
    assert closed.order.standard_subcontract_unit_cost == Decimal("9.0000")

    # A later rate revision must move neither the stored total nor the
    # per-operation breakdown under it: the work order carries the rate the job
    # was released at, so the two can never drift apart.
    stocked["work_centre"].cost_per_hour = Decimal("9999")
    await db.flush()
    frozen = await service.production_cost(db, tenant, order.id)
    assert frozen.is_final is True
    assert frozen.total_cost == Decimal("9250.00")
    assert frozen.subcontract_cost == Decimal("900.00")
    assert frozen.operations[0].cost_per_hour == Decimal("600.00")
    assert sum(line.total_cost for line in frozen.operations) == frozen.operation_cost


async def test_an_operation_of_a_closed_job_can_no_longer_be_changed(
    db, tenant, stocked, planner, approver
):
    """Closure froze a cost computed from these rows."""
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await service.start_order(db, tenant, order.id, StartRequest())
    await service.create_production_entry(
        db, tenant, order.id, ProductionEntryCreate(produced_quantity=Decimal("100"))
    )
    await service.complete_order(db, tenant, order.id, CompleteRequest())
    await service.close_order(db, tenant, order.id, CloseRequest())

    detail = await service.get_order_detail(db, tenant, order.id)
    with pytest.raises(BusinessRuleError, match="can no longer be changed"):
        await service.transition_work_order(
            db,
            tenant,
            detail.work_orders[0].id,
            WorkOrderTransitionRequest(status=WorkOrderStatus.IN_PROGRESS),
        )


# -----------------------------------------------------------------------------
# Issue rules
# -----------------------------------------------------------------------------


async def test_an_issue_past_the_over_issue_tolerance_is_refused(db, tenant, stocked, approver):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    with pytest.raises(BusinessRuleError, match="over-issue tolerance"):
        await service.create_material_issue(
            db,
            tenant,
            order.id,
            MaterialIssueCreate(
                reason="Over-picked by mistake",
                lines=[
                    MaterialIssueLineIn(
                        line_number=1, component_id=body.id, quantity=Decimal("200")
                    )
                ],
            ),
            issue_type=MaterialIssueType.ISSUE,
        )


async def test_every_movement_of_a_back_dated_issue_carries_the_documents_own_date(
    db, tenant, stocked, planner, approver
):
    """One document, one business date - however each line was sourced.

    A kit is part-covered by the job's reservations and part-covered by free
    stock, so the document posts through two different paths: ``allocation``
    consumes the reservations, and the movement engine posts the remainder. The
    consume path defaults ``occurred_at`` to *now* when it is not told otherwise,
    which silently split a March issue across March and April - half the material
    landing in the period the storekeeper keyed it rather than the period it was
    picked in.
    """
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    # Release held 100; asking for 150 forces the balance to come from free stock,
    # so the document posts a consume movement *and* a direct movement.
    back_dated = today() - timedelta(days=20)
    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Picked last month, keyed today",
            issue_date=back_dated,
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=body.id, quantity=Decimal("105"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    result = await service.post_material_issue(
        db, tenant, planner, issue.id, MaterialIssuePostRequest()
    )

    movements = await movements_of(db, tenant, reference_id=issue.id)
    assert len(movements) > 1, "the fixture must exercise both posting paths"
    assert {m.occurred_at.date() for m in movements} == {back_dated}
    assert len(result.movements) == len(movements)


async def test_a_partial_issue_leaves_the_order_released_and_names_what_is_short(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="First half of the kit",
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=body.id, quantity=Decimal("100"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    result = await service.post_material_issue(
        db, tenant, planner, issue.id, MaterialIssuePostRequest()
    )
    assert result.order_status is ProductionOrderStatus.RELEASED
    assert [c.item_code for c in result.outstanding_components] == [stocked["screw"].code]


async def test_forcing_a_partial_issue_needs_the_approval_right(
    db, tenant, stocked, storekeeper, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Start with what we have",
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=body.id, quantity=Decimal("100"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    override = MaterialIssuePostRequest(
        allow_partial_issue=True, override_reason="Screws arrive tomorrow"
    )
    with pytest.raises(AuthorizationError):
        await service.post_material_issue(db, tenant, storekeeper, issue.id, override)


async def test_an_approver_may_force_a_partial_issue_and_it_is_recorded(
    db, tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Start with what we have",
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=body.id, quantity=Decimal("100"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    result = await service.post_material_issue(
        db,
        tenant,
        approver,
        issue.id,
        MaterialIssuePostRequest(
            allow_partial_issue=True, override_reason="Screws arrive tomorrow"
        ),
    )
    assert result.order_status is ProductionOrderStatus.MATERIAL_ISSUED

    # Posting writes both a status change and a movement summary under the same
    # action, so the override is looked for by the field that records it.
    stmt = select(AuditLog).where(
        AuditLog.company_id == tenant.company_id,
        AuditLog.entity_id == issue.id,
        AuditLog.action == AuditAction.INVENTORY_MOVEMENT,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    overrides = [row for row in rows if (row.new_values or {}).get("partial_issue_forced") is True]
    assert len(overrides) == 1
    assert overrides[0].new_values["override_reason"] == "Screws arrive tomorrow"


async def test_a_posted_issue_cannot_be_cancelled(db, tenant, stocked, planner, approver):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    posted = await issue_everything(db, tenant, planner, order)

    with pytest.raises(InvalidStateTransitionError):
        await service.cancel_material_issue(db, tenant, posted.issue.id, "changed my mind")


# -----------------------------------------------------------------------------
# Return and cancellation
# -----------------------------------------------------------------------------


async def test_a_job_holding_material_cannot_be_cancelled_until_it_is_returned(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)

    with pytest.raises(BusinessRuleError, match="still holding material"):
        await service.cancel_order(
            db, tenant, order.id, CancelRequest(reason="Customer pulled the order")
        )

    rows = await components_of(db, tenant, order)
    returned, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Job cancelled, material back to store",
            lines=[
                MaterialIssueLineIn(
                    line_number=index,
                    component_id=row.id,
                    quantity=Decimal(row.issued_quantity),
                )
                for index, row in enumerate(rows, start=1)
            ],
        ),
        issue_type=MaterialIssueType.RETURN,
    )
    result = await service.post_material_issue(
        db, tenant, planner, returned.id, MaterialIssuePostRequest()
    )
    assert {m.transaction_type for m in result.movements} == {"MATERIAL_RETURN"}

    # The stock is back on the shelf at the cost it left at.
    screw_balance = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert screw_balance.on_hand_quantity == Decimal("1000.0000")
    assert screw_balance.average_cost == Decimal("5.000000")

    cancelled = await service.cancel_order(
        db, tenant, order.id, CancelRequest(reason="Customer pulled the order")
    )
    assert cancelled.status is ProductionOrderStatus.CANCELLED


async def test_cancelling_a_released_job_gives_the_reserved_stock_back(
    db, tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)

    before = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert before.reserved_quantity == Decimal("630.0000")

    await service.cancel_order(db, tenant, order.id, CancelRequest(reason="No longer needed"))

    after = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert after.reserved_quantity == Decimal("0.0000")

    stmt = select(StockReservation).where(
        StockReservation.company_id == tenant.company_id,
        StockReservation.demand_type == DemandType.PRODUCTION_ORDER,
        StockReservation.demand_id == order.id,
    )
    reservations = list((await db.execute(stmt)).scalars().all())
    assert reservations
    assert all(r.status is ReservationStatus.RELEASED for r in reservations)


async def test_returning_more_than_the_job_holds_is_refused(db, tenant, stocked, planner, approver):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    rows = await components_of(db, tenant, order)
    body = next(r for r in rows if r.item_code == stocked["body"].code)

    with pytest.raises(BusinessRuleError, match="cannot be returned"):
        await service.create_material_issue(
            db,
            tenant,
            order.id,
            MaterialIssueCreate(
                reason="Too much back",
                lines=[
                    MaterialIssueLineIn(
                        line_number=1, component_id=body.id, quantity=Decimal("500")
                    )
                ],
            ),
            issue_type=MaterialIssueType.RETURN,
        )


# -----------------------------------------------------------------------------
# Backflush
# -----------------------------------------------------------------------------


async def test_a_backflushed_component_is_issued_at_completion_in_proportion(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order, backflush_item_ids=[stocked["screw"].id])

    rows = {c.item_code: c for c in await components_of(db, tenant, order)}
    assert rows[stocked["screw"].code].is_backflushed is True

    body = rows[stocked["body"].code]
    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Pick the bodies only",
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=body.id, quantity=Decimal("100"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    result = await service.post_material_issue(
        db, tenant, planner, issue.id, MaterialIssuePostRequest()
    )
    # The automatic component does not hold the order back.
    assert result.order_status is ProductionOrderStatus.MATERIAL_ISSUED

    await service.start_order(db, tenant, order.id, StartRequest())
    completed = await service.complete_order(
        db, tenant, order.id, CompleteRequest(produced_quantity=Decimal("80"))
    )

    assert completed.backflush_issue_code is not None
    rows = {c.item_code: c for c in await components_of(db, tenant, order)}
    # 630 required for 100; only 80 were made, so 504 were consumed.
    assert rows[stocked["screw"].code].issued_quantity == Decimal("504.0000")
    screw_balance = await balance_of(db, tenant, stocked["screw"], stocked["warehouse"])
    assert screw_balance.on_hand_quantity == Decimal("496.0000")


# -----------------------------------------------------------------------------
# Bookings
# -----------------------------------------------------------------------------


async def test_a_booking_at_an_intermediate_operation_receives_no_stock(
    db, tenant, stocked, planner, approver
):
    """Only the last operation's output is finished goods."""
    extra = RoutingOperation(
        company_id=tenant.company_id,
        routing_id=stocked["routing"].id,
        sequence=20,
        operation_code="PACK",
        operation_name="Pack",
        work_center_id=stocked["work_centre"].id,
        setup_time_minutes=Decimal("0"),
        run_time_per_unit_minutes=Decimal("1"),
    )
    db.add(extra)
    await db.flush()

    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await service.start_order(db, tenant, order.id, StartRequest())

    detail = await service.get_order_detail(db, tenant, order.id)
    first = next(wo for wo in detail.work_orders if wo.sequence == 10)
    result = await service.create_production_entry(
        db,
        tenant,
        order.id,
        ProductionEntryCreate(work_order_id=first.id, produced_quantity=Decimal("100")),
    )
    assert result.output_movement is None
    assert result.order.produced_quantity == Decimal("0.0000")

    stmt = (
        select(func.count())
        .select_from(InventoryBalance)
        .where(
            InventoryBalance.company_id == tenant.company_id,
            InventoryBalance.item_id == stocked["kit"].id,
        )
    )
    assert (await db.execute(stmt)).scalar_one() == 0


async def test_booking_far_more_than_planned_is_refused_before_it_reaches_stock(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await service.start_order(db, tenant, order.id, StartRequest())

    with pytest.raises(BusinessRuleError, match="over-production tolerance"):
        await service.create_production_entry(
            db,
            tenant,
            order.id,
            ProductionEntryCreate(produced_quantity=Decimal("1000")),
        )


async def test_rejected_output_is_counted_but_never_received_into_stock(
    db, tenant, stocked, planner, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await service.start_order(db, tenant, order.id, StartRequest())

    result = await service.create_production_entry(
        db,
        tenant,
        order.id,
        ProductionEntryCreate(
            produced_quantity=Decimal("90"),
            rejected_quantity=Decimal("10"),
            rejection_reason="Plating pinholes",
        ),
    )
    assert result.order.produced_quantity == Decimal("90.0000")
    assert result.order.rejected_quantity == Decimal("10.0000")

    kit_balance = await balance_of(db, tenant, stocked["kit"], stocked["warehouse"])
    assert kit_balance.on_hand_quantity == Decimal("90.0000")


# -----------------------------------------------------------------------------
# Work in progress and tenant isolation
# -----------------------------------------------------------------------------


async def test_the_wip_boards_report_the_live_job_by_order_and_by_work_centre(
    db, tenant, stocked, planner, approver
):
    from src.modules.production.schemas import WipFilters

    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)

    orders, _ = await service.wip_orders(db, tenant, WipFilters(), PageParams())
    row = next(r for r in orders if r.production_order_id == order.id)
    assert row.balance_quantity == Decimal("100.0000")
    assert row.open_work_orders == 1
    assert row.total_work_orders == 1

    centres, _ = await service.wip_work_centres(db, tenant, WipFilters(), PageParams())
    centre = next(c for c in centres if c.work_center_id == stocked["work_centre"].id)
    assert centre.pending_work_orders == 1
    # Nothing made yet: the whole planned setup and run is still to come.
    assert centre.remaining_minutes == Decimal("130.0000")


async def test_another_tenant_cannot_see_or_touch_this_companys_jobs(
    db, tenant, other_tenant, stocked, approver
):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)

    with pytest.raises(NotFoundError):
        await service.get_order(db, other_tenant, order.id)

    rows, _ = await service.list_orders(
        db, other_tenant, ProductionOrderFilters(), PageParams(), SortParams()
    )
    assert all(row.id != order.id for row in rows)


async def test_a_draft_can_be_deleted_but_a_released_job_cannot(db, tenant, stocked, approver):
    draft = await make_order(db, tenant, stocked)
    await service.delete_draft_order(db, tenant, draft.id, reason="Raised twice by mistake")
    with pytest.raises(NotFoundError):
        await service.get_order(db, tenant, draft.id)

    # The row is gone; the audit entry survives it and carries the reason, so
    # a deletion is never unexplained.
    deletion = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == AuditAction.DELETE, AuditLog.entity_id == draft.id
            )
        )
    ).scalar_one()
    assert deletion.context["reason"] == "Raised twice by mistake"

    live = await make_order(db, tenant, stocked)
    await approve(db, tenant, approver, live)
    await release(db, tenant, live)
    with pytest.raises(BusinessRuleError, match="only a draft may be deleted"):
        await service.delete_draft_order(
            db, tenant, live.id, reason="Trying to delete a released job"
        )


async def test_every_posted_document_leaves_an_audit_trail(db, tenant, stocked, planner, approver):
    """Section 29: the audit row is written in the same transaction as the change."""
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)

    stmt = select(AuditLog.action, AuditLog.summary).where(
        AuditLog.company_id == tenant.company_id,
        AuditLog.entity_id == order.id,
    )
    actions = [row.action for row in (await db.execute(stmt)).all()]
    assert AuditAction.CREATE in actions
    assert AuditAction.APPROVE in actions
    assert AuditAction.PRODUCTION_STATUS_CHANGE in actions


async def test_a_draft_issue_can_be_cancelled_and_moves_nothing(db, tenant, stocked, approver):
    order = await make_order(db, tenant, stocked, quantity="100")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    rows = await components_of(db, tenant, order)

    issue, _ = await service.create_material_issue(
        db,
        tenant,
        order.id,
        MaterialIssueCreate(
            reason="Raised in error",
            lines=[
                MaterialIssueLineIn(line_number=1, component_id=rows[0].id, quantity=Decimal("10"))
            ],
        ),
        issue_type=MaterialIssueType.ISSUE,
    )
    cancelled = await service.cancel_material_issue(db, tenant, issue.id, "Raised in error")
    assert cancelled.status is MaterialIssueStatus.CANCELLED
    assert await movements_of(db, tenant, reference_id=issue.id) == []
