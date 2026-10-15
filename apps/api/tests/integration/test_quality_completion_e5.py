"""Quality completion (WP E5) against a real PostgreSQL.

Production output held for QC and released by the verdict, the routing QC gate
on work-order completion, FINAL inspections raised by starting a packing order
with the completion and hand-over gates behind them, manual inspection raising,
inspection-level attachments, rework cycles with decision history, and the
rework-rate report.

Isolation follows ``test_production_service.py``: one private schema per
module, built once from the metadata, with each test's writes rolled back.
Everything below goes through the services, never around them: the behaviour
under test is the hold, the gates and the verdict callback, and a double would
assert only that one function calls another.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.datastructures import Headers, UploadFile

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.documents.models import DocumentCategory
from src.modules.documents.schemas import DocumentFilters, DocumentUploadRequest
from src.modules.documents.service import DocumentService
from src.modules.inventory.models import InventoryBalance, TransactionType
from src.modules.inventory.service import post_movement
from src.modules.organization.models import Company
from src.modules.packing import service as packing_service
from src.modules.packing.models import PackingOrderLine
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    PackingOrderCreate,
    PackingOrderLineIn,
    SealCartonRequest,
)
from src.modules.packing.schemas import (
    CompleteRequest as PackingCompleteRequest,
)
from src.modules.production import service as production_service
from src.modules.production.models import (
    MaterialIssueType,
    ProductionOrderStatus,
    WorkOrderStatus,
)
from src.modules.production.repository import ProductionOrderComponentRepository
from src.modules.production.schemas import (
    ApproveRequest,
    CompleteRequest,
    MaterialIssueCreate,
    MaterialIssueLineIn,
    MaterialIssuePostRequest,
    ProductionEntryCreate,
    ProductionOrderCreate,
    ReleaseRequest,
    StartRequest,
    WorkOrderTransitionRequest,
)
from src.modules.products.models import (
    Item,
    ItemGroup,
    ProductionType,
    UnitOfMeasure,
    UomCategory,
)
from src.modules.quality import service as quality_service
from src.modules.quality.models import (
    InspectionStatus,
    QcInspection,
    QcInspectionDecision,
    ReworkStatus,
)
from src.modules.quality.schemas import (
    DecideRequest,
    InspectionCreate,
    ReworkCompleteRequest,
    ReworkCreate,
)
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.routing.models import (
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from src.modules.users.models import User
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure
from tests.seed_support import seed_customer, seed_vendor

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
    prepare_metadata()

    schema = f"it_e5_{uuid.uuid4().hex[:10]}"
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


def _principal(company_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email="tester@atcraft.test",
        full_name="Tester",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
    )


@pytest.fixture
def planner(_schema) -> Principal:
    _, company_id, _, planner_id, _ = _schema
    return _principal(company_id, planner_id)


@pytest.fixture
def approver(_schema) -> Principal:
    _, company_id, _, _, approver_id = _schema
    return _principal(company_id, approver_id)


# -----------------------------------------------------------------------------
# Master data
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


@pytest_asyncio.fixture
async def masters(db: AsyncSession, tenant: TenantContext):
    """A QC-flagged kit, a plain kit, a final-QC finished good and their kit.

    Built through the ORM: many NOT NULL columns default in Python rather than
    in the database.
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
    customer = await seed_customer(db, company_id=tenant.company_id)
    db.add_all([warehouse, uom, work_centre])
    await db.flush()

    def make_item(code: str, **kwargs) -> Item:
        defaults: dict = {
            "item_group": ItemGroup.COMPONENT,
            "production_type": ProductionType.BUY,
            "can_purchase": True,
            "standard_cost": Decimal("40"),
        }
        return Item(
            company_id=tenant.company_id,
            code=code,
            name=f"Item {code}",
            base_uom_id=uom.id,
            gst_rate=Decimal("18"),
            is_active=True,
            **{**defaults, **kwargs},
        )

    kit = make_item(
        f"KIT-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        requires_production_qc=True,
        default_warehouse_id=warehouse.id,
    )
    plain_kit = make_item(
        f"PLAIN-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_manufacture=True,
        default_warehouse_id=warehouse.id,
    )
    final_item = make_item(
        f"FG-{suffix}",
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.BUY,
        can_sell=True,
        can_scrap=True,
        requires_final_qc=True,
        standard_cost=Decimal("120"),
        default_warehouse_id=warehouse.id,
    )
    body = make_item(f"BODY-{suffix}", can_be_bom_component=True)
    screw = make_item(f"SCREW-{suffix}", can_be_bom_component=True)
    box = make_item(
        f"BOX-{suffix}", item_group=ItemGroup.PACKING_MATERIAL, standard_cost=Decimal("2")
    )
    db.add_all([kit, plain_kit, final_item, body, screw, box])
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
            ),
        ]
    )

    async def make_routing(code: str, item: Item, *, qc_required: bool, version: int) -> Routing:
        routing = Routing(
            company_id=tenant.company_id,
            code=code,
            item_id=item.id,
            version=version,
            status=RoutingStatus.APPROVED,
            is_default=False,
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
                qc_required=qc_required,
            )
        )
        await db.flush()
        return routing

    routing_qc = await make_routing(f"RTQ-{suffix}", kit, qc_required=True, version=1)
    routing_plain = await make_routing(f"RTP-{suffix}", kit, qc_required=False, version=2)
    plain_routing_qc = await make_routing(f"RTQP-{suffix}", plain_kit, qc_required=True, version=1)
    plain_routing_plain = await make_routing(
        f"RTPP-{suffix}", plain_kit, qc_required=False, version=2
    )

    await db.flush()
    return {
        "warehouse": warehouse,
        "uom": uom,
        "work_centre": work_centre,
        "customer": customer,
        "kit": kit,
        "plain_kit": plain_kit,
        "final_item": final_item,
        "body": body,
        "screw": screw,
        "box": box,
        "bom": bom,
        "routing_qc": routing_qc,
        "routing_plain": routing_plain,
        "plain_routing_qc": plain_routing_qc,
        "plain_routing_plain": plain_routing_plain,
    }


