"""Service-level procurement tests against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible
without it: the generated ``available_quantity`` column that decides what may be
promised out of a receipt, the CHECK constraints that bound received quantity by
the over-receipt tolerance and force ``accepted + rejected = received``, the row
locks that serialise two deliveries against one order, the ``UNION ALL`` behind
the approvals queue, and the audit rows written inside the same transaction as
the movement they describe.

The inventory engine is exercised for real, not doubled. The point of the goods
receipt path is that it moves stock correctly; a double would assert only that
this module calls something.

Isolation follows the module-scoped schema pattern from
``test_sales_service.py``: one throwaway schema, built once, with each test's
writes rolled back.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.finance.models import LedgerAccount, LedgerEntry, PartyType, SystemAccount
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    StockSerial,
    StockState,
    TransactionType,
)
from src.modules.organization.models import Company
from src.modules.procurement import (
    purchase_return_debit_note_pdf_repository,
    purchase_return_debit_note_pdf_service,
    service,
)
from src.modules.procurement.models import (
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    ReceiptLineQcStatus,
    RequisitionStatus,
)
from src.modules.procurement.purchase_return_debit_note_pdf import (
    render_purchase_return_debit_note_pdf,
)
from src.modules.procurement.schemas import (
    DuplicateCheckLine,
    DuplicateCheckRequest,
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PendingApprovalFilters,
    PurchaseOrderCreate,
    PurchaseOrderFilters,
    PurchaseOrderLineIn,
    PurchaseOrderUpdate,
    PurchaseReturnCreate,
    PurchaseReturnLineIn,
    QuotationCreate,
    QuotationLineIn,
    QuotationUpdate,
    RequisitionCreate,
    RequisitionLineIn,
    RfqCreate,
    RfqLineIn,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.quality import service as quality_service
from src.modules.quality.schemas import DecideRequest
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey, ReportSnapshot
from src.modules.users.models import User
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure

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

    schema = f"it_proc_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    buyer_id = uuid.uuid4()
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
                for uid, handle in ((buyer_id, "buyer"), (approver_id, "approver")):
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

    yield schema, company_id, other_company_id, buyer_id, approver_id
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
    _, company_id, _, buyer_id, _ = _schema
    return TenantContext(company_id=company_id, user_id=buyer_id)


@pytest.fixture
def other_tenant(_schema) -> TenantContext:
    _, _, other_company_id, buyer_id, _ = _schema
    return TenantContext(company_id=other_company_id, user_id=buyer_id)


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
        role_codes=("PURCHASE_MANAGER",),
    )


@pytest.fixture
def buyer(_schema) -> Principal:
    _, company_id, _, buyer_id, _ = _schema
    return _principal(company_id, buyer_id)


@pytest.fixture
def approver(_schema) -> Principal:
    _, company_id, _, _, approver_id = _schema
    return _principal(company_id, approver_id)


@pytest.fixture
def buyer_without_approval(_schema) -> Principal:
    """Holds every procurement right except approval - the section 16 case."""
    _, company_id, _, buyer_id, _ = _schema
    return _principal(
        company_id,
        buyer_id,
        Permission.PURCHASE_ORDER_VIEW,
        Permission.PURCHASE_ORDER_CREATE,
        Permission.PURCHASE_ORDER_UPDATE,
        Permission.GRN_CREATE,
        Permission.GRN_POST,
    )


# -----------------------------------------------------------------------------
# Master data
# -----------------------------------------------------------------------------


@pytest_asyncio.fixture
async def masters(db: AsyncSession, tenant: TenantContext):
    """Warehouse, unit, vendor and three items, built through the ORM.

    Through the ORM deliberately: many NOT NULL columns default in Python rather
    than in the database, and a hand-written INSERT column list goes stale the
    moment another module adds one.
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
    vendor = Vendor(
        company_id=tenant.company_id,
        code=f"VEN{suffix}",
        name="Acme Fasteners",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER.value],
        payment_terms_days=30,
        lead_time_days=7,
        currency="INR",
        is_approved=True,
        # The database refuses an approved vendor with no approval timestamp.
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
        state_code="27",
    )
    db.add_all([warehouse, uom, vendor])
    await db.flush()

    def make_item(code: str, **kwargs) -> Item:
        return Item(
            company_id=tenant.company_id,
            code=code,
            name=f"Item {code}",
            item_group=ItemGroup.RAW_MATERIAL,
            base_uom_id=uom.id,
            purchase_uom_id=uom.id,
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
            production_type=ProductionType.BUY,
            can_purchase=True,
            is_active=True,
            **kwargs,
        )

    plain = make_item(f"RM-{suffix}")
    batched = make_item(f"BT-{suffix}", is_batch_tracked=True)
    serialised = make_item(f"SR-{suffix}", is_serial_tracked=True)
    db.add_all([plain, batched, serialised])
    await db.flush()

    return {
        "warehouse": warehouse,
        "uom": uom,
        "vendor": vendor,
        "item": plain,
        "batched_item": batched,
        "serialised_item": serialised,
    }


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def make_order(
    db: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters,
    *,
    quantity: str = "100",
    unit_price: str = "50",
    requires_qc: bool = False,
    item=None,
    expected_delivery_date: date | None = None,
    po_date: date | None = None,
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL,
    source_id: uuid.UUID | None = None,
    acknowledge_duplicates: bool = False,
):
    payload = PurchaseOrderCreate(
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        po_date=po_date,
        expected_delivery_date=expected_delivery_date or (today() + timedelta(days=10)),
        source_type=source_type,
        source_id=source_id,
        acknowledge_duplicates=acknowledge_duplicates,
        duplicate_acknowledgement_reason=(
            "Genuine additional demand for a rush job" if acknowledge_duplicates else None
        ),
        lines=[
            PurchaseOrderLineIn(
                line_number=1,
                item_id=(item or masters["item"]).id,
                quantity=Decimal(quantity),
                unit_price=Decimal(unit_price),
                requires_qc=requires_qc,
            )
        ],
    )
    order, lines, candidates = await service.create_purchase_order(db, tenant, principal, payload)
    return order, lines, candidates


async def send_order(db, tenant, principal, order):
    """Walk an order to SENT so a receipt can be posted against it.

    The approval step runs as a superuser: segregation of duties has its own
    test, and every receipt test would otherwise have to model a second person
    to get to the state it actually cares about.
    """
    approver = dataclasses.replace(principal, is_superuser=True)
    await service.transition_purchase_order(
        db, tenant, principal, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )
    await service.transition_purchase_order(
        db, tenant, approver, order.id, PurchaseOrderStatus.APPROVED, reason="approve"
    )
    return await service.transition_purchase_order(
        db, tenant, principal, order.id, PurchaseOrderStatus.SENT, reason="send"
    )


