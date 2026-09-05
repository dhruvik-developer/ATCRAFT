"""Service-level tests for job work against a real PostgreSQL.

What is under test is the sentence master rules section 20 turns on: *materials
issued for job work must remain traceable*. Every assertion here is about the
balances and the ledger afterwards - specifically that company-owned stock is
conserved across a despatch to a vendor and a return from one:

    on hand + job work + damaged + scrap, before == after

Issuing must move quantity into ``inventory_balances.job_work_quantity`` and out
of the available pool without ever reducing what the company owns; receiving
must draw that pool down by exactly what it accounted for. A module that posted
a job-work issue as a dispatch would fail the very first test in this file.

These need PostgreSQL because none of it is expressible without it: the
generated ``available_quantity`` column, the check constraints that make an
over-consuming receipt impossible, ``SELECT ... FOR UPDATE`` on the header and
the balances, and the audit rows written inside the same transaction as the
movements they describe.

Isolation follows ``test_assembly_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
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
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    ApprovalRequiredError,
    BusinessRuleError,
    InvalidStateTransitionError,
    PermissionDeniedError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.audit.models import AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    StockState,
    TransactionType,
)
from src.modules.job_work import service
from src.modules.job_work.models import (
    JobWorkChallan,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkReceipt,
    RateBasis,
    RejectionAction,
)
from src.modules.job_work.schemas import (
    IssueRequest,
    JobWorkOrderCreate,
    JobWorkOrderFilters,
    JobWorkOrderLineCreate,
    QcResultRequest,
    QcVerdictItem,
    ReceiptLineIn,
    ReceiveRequest,
)
from src.modules.products.models import Item, ItemGroup, UnitOfMeasure, UomCategory
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
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
    schema = f"it_jobwork_{uuid.uuid4().hex[:10]}"
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
                await seed_company(session, company_id=company_id, prefix="JW")
                await seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email="operator@atcraft.test",
                    full_name="Operator",
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


def principal_for(tenant: TenantContext, *permissions: Permission) -> Principal:
    """The operator as a caller. Naming no rights means every right."""
    held = permissions or tuple(Permission)
    return Principal(
        user_id=tenant.user_id,
        company_id=tenant.company_id,
        email="operator@atcraft.test",
        full_name="Operator",
        is_superuser=False,
        permissions=frozenset(str(p) for p in held),
    )


# -----------------------------------------------------------------------------
# Seeding - through the ORM, so Python-side column defaults are applied
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def seed_uom(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    uom = UnitOfMeasure(
        company_id=company_id,
        code=_code("NOS")[:16],
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=False,
    )
    session.add(uom)
    await session.flush()
    return uom.id


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    uom_id: uuid.UUID,
    *,
    code: str,
    can_job_work: bool = True,
    standard_cost: str = "100",
    batch_tracked: bool = False,
) -> uuid.UUID:
    item = Item(
        company_id=company_id,
        code=code,
        name=f"Item {code}",
        item_group=ItemGroup.COMPONENT,
        base_uom_id=uom_id,
        standard_cost=Decimal(standard_cost),
        can_job_work=can_job_work,
        is_batch_tracked=batch_tracked,
    )
    session.add(item)
    await session.flush()
    return item.id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    code = _code("WH")
    warehouse = Warehouse(
        company_id=company_id,
        code=code,
        name=f"Store {code}",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    session.add(warehouse)
    await session.flush()
    return warehouse.id


async def seed_vendor(
    session: AsyncSession, company_id: uuid.UUID, *, approved: bool = True, active: bool = True
) -> uuid.UUID:
    vendor = Vendor(
        company_id=company_id,
        code=_code("V")[:32],
        name="Chrome Plating Works",
        vendor_types=[VendorType.JOB_WORK.value],
        is_approved=approved,
        # The vendors table refuses an approval with no timestamp behind it.
        approved_at=utcnow() if approved else None,
        is_active=active,
    )
    session.add(vendor)
    await session.flush()
    return vendor.id


async def stock_in(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    unit_cost: str = "100",
) -> None:
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="job work fixture opening stock",
            unit_cost=Decimal(unit_cost),
        )


async def balance(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> InventoryBalance | None:
    return await inv.get_balance(session, tenant, item_id, warehouse_id)


async def company_owned(
    session: AsyncSession, tenant: TenantContext, *item_ids: uuid.UUID
) -> Decimal:
    """Everything the company still owns for these items, wherever it sits.

    ``on_hand`` already contains the QC-held and damaged sub-pools (the generated
    ``available_quantity`` column depends on that), so they are not added again.
    ``job_work`` and ``scrap`` sit outside on-hand and are.
    """
    stmt = select(
        func.coalesce(func.sum(InventoryBalance.on_hand_quantity), 0),
        func.coalesce(func.sum(InventoryBalance.job_work_quantity), 0),
        func.coalesce(func.sum(InventoryBalance.scrap_quantity), 0),
    ).where(
        InventoryBalance.company_id == tenant.company_id,
        InventoryBalance.item_id.in_(list(item_ids)),
    )
    on_hand, job_work, scrap = (await session.execute(stmt)).one()
    return Decimal(str(on_hand)) + Decimal(str(job_work)) + Decimal(str(scrap))


class PvdJob:
    """The worked example: raw handles out for PVD, coated handles back."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        raw_item_id: uuid.UUID,
        coated_item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        vendor_id: uuid.UUID,
    ) -> None:
        self.uom_id = uom_id
        self.raw_item_id = raw_item_id
        self.coated_item_id = coated_item_id
        self.warehouse_id = warehouse_id
        self.vendor_id = vendor_id

    def payload(
        self,
        *,
        quantity: str = "100",
        expected_output: str | None = None,
        loss_percentage: str = "0",
        rate: str = "12.50",
        rate_basis: RateBasis = RateBasis.PER_PIECE,
        requires_qc: bool = False,
    ) -> JobWorkOrderCreate:
        return JobWorkOrderCreate(
            vendor_id=self.vendor_id,
            process_type=JobWorkProcess.PVD,
            process_description="PVD, rose gold, 3 micron",
            expected_return_date=today() + timedelta(days=14),
            warehouse_id=self.warehouse_id,
            rate_per_unit=Decimal(rate),
            rate_basis=rate_basis,
            requires_qc=requires_qc,
            lines=[
                JobWorkOrderLineCreate(
                    line_number=1,
                    item_id=self.raw_item_id,
                    quantity_to_issue=Decimal(quantity),
                    expected_output_item_id=self.coated_item_id,
                    expected_output_quantity=(
                        Decimal(expected_output) if expected_output else None
                    ),
                    expected_loss_percentage=Decimal(loss_percentage),
                )
            ],
        )