async def stock(
    db: AsyncSession, tenant: TenantContext, item: Item, warehouse: Warehouse, quantity: str
) -> None:
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
            reason="Opening stock for the E5 test",
            unit_cost=Decimal("10"),
        )


async def balance_of(db: AsyncSession, tenant: TenantContext, item, warehouse) -> InventoryBalance:
    stmt = select(InventoryBalance).where(
        InventoryBalance.company_id == tenant.company_id,
        InventoryBalance.item_id == item.id,
        InventoryBalance.warehouse_id == warehouse.id,
    )
    return (await db.execute(stmt)).scalars().one()


async def make_order(db, tenant, masters, item_key: str, routing_key: str | None, **kwargs):
    return await production_service.create_order(
        db,
        tenant,
        ProductionOrderCreate(
            item_id=masters[item_key].id,
            planned_quantity=Decimal("100"),
            warehouse_id=masters["warehouse"].id,
            planned_start_date=today(),
            planned_end_date=today() + timedelta(days=5),
            routing_id=masters[routing_key].id if routing_key else None,
            **kwargs,
        ),
    )


async def approve(db, tenant, approver, order):
    second_person = dataclasses.replace(approver, is_superuser=True)
    return await production_service.approve_order(
        db, tenant, second_person, order.id, ApproveRequest(note="approved")
    )


async def release(db, tenant, order):
    return await production_service.release_order(db, tenant, order.id, ReleaseRequest())


async def issue_everything(db, tenant, planner, order):
    """Draft and post one issue covering every component of the job."""
    from src.modules.production.service import create_material_issue, post_material_issue

    components = await ProductionOrderComponentRepository(db, tenant).lock_for_order(order.id)
    payload = MaterialIssueCreate(
        reason="Kit issued to the line",
        lines=[
            MaterialIssueLineIn(
                line_number=index,
                component_id=component.id,
                quantity=Decimal(component.required_quantity),
            )
            for index, component in enumerate(components, start=1)
        ],
    )
    issue, _ = await create_material_issue(
        db, tenant, order.id, payload, issue_type=MaterialIssueType.ISSUE
    )
    return await post_material_issue(db, tenant, planner, issue.id, MaterialIssuePostRequest())


async def book(db, tenant, order, work_order_id, produced: str) -> tuple:
    return await production_service.create_production_entry(
        db,
        tenant,
        order.id,
        ProductionEntryCreate(
            work_order_id=work_order_id,
            produced_quantity=Decimal(produced),
        ),
    )