async def receive(
    db,
    tenant,
    principal,
    order,
    order_line,
    *,
    received: str,
    accepted: str | None = None,
    rejected: str = "0",
    batch_number: str | None = None,
    serial_numbers: list[str] | None = None,
    post: bool = True,
):
    accepted_quantity = Decimal(accepted if accepted is not None else received) - Decimal(rejected)
    receipt, _ = await service.create_goods_receipt(
        db,
        tenant,
        GoodsReceiptCreate(
            purchase_order_id=order.id,
            lines=[
                GoodsReceiptLineIn(
                    line_number=1,
                    purchase_order_line_id=order_line.id,
                    received_quantity=Decimal(received),
                    accepted_quantity=accepted_quantity,
                    rejected_quantity=Decimal(rejected),
                    rejection_reason="Damaged in transit" if Decimal(rejected) > 0 else None,
                    batch_number=batch_number,
                    serial_numbers=serial_numbers or [],
                )
            ],
        ),
    )
    if not post:
        return receipt, None
    result = await service.post_goods_receipt(db, tenant, principal, receipt.id)
    return receipt, result


async def balance_of(db: AsyncSession, tenant: TenantContext, item_id, warehouse_id):
    stmt = select(InventoryBalance).where(
        InventoryBalance.company_id == tenant.company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
    )
    return (await db.execute(stmt)).scalars().first()


async def reload_line(db: AsyncSession, line_id) -> PurchaseOrderLine:
    return (
        await db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.id == line_id))
    ).scalar_one()


# =============================================================================
# Goods receipt posting - the critical integration
# =============================================================================


async def test_posting_a_receipt_moves_stock_and_completes_the_order(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="100")

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance is not None
    assert balance.on_hand_quantity == Decimal("100.0000")
    assert balance.qc_hold_quantity == Decimal("0.0000")
    # The generated column: this is what a sales order may be promised.
    assert balance.available_quantity == Decimal("100.0000")

    assert len(result.movements) == 1
    assert result.movements[0].stock_state == "AVAILABLE"
    assert result.purchase_order_status is PurchaseOrderStatus.GRN_COMPLETED

    line = await reload_line(db, lines[0].id)
    assert line.received_quantity == Decimal("100.0000")
    assert line.accepted_quantity == Decimal("100.0000")
    assert line.status is PurchaseOrderLineStatus.RECEIVED


async def test_the_ledger_records_a_purchase_receipt_pointing_back_at_the_grn(
    db, tenant, buyer, masters
):
    """ "What did this GRN post?" has to be answerable from the ledger alone."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="60")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="60")

    stmt = select(InventoryTransaction).where(
        InventoryTransaction.company_id == tenant.company_id,
        InventoryTransaction.reference_type == "goods_receipt",
        InventoryTransaction.reference_id == receipt.id,
    )
    movements = list((await db.execute(stmt)).scalars().all())

    assert len(movements) == 1
    movement = movements[0]
    assert movement.transaction_type is TransactionType.PURCHASE_RECEIPT
    assert movement.quantity == Decimal("60.0000")
    assert movement.reference_code == receipt.code
    assert receipt.code in movement.reason
    # Valuation is the net price after discount and before tax.
    assert movement.unit_cost == Decimal("50.000000")


async def test_a_short_receipt_leaves_the_order_partially_received(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="60")

    assert result.purchase_order_status is PurchaseOrderStatus.PARTIALLY_RECEIVED
    line = await reload_line(db, lines[0].id)
    assert line.status is PurchaseOrderLineStatus.PARTIALLY_RECEIVED
    assert line.received_quantity == Decimal("60.0000")

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.available_quantity == Decimal("60.0000")


async def test_a_second_delivery_completes_the_order_and_adds_to_the_same_position(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    await receive(db, tenant, buyer, order, lines[0], received="60")
    _, result = await receive(db, tenant, buyer, order, lines[0], received="40")

    assert result.purchase_order_status is PurchaseOrderStatus.GRN_COMPLETED
    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("100.0000")

    line = await reload_line(db, lines[0].id)
    assert line.received_quantity == Decimal("100.0000")
    assert line.status is PurchaseOrderLineStatus.RECEIVED


async def test_rejected_quantity_is_recorded_but_never_taken_into_stock(db, tenant, buyer, masters):
    """Goods refused at the gate leave on the lorry they arrived on."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="100", rejected="10")

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("90.0000")
    assert result.movements[0].quantity == Decimal("90.0000")

    line = await reload_line(db, lines[0].id)
    assert line.accepted_quantity == Decimal("90.0000")
    assert line.rejected_quantity == Decimal("10.0000")
    # Everything ordered has now arrived, whatever happened to it afterwards.
    assert line.status is PurchaseOrderLineStatus.RECEIVED


async def test_posting_writes_an_audit_row_for_the_receipt_and_the_order(
    db, tenant, buyer, masters
):
    """Audit is written in the same transaction as the movement, not queued."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="25")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="25")

    stmt = select(AuditLog).where(
        AuditLog.company_id == tenant.company_id, AuditLog.entity_id == receipt.id
    )
    entries = list((await db.execute(stmt)).scalars().all())
    summaries = " | ".join(entry.summary for entry in entries)

    assert any(entry.entity_code == receipt.code for entry in entries)
    assert "POSTED" in summaries or "posted" in summaries.lower()

    order_stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.company_id == tenant.company_id,
            AuditLog.entity_id == order.id,
            AuditLog.entity_type == "purchase_order",
        )
    )
    # Created, submitted, approved, sent, received, GRN completed.
    assert int((await db.execute(order_stmt)).scalar_one()) >= 6


# --- the over-receipt tolerance, against the database --------------------------


async def test_a_delivery_inside_the_tolerance_is_accepted(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="105")

    assert result.purchase_order_status is PurchaseOrderStatus.GRN_COMPLETED
    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("105.0000")


async def test_a_delivery_beyond_the_tolerance_is_refused_with_a_usable_message(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)

    with pytest.raises(BusinessRuleError) as exc:
        await receive(db, tenant, buyer, order, lines[0], received="120")

    assert exc.value.details["maximum_receivable"] == "105.0000"
    assert exc.value.details["remaining_receivable"] == "105.0000"


async def test_the_tolerance_is_measured_against_what_was_already_received(
    db, tenant, buyer, masters
):
    """Two deliveries that individually look fine can still over-receive."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    await receive(db, tenant, buyer, order, lines[0], received="100")

    with pytest.raises(BusinessRuleError):
        await receive(db, tenant, buyer, order, lines[0], received="10")

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("100.0000")


# --- QC routing ----------------------------------------------------------------


