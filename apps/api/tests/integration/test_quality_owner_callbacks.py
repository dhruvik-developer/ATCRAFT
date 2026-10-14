"""Quality tells the document that raised an inspection what it decided (WP E4).

Against a real PostgreSQL, the two owners beside assembly:

* **goods receipts** - a decided incoming inspection sets its receipt line PASSED
  or FAILED once nothing is held, and a purchase order with no line left PENDING
  moves QC_PENDING -> QC_COMPLETED -> GRN_COMPLETED. Before this the line stayed
  PENDING and the order sat in QC_PENDING for ever;
* **job-work lots** - a lot that came back as several output items raises one
  inspection per item, a lot is released once all of them are decided (even
  while the rest of the order is still at the vendor), and the order completes
  once nothing is held - with the stock moved exactly once.

And the sweep that repairs documents a decision stranded before the callback
existed, run as the system actor.

Isolation follows ``test_assembly_service.py``: one private schema per module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal, TenantContext, system_tenant
from src.core.errors import BusinessRuleError
from src.core.models import Base
from src.core.permissions import Permission
from src.modules.audit.models import AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import InventoryTransaction, StockState, TransactionType
from src.modules.job_work import service as job_work
from src.modules.job_work.models import (
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkReceipt,
    JobWorkReceiptLine,
)
from src.modules.job_work.schemas import (
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderLineCreate,
    QcResultRequest,
    QcVerdictItem,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.procurement import service as procurement
from src.modules.procurement.models import (
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderStatus,
    ReceiptLineQcStatus,
)
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderCreate,
    PurchaseOrderLineIn,
)
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.quality import owners as quality_owners
from src.modules.quality import service as quality_service
from src.modules.quality.models import QcInspection
from src.modules.quality.schemas import DecideRequest
from src.modules.vendors.models import VendorType
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

prepare_metadata()


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """Private schema with one company and one operator."""
    schema = f"it_qc_owner_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                await seed_support.seed_company(
                    session, company_id=company_id, prefix="QCO", state_code="27"
                )
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email="inspector@atcraft.test",
                    full_name="Inspector",
                )
        finally:
            await engine.dispose()

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

    yield schema, company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext]]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, user_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield session, tenant
            await outer.rollback()
    finally:
        await engine.dispose()


def principal_for(tenant: TenantContext) -> Principal:
    return Principal(
        user_id=tenant.user_id,
        company_id=tenant.company_id,
        email="inspector@atcraft.test",
        full_name="Inspector",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("PURCHASE_MANAGER",),
    )


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def released_from_hold(session: AsyncSession, item_id: uuid.UUID) -> Decimal:
    """Everything the ledger has ever moved *out* of QC hold for one item."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(func.abs(InventoryTransaction.quantity)), 0)).where(
                InventoryTransaction.item_id == item_id,
                InventoryTransaction.source_state == StockState.QC_HOLD,
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def position(session: AsyncSession, tenant: TenantContext, item_id, warehouse_id):
    row = await inv.get_balance(session, tenant, item_id, warehouse_id)
    assert row is not None
    await session.refresh(row)
    return row