def decide_payload(accepted: str = "0", **kwargs) -> DecideRequest:
    quantities = {
        "accepted_quantity": Decimal(accepted),
        "rejected_quantity": Decimal(kwargs.get("rejected", "0")),
        "rework_quantity": Decimal(kwargs.get("rework", "0")),
        "hold_quantity": Decimal(kwargs.get("hold", "0")),
        "scrap_quantity": Decimal(kwargs.get("scrap", "0")),
    }
    refused = quantities["rejected_quantity"] + quantities["scrap_quantity"]
    return DecideRequest(
        **quantities,
        reason="E5 verdict",
        rejection_reason="Face scored" if refused > 0 else None,
    )


async def history_of(db, inspection_id: uuid.UUID):
    stmt = (
        select(QcInspectionDecision)
        .where(QcInspectionDecision.inspection_id == inspection_id)
        .order_by(QcInspectionDecision.sequence)
    )
    return list((await db.execute(stmt)).scalars().all())


# -----------------------------------------------------------------------------
# Production QC: the hold, the refusal, the release
# -----------------------------------------------------------------------------


async def test_requires_qc_defaults_from_the_item_flag_and_the_routing(db, tenant, masters) -> None:
    flagged = await make_order(db, tenant, masters, "kit", "routing_plain")
    assert flagged.requires_qc is True

    gated = await make_order(db, tenant, masters, "plain_kit", "plain_routing_qc")
    assert gated.requires_qc is True

    free = await make_order(db, tenant, masters, "plain_kit", "plain_routing_plain")
    assert free.requires_qc is False

    explicit = await make_order(
        db, tenant, masters, "plain_kit", "plain_routing_plain", requires_qc=True
    )
    assert explicit.requires_qc is True

    opted_out = await make_order(db, tenant, masters, "kit", "routing_plain", requires_qc=False)
    assert opted_out.requires_qc is False

    with pytest.raises(BusinessRuleError, match="cannot skip QC"):
        await make_order(db, tenant, masters, "plain_kit", "plain_routing_qc", requires_qc=False)


async def test_output_held_then_decided_then_the_order_completes(
    db, tenant, masters, planner, approver
) -> None:
    await stock(db, tenant, masters["body"], masters["warehouse"], "1000")
    await stock(db, tenant, masters["screw"], masters["warehouse"], "1000")
    order = await make_order(db, tenant, masters, "kit", "routing_plain")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await production_service.start_order(db, tenant, order.id, StartRequest())
    detail = await production_service.get_order_detail(db, tenant, order.id)
    work_order_id = detail.work_orders[0].id

    result = await book(db, tenant, order, work_order_id, "100")

    position = await balance_of(db, tenant, masters["kit"], masters["warehouse"])
    assert position.qc_hold_quantity == Decimal("100.0000")
    assert position.available_quantity == Decimal("0.0000")

    entry = result.entry
    assert entry.qc_hold_open is True
    assert entry.qc_inspection_id is not None
    inspection = (
        await db.execute(select(QcInspection).where(QcInspection.id == entry.qc_inspection_id))
    ).scalar_one()
    assert inspection.reference_type == "production_entry"
    assert inspection.reference_id == entry.id
    assert inspection.qc_stage == "IN_PROCESS"

    with pytest.raises(BusinessRuleError) as caught:
        await production_service.complete_order(db, tenant, order.id, CompleteRequest())
    assert inspection.code in str(caught.value)

    await quality_service.decide_inspection(
        db, tenant, entry.qc_inspection_id, decide_payload(accepted="100")
    )

    position = await balance_of(db, tenant, masters["kit"], masters["warehouse"])
    assert position.qc_hold_quantity == Decimal("0.0000")
    assert position.available_quantity == Decimal("100.0000")

    completed = await production_service.complete_order(db, tenant, order.id, CompleteRequest())
    assert completed.order.status is ProductionOrderStatus.COMPLETED

    rows = await history_of(db, entry.qc_inspection_id)
    assert [row.sequence for row in rows] == [1]
    assert rows[0].accepted_quantity == Decimal("100.0000")