async def test_a_line_requiring_inspection_lands_on_qc_hold_not_available(
    db, tenant, buyer, masters
):
    """Held stock is physically present and must never be promised to a customer."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="80", requires_qc=True)
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="80")

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("80.0000")
    assert balance.qc_hold_quantity == Decimal("80.0000")
    assert balance.available_quantity == Decimal("0.0000")

    assert result.movements[0].stock_state == "QC_HOLD"
    assert result.qc_hold_line_count == 1
    assert result.purchase_order_status is PurchaseOrderStatus.QC_PENDING


async def test_without_the_quality_module_the_receipt_line_is_flagged_for_it(
    db, tenant, buyer, masters, monkeypatch
):
    """The PENDING flag is the work queue quality picks up when it lands.

    The absence is *simulated*, not assumed. This test used to assert that
    ``resolve_quality_port()`` returned nothing, which was true only while the
    quality module did not exist - so the day it landed the test failed for a
    reason with nothing to do with the fallback it was written to protect.
    Forcing the port to resolve to nothing keeps that branch under test on every
    deployment, including one where quality is simply not installed.
    """
    monkeypatch.setattr(service, "resolve_quality_port", lambda: None)

    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="80", requires_qc=True)
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="80")

    receipt_line = result.receipt.lines[0]
    assert receipt_line.qc_status is ReceiptLineQcStatus.PENDING
    assert receipt_line.posted_stock_state == "QC_HOLD"
    # Nothing raised the inspection - but the quantity is already held and the
    # line is flagged for whoever does.
    assert receipt_line.qc_inspection_id is None
    assert result.qc_inspections_created is False


async def test_the_quality_module_is_called_when_it_is_available(db, tenant, buyer, masters):
    """The guarded seam: when quality is deployed, inspections are raised here."""
    raised: list[dict] = []

    class _Inspection:
        def __init__(self) -> None:
            self.id = uuid.uuid4()

    async def fake_quality(session, tenant_ctx, **kwargs):
        raised.append(kwargs)
        return _Inspection()

    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="40", requires_qc=True)
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="40", post=False)
    result = await service.post_goods_receipt(db, tenant, buyer, receipt.id, quality=fake_quality)

    assert result.qc_inspections_created is True
    assert len(raised) == 1
    assert raised[0]["item_id"] == masters["item"].id
    assert raised[0]["quantity"] == Decimal("40.0000")
    assert raised[0]["reference_code"] == receipt.code
    assert result.receipt.lines[0].qc_inspection_id is not None


async def test_a_receipt_with_no_inspection_skips_qc_pending_entirely(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="10")
    await send_order(db, tenant, buyer, order)

    _, result = await receive(db, tenant, buyer, order, lines[0], received="10")

    assert result.purchase_order_status is PurchaseOrderStatus.GRN_COMPLETED
    assert result.qc_hold_line_count == 0


# --- traceability ---------------------------------------------------------------


async def test_a_batch_tracked_item_creates_and_posts_against_its_lot(db, tenant, buyer, masters):
    order, lines, _ = await make_order(
        db, tenant, buyer, masters, quantity="30", item=masters["batched_item"]
    )
    await send_order(db, tenant, buyer, order)

    _, result = await receive(
        db, tenant, buyer, order, lines[0], received="30", batch_number="lot-9001"
    )

    assert result.movements[0].batch_id is not None
    assert result.receipt.lines[0].batch_id == result.movements[0].batch_id
    # Normalised on the way in, so "lot-9001" and "LOT 9001" cannot be two lots.
    assert result.receipt.lines[0].batch_number == "LOT-9001"


async def test_a_batch_tracked_item_without_a_lot_number_is_refused_at_the_gate(
    db, tenant, buyer, masters
):
    """A movement posted without its batch cannot be attributed to a lot later."""
    order, lines, _ = await make_order(
        db, tenant, buyer, masters, quantity="30", item=masters["batched_item"]
    )
    await send_order(db, tenant, buyer, order)

    with pytest.raises(BusinessRuleError) as exc:
        await receive(db, tenant, buyer, order, lines[0], received="30")
    assert exc.value.code is ErrorCode.BATCH_REQUIRED


async def test_a_serial_tracked_receipt_without_labels_is_refused(db, tenant, buyer, masters):
    """A quantity-only receipt would leave three units untraceable."""
    order, lines, _ = await make_order(
        db, tenant, buyer, masters, quantity="3", item=masters["serialised_item"]
    )
    await send_order(db, tenant, buyer, order)

    with pytest.raises(BusinessRuleError) as exc:
        await receive(db, tenant, buyer, order, lines[0], received="3")
    assert exc.value.code is ErrorCode.SERIAL_REQUIRED
    assert "serial tracked" in exc.value.message


@pytest.mark.parametrize("requires_qc", [False, True])
async def test_serial_numbers_mint_one_movement_per_received_unit(
    db, tenant, buyer, masters, requires_qc
):
    """The labels scanned at the gate become the units the ledger moves."""
    order, lines, _ = await make_order(
        db,
        tenant,
        buyer,
        masters,
        quantity="3",
        item=masters["serialised_item"],
        requires_qc=requires_qc,
    )
    await send_order(db, tenant, buyer, order)

    _, result = await receive(
        db,
        tenant,
        buyer,
        order,
        lines[0],
        received="3",
        serial_numbers=[" sn 001 ", "SN-002", "SN-003"],
    )

    receipt_line = result.receipt.lines[0]
    assert receipt_line.serial_numbers == ["SN001", "SN-002", "SN-003"]
    assert receipt_line.serial_ids is not None and len(receipt_line.serial_ids) == 3
    assert len(result.movements) == 3
    legs = list(
        (
            await db.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.reference_id == receipt_line.goods_receipt_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {leg.serial_id for leg in legs} == set(receipt_line.serial_ids)
    assert all(leg.quantity == Decimal("1.0000") for leg in legs)

    balance = await balance_of(db, tenant, masters["serialised_item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("3.0000")
    serials = list(
        (
            await db.execute(
                select(StockSerial).where(StockSerial.item_id == masters["serialised_item"].id)
            )
        )
        .scalars()
        .all()
    )
    assert {unit.serial_number for unit in serials} == {"SN001", "SN-002", "SN-003"}
    if requires_qc:
        assert receipt_line.qc_inspection_id is not None
        await quality_service.decide_inspection(
            db,
            tenant,
            receipt_line.qc_inspection_id,
            DecideRequest(
                accepted_quantity=Decimal("2"),
                rejected_quantity=Decimal("1"),
                reason="Inspected each received unit",
                rejection_reason="Finish damaged on one unit",
                serial_dispositions=dict(
                    zip(receipt_line.serial_ids, ["ACCEPTED", "ACCEPTED", "REJECTED"], strict=True)
                ),
            ),
        )
        await db.refresh(balance)
        assert balance.qc_hold_quantity == 0
        assert balance.available_quantity == 2
        assert balance.damaged_quantity == 1


# --- posting is guarded ---------------------------------------------------------


async def test_a_receipt_cannot_be_posted_twice(db, tenant, buyer, masters):
    """The second posting would double the stock it created."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="50")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="50")

    with pytest.raises(InvalidStateTransitionError):
        await service.post_goods_receipt(db, tenant, buyer, receipt.id)

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("50.0000")