@pytest_asyncio.fixture
async def job(ctx) -> PvdJob:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    return PvdJob(
        uom_id=uom_id,
        raw_item_id=await seed_item(session, tenant.company_id, uom_id, code=_code("RAW")),
        coated_item_id=await seed_item(
            session, tenant.company_id, uom_id, code=_code("PVD"), standard_cost="130"
        ),
        warehouse_id=await seed_warehouse(session, tenant.company_id),
        vendor_id=await seed_vendor(session, tenant.company_id),
    )


async def issued_order(ctx, job: PvdJob, *, stock: str = "100", **payload_kwargs):
    """Raise, approve and issue an order, leaving its material at the vendor."""
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, stock)
    order, _ = await service.create_order(session, tenant, job.payload(**payload_kwargs))
    await service.approve_order(session, tenant, order.id, reason="Rate agreed with vendor")
    result = await service.issue_order(
        session,
        tenant,
        order.id,
        IssueRequest(reason="Handed to the vendor's vehicle", vehicle_number="GJ01AB1234"),
    )
    return order, result


# -----------------------------------------------------------------------------
# THE rule: material at a vendor is still company-owned stock
# -----------------------------------------------------------------------------


async def test_issuing_moves_stock_into_the_job_work_pool_and_off_availability(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    _order, result = await issued_order(ctx, job)

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None
    # The whole point: it left the available pool without leaving the company.
    assert Decimal(row.job_work_quantity) == Decimal("100.0000")
    assert Decimal(row.available_quantity) == 0
    assert Decimal(row.on_hand_quantity) == 0
    assert result.quantity_at_vendor == Decimal("100.0000")


async def test_issuing_posts_a_job_work_issue_and_not_a_dispatch(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    stmt = select(InventoryTransaction).where(
        InventoryTransaction.company_id == tenant.company_id,
        InventoryTransaction.reference_id == order.id,
    )
    rows = list((await session.execute(stmt)).scalars().all())

    assert [r.transaction_type for r in rows] == [TransactionType.JOB_WORK_ISSUE]
    assert rows[0].stock_state == StockState.JOB_WORK
    # A dispatch or a purchase return would say the goods stopped being ours.
    assert TransactionType.DISPATCH not in {r.transaction_type for r in rows}


async def test_company_owned_stock_is_conserved_across_a_clean_issue_and_receipt(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    before = await company_owned(session, tenant, job.raw_item_id, job.coated_item_id)

    order, _ = await service.create_order(session, tenant, job.payload())
    await service.approve_order(session, tenant, order.id, reason="Rate agreed")
    await service.issue_order(session, tenant, order.id, IssueRequest(reason="Despatched"))
    during = await company_owned(session, tenant, job.raw_item_id, job.coated_item_id)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Full lot returned coated",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )
    after = await company_owned(session, tenant, job.raw_item_id, job.coated_item_id)

    # 100 units owned throughout: raw on hand, then raw at the vendor, then
    # coated on hand. Not one of those steps may lose a unit off the books.
    assert before == Decimal("100.0000")
    assert during == Decimal("100.0000")
    assert after == Decimal("100.0000")


async def test_a_short_return_conserves_stock_with_the_shortfall_as_scrap(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="5")

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="98 came back; 2 lost in the plating bath",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("98"), is_final=True)],
        ),
    )

    raw = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    coated = await balance(session, tenant, job.coated_item_id, job.warehouse_id)
    assert raw is not None and coated is not None

    # The pool is empty, the loss is stated as scrap, and the company still owns
    # 100 units of something.
    assert Decimal(raw.job_work_quantity) == 0
    assert Decimal(raw.scrap_quantity) == Decimal("2.0000")
    assert Decimal(coated.on_hand_quantity) == Decimal("98.0000")
    assert await company_owned(session, tenant, job.raw_item_id, job.coated_item_id) == Decimal(
        "100.0000"
    )