async def test_the_routing_gate_holds_the_operation_until_accepted(
    db, tenant, masters, planner, approver
) -> None:
    await stock(db, tenant, masters["body"], masters["warehouse"], "1000")
    await stock(db, tenant, masters["screw"], masters["warehouse"], "1000")
    order = await make_order(db, tenant, masters, "kit", "routing_qc")
    await approve(db, tenant, approver, order)
    await release(db, tenant, order)
    await issue_everything(db, tenant, planner, order)
    await production_service.start_order(db, tenant, order.id, StartRequest())
    detail = await production_service.get_order_detail(db, tenant, order.id)
    work_order_id = detail.work_orders[0].id

    result = await book(db, tenant, order, work_order_id, "100")
    inspection = (
        await db.execute(
            select(QcInspection).where(QcInspection.id == result.entry.qc_inspection_id)
        )
    ).scalar_one()
    assert inspection.reference_type == "work_order"
    assert inspection.reference_id == work_order_id

    with pytest.raises(BusinessRuleError, match="gates on QC"):
        await production_service.transition_work_order(
            db, tenant, work_order_id, WorkOrderTransitionRequest(status=WorkOrderStatus.COMPLETED)
        )

    # A partial decision keeps the hold: the flag clears only on a settled verdict.
    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="60", rework="40")
    )
    with pytest.raises(BusinessRuleError, match="gates on QC"):
        await production_service.transition_work_order(
            db, tenant, work_order_id, WorkOrderTransitionRequest(status=WorkOrderStatus.COMPLETED)
        )

    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="100")
    )
    await production_service.transition_work_order(
        db, tenant, work_order_id, WorkOrderTransitionRequest(status=WorkOrderStatus.COMPLETED)
    )
    reread = await production_service.get_order_detail(db, tenant, order.id)
    assert reread.work_orders[0].status is WorkOrderStatus.COMPLETED

    rows = await history_of(db, inspection.id)
    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].rework_quantity == Decimal("40.0000")
    assert rows[1].accepted_quantity == Decimal("100.0000")


# -----------------------------------------------------------------------------
# Final QC on packing
# -----------------------------------------------------------------------------


async def packing_bench(db, tenant, masters):
    await stock(db, tenant, masters["box"], masters["warehouse"], "500")
    await stock(db, tenant, masters["final_item"], masters["warehouse"], "200")
    order, _ = await packing_service.create_order(
        db,
        tenant,
        PackingOrderCreate(
            customer_id=masters["customer"].id,
            warehouse_id=masters["warehouse"].id,
            planned_date=date(2026, 9, 1),
            lines=[
                PackingOrderLineIn(
                    item_id=masters["final_item"].id,
                    quantity_to_pack=Decimal("40"),
                    uom_id=masters["uom"].id,
                )
            ],
        ),
    )
    await packing_service.start_packing(db, tenant, order.id, reason="Bench is free")
    return order


async def pack_full_carton(db, tenant, masters, order) -> None:
    carton = await packing_service.add_carton(
        db,
        tenant,
        order.id,
        CartonCreate(
            packing_material_item_id=masters["box"].id,
            packing_material_quantity=Decimal("1"),
            tare_weight_kg=Decimal("0.5"),
            contents=[
                CartonContentIn(line_number=1, quantity=Decimal("40"), net_weight_kg=Decimal("10"))
            ],
        ),
    )
    await packing_service.seal_carton(
        db, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )


async def line_inspection(db, order) -> QcInspection:
    line = (
        (
            await db.execute(
                select(PackingOrderLine).where(PackingOrderLine.packing_order_id == order.id)
            )
        )
        .scalars()
        .one()
    )
    assert line.qc_inspection_id is not None
    return (
        await db.execute(select(QcInspection).where(QcInspection.id == line.qc_inspection_id))
    ).scalar_one()


async def test_starting_packing_raises_final_inspections_without_holding_stock(
    db, tenant, masters
) -> None:
    order = await packing_bench(db, tenant, masters)
    inspection = await line_inspection(db, order)
    assert inspection.qc_stage == "FINAL"
    assert inspection.reference_type == "packing_order"

    # Reserved, not held: the goods stay available until the verdict moves them.
    position = await balance_of(db, tenant, masters["final_item"], masters["warehouse"])
    assert position.qc_hold_quantity == Decimal("0.0000")
    assert position.available_quantity == Decimal("200.0000")