async def test_goods_cannot_be_received_against_an_unsent_order(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="50")

    with pytest.raises(BusinessRuleError):
        await receive(db, tenant, buyer, order, lines[0], received="50", post=False)


async def test_a_posted_receipt_cannot_be_cancelled(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="50")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="50")

    with pytest.raises(BusinessRuleError):
        await service.cancel_goods_receipt(db, tenant, receipt.id, reason="changed my mind")


async def test_a_failed_posting_leaves_no_stock_behind(db, tenant, buyer, masters):
    """The whole posting is one transaction: tolerance, stock, status, audit."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="100", post=False)

    # Shrink the order underneath the draft receipt so the tolerance check fails
    # at posting time rather than at keying time.
    line = await reload_line(db, lines[0].id)
    line.quantity = Decimal("10")
    await db.flush()

    with pytest.raises(BusinessRuleError):
        await service.post_goods_receipt(db, tenant, buyer, receipt.id)

    assert await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id) is None


# =============================================================================
# Purchase return
# =============================================================================


async def test_a_return_posts_a_negative_movement_against_the_receipt_line(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="100")
    receipt_line_id = posted.receipt.lines[0].id

    purchase_return, _ = await service.create_purchase_return(
        db,
        tenant,
        PurchaseReturnCreate(
            goods_receipt_id=posted.receipt.id,
            reason="Wrong finish supplied",
            lines=[
                PurchaseReturnLineIn(
                    line_number=1, goods_receipt_line_id=receipt_line_id, quantity=Decimal("25")
                )
            ],
        ),
    )
    result = await service.post_purchase_return(db, tenant, buyer, purchase_return.id)

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("75.0000")

    stmt = select(InventoryTransaction).where(
        InventoryTransaction.reference_type == "purchase_return",
        InventoryTransaction.reference_id == purchase_return.id,
    )
    movement = (await db.execute(stmt)).scalars().one()
    assert movement.transaction_type is TransactionType.PURCHASE_RETURN
    # The type carries the sign; the ledger row is the decrease.
    assert movement.quantity == Decimal("-25.0000")
    assert result.purchase_return.status is PurchaseReturnStatus.POSTED
    assert result.debit_note_journal_id is not None

    journal_rows = list(
        (
            await db.execute(
                select(LedgerEntry)
                .where(LedgerEntry.journal_id == result.debit_note_journal_id)
                .order_by(LedgerEntry.line_number)
            )
        )
        .scalars()
        .all()
    )
    account_rows = (
        await db.execute(
            select(LedgerAccount).where(
                LedgerAccount.id.in_({row.account_id for row in journal_rows})
            )
        )
    ).scalars()
    system_codes = {row.id: str(row.system_code) for row in account_rows}
    by_account = {
        system_code: (
            sum(
                (
                    Decimal(row.debit_amount)
                    for row in journal_rows
                    if system_codes[row.account_id] == system_code
                ),
                Decimal("0"),
            ),
            sum(
                (
                    Decimal(row.credit_amount)
                    for row in journal_rows
                    if system_codes[row.account_id] == system_code
                ),
                Decimal("0"),
            ),
        )
        for system_code in set(system_codes.values())
    }
    assert (
        sum((Decimal(row.debit_amount) for row in journal_rows), Decimal("0"))
        == purchase_return.total_amount
    )
    assert (
        sum((Decimal(row.credit_amount) for row in journal_rows), Decimal("0"))
        == purchase_return.total_amount
    )
    assert by_account[str(SystemAccount.ACCOUNTS_PAYABLE)] == (
        purchase_return.total_amount,
        Decimal("0"),
    )
    if purchase_return.subtotal > 0:
        assert by_account[str(SystemAccount.PURCHASE_EXPENSE)] == (
            Decimal("0"),
            purchase_return.subtotal,
        )
    if purchase_return.tax_amount > 0:
        assert by_account[str(SystemAccount.INPUT_TAX)] == (
            Decimal("0"),
            purchase_return.tax_amount,
        )
    payable_entry = next(
        row
        for row in journal_rows
        if system_codes[row.account_id] == str(SystemAccount.ACCOUNTS_PAYABLE)
    )
    assert str(payable_entry.party_type) == str(PartyType.VENDOR)
    assert payable_entry.party_id == purchase_return.vendor_id

    line = await reload_line(db, lines[0].id)
    assert line.returned_quantity == Decimal("25.0000")
    # What was accepted is a historical fact; the return is an offsetting event.
    assert line.accepted_quantity == Decimal("100.0000")


async def test_posted_return_queues_a_downloadable_vendor_debit_note(
    db, tenant, buyer, masters, monkeypatch
):
    order, order_lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, order_lines[0], received="100")
    purchase_return, _ = await service.create_purchase_return(
        db,
        tenant,
        PurchaseReturnCreate(
            goods_receipt_id=posted.receipt.id,
            reason="Defective finish & wrong size",
            debit_note_number="DN/2026/17",
            lines=[
                PurchaseReturnLineIn(
                    line_number=1,
                    goods_receipt_line_id=posted.receipt.lines[0].id,
                    quantity=Decimal("25"),
                )
            ],
        ),
    )
    await service.post_purchase_return(db, tenant, buyer, purchase_return.id)

    pdf_data = await purchase_return_debit_note_pdf_repository.purchase_return_debit_note_pdf_data(
        db, tenant, purchase_return.id
    )
    assert pdf_data["vendor"]["name"] == masters["vendor"].name
    assert pdf_data["goods_receipt_code"] == posted.receipt.code
    assert pdf_data["debit_note_number"] == "DN/2026/17"
    assert pdf_data["lines"][0]["item_code"] == masters["item"].code
    assert render_purchase_return_debit_note_pdf(pdf_data).startswith(b"%PDF-")

    dispatched: list[dict[str, object]] = []

    async def capture_dispatch(*_args, **kwargs):
        dispatched.append(kwargs)
        return "task-id"

    monkeypatch.setattr(reports_service, "dispatch_job", capture_dispatch)
    job_id = await purchase_return_debit_note_pdf_service.queue_purchase_return_debit_note_pdf(
        db, tenant, purchase_return.id
    )
    job = await db.get(ReportSnapshot, uuid.UUID(job_id))
    assert job is not None
    assert job.report_key is ReportKey.PURCHASE_RETURN_DEBIT_NOTE
    assert job.parameters == {"purchase_return_id": str(purchase_return.id)}
    assert dispatched[0]["task_name"] == "reports.generate_purchase_return_debit_note"


async def test_a_failed_finance_post_rolls_back_the_stock_return(
    db, tenant, buyer, masters, monkeypatch
):
    order, order_lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, order_lines[0], received="100")
    receipt_line_id = posted.receipt.lines[0].id
    order_line_id = order_lines[0].id
    purchase_return, _ = await service.create_purchase_return(
        db,
        tenant,
        PurchaseReturnCreate(
            goods_receipt_id=posted.receipt.id,
            reason="Return awaiting vendor credit",
            lines=[
                PurchaseReturnLineIn(
                    line_number=1, goods_receipt_line_id=receipt_line_id, quantity=Decimal("25")
                )
            ],
        ),
    )
    return_id = purchase_return.id

    from src.modules.finance import service as finance_service

    async def fail_finance_post(*args, **kwargs):
        raise BusinessRuleError("Finance posting refused")

    monkeypatch.setattr(finance_service, "post_purchase_return_debit_note", fail_finance_post)
    with pytest.raises(BusinessRuleError, match="Finance posting refused"):
        await service.post_purchase_return(db, tenant, buyer, return_id)

    detail = await service.get_purchase_return_detail(db, tenant, return_id)
    assert detail.status is PurchaseReturnStatus.DRAFT
    receipt_returned = await db.scalar(
        select(GoodsReceiptLine.returned_quantity).where(GoodsReceiptLine.id == receipt_line_id)
    )
    order_returned = await db.scalar(
        select(PurchaseOrderLine.returned_quantity).where(PurchaseOrderLine.id == order_line_id)
    )
    assert receipt_returned == Decimal("0.0000")
    assert order_returned == Decimal("0.0000")
    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.on_hand_quantity == Decimal("100.0000")
    assert (
        await db.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.reference_type == "purchase_return",
                InventoryTransaction.reference_id == return_id,
            )
        )
    ).scalars().all() == []


async def test_a_return_cannot_exceed_what_the_receipt_accepted(db, tenant, buyer, masters):
    """Otherwise the position goes negative against goods that never arrived."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="100", rejected="20")

    with pytest.raises(BusinessRuleError) as exc:
        await service.create_purchase_return(
            db,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=posted.receipt.id,
                reason="Too many",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=posted.receipt.lines[0].id,
                        quantity=Decimal("90"),
                    )
                ],
            ),
        )
    assert exc.value.details["returnable_quantity"] == "80.0000"