def strand_owners(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Decide without telling any owner - the release before the callback."""
    monkeypatch.setattr(quality_owners, "resolve_owner", lambda _reference_type: None)
    return monkeypatch


# =============================================================================
# Goods receipts
# =============================================================================


@dataclasses.dataclass
class Purchase:
    warehouse_id: uuid.UUID
    vendor_id: uuid.UUID
    item_ids: list[uuid.UUID]


@pytest_asyncio.fixture
async def purchase(ctx) -> Purchase:
    session, tenant = ctx
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH"), state_code="27"
    )
    vendor = await seed_support.seed_vendor(
        session,
        company_id=tenant.company_id,
        vendor_types=(VendorType.RAW_MATERIAL_SUPPLIER,),
        is_approved=True,
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        state_code="27",
    )
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    items = [
        await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code(f"RM{index}"),
            base_uom_id=uom.id,
            purchase_uom_id=uom.id,
            item_group=ItemGroup.RAW_MATERIAL,
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
            production_type=ProductionType.BUY,
            can_purchase=True,
        )
        for index in (1, 2)
    ]
    return Purchase(
        warehouse_id=warehouse.id, vendor_id=vendor.id, item_ids=[item.id for item in items]
    )


async def sent_order(
    session: AsyncSession, tenant: TenantContext, purchase: Purchase, *quantities: str
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A sent purchase order with one QC line per quantity. (order id, line ids)"""
    principal = principal_for(tenant)
    order, lines, _ = await procurement.create_purchase_order(
        session,
        tenant,
        principal,
        PurchaseOrderCreate(
            vendor_id=purchase.vendor_id,
            warehouse_id=purchase.warehouse_id,
            expected_delivery_date=today() + timedelta(days=10),
            lines=[
                PurchaseOrderLineIn(
                    line_number=number,
                    item_id=purchase.item_ids[number - 1],
                    quantity=Decimal(quantity),
                    unit_price=Decimal("50"),
                    requires_qc=True,
                )
                for number, quantity in enumerate(quantities, start=1)
            ],
        ),
    )
    approver = dataclasses.replace(principal, is_superuser=True)
    for target, who in (
        (PurchaseOrderStatus.PENDING_APPROVAL, principal),
        (PurchaseOrderStatus.APPROVED, approver),
        (PurchaseOrderStatus.SENT, principal),
    ):
        await procurement.transition_purchase_order(
            session, tenant, who, order.id, target, reason="walk to sent"
        )
    return order.id, [line.id for line in lines]


async def post_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    received: dict[uuid.UUID, str],
) -> list[uuid.UUID]:
    """Receive and post. Returns the inspection id raised for each line."""
    receipt, _ = await procurement.create_goods_receipt(
        session,
        tenant,
        GoodsReceiptCreate(
            purchase_order_id=order_id,
            lines=[
                GoodsReceiptLineIn(
                    line_number=number,
                    purchase_order_line_id=line_id,
                    received_quantity=Decimal(quantity),
                    accepted_quantity=Decimal(quantity),
                )
                for number, (line_id, quantity) in enumerate(received.items(), start=1)
            ],
        ),
    )
    result = await procurement.post_goods_receipt(
        session, tenant, principal_for(tenant), receipt.id
    )
    inspection_ids = [line.qc_inspection_id for line in result.receipt.lines]
    assert all(inspection_ids)
    return [inspection_id for inspection_id in inspection_ids if inspection_id is not None]


async def order_status(session: AsyncSession, order_id: uuid.UUID) -> PurchaseOrderStatus:
    status = (
        await session.execute(select(PurchaseOrder.status).where(PurchaseOrder.id == order_id))
    ).scalar_one()
    return PurchaseOrderStatus(status)


async def line_status(session: AsyncSession, inspection_id: uuid.UUID) -> ReceiptLineQcStatus:
    status = (
        await session.execute(
            select(GoodsReceiptLine.qc_status).where(
                GoodsReceiptLine.qc_inspection_id == inspection_id
            )
        )
    ).scalar_one()
    return ReceiptLineQcStatus(status)


async def decide(
    session: AsyncSession, tenant: TenantContext, inspection_id: uuid.UUID, **quantities: str
) -> None:
    refused = Decimal(quantities.get("rejected", "0")) + Decimal(quantities.get("scrap", "0"))
    await quality_service.decide_inspection(
        session,
        tenant,
        inspection_id,
        DecideRequest(
            accepted_quantity=Decimal(quantities.get("accepted", "0")),
            rejected_quantity=Decimal(quantities.get("rejected", "0")),
            hold_quantity=Decimal(quantities.get("hold", "0")),
            scrap_quantity=Decimal(quantities.get("scrap", "0")),
            reason="Incoming inspection",
            rejection_reason="Thread out of tolerance" if refused > 0 else None,
        ),
    )