async def test_the_reconciliation_agrees_with_the_ledgers_job_work_pool(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Part lot returned",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("60"))],
        ),
    )
    report = await service.reconciliation(session, tenant, order.id)

    assert report.total_issued == Decimal("100.0000")
    assert report.total_received == Decimal("60.0000")
    assert report.total_outstanding == Decimal("40.0000")
    # THE cross-check: what this module says is at the vendor is what the stock
    # ledger is holding in the JOB_WORK state.
    assert report.stock_in_job_work_pool == report.total_outstanding
    assert report.lines[0].output_shortfall_quantity == Decimal("40.0000")


async def test_material_at_a_vendor_is_reported_with_its_ageing(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    _order, _ = await issued_order(ctx, job)

    rows = await service.pending_at_vendor(session, tenant)

    assert len(rows) == 1
    assert rows[0].vendor_id == job.vendor_id
    assert rows[0].item_id == job.raw_item_id
    assert rows[0].outstanding_quantity == Decimal("100.0000")
    assert rows[0].order_count == 1
    assert rows[0].ageing_days == 0
    assert rows[0].is_overdue is False


async def test_a_fully_received_order_leaves_the_pending_feed(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="All back",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    assert await service.pending_at_vendor(session, tenant) == []


# -----------------------------------------------------------------------------
# The challan
# -----------------------------------------------------------------------------


async def test_issuing_raises_a_challan_valued_from_the_ledger(ctx, job: PvdJob) -> None:
    session, _tenant = ctx
    order, result = await issued_order(ctx, job)

    challan = (
        await session.execute(
            select(JobWorkChallan).where(JobWorkChallan.job_work_order_id == order.id)
        )
    ).scalar_one()

    assert challan.code.startswith("JWC/")
    assert challan.vendor_id == job.vendor_id
    assert challan.total_quantity == Decimal("100.0000")
    # Costed at the position's moving average (the 100 opening units at 100.00),
    # not at a price list that may have moved since.
    assert challan.total_value == Decimal("10000.0000")
    assert result.challan.lines[0].quantity == Decimal("100.0000")


async def test_a_challan_cannot_be_raised_for_an_unapproved_order(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    order, _ = await service.create_order(session, tenant, job.payload())

    with pytest.raises(InvalidStateTransitionError):
        await service.issue_order(session, tenant, order.id, IssueRequest(reason="Too early"))

    assert (
        await session.execute(select(func.count()).select_from(JobWorkChallan))
    ).scalar_one() == 0


# -----------------------------------------------------------------------------
# Reservations
# -----------------------------------------------------------------------------


async def test_approving_reserves_the_material_so_nobody_else_is_promised_it(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    order, _ = await service.create_order(session, tenant, job.payload())

    await service.approve_order(session, tenant, order.id, reason="Rate agreed")

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None
    assert Decimal(row.reserved_quantity) == Decimal("100.0000")
    assert Decimal(row.available_quantity) == 0


async def test_approving_without_the_stock_to_send_is_refused(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "40")
    order, _ = await service.create_order(session, tenant, job.payload(quantity="100"))

    with pytest.raises(BusinessRuleError):
        await service.approve_order(session, tenant, order.id, reason="Rate agreed")


async def test_cancelling_gives_the_reserved_material_back(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    order, _ = await service.create_order(session, tenant, job.payload())
    await service.approve_order(session, tenant, order.id, reason="Rate agreed")

    await service.cancel_order(session, tenant, order.id, reason="Vendor cannot take it")

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None
    assert Decimal(row.reserved_quantity) == 0
    assert Decimal(row.available_quantity) == Decimal("100.0000")

    reservations = list(
        (
            await session.execute(
                select(StockReservation).where(StockReservation.demand_id == order.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.status for r in reservations] == [ReservationStatus.RELEASED]


async def test_issuing_consumes_the_reservation_before_the_stock_moves(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    _order, _ = await issued_order(ctx, job)

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None
    # If the reservation were still standing, reserved would exceed the on-hand
    # the issue reduced to zero, and the balance check constraint would have
    # refused the whole transaction.
    assert Decimal(row.reserved_quantity) == 0
    assert Decimal(row.job_work_quantity) == Decimal("100.0000")


# -----------------------------------------------------------------------------
# Loss, rejection and rework
# -----------------------------------------------------------------------------


async def test_loss_beyond_the_allowance_needs_approval(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="2")

    with pytest.raises(ApprovalRequiredError) as exc:
        await service.receive(
            session,
            tenant,
            principal_for(tenant),
            order.id,
            ReceiveRequest(
                reason="Only 90 usable",
                lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("90"), is_final=True)],
            ),
        )

    assert exc.value.details["excess_loss_quantity"] == "8.0000"
    # Nothing was posted: the pool is untouched and the order is still ISSUED.
    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None and Decimal(row.job_work_quantity) == Decimal("100.0000")


async def test_approved_excess_loss_posts_scrap_and_debits_the_vendor(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="2")

    result = await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Only 90 usable; vendor accepts the debit",
            approve_excess_loss=True,
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("90"), is_final=True)],
        ),
    )

    assert result.receipt.has_excess_loss is True
    assert result.receipt.total_scrap_quantity == Decimal("10.0000")
    # 8 units beyond the 2% allowance, at the raw item's 100.00 average cost.
    assert result.receipt.vendor_debit_amount == Decimal("800.0000")

    raw = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert raw is not None and Decimal(raw.scrap_quantity) == Decimal("10.0000")


async def test_the_excess_flag_from_a_storekeeper_is_refused_and_posts_nothing(
    ctx, job: PvdJob
) -> None:
    """``approve_excess_loss`` is only a request; the right that grants it is
    JOB_WORK_CLOSE, because what it accepts is debited to the vendor ledger."""
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="2")

    with pytest.raises(PermissionDeniedError) as exc:
        await service.receive(
            session,
            tenant,
            principal_for(tenant, Permission.JOB_WORK_RECEIVE),
            order.id,
            ReceiveRequest(
                reason="Only 90 usable",
                approve_excess_loss=True,
                lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("90"), is_final=True)],
            ),
        )

    assert exc.value.details["required_permission"] == str(Permission.JOB_WORK_CLOSE)
    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None and Decimal(row.job_work_quantity) == Decimal("100.0000")