async def test_two_returns_cannot_between_them_exceed_the_accepted_quantity(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="100")
    receipt_line_id = posted.receipt.lines[0].id

    first, _ = await service.create_purchase_return(
        db,
        tenant,
        PurchaseReturnCreate(
            goods_receipt_id=posted.receipt.id,
            reason="First batch defective",
            lines=[
                PurchaseReturnLineIn(
                    line_number=1, goods_receipt_line_id=receipt_line_id, quantity=Decimal("60")
                )
            ],
        ),
    )
    await service.post_purchase_return(db, tenant, buyer, first.id)

    with pytest.raises(BusinessRuleError):
        await service.create_purchase_return(
            db,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=posted.receipt.id,
                reason="Rest also defective",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=receipt_line_id,
                        quantity=Decimal("50"),
                    )
                ],
            ),
        )


async def test_a_return_against_an_unposted_receipt_is_refused(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="100", post=False)

    with pytest.raises(BusinessRuleError):
        await service.create_purchase_return(
            db,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=receipt.id,
                reason="Never arrived",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=uuid.uuid4(),
                        quantity=Decimal("1"),
                    )
                ],
            ),
        )


# =============================================================================
# Duplicate purchase order prevention, end to end
# =============================================================================


async def test_a_duplicate_order_is_refused_without_an_acknowledgement(db, tenant, buyer, masters):
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    with pytest.raises(BusinessRuleError) as exc:
        await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    assert exc.value.code is ErrorCode.DUPLICATE_DOCUMENT
    assert exc.value.details["candidate_count"] == 1


async def test_an_acknowledged_duplicate_is_created_and_the_reason_is_recorded(
    db, tenant, buyer, masters
):
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    order, _, candidates = await make_order(
        db, tenant, buyer, masters, expected_delivery_date=required, acknowledge_duplicates=True
    )

    assert len(candidates) == 1
    assert order.duplicate_acknowledged is True
    assert order.duplicate_acknowledgement_reason


async def test_editing_delivery_date_rechecks_duplicates_and_rolls_back_on_refusal(
    db, tenant, buyer, masters
):
    existing_date = today() + timedelta(days=10)
    edited_date = existing_date + timedelta(days=40)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=existing_date)
    order, _, _ = await make_order(db, tenant, buyer, masters, expected_delivery_date=edited_date)
    order_id = order.id

    with pytest.raises(BusinessRuleError) as failure:
        await service.update_purchase_order(
            db,
            tenant,
            buyer,
            order_id,
            PurchaseOrderUpdate(expected_delivery_date=existing_date),
        )

    assert failure.value.code is ErrorCode.DUPLICATE_DOCUMENT
    persisted_date = await db.scalar(
        select(PurchaseOrder.expected_delivery_date).where(PurchaseOrder.id == order_id)
    )
    assert persisted_date == edited_date


async def test_editing_a_duplicate_requires_approval_and_records_acknowledgement(
    db, tenant, buyer, buyer_without_approval, masters
):
    existing_date = today() + timedelta(days=10)
    edited_date = existing_date + timedelta(days=40)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=existing_date)
    order, _, _ = await make_order(db, tenant, buyer, masters, expected_delivery_date=edited_date)
    order_id = order.id
    update = PurchaseOrderUpdate(
        expected_delivery_date=existing_date,
        acknowledge_duplicates=True,
        duplicate_acknowledgement_reason="Additional supply for a separate production run",
    )

    with pytest.raises(PermissionDeniedError):
        await service.update_purchase_order(db, tenant, buyer_without_approval, order_id, update)

    updated, _ = await service.update_purchase_order(db, tenant, buyer, order_id, update)

    assert updated.duplicate_acknowledged is True
    assert updated.duplicate_acknowledgement_reason == (
        "Additional supply for a separate production run"
    )