async def test_a_passed_inspection_passes_the_line_and_completes_the_order(
    ctx, purchase: Purchase
) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "80")
    [inspection_id] = await post_receipt(session, tenant, order_id, {line_ids[0]: "80"})
    assert await order_status(session, order_id) is PurchaseOrderStatus.QC_PENDING

    await decide(session, tenant, inspection_id, accepted="80")

    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.PASSED
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED
    row = await position(session, tenant, purchase.item_ids[0], purchase.warehouse_id)
    assert Decimal(row.qc_hold_quantity) == 0
    assert Decimal(row.available_quantity) == Decimal("80.0000")
    assert await released_from_hold(session, purchase.item_ids[0]) == Decimal("80.0000")


async def test_any_refused_share_fails_the_line(ctx, purchase: Purchase) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "80")
    [inspection_id] = await post_receipt(session, tenant, order_id, {line_ids[0]: "80"})

    await decide(session, tenant, inspection_id, accepted="75", rejected="5")

    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.FAILED
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED


async def test_a_lot_partly_on_hold_keeps_the_line_and_the_order_waiting(
    ctx, purchase: Purchase
) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "80")
    [inspection_id] = await post_receipt(session, tenant, order_id, {line_ids[0]: "80"})

    await decide(session, tenant, inspection_id, accepted="50", hold="30")
    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.PENDING
    assert await order_status(session, order_id) is PurchaseOrderStatus.QC_PENDING

    await decide(session, tenant, inspection_id, accepted="80")
    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.PASSED
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED
    assert await released_from_hold(session, purchase.item_ids[0]) == Decimal("80.0000")