async def test_a_close_holder_is_recorded_as_the_excess_approver(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="2")
    approver = principal_for(tenant, Permission.JOB_WORK_RECEIVE, Permission.JOB_WORK_CLOSE)

    result = await service.receive(
        session,
        tenant,
        approver,
        order.id,
        ReceiveRequest(
            reason="Only 90 usable; vendor accepts the debit",
            approve_excess_loss=True,
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("90"), is_final=True)],
        ),
    )

    receipt = (
        await session.execute(select(JobWorkReceipt).where(JobWorkReceipt.id == result.receipt.id))
    ).scalar_one()
    assert receipt.has_excess_loss is True
    assert receipt.excess_loss_approved_by_id == approver.user_id


async def test_rejected_output_is_received_into_the_damaged_pool(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Ten pieces plated badly and sent back",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("90"),
                    rejected_quantity=Decimal("10"),
                    rejection_action=RejectionAction.RETURN_TO_STOCK,
                    is_final=True,
                )
            ],
        ),
    )

    coated = await balance(session, tenant, job.coated_item_id, job.warehouse_id)
    assert coated is not None
    assert Decimal(coated.on_hand_quantity) == Decimal("100.0000")
    assert Decimal(coated.damaged_quantity) == Decimal("10.0000")
    # Rejected stock is on the books and counted, but nobody can be promised it.
    assert Decimal(coated.available_quantity) == Decimal("90.0000")