async def test_editing_away_from_a_duplicate_clears_its_acknowledgement(db, tenant, buyer, masters):
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)
    order, _, _ = await make_order(
        db,
        tenant,
        buyer,
        masters,
        expected_delivery_date=required,
        acknowledge_duplicates=True,
    )

    updated, _ = await service.update_purchase_order(
        db,
        tenant,
        buyer,
        order.id,
        PurchaseOrderUpdate(
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=masters["batched_item"].id,
                    quantity=Decimal("100"),
                    unit_price=Decimal("10"),
                )
            ]
        ),
    )

    assert updated.duplicate_acknowledged is False
    assert updated.duplicate_acknowledgement_reason is None


async def test_acknowledging_a_duplicate_requires_the_approval_permission(
    db, tenant, buyer, buyer_without_approval, masters
):
    """Overriding the warning is an approval decision, not a data-entry one."""
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    with pytest.raises(PermissionDeniedError):
        await make_order(
            db,
            tenant,
            buyer_without_approval,
            masters,
            expected_delivery_date=required,
            acknowledge_duplicates=True,
        )


async def test_an_order_for_a_different_item_is_not_a_duplicate(db, tenant, buyer, masters):
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    order, _, candidates = await make_order(
        db,
        tenant,
        buyer,
        masters,
        item=masters["batched_item"],
        expected_delivery_date=required,
    )
    assert candidates == []
    assert order.duplicate_acknowledged is False


async def test_an_order_well_outside_the_window_is_not_a_duplicate(db, tenant, buyer, masters):
    await make_order(
        db, tenant, buyer, masters, expected_delivery_date=today() + timedelta(days=10)
    )

    _, _, candidates = await make_order(
        db, tenant, buyer, masters, expected_delivery_date=today() + timedelta(days=90)
    )
    assert candidates == []


async def test_a_cancelled_order_stops_being_a_duplicate_candidate(db, tenant, buyer, masters):
    required = today() + timedelta(days=10)
    first, _, _ = await make_order(db, tenant, buyer, masters, expected_delivery_date=required)
    await service.transition_purchase_order(
        db, tenant, buyer, first.id, PurchaseOrderStatus.CANCELLED, reason="Vendor cannot supply"
    )

    _, _, candidates = await make_order(db, tenant, buyer, masters, expected_delivery_date=required)
    assert candidates == []


async def test_the_matcher_finds_an_order_raised_from_the_same_mrp_suggestion(
    db, tenant, buyer, masters
):
    """The section 16 case: one shortage, two buyers, two orders."""
    suggestion = uuid.uuid4()
    await make_order(
        db,
        tenant,
        buyer,
        masters,
        expected_delivery_date=today() + timedelta(days=10),
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=suggestion,
    )

    candidates = await service.find_duplicate_candidates(
        db,
        tenant,
        vendor_id=masters["vendor"].id,
        item_id=masters["item"].id,
        # Deliberately far outside the window - the shared source still matches.
        required_date=today() + timedelta(days=120),
        warehouse_id=masters["warehouse"].id,
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=suggestion,
    )

    assert len(candidates) == 1
    assert str(candidates[0].confidence) == "EXACT"


async def test_the_preview_endpoint_reports_candidates_without_writing_anything(
    db, tenant, buyer, masters
):
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    result = await service.check_duplicates(
        db,
        tenant,
        DuplicateCheckRequest(
            vendor_id=masters["vendor"].id,
            warehouse_id=masters["warehouse"].id,
            expected_delivery_date=required,
            lines=[DuplicateCheckLine(item_id=masters["item"].id)],
        ),
    )

    assert result.requires_acknowledgement is True
    assert len(result.candidates) == 1
    assert result.candidates[0].recommended_action in result.candidates[0].available_actions


async def test_duplicates_are_scoped_to_the_tenant(db, tenant, other_tenant, buyer, masters):
    """Another company's open order must never surface as a duplicate."""
    required = today() + timedelta(days=10)
    await make_order(db, tenant, buyer, masters, expected_delivery_date=required)

    candidates = await service.find_duplicate_candidates(
        db,
        other_tenant,
        vendor_id=masters["vendor"].id,
        item_id=masters["item"].id,
        required_date=required,
        warehouse_id=masters["warehouse"].id,
    )
    assert candidates == []


# =============================================================================
# Purchase order lifecycle
# =============================================================================


async def test_the_approver_may_not_be_the_person_who_raised_the_order(db, tenant, buyer, masters):
    """Segregation of duties (docs/assumptions.md section 2)."""
    order, _, _ = await make_order(db, tenant, buyer, masters)
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )

    with pytest.raises(AuthorizationError):
        await service.transition_purchase_order(
            db, tenant, buyer, order.id, PurchaseOrderStatus.APPROVED, reason="self-approve"
        )


async def test_a_second_person_can_approve_and_the_evidence_is_stored(
    db, tenant, buyer, approver, masters
):
    order, _, _ = await make_order(db, tenant, buyer, masters)
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )
    approved = await service.transition_purchase_order(
        db, tenant, approver, order.id, PurchaseOrderStatus.APPROVED, reason="Prices checked"
    )

    assert approved.status is PurchaseOrderStatus.APPROVED
    assert approved.approved_by_id == approver.user_id
    assert approved.approved_at is not None


async def test_an_order_that_has_received_stock_cannot_be_cancelled(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    await receive(db, tenant, buyer, order, lines[0], received="40")

    with pytest.raises(InvalidStateTransitionError):
        await service.transition_purchase_order(
            db, tenant, buyer, order.id, PurchaseOrderStatus.CANCELLED, reason="too late"
        )


async def test_short_closing_stops_the_undelivered_balance_being_expected(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="100")
    await send_order(db, tenant, buyer, order)
    await receive(db, tenant, buyer, order, lines[0], received="40")

    closed = await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.CLOSED, reason="Vendor cannot supply rest"
    )

    assert closed.status is PurchaseOrderStatus.CLOSED
    line = await reload_line(db, lines[0].id)
    assert line.status is PurchaseOrderLineStatus.CLOSED


async def test_an_approved_order_can_no_longer_be_edited(db, tenant, buyer, approver, masters):
    from src.modules.procurement.schemas import PurchaseOrderUpdate

    order, _, _ = await make_order(db, tenant, buyer, masters)
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )
    await service.transition_purchase_order(
        db, tenant, approver, order.id, PurchaseOrderStatus.APPROVED, reason="approve"
    )

    with pytest.raises(BusinessRuleError):
        await service.update_purchase_order(
            db,
            tenant,
            buyer,
            order.id,
            PurchaseOrderUpdate(notes="sneaking a change past the approver"),
        )


async def test_an_order_cannot_be_raised_on_an_unapproved_vendor(db, tenant, buyer, masters):
    masters["vendor"].is_approved = False
    await db.flush()

    with pytest.raises(BusinessRuleError):
        await make_order(db, tenant, buyer, masters)