async def test_packing_blocks_complete_and_handover_until_accepted(db, tenant, masters) -> None:
    order = await packing_bench(db, tenant, masters)
    await pack_full_carton(db, tenant, masters, order)
    inspection = await line_inspection(db, order)

    with pytest.raises(BusinessRuleError, match="ACCEPTED"):
        await packing_service.complete_packing(
            db, tenant, order.id, PackingCompleteRequest(reason="Done")
        )

    # Rejection moves the stock to DAMAGED through the ordinary decision path.
    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(rejected="40")
    )
    position = await balance_of(db, tenant, masters["final_item"], masters["warehouse"])
    assert position.damaged_quantity == Decimal("40.0000")

    # The rejected line is replaced by re-inspection, not by force.
    with pytest.raises(BusinessRuleError, match="ACCEPTED"):
        await packing_service.complete_packing(
            db, tenant, order.id, PackingCompleteRequest(reason="Done")
        )
    fresh = await packing_service.reinspect_line(
        db, tenant, order.id, inspection.reference_id, reason="Repacked replacements"
    )
    assert fresh.qc_inspection_id != inspection.id

    await quality_service.decide_inspection(
        db, tenant, fresh.qc_inspection_id, decide_payload(accepted="40")
    )
    completed = await packing_service.complete_packing(
        db, tenant, order.id, PackingCompleteRequest(reason="Done")
    )
    assert completed.order.status == "PACKED"

    handed = await packing_service.hand_over_to_dispatch(
        db, tenant, order.id, dispatch_id=uuid.uuid4(), reason="Load it"
    )
    assert handed.dispatch_id is not None


# -----------------------------------------------------------------------------
# Manual inspection, attachments, rework
# -----------------------------------------------------------------------------


async def raise_held_lot(db, tenant, masters, *, quantity: str = "100"):
    await stock(db, tenant, masters["final_item"], masters["warehouse"], quantity)
    inspection = await quality_service.create_inspection(
        db,
        tenant,
        InspectionCreate(
            item_id=masters["final_item"].id,
            warehouse_id=masters["warehouse"].id,
            qc_stage="IN_PROCESS",
            inspected_quantity=Decimal(quantity),
            hold_stock=True,
            reason="Supervisor pulled the lot",
        ),
    )
    return inspection


async def test_a_manual_raise_holds_stock_and_decides_it(db, tenant, masters) -> None:
    inspection = await raise_held_lot(db, tenant, masters)
    position = await balance_of(db, tenant, masters["final_item"], masters["warehouse"])
    assert position.qc_hold_quantity == Decimal("100.0000")
    assert position.available_quantity == Decimal("0.0000")

    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="100")
    )
    position = await balance_of(db, tenant, masters["final_item"], masters["warehouse"])
    assert position.available_quantity == Decimal("100.0000")


async def test_an_inspection_carries_attachments_and_downloads_them(
    db, tenant, masters, monkeypatch, tmp_path
) -> None:
    from src.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "storage_path", tmp_path)
    inspection = await raise_held_lot(db, tenant, masters, quantity="10")

    pdf = b"%PDF-1.4 defect photo bytes"
    uploaded = await DocumentService(db, tenant).upload(
        UploadFile(
            io.BytesIO(pdf),
            filename="defect.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        ),
        DocumentUploadRequest(
            category=DocumentCategory.DOCUMENT,
            entity_type="qc_inspection",
            entity_id=inspection.id,
        ),
    )
    listed, _ = await DocumentService(db, tenant).list_documents(
        DocumentFilters(entity_type="qc_inspection", entity_id=inspection.id),
        PageParams(),
        SortParams(),
    )
    assert [row.id for row in listed] == [uploaded.id]

    downloaded = await DocumentService(db, tenant).open_download(uploaded.id)
    assert downloaded.path.read_bytes() == pdf


async def test_a_rework_cycle_reopens_the_inspection_and_keeps_history(db, tenant, masters) -> None:
    inspection = await raise_held_lot(db, tenant, masters)
    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="60", rework="40")
    )

    record = await quality_service.create_rework(
        db,
        tenant,
        ReworkCreate(
            inspection_id=inspection.id,
            quantity=Decimal("40"),
            assigned_to=tenant.user_id,
            reason="Bench rework",
        ),
    )
    assert record.status is ReworkStatus.OPEN

    # A second cycle cannot cover what the first already covers.
    with pytest.raises(BusinessRuleError, match="already on the bench"):
        await quality_service.create_rework(
            db,
            tenant,
            ReworkCreate(
                inspection_id=inspection.id,
                quantity=Decimal("40"),
                work_center_id=masters["work_centre"].id,
                reason="Double cover",
            ),
        )

    await quality_service.start_rework(db, tenant, record.id, reason="On the bench")
    completed = await quality_service.complete_rework(
        db,
        tenant,
        record.id,
        ReworkCompleteRequest(outcome="REWORKED_OK", reason="Polished out"),
    )
    assert completed.status is ReworkStatus.DONE
    assert completed.completed_at is not None

    reread = (
        await db.execute(select(QcInspection).where(QcInspection.id == inspection.id))
    ).scalar_one()
    assert InspectionStatus(reread.status) is InspectionStatus.IN_PROGRESS
    assert str(reread.result) == "PENDING"
    assert reread.rework_quantity == Decimal("40.0000")

    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="100")
    )
    rows = await history_of(db, inspection.id)
    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].rework_quantity == Decimal("40.0000")
    assert rows[1].accepted_quantity == Decimal("100.0000")

    detail = await quality_service.get_inspection_detail(db, tenant, inspection.id)
    assert [row.sequence for row in detail.decisions] == [1, 2]
    assert [row.code for row in detail.rework_records] == [record.code]