async def test_rework_leaves_the_material_at_the_vendor_and_the_order_open(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Ten going back through the bath",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("90"),
                    rejected_quantity=Decimal("10"),
                    rejection_action=RejectionAction.REWORK_AT_VENDOR,
                )
            ],
        ),
    )

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None
    # The ten pieces being reworked are still the company's, still at the vendor
    # and still visible - that is what "traceable" means here.
    assert Decimal(row.job_work_quantity) == Decimal("10.0000")

    refreshed = await service.get_order_detail(session, tenant, order.id)
    assert refreshed.status is JobWorkOrderStatus.PARTIALLY_RECEIVED
    pending = await service.pending_at_vendor(session, tenant)
    assert pending[0].outstanding_quantity == Decimal("10.0000")


async def test_unprocessed_material_handed_back_returns_to_availability(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Vendor could not take the whole lot",
            lines=[
                ReceiptLineIn(
                    line_number=1,
                    output_quantity=Decimal("70"),
                    returned_unprocessed_quantity=Decimal("30"),
                    is_final=True,
                )
            ],
        ),
    )

    raw = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert raw is not None
    assert Decimal(raw.job_work_quantity) == 0
    assert Decimal(raw.available_quantity) == Decimal("30.0000")
    assert await company_owned(session, tenant, job.raw_item_id, job.coated_item_id) == Decimal(
        "100.0000"
    )


async def test_receiving_more_output_than_the_order_expected_is_refused(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    with pytest.raises(BusinessRuleError) as exc:
        await service.receive(
            session,
            tenant,
            principal_for(tenant),
            order.id,
            ReceiveRequest(
                reason="Vendor sent 110",
                lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("110"))],
            ),
        )

    assert "tolerance" in str(exc.value)
    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None and Decimal(row.job_work_quantity) == Decimal("100.0000")


# -----------------------------------------------------------------------------
# Partial receipts and the order lifecycle
# -----------------------------------------------------------------------------