async def test_totals_are_stored_on_the_header(db, tenant, buyer, masters):
    order, _, _ = await make_order(db, tenant, buyer, masters, quantity="10", unit_price="100")

    assert order.subtotal == Decimal("1000.0000")
    assert order.tax_amount == Decimal("180.0000")
    assert order.total_amount == Decimal("1180.0000")


# =============================================================================
# Tracking, approvals queue and tenant isolation
# =============================================================================


async def test_the_tracking_view_reports_delay_qc_and_what_is_still_due(db, tenant, buyer, masters):
    order, lines, _ = await make_order(
        db,
        tenant,
        buyer,
        masters,
        quantity="100",
        requires_qc=True,
        po_date=today() - timedelta(days=20),
        expected_delivery_date=today() - timedelta(days=3),
    )
    await send_order(db, tenant, buyer, order)
    await service.record_shipment(
        db,
        tenant,
        order.id,
        courier_name="Blue Dart",
        tracking_reference="BD-99881",
        shipped_at=None,
    )
    await receive(db, tenant, buyer, order, lines[0], received="40")

    tracking = await service.order_tracking(db, tenant, order.id)

    assert tracking.courier_name == "Blue Dart"
    assert tracking.tracking_reference == "BD-99881"
    assert tracking.ordered_quantity == Decimal("100.0000")
    assert tracking.received_quantity == Decimal("40.0000")
    assert tracking.pending_quantity == Decimal("60.0000")
    assert tracking.grn_pending is True
    assert tracking.is_delayed is True
    assert tracking.days_late == 3
    assert tracking.qc_pending is True
    assert tracking.qc_pending_line_count == 1
    assert len(tracking.receipts) == 1


async def test_a_completed_order_is_not_reported_as_delayed(db, tenant, buyer, masters):
    """A late but complete delivery is a performance fact, not a live problem."""
    order, lines, _ = await make_order(
        db,
        tenant,
        buyer,
        masters,
        quantity="50",
        po_date=today() - timedelta(days=20),
        expected_delivery_date=today() - timedelta(days=5),
    )
    await send_order(db, tenant, buyer, order)
    await receive(db, tenant, buyer, order, lines[0], received="50")

    tracking = await service.order_tracking(db, tenant, order.id)

    assert tracking.days_late == 5
    assert tracking.is_delayed is False
    assert tracking.grn_pending is False
    assert tracking.pending_quantity == Decimal("0.0000")


async def test_the_approvals_queue_merges_requisitions_and_orders(
    db, tenant, buyer, approver, masters
):
    requisition, _ = await service.create_requisition(
        db,
        tenant,
        buyer,
        RequisitionCreate(
            required_date=today() + timedelta(days=14),
            warehouse_id=masters["warehouse"].id,
            lines=[
                RequisitionLineIn(line_number=1, item_id=masters["item"].id, quantity=Decimal("25"))
            ],
        ),
    )
    await service.transition_requisition(
        db, tenant, buyer, requisition.id, RequisitionStatus.PENDING_APPROVAL, reason="submit"
    )

    order, _, _ = await make_order(db, tenant, buyer, masters)
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )

    rows, pagination = await service.pending_approvals(
        db, tenant, approver, PendingApprovalFilters(), PageParams(page=1, limit=50)
    )

    kinds = {row.document_type for row in rows}
    assert kinds == {"purchase_requisition", "purchase_order"}
    assert pagination.total == 2
    order_row = next(row for row in rows if row.document_type == "purchase_order")
    assert order_row.total_amount == Decimal("5900.0000")
    assert order_row.counterparty_id == masters["vendor"].id


async def test_the_queue_only_shows_what_the_caller_can_approve(
    db, tenant, buyer, buyer_without_approval, masters
):
    order, _, _ = await make_order(db, tenant, buyer, masters)
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )

    rows, _ = await service.pending_approvals(
        db,
        tenant,
        buyer_without_approval,
        PendingApprovalFilters(),
        PageParams(page=1, limit=50),
    )
    assert rows == []


async def test_another_tenant_cannot_see_or_fetch_this_company_s_orders(
    db, tenant, other_tenant, buyer, masters
):
    order, _, _ = await make_order(db, tenant, buyer, masters)

    rows, _ = await service.list_purchase_orders(
        db, other_tenant, PurchaseOrderFilters(), PageParams(page=1, limit=50), SortParams()
    )
    assert rows == []

    with pytest.raises(NotFoundError):
        await service.get_purchase_order_detail(db, other_tenant, order.id)


async def test_a_requisition_is_marked_converted_when_an_order_is_raised_from_it(
    db, tenant, buyer, approver, masters
):
    """Otherwise it stays open and the next buyer re-orders it."""
    requisition, requisition_lines = await service.create_requisition(
        db,
        tenant,
        buyer,
        RequisitionCreate(
            required_date=today() + timedelta(days=14),
            warehouse_id=masters["warehouse"].id,
            lines=[
                RequisitionLineIn(
                    line_number=1, item_id=masters["item"].id, quantity=Decimal("100")
                )
            ],
        ),
    )
    await service.transition_requisition(
        db, tenant, buyer, requisition.id, RequisitionStatus.PENDING_APPROVAL, reason="submit"
    )
    await service.transition_requisition(
        db, tenant, approver, requisition.id, RequisitionStatus.APPROVED, reason="approve"
    )

    await service.create_purchase_order(
        db,
        tenant,
        buyer,
        PurchaseOrderCreate(
            vendor_id=masters["vendor"].id,
            warehouse_id=masters["warehouse"].id,
            expected_delivery_date=today() + timedelta(days=10),
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=masters["item"].id,
                    quantity=Decimal("100"),
                    unit_price=Decimal("50"),
                    requisition_line_id=requisition_lines[0].id,
                )
            ],
        ),
    )

    detail = await service.get_requisition_detail(db, tenant, requisition.id)
    assert detail.status is RequisitionStatus.CONVERTED
    assert detail.lines[0].ordered_quantity == Decimal("100.0000")
    assert str(detail.lines[0].status) == "ORDERED"


async def test_a_receipt_updates_the_vendor_s_last_purchase_price(db, tenant, buyer, masters):
    """The products module documents this column as written by GRN posting."""
    from src.modules.products.models import ItemVendorLink

    link = ItemVendorLink(
        company_id=tenant.company_id,
        item_id=masters["item"].id,
        vendor_id=masters["vendor"].id,
        is_preferred=True,
        lead_time_days=7,
    )
    db.add(link)
    await db.flush()

    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="10", unit_price="77.5")
    await send_order(db, tenant, buyer, order)
    await receive(db, tenant, buyer, order, lines[0], received="10")

    await db.refresh(link)
    assert link.last_purchase_price == Decimal("77.500000")
    assert link.last_purchase_date == today()