async def test_an_order_with_two_held_lines_waits_for_both_verdicts(
    ctx, purchase: Purchase
) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "40", "60")
    first, second = await post_receipt(
        session, tenant, order_id, {line_ids[0]: "40", line_ids[1]: "60"}
    )

    await decide(session, tenant, first, accepted="40")
    assert await order_status(session, order_id) is PurchaseOrderStatus.QC_PENDING

    await decide(session, tenant, second, accepted="60")
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED

    history = (
        (
            await session.execute(
                select(AuditLog.new_values)
                .where(
                    AuditLog.entity_type == procurement.AUDIT_PURCHASE_ORDER,
                    AuditLog.entity_id == order_id,
                    AuditLog.changed_fields.is_not(None),
                )
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    statuses = [values["status"] for values in history if values and "status" in values]
    assert statuses[-2:] == ["QC_COMPLETED", "GRN_COMPLETED"]


async def test_a_line_decided_before_the_order_is_complete_keeps_the_order_receiving(
    ctx, purchase: Purchase
) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "80")
    [first] = await post_receipt(session, tenant, order_id, {line_ids[0]: "40"})
    assert await order_status(session, order_id) is PurchaseOrderStatus.PARTIALLY_RECEIVED

    await decide(session, tenant, first, accepted="40")
    assert await line_status(session, first) is ReceiptLineQcStatus.PASSED
    assert await order_status(session, order_id) is PurchaseOrderStatus.PARTIALLY_RECEIVED

    [second] = await post_receipt(session, tenant, order_id, {line_ids[0]: "40"})
    assert await order_status(session, order_id) is PurchaseOrderStatus.QC_PENDING
    await decide(session, tenant, second, accepted="40")
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED


async def test_the_sweep_repairs_a_stranded_receipt_as_the_system(
    ctx, purchase: Purchase, monkeypatch
) -> None:
    session, tenant = ctx
    order_id, line_ids = await sent_order(session, tenant, purchase, "80")
    [inspection_id] = await post_receipt(session, tenant, order_id, {line_ids[0]: "80"})
    with monkeypatch.context() as patched:
        strand_owners(patched)
        await decide(session, tenant, inspection_id, accepted="80")
    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.PENDING
    assert await order_status(session, order_id) is PurchaseOrderStatus.QC_PENDING

    result = await quality_owners.reconcile_owners(session, system_tenant(tenant.company_id))

    assert result.owners_repaired == 1
    assert result.failed == 0
    assert await line_status(session, inspection_id) is ReceiptLineQcStatus.PASSED
    assert await order_status(session, order_id) is PurchaseOrderStatus.GRN_COMPLETED
    system_rows = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == procurement.AUDIT_PURCHASE_ORDER,
                    AuditLog.entity_id == order_id,
                    AuditLog.actor_label == "System",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(system_rows) == 2
    assert all(row.created_by_id is None for row in system_rows)
    order = (
        await session.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == order_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert order.updated_by_id is None

    again = await quality_owners.reconcile_owners(session, system_tenant(tenant.company_id))
    assert again.owners_repaired == 0
    assert await released_from_hold(session, purchase.item_ids[0]) == Decimal("80.0000")


async def test_an_inspection_quoting_no_real_receipt_changes_nothing(ctx, purchase) -> None:
    """A manual inspection that merely names a receipt type is decided as before."""
    session, tenant = ctx
    item_id = purchase.item_ids[0]
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=purchase.warehouse_id,
            quantity=Decimal("10"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="manual inspection fixture",
            unit_cost=Decimal("10"),
        )
    async with transaction(session):
        inspection = await quality_service.create_incoming_inspection(
            session,
            tenant,
            item_id=item_id,
            quantity=Decimal("10"),
            warehouse_id=purchase.warehouse_id,
            reference_type="goods_receipt",
            reference_id=uuid.uuid4(),
            reference_code="GRN/NOWHERE",
        )
        await inv.change_stock_state(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=purchase.warehouse_id,
            quantity=Decimal("10"),
            from_state=StockState.AVAILABLE,
            to_state=StockState.QC_HOLD,
            reason="held for the manual inspection",
        )

    await decide(session, tenant, inspection.id, accepted="10")

    decided = await session.get(QcInspection, inspection.id)
    assert decided is not None
    assert Decimal(decided.accepted_quantity) == Decimal("10.0000")


# =============================================================================
# Job-work lots
# =============================================================================


@dataclasses.dataclass
class TwoFinishes:
    """Two raw parts out for PVD; each comes back as its own coated item."""

    warehouse_id: uuid.UUID
    vendor_id: uuid.UUID
    raw: list[uuid.UUID]
    coated: list[uuid.UUID]


@pytest_asyncio.fixture
async def finishes(ctx) -> TwoFinishes:
    session, tenant = ctx
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    vendor = await seed_support.seed_vendor(
        session,
        company_id=tenant.company_id,
        vendor_types=(VendorType.JOB_WORK,),
        is_approved=True,
        approved_at=utcnow(),
    )
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)

    async def item(prefix: str, cost: str) -> uuid.UUID:
        row = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code(prefix),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            standard_cost=Decimal(cost),
            can_job_work=True,
        )
        return row.id

    raw = [await item("RAW", "100"), await item("RAW", "90")]
    coated = [await item("PVD", "130"), await item("PVD", "120")]
    for item_id in raw:
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse.id,
                quantity=Decimal("100"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="job work fixture opening stock",
                unit_cost=Decimal("100"),
            )
    return TwoFinishes(warehouse_id=warehouse.id, vendor_id=vendor.id, raw=raw, coated=coated)


async def issued_two_line_order(
    session: AsyncSession, tenant: TenantContext, finishes: TwoFinishes
) -> uuid.UUID:
    order, _ = await job_work.create_order(
        session,
        tenant,
        JobWorkOrderCreate(
            vendor_id=finishes.vendor_id,
            process_type=JobWorkProcess.PVD,
            expected_return_date=today() + timedelta(days=14),
            warehouse_id=finishes.warehouse_id,
            rate_per_unit=Decimal("10"),
            requires_qc=True,
            lines=[
                JobWorkOrderLineCreate(
                    line_number=number,
                    item_id=finishes.raw[number - 1],
                    quantity_to_issue=Decimal("100"),
                    expected_output_item_id=finishes.coated[number - 1],
                )
                for number in (1, 2)
            ],
        ),
    )
    order_id = order.id
    await job_work.approve_order(session, tenant, order_id, reason="Rate agreed")
    await job_work.issue_order(
        session,
        tenant,
        order_id,
        IssueRequest(reason="To the plater", vehicle_number="GJ01AB1234"),
    )
    return order_id


async def receive(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    outputs: dict[int, str],
    *,
    final: bool,
) -> uuid.UUID:
    result = await job_work.receive(
        session,
        tenant,
        principal_for(tenant),
        order_id,
        ReceiveRequest(
            reason="Back from plating",
            lines=[
                ReceiptLineIn(
                    line_number=line_number, output_quantity=Decimal(quantity), is_final=final
                )
                for line_number, quantity in outputs.items()
            ],
        ),
    )
    return result.receipt.id


async def lot_inspections(session: AsyncSession, receipt_id: uuid.UUID) -> list[QcInspection]:
    return list(
        (
            await session.execute(
                select(QcInspection)
                .where(QcInspection.reference_id == receipt_id)
                .order_by(QcInspection.created_at, QcInspection.id)
            )
        )
        .scalars()
        .all()
    )


async def order_state(session: AsyncSession, order_id: uuid.UUID) -> JobWorkOrderStatus:
    from src.modules.job_work.models import JobWorkOrder

    status = (
        await session.execute(select(JobWorkOrder.status).where(JobWorkOrder.id == order_id))
    ).scalar_one()
    return JobWorkOrderStatus(status)


async def lot(session: AsyncSession, receipt_id: uuid.UUID) -> JobWorkReceipt:
    return (
        await session.execute(
            select(JobWorkReceipt)
            .where(JobWorkReceipt.id == receipt_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_a_lot_of_two_output_items_raises_one_inspection_per_item(
    ctx, finishes: TwoFinishes
) -> None:
    session, tenant = ctx
    order_id = await issued_two_line_order(session, tenant, finishes)

    receipt_id = await receive(session, tenant, order_id, {1: "100", 2: "100"}, final=True)

    inspections = await lot_inspections(session, receipt_id)
    assert [(row.item_id, Decimal(row.inspected_quantity)) for row in inspections] == [
        (finishes.coated[0], Decimal("100.0000")),
        (finishes.coated[1], Decimal("100.0000")),
    ]
    assert {row.reference_type for row in inspections} == {job_work.QC_REFERENCE_RECEIPT}
    lines = (
        (
            await session.execute(
                select(JobWorkReceiptLine)
                .where(JobWorkReceiptLine.receipt_id == receipt_id)
                .order_by(JobWorkReceiptLine.line_number)
            )
        )
        .scalars()
        .all()
    )
    assert [line.qc_inspection_id for line in lines] == [row.id for row in inspections]
    assert (await lot(session, receipt_id)).qc_inspection_id == inspections[0].id
    assert await order_state(session, order_id) is JobWorkOrderStatus.QC_PENDING


async def test_deciding_every_inspection_releases_the_lot_and_completes_the_order_once(
    ctx, finishes: TwoFinishes
) -> None:
    session, tenant = ctx
    order_id = await issued_two_line_order(session, tenant, finishes)
    receipt_id = await receive(session, tenant, order_id, {1: "100", 2: "100"}, final=True)
    first, second = [row.id for row in await lot_inspections(session, receipt_id)]

    await decide(session, tenant, first, accepted="95", rejected="5")
    # One of two items decided: the lot is still held, the order still waiting.
    assert (await lot(session, receipt_id)).qc_released_at is None
    assert await order_state(session, order_id) is JobWorkOrderStatus.QC_PENDING

    await decide(session, tenant, second, accepted="100")
    assert (await lot(session, receipt_id)).qc_released_at is not None
    assert await order_state(session, order_id) is JobWorkOrderStatus.COMPLETED

    first_item = await position(session, tenant, finishes.coated[0], finishes.warehouse_id)
    assert Decimal(first_item.available_quantity) == Decimal("95.0000")
    assert Decimal(first_item.damaged_quantity) == Decimal("5.0000")
    assert Decimal(first_item.qc_hold_quantity) == 0
    # Exactly once per item.
    assert await released_from_hold(session, finishes.coated[0]) == Decimal("100.0000")
    assert await released_from_hold(session, finishes.coated[1]) == Decimal("100.0000")

    # The order's own verdict cannot release the lots a second time.
    with pytest.raises(BusinessRuleError):
        await job_work.record_qc_result(
            session,
            tenant,
            order_id,
            QcResultRequest(
                reason="Recorded again",
                items=[
                    QcVerdictItem(
                        output_item_id=finishes.coated[0], accepted_quantity=Decimal("100")
                    )
                ],
            ),
        )
    assert await released_from_hold(session, finishes.coated[0]) == Decimal("100.0000")


async def test_a_lot_back_early_is_released_while_the_rest_is_at_the_vendor(
    ctx, finishes: TwoFinishes
) -> None:
    session, tenant = ctx
    order_id = await issued_two_line_order(session, tenant, finishes)
    early = await receive(session, tenant, order_id, {1: "100"}, final=True)
    assert await order_state(session, order_id) is JobWorkOrderStatus.PARTIALLY_RECEIVED

    [inspection] = await lot_inspections(session, early)
    await decide(session, tenant, inspection.id, accepted="100")
    assert (await lot(session, early)).qc_released_at is not None
    assert await order_state(session, order_id) is JobWorkOrderStatus.PARTIALLY_RECEIVED

    late = await receive(session, tenant, order_id, {2: "100"}, final=True)
    assert await order_state(session, order_id) is JobWorkOrderStatus.QC_PENDING
    [inspection] = await lot_inspections(session, late)
    await decide(session, tenant, inspection.id, accepted="100")
    assert await order_state(session, order_id) is JobWorkOrderStatus.COMPLETED


async def test_the_owner_side_verdict_points_at_an_open_inspection(
    ctx, finishes: TwoFinishes
) -> None:
    session, tenant = ctx
    order_id = await issued_two_line_order(session, tenant, finishes)
    await receive(session, tenant, order_id, {1: "100", 2: "100"}, final=True)

    with pytest.raises(BusinessRuleError, match="held for inspection"):
        await job_work.record_qc_result(
            session,
            tenant,
            order_id,
            QcResultRequest(
                reason="Looks fine",
                items=[
                    QcVerdictItem(output_item_id=item_id, accepted_quantity=Decimal("100"))
                    for item_id in finishes.coated
                ],
            ),
        )
    assert await released_from_hold(session, finishes.coated[0]) == 0


async def test_the_sweep_releases_a_stranded_lot_and_completes_its_order(
    ctx, finishes: TwoFinishes, monkeypatch
) -> None:
    session, tenant = ctx
    order_id = await issued_two_line_order(session, tenant, finishes)
    receipt_id = await receive(session, tenant, order_id, {1: "100", 2: "100"}, final=True)
    inspection_ids = [row.id for row in await lot_inspections(session, receipt_id)]
    with monkeypatch.context() as patched:
        strand_owners(patched)
        for inspection_id in inspection_ids:
            await decide(session, tenant, inspection_id, accepted="100")
    assert await order_state(session, order_id) is JobWorkOrderStatus.QC_PENDING

    result = await quality_owners.reconcile_owners(session, system_tenant(tenant.company_id))

    assert result.failed == 0
    assert result.owners_repaired >= 1
    assert (await lot(session, receipt_id)).qc_released_at is not None
    assert await order_state(session, order_id) is JobWorkOrderStatus.COMPLETED
    assert await released_from_hold(session, finishes.coated[0]) == Decimal("100.0000")