async def test_two_lots_reconcile_to_the_whole_consignment(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="5")

    first = await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="First half",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("50"))],
        ),
    )
    assert first.order.status is JobWorkOrderStatus.PARTIALLY_RECEIVED

    second = await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Balance, two lost",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("48"), is_final=True)],
        ),
    )

    assert second.order.status is JobWorkOrderStatus.RECEIVED
    assert second.receipt.receipt_number == 2
    assert second.receipt.code.endswith("/R2")

    report = await service.reconciliation(session, tenant, order.id)
    assert report.total_output_received == Decimal("98.0000")
    assert report.total_scrap == Decimal("2.0000")
    assert report.total_outstanding == 0
    assert report.stock_in_job_work_pool == 0


async def test_an_order_with_material_still_out_cannot_be_closed(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Part lot",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("60"))],
        ),
    )

    with pytest.raises(InvalidStateTransitionError):
        await service.close_order(session, tenant, order.id, reason="Give up on the rest")

    row = await balance(session, tenant, job.raw_item_id, job.warehouse_id)
    assert row is not None and Decimal(row.job_work_quantity) == Decimal("40.0000")


async def test_closing_a_received_order_completes_and_closes_it(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="All back",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    closed = await service.close_order(session, tenant, order.id, reason="Vendor invoice matched")

    assert JobWorkOrderStatus(closed.status) is JobWorkOrderStatus.CLOSED
    assert closed.closed_at is not None


# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------


async def test_output_that_needs_inspecting_is_held_rather_than_released(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, requires_qc=True)

    result = await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Back from plating, needs checking",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    coated = await balance(session, tenant, job.coated_item_id, job.warehouse_id)
    assert coated is not None
    assert Decimal(coated.qc_hold_quantity) == Decimal("100.0000")
    assert Decimal(coated.available_quantity) == 0
    assert result.output_held_for_qc is True
    assert result.order.status is JobWorkOrderStatus.QC_PENDING


async def test_the_inspection_verdict_releases_and_damages_the_right_quantities(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, requires_qc=True)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Back from plating",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    await service.record_qc_result(
        session,
        tenant,
        order.id,
        QcResultRequest(
            reason="Coating thickness checked",
            items=[
                QcVerdictItem(
                    output_item_id=job.coated_item_id,
                    accepted_quantity=Decimal("95"),
                    rejected_quantity=Decimal("5"),
                )
            ],
        ),
    )

    coated = await balance(session, tenant, job.coated_item_id, job.warehouse_id)
    assert coated is not None
    assert Decimal(coated.qc_hold_quantity) == 0
    assert Decimal(coated.damaged_quantity) == Decimal("5.0000")
    assert Decimal(coated.available_quantity) == Decimal("95.0000")

    receipt = (
        await session.execute(
            select(JobWorkReceipt).where(JobWorkReceipt.job_work_order_id == order.id)
        )
    ).scalar_one()
    assert receipt.qc_released_at is not None


async def test_a_verdict_that_does_not_account_for_everything_held_is_refused(
    ctx, job: PvdJob
) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, requires_qc=True)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Back from plating",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    with pytest.raises(BusinessRuleError):
        await service.record_qc_result(
            session,
            tenant,
            order.id,
            QcResultRequest(
                reason="Only looked at 60",
                items=[
                    QcVerdictItem(
                        output_item_id=job.coated_item_id, accepted_quantity=Decimal("60")
                    )
                ],
            ),
        )


# -----------------------------------------------------------------------------
# Vendor ledger
# -----------------------------------------------------------------------------


async def test_charges_accrue_on_good_output_and_net_off_the_vendor_debit(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, loss_percentage="2")
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="90 good, 10 lost",
            approve_excess_loss=True,
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("90"), is_final=True)],
        ),
    )

    rows = await service.job_work_charges_for_period(
        session, tenant, date_from=today(), date_to=today()
    )

    assert len(rows) == 1
    assert rows[0].vendor_id == job.vendor_id
    # 90 good pieces at 12.50 - the 10 that never arrived earn nothing.
    assert rows[0].gross_charges == Decimal("1125.0000")
    assert rows[0].vendor_debits == Decimal("800.0000")
    assert rows[0].net_charges == Decimal("325.0000")
    assert rows[0].receipt_count == 1
    assert rows[0].output_quantity == Decimal("90.0000")