# =============================================================================
# Quotations
# =============================================================================


async def make_rfq(db, tenant, masters, *, quantity: str = "100"):
    rfq, _ = await service.create_rfq(
        db,
        tenant,
        RfqCreate(
            warehouse_id=masters["warehouse"].id,
            lines=[
                RfqLineIn(line_number=1, item_id=masters["item"].id, quantity=Decimal(quantity))
            ],
        ),
    )
    return rfq


def quotation_payload(rfq, masters, *, unit_price: str = "50", **terms) -> QuotationCreate:
    return QuotationCreate(
        rfq_id=rfq.id,
        vendor_id=masters["vendor"].id,
        vendor_reference=f"Q-{uuid.uuid4().hex[:6].upper()}",
        lines=[
            QuotationLineIn(
                line_number=1,
                item_id=masters["item"].id,
                quantity=Decimal("100"),
                unit_price=Decimal(unit_price),
            )
        ],
        **terms,
    )


async def test_a_quotation_takes_the_vendor_s_terms_when_omitted_and_keeps_an_explicit_zero(
    db, tenant, masters
):
    """``None`` means "as agreed with the vendor"; ``0`` means ex-stock, or cash.
    ``or`` would have quietly turned the second into the first."""
    rfq = await make_rfq(db, tenant, masters)
    as_agreed, _ = await service.create_quotation(db, tenant, quotation_payload(rfq, masters))
    assert (as_agreed.payment_terms_days, as_agreed.lead_time_days) == (30, 7)

    ex_stock = await make_rfq(db, tenant, masters)
    cash, _ = await service.create_quotation(
        db, tenant, quotation_payload(ex_stock, masters, payment_terms_days=0, lead_time_days=0)
    )
    assert (cash.payment_terms_days, cash.lead_time_days) == (0, 0)


async def test_a_rejected_quotation_is_reopened_by_its_revision_not_by_a_second_one(
    db, tenant, buyer, masters
):
    """The RFQ holds one row per vendor. A rejected offer is revised back to
    RECEIVED - a state change with its own audit row - and recording a *second*
    quotation for the same vendor is still a 409."""
    rfq = await make_rfq(db, tenant, masters)
    quotation, _ = await service.create_quotation(db, tenant, quotation_payload(rfq, masters))
    await service.decide_quotation(
        db, tenant, buyer, quotation.id, QuotationStatus.REJECTED, reason="Too dear"
    )
    assert QuotationStatus(quotation.status) is QuotationStatus.REJECTED

    revised, _ = await service.update_quotation(
        db,
        tenant,
        quotation.id,
        QuotationUpdate(
            reason="Vendor requoted at a lower price",
            lines=[
                QuotationLineIn(
                    line_number=1,
                    item_id=masters["item"].id,
                    quantity=Decimal("100"),
                    unit_price=Decimal("45"),
                )
            ],
        ),
    )
    assert QuotationStatus(revised.status) is QuotationStatus.RECEIVED
    assert revised.decision_reason is None
    assert revised.selected_by_id is None

    trail = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == service.AUDIT_QUOTATION,
                    AuditLog.entity_id == quotation.id,
                )
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    reopened = [
        row
        for row in trail
        if row.action == AuditAction.STATUS_CHANGE
        and row.old_values == {"status": "REJECTED"}
        and row.new_values == {"status": "RECEIVED"}
    ]
    assert len(reopened) == 1, [(row.action, row.old_values, row.new_values) for row in trail]
    assert reopened[0].context == {"reason": "Vendor requoted at a lower price"}

    with pytest.raises(BusinessRuleError) as excinfo:
        await service.create_quotation(db, tenant, quotation_payload(rfq, masters))
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == ErrorCode.DUPLICATE_DOCUMENT


# =============================================================================
# Returns drawn against inspected lots
# =============================================================================


async def test_a_rejected_lot_is_returned_from_damaged_stock(db, tenant, buyer, masters):
    """Receive on hold, fail the inspection, send the lot back.

    The inspection moves the quantity from QC_HOLD to DAMAGED; the return then
    leaves the DAMAGED pool the inspection put it in - not AVAILABLE, which it
    never reached - and the position ends at nothing in either.
    """
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="80", requires_qc=True)
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="80")
    receipt_line = posted.receipt.lines[0]
    assert receipt_line.qc_inspection_id is not None

    await quality_service.decide_inspection(
        db,
        tenant,
        receipt_line.qc_inspection_id,
        DecideRequest(
            rejected_quantity=Decimal("80"),
            reason="Failed incoming inspection",
            rejection_reason="Thread pitch out of specification across the lot",
        ),
    )
    inspected = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert inspected.qc_hold_quantity == Decimal("0.0000")
    assert inspected.damaged_quantity == Decimal("80.0000")

    purchase_return, _ = await service.create_purchase_return(
        db,
        tenant,
        PurchaseReturnCreate(
            goods_receipt_id=posted.receipt.id,
            reason="Rejected at incoming inspection",
            lines=[
                PurchaseReturnLineIn(
                    line_number=1, goods_receipt_line_id=receipt_line.id, quantity=Decimal("80")
                )
            ],
        ),
    )
    await service.post_purchase_return(db, tenant, buyer, purchase_return.id)

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.damaged_quantity == Decimal("0.0000")
    assert balance.on_hand_quantity == Decimal("0.0000")

    movement = (
        await db.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.reference_type == "purchase_return",
                InventoryTransaction.reference_id == purchase_return.id,
            )
        )
    ).scalar_one()
    assert str(movement.stock_state) == str(StockState.DAMAGED)
    assert movement.quantity == Decimal("-80.0000")


async def test_a_return_against_a_line_still_on_qc_hold_is_refused(db, tenant, buyer, masters):
    """Until the inspection decides, nobody knows which pool the goods are in -
    so nothing can leave it."""
    order, lines, _ = await make_order(db, tenant, buyer, masters, quantity="80", requires_qc=True)
    await send_order(db, tenant, buyer, order)
    _, posted = await receive(db, tenant, buyer, order, lines[0], received="80")

    with pytest.raises(BusinessRuleError, match="still on QC hold"):
        await service.create_purchase_return(
            db,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=posted.receipt.id,
                reason="Trying to return goods nobody has looked at",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=posted.receipt.lines[0].id,
                        quantity=Decimal("10"),
                    )
                ],
            ),
        )

    balance = await balance_of(db, tenant, masters["item"].id, masters["warehouse"].id)
    assert balance.qc_hold_quantity == Decimal("80.0000")
    assert balance.on_hand_quantity == Decimal("80.0000")