async def test_a_returned_cycle_flags_the_vendor_return(db, tenant, masters) -> None:
    vendor = await seed_vendor(db, company_id=tenant.company_id)
    await stock(db, tenant, masters["final_item"], masters["warehouse"], "50")
    inspection = await quality_service.create_inspection(
        db,
        tenant,
        InspectionCreate(
            item_id=masters["final_item"].id,
            warehouse_id=masters["warehouse"].id,
            qc_stage="INCOMING",
            inspected_quantity=Decimal("50"),
            vendor_id=vendor.id,
            hold_stock=True,
            reason="Vendor lot held",
        ),
    )
    await quality_service.decide_inspection(
        db, tenant, inspection.id, decide_payload(accepted="30", rework="20")
    )
    record = await quality_service.create_rework(
        db,
        tenant,
        ReworkCreate(
            inspection_id=inspection.id,
            quantity=Decimal("20"),
            work_center_id=masters["work_centre"].id,
            reason="Vendor sorts it",
        ),
    )
    await quality_service.start_rework(db, tenant, record.id, reason="Sent back")
    await quality_service.complete_rework(
        db,
        tenant,
        record.id,
        ReworkCompleteRequest(outcome="RETURNED", reason="Beyond repair here"),
    )
    reread = (
        await db.execute(select(QcInspection).where(QcInspection.id == inspection.id))
    ).scalar_one()
    assert reread.return_required is True


# -----------------------------------------------------------------------------
# The rework-rate report
# -----------------------------------------------------------------------------


async def test_the_rework_rate_report_sums_per_item_and_vendor(db, tenant, masters) -> None:
    vendor = await seed_vendor(db, company_id=tenant.company_id)
    await stock(db, tenant, masters["final_item"], masters["warehouse"], "300")
    await stock(db, tenant, masters["plain_kit"], masters["warehouse"], "300")

    async def decided(item_id, vendor_id, accepted: str, rework: str):
        inspection = await quality_service.create_inspection(
            db,
            tenant,
            InspectionCreate(
                item_id=item_id,
                warehouse_id=masters["warehouse"].id,
                qc_stage="IN_PROCESS",
                inspected_quantity=Decimal(accepted) + Decimal(rework),
                vendor_id=vendor_id,
                hold_stock=True,
                reason="Report fixture",
            ),
        )
        await quality_service.decide_inspection(
            db, tenant, inspection.id, decide_payload(accepted=accepted, rework=rework)
        )

    await decided(masters["final_item"].id, vendor.id, "60", "40")
    await decided(masters["final_item"].id, vendor.id, "90", "10")
    await decided(masters["plain_kit"].id, None, "100", "0")

    result = await reports_service.run_report(
        db,
        tenant,
        key=ReportKey.REWORK_RATE,
        period=ReportPeriod(date_from=today() - timedelta(days=1), date_to=today()),
        filters=ReportFilters(),
        params=PageParams(),
        may_export=False,
    )
    assert isinstance(result, reports_service.InlineReport)
    rows = {row.item_code: row for row in result.rows}  # type: ignore[attr-defined]
    assert set(rows) == {masters["final_item"].code, masters["plain_kit"].code}
    flagged = rows[masters["final_item"].code]
    assert flagged.inspected_quantity == Decimal("200.0000")
    assert flagged.rework_quantity == Decimal("50.0000")
    assert flagged.rework_rate_percent == Decimal("25.00")
    assert flagged.vendor_name is not None
    clean = rows[masters["plain_kit"].code]
    assert clean.rework_rate_percent == Decimal("0.00")
    assert clean.vendor_name is None


async def test_the_catalogue_lists_the_rework_rate_report() -> None:
    keys = {descriptor.key for descriptor in reports_service.descriptors()}
    assert ReportKey.REWORK_RATE in keys