async def test_a_period_outside_the_receipt_dates_returns_nothing(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="All back",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    rows = await service.job_work_charges_for_period(
        session,
        tenant,
        date_from=today() - timedelta(days=30),
        date_to=today() - timedelta(days=1),
    )

    assert rows == []


async def test_a_lot_rate_is_charged_once_across_two_deliveries(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job, rate="5000", rate_basis=RateBasis.LOT)

    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="First half",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("50"))],
        ),
    )
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="Second half",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("50"), is_final=True)],
        ),
    )

    report = await service.reconciliation(session, tenant, order.id)
    assert report.total_charges == Decimal("5000.0000")


# -----------------------------------------------------------------------------
# Order entry rules and tenancy
# -----------------------------------------------------------------------------


async def test_an_item_that_is_not_flagged_for_job_work_is_refused(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    blocked = await seed_item(
        session, tenant.company_id, job.uom_id, code=_code("NOJW"), can_job_work=False
    )

    payload = job.payload()
    payload.lines[0].item_id = blocked

    with pytest.raises(BusinessRuleError) as exc:
        await service.create_order(session, tenant, payload)

    assert "not flagged as usable for job work" in str(exc.value)


async def test_an_unapproved_vendor_cannot_be_sent_company_material(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    unapproved = await seed_vendor(session, tenant.company_id, approved=False)

    payload = job.payload()
    payload.vendor_id = unapproved

    with pytest.raises(BusinessRuleError) as exc:
        await service.create_order(session, tenant, payload)

    assert "not approved" in str(exc.value)


async def test_a_batch_tracked_item_is_refused_at_order_entry(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    batched = await seed_item(
        session, tenant.company_id, job.uom_id, code=_code("BATCH"), batch_tracked=True
    )

    payload = job.payload()
    payload.lines[0].item_id = batched

    with pytest.raises(BusinessRuleError):
        await service.create_order(session, tenant, payload)


async def test_only_a_draft_may_be_edited_or_deleted(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    order, _ = await service.create_order(session, tenant, job.payload())
    await service.approve_order(session, tenant, order.id, reason="Rate agreed")

    with pytest.raises(BusinessRuleError):
        await service.delete_draft_order(session, tenant, order.id, reason="Changed my mind")


async def test_every_stock_moving_step_writes_its_own_audit_entry(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)
    await service.receive(
        session,
        tenant,
        principal_for(tenant),
        order.id,
        ReceiveRequest(
            reason="All back",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )

    stmt = select(AuditLog.entity_type, AuditLog.summary).where(
        AuditLog.company_id == tenant.company_id
    )
    rows = list((await session.execute(stmt)).all())
    entity_types = {row.entity_type for row in rows}

    assert {"job_work_order", "job_work_challan", "job_work_receipt"} <= entity_types
    # The status transitions are audited too, not just the documents.
    assert any("ISSUED" in (row.summary or "") for row in rows)


async def test_another_tenants_orders_are_invisible(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    await issued_order(ctx, job)
    other = TenantContext(company_id=uuid.uuid4(), user_id=tenant.user_id)

    rows, _ = await service.list_orders(
        session, other, JobWorkOrderFilters(), PageParams(), SortParams()
    )

    assert rows == []
    assert await service.pending_at_vendor(session, other) == []


async def test_the_order_list_can_be_filtered_to_material_at_a_vendor(ctx, job: PvdJob) -> None:
    session, tenant = ctx
    order, _ = await issued_order(ctx, job)

    rows, pagination = await service.list_orders(
        session,
        tenant,
        JobWorkOrderFilters(at_vendor_only=True, vendor_id=job.vendor_id),
        PageParams(),
        SortParams(),
    )

    assert [row.id for row in rows] == [order.id]
    assert pagination.total == 1
