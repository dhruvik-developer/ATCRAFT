"""Service-level tests for quality control against a real PostgreSQL.

What is under test is the sentence master rules section 21 turns on: deciding an
inspection moves the stock it decided. Every assertion here is about the balance
row and the ledger afterwards - ACCEPTED leaves QC hold for the available pool,
REJECTED for the damaged pool, SCRAP is written off, and REWORK and HOLD
deliberately move nothing at all.

These need PostgreSQL because none of it is expressible without it: the generated
``available_quantity`` column, the per-state balance buckets, the check
constraints that make an unbalanced closed inspection impossible,
``SELECT ... FOR UPDATE`` on the inspection header, the ``num_nonnulls``
constraint on a measurement, the partial unique indexes behind template
resolution, and the audit rows written inside the same transaction as the
movements they describe.

Isolation follows ``test_assembly_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import src.modules.organization.models
import src.modules.users.models  # noqa: F401 - registers `users`
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    InventoryBalance,
    InventoryTransaction,
    StockState,
    TransactionType,
)
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.quality import service
from src.modules.quality.models import (
    InspectionResult,
    InspectionStatus,
    ParameterType,
    QcInspection,
    QcStage,
    SamplingPlan,
)
from src.modules.quality.schemas import (
    CloseRequest,
    DecideRequest,
    InspectionCreate,
    InspectionFilters,
    MeasurementIn,
    RecordResultsRequest,
    TemplateCreate,
    TemplateParameterIn,
    TemplateUpdate,
    TemplateVersionRequest,
)
from src.modules.vendors.models import VendorType
from src.modules.warehouses.models import WarehouseType
from tests import seed_support
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

# These modules own tables this one points at and are built beside it; prefer
# their real definitions and fall back to whatever the metadata already holds.
for _module_path, _table in (
    ("src.modules.vendors.models", "vendors"),
    ("src.modules.warehouses.models", "warehouses"),
    ("src.modules.products.models", "items"),
    ("src.modules.inventory.models", "inventory_balances"),
):
    if _table not in Base.metadata.tables:
        with contextlib.suppress(ModuleNotFoundError):
            importlib.import_module(_module_path)


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with two companies and one inspector.

    The second company exists so tenant isolation can be asserted rather than
    assumed - a scoping rule that is never tested is a claim, not a property.
    """
    schema = f"it_quality_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
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
                # Through the ORM builders, not a hand-written INSERT: this is
                # one of the two files CLAUDE.md names for the column list that
                # went stale twice.
                for identifier in (company_id, other_company_id):
                    await seed_support.seed_company(session, company_id=identifier, prefix="QC")
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

    yield schema, company_id, other_company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext]]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, _other, user_id = _schema
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


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# Every master row below is built through ``tests.seed_support`` - the owning
# module's ORM model, with its Python-side defaults - and only the id is handed
# back, which is all the service calls in this file take. The hand-written
# INSERTs these replaced were the column lists that broke 43 tests twice.


async def seed_uom(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    return (await seed_support.seed_uom(session, company_id=company_id)).id


async def seed_category(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    return (await seed_support.seed_category(session, company_id=company_id, code=_code("CAT"))).id


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    uom_id: uuid.UUID,
    *,
    code: str,
    category_id: uuid.UUID | None = None,
    batch_tracked: bool = False,
) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=code,
        base_uom_id=uom_id,
        item_group=ItemGroup.COMPONENT,
        category_id=category_id,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("10"),
        production_type=ProductionType.BUY,
        can_purchase=True,
        can_scrap=True,
        is_batch_tracked=batch_tracked,
    )
    return item.id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=company_id,
        code=_code("WH"),
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    return warehouse.id


async def seed_vendor(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    vendor = await seed_support.seed_vendor(
        session,
        company_id=company_id,
        code=_code("VEND"),
        vendor_types=(VendorType.RAW_MATERIAL_SUPPLIER,),
    )
    return vendor.id


async def hold_stock(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
) -> None:
    """Put a quantity into the QC hold pool, the way a goods receipt does."""
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
            reason="quality fixture opening stock",
            unit_cost=Decimal("10"),
        )
    async with transaction(session):
        await inv.change_stock_state(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            from_state=StockState.AVAILABLE,
            to_state=StockState.QC_HOLD,
            reason="held pending inspection",
            reference_type="goods_receipt",
            reference_id=None,
            reference_code="GRN/FIXTURE",
        )


async def balance(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> InventoryBalance:
    row = await inv.get_balance(session, tenant, item_id, warehouse_id)
    assert row is not None, "the fixture should have created a balance row"
    await session.refresh(row)
    return row


class Lot:
    """One item, one store, one vendor, with a quantity already on QC hold."""

    def __init__(
        self,
        *,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        vendor_id: uuid.UUID,
        uom_id: uuid.UUID,
        category_id: uuid.UUID,
    ) -> None:
        self.item_id = item_id
        self.warehouse_id = warehouse_id
        self.vendor_id = vendor_id
        self.uom_id = uom_id
        self.category_id = category_id


async def seed_lot(session: AsyncSession, tenant: TenantContext, *, quantity: str = "100") -> Lot:
    uom_id = await seed_uom(session, tenant.company_id)
    category_id = await seed_category(session, tenant.company_id)
    item_id = await seed_item(
        session, tenant.company_id, uom_id, code=_code("RM"), category_id=category_id
    )
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    vendor_id = await seed_vendor(session, tenant.company_id)
    await hold_stock(session, tenant, item_id, warehouse_id, quantity)
    return Lot(
        item_id=item_id,
        warehouse_id=warehouse_id,
        vendor_id=vendor_id,
        uom_id=uom_id,
        category_id=category_id,
    )


async def raise_incoming(
    session: AsyncSession, tenant: TenantContext, lot: Lot, *, quantity: str = "100"
) -> QcInspection:
    async with transaction(session):
        return await service.create_incoming_inspection(
            session,
            tenant,
            item_id=lot.item_id,
            quantity=Decimal(quantity),
            warehouse_id=lot.warehouse_id,
            reference_type="goods_receipt",
            reference_id=uuid.uuid4(),
            reference_code="GRN/2026-27/00012",
            vendor_id=lot.vendor_id,
        )


def numeric_template(
    *, code: str | None = None, critical: bool = False, mandatory: bool = True
) -> TemplateCreate:
    return TemplateCreate(
        code=code or _code("QCT"),
        name="Incoming dimensional check",
        qc_stage=QcStage.INCOMING,
        sampling_plan=SamplingPlan.PERCENTAGE,
        sample_percentage=Decimal("10"),
        parameters=[
            TemplateParameterIn(
                sequence=1,
                parameter_name="Bore diameter",
                parameter_type=ParameterType.NUMERIC,
                unit="mm",
                min_value=Decimal("9.95"),
                max_value=Decimal("10.05"),
                is_critical=critical,
                is_mandatory=mandatory or critical,
            )
        ],
    )


async def audit_actions(
    session: AsyncSession, tenant: TenantContext, entity_id: uuid.UUID
) -> list[AuditAction]:
    rows = (
        (
            await session.execute(
                select(AuditLog.action)
                .where(
                    AuditLog.company_id == tenant.company_id,
                    AuditLog.entity_type == service.AUDIT_QC_INSPECTION,
                    AuditLog.entity_id == entity_id,
                )
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    return [AuditAction(row) for row in rows]


# =============================================================================
# The stock consequence - the reason this module exists
# =============================================================================


class TestDecisionMovesStock:
    async def test_item_without_scrap_capability_can_be_damaged_but_not_scrapped(self, ctx):
        from src.modules.products.models import Item

        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        item = await session.get(Item, lot.item_id)
        item.can_scrap = False
        await session.flush()
        inspection = await raise_incoming(session, tenant, lot)
        inspection_id = inspection.id
        with pytest.raises(BusinessRuleError, match="cannot be scrapped"):
            await service.decide_inspection(
                session,
                tenant,
                inspection_id,
                DecideRequest(
                    scrap_quantity=Decimal("100"),
                    reason="Destructive test loss",
                    rejection_reason="Destroyed during the test",
                ),
            )
        held = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert held.qc_hold_quantity == 100 and held.scrap_quantity == 0
        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection_id,
            DecideRequest(
                rejected_quantity=Decimal("100"),
                reason="Keep damaged material identifiable",
                rejection_reason="Failed inspection",
            ),
        )
        assert outcome.damaged_quantity == 100

    async def test_accepting_releases_the_lot_from_qc_hold(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        held = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert held.qc_hold_quantity == Decimal("100.0000")
        assert held.available_quantity == Decimal("0.0000")

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Conforms to specification"),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("0.0000")
        # The quantity never left the warehouse - it changed pool, and the
        # generated availability column follows from that alone.
        assert after.on_hand_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("100.0000")
        assert outcome.released_quantity == Decimal("100.0000")
        assert outcome.inspection.result is InspectionResult.ACCEPTED
        assert outcome.inspection.status is InspectionStatus.ACCEPTED

    async def test_rejecting_moves_the_lot_to_the_damaged_pool(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                rejected_quantity=Decimal("100"),
                reason="Plating blistered across the lot",
                rejection_reason="Plating blistered; adhesion test failed",
            ),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("0.0000")
        # Damaged stock is still present and still counted - it is simply
        # promisable to nobody until it goes back to the vendor.
        assert after.damaged_quantity == Decimal("100.0000")
        assert after.on_hand_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("0.0000")
        assert outcome.damaged_quantity == Decimal("100.0000")
        # The vendor is owed a return; quality states the requirement and
        # procurement raises the document.
        assert outcome.return_required is True

    async def test_scrapping_writes_the_lot_off(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                scrap_quantity=Decimal("100"),
                reason="Destroyed by the pull test",
                rejection_reason="Destructive test consumed the lot",
            ),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("0.0000")
        assert after.scrap_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("0.0000")
        assert outcome.scrapped_quantity == Decimal("100.0000")

    @pytest.mark.parametrize(
        ("field", "expected_status"),
        [
            ("rework_quantity", InspectionStatus.REWORK),
            ("hold_quantity", InspectionStatus.HOLD),
        ],
    )
    async def test_rework_and_hold_move_no_stock_at_all(
        self,
        ctx: tuple[AsyncSession, TenantContext],
        field: str,
        expected_status: InspectionStatus,
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(**{field: Decimal("100")}, reason="Sent to the bench"),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        # The whole point: the quantity stays exactly where it was, flagged, so
        # it can be decided again once the rework is done.
        assert after.qc_hold_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("0.0000")
        assert after.damaged_quantity == Decimal("0.0000")
        assert outcome.still_on_hold_quantity == Decimal("100.0000")
        assert outcome.inspection.status is expected_status
        assert outcome.released_quantity == Decimal("0")

    async def test_a_mixed_disposition_splits_the_lot_across_every_pool(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("70"),
                rejected_quantity=Decimal("10"),
                rework_quantity=Decimal("12"),
                hold_quantity=Decimal("5"),
                scrap_quantity=Decimal("3"),
                reason="Sorted by defect",
                rejection_reason="10 blistered, 3 destroyed on test",
            ),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.available_quantity == Decimal("70.0000")
        assert after.damaged_quantity == Decimal("10.0000")
        assert after.scrap_quantity == Decimal("3.0000")
        # Rework and hold are the two that stayed put.
        assert after.qc_hold_quantity == Decimal("17.0000")
        # 70 of 100 dominates, so the lot reads as accepted even though four
        # other dispositions were applied - but 17 pieces are still held, so the
        # document itself stays open for the rework bench.
        assert outcome.inspection.result is InspectionResult.ACCEPTED
        assert outcome.inspection.status is InspectionStatus.REWORK
        assert outcome.still_on_hold_quantity == Decimal("17.0000")

    async def test_every_movement_names_the_inspection_that_caused_it(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("60"),
                rejected_quantity=Decimal("40"),
                reason="Sorted",
                rejection_reason="Short thread",
            ),
        )

        rows = (
            (
                await session.execute(
                    select(InventoryTransaction).where(
                        InventoryTransaction.company_id == tenant.company_id,
                        InventoryTransaction.reference_type == service.MOVEMENT_REFERENCE_QC,
                        InventoryTransaction.reference_id == inspection.id,
                    )
                )
            )
            .scalars()
            .all()
        )

        # Two destinations, two ledger rows - the ledger says where each part of
        # the lot went, not merely that something happened.
        assert len(rows) == 2
        assert {row.reference_code for row in rows} == {inspection.code}
        assert all(row.reason for row in rows)
        by_state = {StockState(row.stock_state): row for row in rows}
        # The ledger quantity is signed by the transaction type, not by this
        # module: releasing to AVAILABLE is a QC_RELEASE and adds to on-hand,
        # while moving to DAMAGED is a QC_HOLD-family movement out of the free
        # pool. Both describe the same 60/40 split of one lot.
        assert by_state[StockState.AVAILABLE].quantity == Decimal("60.0000")
        assert abs(by_state[StockState.DAMAGED].quantity) == Decimal("40.0000")


# =============================================================================
# Re-deciding held quantity
# =============================================================================


class TestRedecision:
    async def test_reworked_material_that_passes_releases_only_the_delta(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("60"),
                rework_quantity=Decimal("40"),
                reason="40 need re-polishing",
            ),
        )
        midway = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert midway.available_quantity == Decimal("60.0000")
        assert midway.qc_hold_quantity == Decimal("40.0000")

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Rework passed re-inspection"),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("0.0000")
        # 100 available, not 160: the first 60 were released once, not twice.
        assert after.available_quantity == Decimal("100.0000")
        assert after.on_hand_quantity == Decimal("100.0000")
        assert outcome.released_quantity == Decimal("40.0000")

    async def test_a_partial_rework_leaves_the_remainder_held(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(rework_quantity=Decimal("100"), reason="Whole lot to the bench"),
        )
        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("70"),
                rework_quantity=Decimal("30"),
                reason="70 recovered so far",
            ),
        )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.available_quantity == Decimal("70.0000")
        assert after.qc_hold_quantity == Decimal("30.0000")
        # The verdict on the goods is ACCEPTED - 70 of 100 were good - but the
        # document is NOT finished: 30 pieces are still in the QC hold pool and
        # owe a second decision, so the status stays re-openable. Stamping it
        # ACCEPTED would close the only door those 30 have out of the pool.
        assert outcome.inspection.result is InspectionResult.ACCEPTED
        assert outcome.inspection.status is InspectionStatus.REWORK
        assert outcome.inspection.rework_quantity == Decimal("30.0000")

    async def test_released_quantity_cannot_be_taken_back(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("60"), rework_quantity=Decimal("40"), reason="Sorted"
            ),
        )

        with pytest.raises(BusinessRuleError):
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(
                    accepted_quantity=Decimal("50"),
                    rework_quantity=Decimal("50"),
                    reason="Changed my mind",
                ),
            )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.available_quantity == Decimal("60.0000")
        assert after.qc_hold_quantity == Decimal("40.0000")

    async def test_a_settled_verdict_cannot_be_revisited(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Released"),
        )

        # The stock has left QC hold and may already have been issued; there is
        # nothing to move back.
        with pytest.raises(InvalidStateTransitionError):
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(
                    rejected_quantity=Decimal("100"),
                    reason="Customer complained",
                    rejection_reason="Field failure",
                ),
            )


# =============================================================================
# The balance rule, and what happens when it is broken
# =============================================================================


class TestQuantityBalance:
    async def test_a_disposition_that_does_not_account_for_the_lot_is_refused(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(BusinessRuleError) as caught:
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(accepted_quantity=Decimal("95"), reason="Most of it was fine"),
            )
        assert (caught.value.details or {})["inspected_quantity"] == "100.0000"

        # Nothing moved and nothing was written: the whole decision rolled back.
        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("0.0000")
        refreshed = await session.get(QcInspection, inspection.id)
        assert refreshed is not None
        await session.refresh(refreshed)
        assert refreshed.status is InspectionStatus.PENDING
        assert refreshed.result is InspectionResult.PENDING

    async def test_an_inspection_still_holding_quantity_cannot_be_closed(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("80"), hold_quantity=Decimal("20"), reason="20 queried"
            ),
        )

        with pytest.raises(BusinessRuleError):
            await service.close_inspection(
                session, tenant, inspection.id, reason="Tidying the queue"
            )

    async def test_a_fully_dispositioned_inspection_closes(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("90"),
                rejected_quantity=Decimal("10"),
                reason="Sorted",
                rejection_reason="Ten short-threaded",
            ),
        )
        closed = await service.close_inspection(
            session, tenant, inspection.id, reason="Return raised, file closed"
        )

        assert closed.status is InspectionStatus.CLOSED
        assert closed.closed_at is not None

    async def test_deciding_and_closing_in_one_call_is_one_transaction(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Conforms", close=True),
        )

        assert outcome.inspection.status is InspectionStatus.CLOSED
        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.available_quantity == Decimal("100.0000")


# =============================================================================
# The audit trail - the master rules require it explicitly for QC
# =============================================================================


class TestAudit:
    async def test_a_decision_is_audited_as_a_qc_decision(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("90"),
                rejected_quantity=Decimal("10"),
                reason="Sorted",
                rejection_reason="Ten blistered",
            ),
        )

        actions = await audit_actions(session, tenant, inspection.id)
        assert AuditAction.CREATE in actions
        # Both the status change and the disposition detail are recorded under
        # QC_DECISION, in the same transaction as the movements.
        assert actions.count(AuditAction.QC_DECISION) >= 2

    async def test_the_audit_entry_carries_the_disposition_and_the_reason(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                rejected_quantity=Decimal("100"),
                reason="Whole lot refused",
                rejection_reason="Wrong alloy",
            ),
        )

        row = (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.company_id == tenant.company_id,
                    AuditLog.entity_id == inspection.id,
                    AuditLog.action == AuditAction.QC_DECISION,
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(1)
            )
        ).scalar_one()

        assert row.new_values is not None
        assert row.new_values["rejected_quantity"] == "100.0000"
        assert row.new_values["return_required"] is True
        assert row.context is not None
        assert row.context["reason"] == "Whole lot refused"
        assert row.created_by_id == tenant.user_id

    async def test_a_refused_decision_writes_no_audit_row(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(BusinessRuleError):
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(accepted_quantity=Decimal("40"), reason="Half a decision"),
            )

        assert AuditAction.QC_DECISION not in await audit_actions(session, tenant, inspection.id)


# =============================================================================
# Templates, sampling and measurements
# =============================================================================


class TestTemplatesAndMeasurements:
    async def test_the_sample_size_comes_from_the_resolved_template(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant, quantity="250")

        payload = numeric_template()
        payload.item_id = lot.item_id
        template, _ = await service.create_template(session, tenant, payload)

        inspection = await raise_incoming(session, tenant, lot, quantity="250")

        assert inspection.template_id == template.id
        assert inspection.template_version == 1
        # 10% of 250, rounded up.
        assert inspection.sample_size == Decimal("25.0000")

    async def test_an_item_template_beats_a_category_template(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)

        broad = numeric_template()
        broad.item_category_id = lot.category_id
        broad.sampling_plan = SamplingPlan.ALL
        broad.sample_percentage = None
        await service.create_template(session, tenant, broad)

        narrow = numeric_template()
        narrow.item_id = lot.item_id
        specific, _ = await service.create_template(session, tenant, narrow)

        inspection = await raise_incoming(session, tenant, lot)
        assert inspection.template_id == specific.id
        assert inspection.sample_size == Decimal("10.0000")

    async def test_two_active_templates_cannot_claim_the_same_scope(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)

        first = numeric_template()
        first.item_id = lot.item_id
        await service.create_template(session, tenant, first)

        second = numeric_template()
        second.item_id = lot.item_id
        with pytest.raises(DuplicateError):
            await service.create_template(session, tenant, second)

    async def test_an_item_with_no_template_is_inspected_in_full(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        assert inspection.template_id is None
        # A sample size invented for an unknown specification would be a guess
        # presented as a statistic.
        assert inspection.sample_size == inspection.inspected_quantity

    async def test_recording_a_reading_stores_the_verdict_the_server_computed(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        _, parameters = await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        outcome = await service.record_results(
            session,
            tenant,
            inspection.id,
            RecordResultsRequest(
                measurements=[
                    MeasurementIn(
                        parameter_id=parameters[0].id,
                        measured_value_numeric=Decimal("10.200000"),
                    )
                ]
            ),
        )

        assert outcome.inspection.status is InspectionStatus.IN_PROGRESS
        assert outcome.inspection.results[0].is_within_spec is False
        assert outcome.evaluation.failures == ["Bore diameter"]
        assert outcome.evaluation.recommended_result is InspectionResult.REWORK

    async def test_a_second_reading_corrects_the_first_rather_than_adding_a_row(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        _, parameters = await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        for reading in ("10.200000", "10.000000"):
            await service.record_results(
                session,
                tenant,
                inspection.id,
                RecordResultsRequest(
                    measurements=[
                        MeasurementIn(
                            parameter_id=parameters[0].id,
                            measured_value_numeric=Decimal(reading),
                        )
                    ]
                ),
            )

        detail = await service.get_inspection_detail(session, tenant, inspection.id)
        assert len(detail.results) == 1
        assert detail.results[0].is_within_spec is True

    async def test_a_reading_for_another_templates_parameter_is_refused(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(ValidationError):
            await service.record_results(
                session,
                tenant,
                inspection.id,
                RecordResultsRequest(
                    measurements=[
                        MeasurementIn(
                            parameter_id=uuid.uuid4(), measured_value_numeric=Decimal("10")
                        )
                    ]
                ),
            )

    async def test_a_used_template_is_frozen_and_forks_instead(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        template, _ = await service.create_template(session, tenant, payload)
        await raise_incoming(session, tenant, lot)

        # Presentation still moves freely.
        await service.update_template(
            session, tenant, template.id, TemplateUpdate(name="Incoming check (rev A)")
        )
        # The specification does not: editing it would restate whether the
        # inspection above passed.
        with pytest.raises(BusinessRuleError):
            await service.update_template(
                session,
                tenant,
                template.id,
                TemplateUpdate(sampling_plan=SamplingPlan.ALL),
            )

        forked, parameters = await service.create_template_version(
            session,
            tenant,
            template.id,
            TemplateVersionRequest(reason="Tolerance tightened after field returns"),
        )
        assert forked.version == 2
        assert forked.is_active is True
        assert [p.parameter_name for p in parameters] == ["Bore diameter"]

        await session.refresh(template)
        assert template.is_active is False

    async def test_a_new_inspection_picks_up_the_new_version(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        template, _ = await service.create_template(session, tenant, payload)
        await raise_incoming(session, tenant, lot)

        forked, _ = await service.create_template_version(
            session,
            tenant,
            template.id,
            TemplateVersionRequest(
                reason="Sample every piece from now on", sampling_plan=SamplingPlan.ALL
            ),
        )
        later = await raise_incoming(session, tenant, lot)

        assert later.template_id == forked.id
        assert later.template_version == 2
        assert later.sample_size == later.inspected_quantity


# =============================================================================
# The critical override, end to end
# =============================================================================


class TestCriticalParameter:
    async def test_a_critical_failure_blocks_acceptance_and_leaves_the_stock_held(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template(critical=True)
        payload.item_id = lot.item_id
        _, parameters = await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        await service.record_results(
            session,
            tenant,
            inspection.id,
            RecordResultsRequest(
                measurements=[
                    MeasurementIn(
                        parameter_id=parameters[0].id, measured_value_numeric=Decimal("11")
                    )
                ]
            ),
        )

        with pytest.raises(BusinessRuleError) as caught:
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(accepted_quantity=Decimal("100"), reason="Looks fine to me"),
            )
        assert "Bore diameter" in str(caught.value)

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("100.0000")
        assert after.available_quantity == Decimal("0.0000")

    async def test_a_critical_failure_forces_the_result_to_rejected(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template(critical=True)
        payload.item_id = lot.item_id
        _, parameters = await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        await service.record_results(
            session,
            tenant,
            inspection.id,
            RecordResultsRequest(
                measurements=[
                    MeasurementIn(
                        parameter_id=parameters[0].id, measured_value_numeric=Decimal("11")
                    )
                ]
            ),
        )
        outcome = await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                # Most of the lot is scrapped, so the dominant bucket would
                # otherwise read SCRAP. The critical failure settles it anyway.
                rejected_quantity=Decimal("30"),
                scrap_quantity=Decimal("70"),
                reason="Critical dimension out",
                rejection_reason="Bore 11.0 against 9.95-10.05",
            ),
        )

        assert outcome.evaluation.has_critical_failure is True
        assert outcome.inspection.result is InspectionResult.REJECTED
        assert outcome.inspection.status is InspectionStatus.REJECTED

    async def test_an_outstanding_mandatory_reading_blocks_the_decision(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        payload = numeric_template()
        payload.item_id = lot.item_id
        await service.create_template(session, tenant, payload)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(BusinessRuleError) as caught:
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(accepted_quantity=Decimal("100"), reason="Nobody measured it"),
            )
        assert "Bore diameter" in str(caught.value)


# =============================================================================
# Raising inspections
# =============================================================================


class TestRaisingInspections:
    async def test_the_in_process_entry_point_derives_its_stage_from_the_caller(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)

        async with transaction(session):
            inspection = await service.create_in_process_inspection(
                session,
                tenant,
                item_id=lot.item_id,
                quantity=Decimal("100"),
                warehouse_id=lot.warehouse_id,
                reference_type="assembly_order",
                reference_id=uuid.uuid4(),
                reference_code="AO/2026-27/00003",
            )

        assert inspection.qc_stage is QcStage.ASSEMBLY
        assert inspection.status is InspectionStatus.PENDING
        assert inspection.vendor_id is None

    async def test_raising_an_inspection_does_not_hold_the_stock_a_second_time(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        before = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert before.qc_hold_quantity == Decimal("100.0000")

        await raise_incoming(session, tenant, lot)

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        # The receipt already held it; holding it again would take the quantity
        # out of availability twice.
        assert after.qc_hold_quantity == Decimal("100.0000")
        assert after.on_hand_quantity == Decimal("100.0000")

    async def test_a_manual_inspection_can_pull_stock_off_the_shelf(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        uom_id = await seed_uom(session, tenant.company_id)
        item_id = await seed_item(session, tenant.company_id, uom_id, code=_code("FG"))
        warehouse_id = await seed_warehouse(session, tenant.company_id)
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("40"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="available stock",
                unit_cost=Decimal("10"),
            )

        inspection = await service.create_inspection(
            session,
            tenant,
            InspectionCreate(
                item_id=item_id,
                warehouse_id=warehouse_id,
                qc_stage=QcStage.FINAL,
                inspected_quantity=Decimal("15"),
                hold_stock=True,
                reason="Pre-dispatch audit of a finished lot",
            ),
        )

        after = await balance(session, tenant, item_id, warehouse_id)
        assert after.qc_hold_quantity == Decimal("15.0000")
        assert after.available_quantity == Decimal("25.0000")
        assert inspection.qc_stage is QcStage.FINAL

    async def test_a_batch_tracked_item_must_name_its_lot(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        uom_id = await seed_uom(session, tenant.company_id)
        item_id = await seed_item(
            session, tenant.company_id, uom_id, code=_code("BT"), batch_tracked=True
        )
        warehouse_id = await seed_warehouse(session, tenant.company_id)

        # Refused at creation rather than at decision time: an inspection whose
        # decision could never move the quantity would strand the stock.
        with pytest.raises(BusinessRuleError):
            async with transaction(session):
                await service.create_incoming_inspection(
                    session,
                    tenant,
                    item_id=item_id,
                    quantity=Decimal("10"),
                    warehouse_id=warehouse_id,
                    reference_type="goods_receipt",
                    reference_id=uuid.uuid4(),
                    reference_code="GRN/2026-27/00099",
                )

    async def test_each_inspection_takes_the_next_number_in_the_series(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant, quantity="30")
        codes = [(await raise_incoming(session, tenant, lot, quantity="10")).code for _ in range(3)]
        assert len(set(codes)) == 3
        assert all(code.startswith("QC/") for code in codes)


# =============================================================================
# Reads, reports and tenant isolation
# =============================================================================


class TestReadsAndReports:
    async def test_the_pending_queue_holds_only_undecided_inspections(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant, quantity="200")
        waiting = await raise_incoming(session, tenant, lot, quantity="100")
        decided = await raise_incoming(session, tenant, lot, quantity="100")
        await service.decide_inspection(
            session,
            tenant,
            decided.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Released"),
        )

        rows, pagination = await service.list_pending(session, tenant, PageParams())

        ids = {row.id for row in rows}
        assert waiting.id in ids
        assert decided.id not in ids
        assert pagination.total == len(rows)

    async def test_the_rework_filter_finds_flagged_quantity(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)
        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(rework_quantity=Decimal("100"), reason="To the bench"),
        )

        rows, _ = await service.list_inspections(
            session, tenant, InspectionFilters(rework_only=True), PageParams(), SortParams()
        )
        assert [row.id for row in rows] == [inspection.id]

    async def test_item_quality_history_reports_the_rejection_rate(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant, quantity="300")
        for accepted, rejected in (("90", "10"), ("80", "20")):
            inspection = await raise_incoming(session, tenant, lot, quantity="100")
            await service.decide_inspection(
                session,
                tenant,
                inspection.id,
                DecideRequest(
                    accepted_quantity=Decimal(accepted),
                    rejected_quantity=Decimal(rejected),
                    reason="Sorted",
                    rejection_reason="Blistering",
                ),
            )
        # A third inspection is left pending: it has no verdict, so it must not
        # drag the rate towards zero.
        await raise_incoming(session, tenant, lot, quantity="100")

        history = await service.item_quality_history(session, tenant, lot.item_id)

        assert history.inspection_count == 2
        assert history.inspected_quantity == Decimal("200.0000")
        assert history.rejected_quantity == Decimal("30.0000")
        assert history.rejection_rate == Decimal("15.0000")
        assert len(history.periods) == 1
        assert history.periods[0].rejection_rate == Decimal("15.0000")

    async def test_vendor_quality_summary_breaks_the_rate_down_by_item(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        first = await seed_lot(session, tenant, quantity="100")
        second_item = await seed_item(session, tenant.company_id, first.uom_id, code=_code("RM2"))
        await hold_stock(session, tenant, second_item, first.warehouse_id, "100")

        good = await raise_incoming(session, tenant, first)
        await service.decide_inspection(
            session,
            tenant,
            good.id,
            DecideRequest(accepted_quantity=Decimal("100"), reason="Conforms"),
        )

        async with transaction(session):
            bad = await service.create_incoming_inspection(
                session,
                tenant,
                item_id=second_item,
                quantity=Decimal("100"),
                warehouse_id=first.warehouse_id,
                reference_type="goods_receipt",
                reference_id=uuid.uuid4(),
                reference_code="GRN/2026-27/00013",
                vendor_id=first.vendor_id,
            )
        await service.decide_inspection(
            session,
            tenant,
            bad.id,
            DecideRequest(
                accepted_quantity=Decimal("50"),
                rejected_quantity=Decimal("30"),
                scrap_quantity=Decimal("20"),
                reason="Half the lot was wrong",
                rejection_reason="Wrong finish",
            ),
        )

        summary = await service.vendor_quality_summary(session, tenant, first.vendor_id)

        assert summary.has_evidence is True
        assert summary.inspected_quantity == Decimal("200.0000")
        # Scrap counts as a rejection: 50 refused of 200.
        assert summary.rejection_rate == Decimal("25.0000")
        assert summary.items_truncated is False
        by_item = {row.item_id: row for row in summary.items}
        assert by_item[first.item_id].rejection_rate == Decimal("0.0000")
        assert by_item[second_item].rejection_rate == Decimal("50.0000")

    async def test_a_vendor_with_no_inspections_reports_no_evidence(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        vendor_id = await seed_vendor(session, tenant.company_id)

        summary = await service.vendor_quality_summary(session, tenant, vendor_id)

        # Distinguishable from a perfect record, which is what the rating engine
        # needs: never rejected and never delivered are not the same vendor.
        assert summary.has_evidence is False
        assert summary.rejection_rate == Decimal("0")
        assert summary.items == []

    async def test_an_over_long_reporting_window_is_refused_not_truncated(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        with pytest.raises(ValidationError):
            await service.item_quality_history(session, tenant, uuid.uuid4(), window_days=5_000)

    async def test_the_columns_the_vendor_rating_engine_reads_are_populated(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)
        await service.decide_inspection(
            session,
            tenant,
            inspection.id,
            DecideRequest(
                accepted_quantity=Decimal("95"),
                rejected_quantity=Decimal("5"),
                reason="Sorted",
                rejection_reason="Five short",
            ),
        )

        # The exact aggregate vendors.service.load_performance runs.
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(QcInspection.inspected_quantity), 0),
                    func.coalesce(func.sum(QcInspection.rejected_quantity), 0),
                ).where(
                    QcInspection.company_id == tenant.company_id,
                    QcInspection.vendor_id == lot.vendor_id,
                    QcInspection.inspected_at.is_not(None),
                )
            )
        ).one()
        assert Decimal(str(row[0])) == Decimal("100.0000")
        assert Decimal(str(row[1])) == Decimal("5.0000")


class TestTenantIsolation:
    async def test_another_companys_inspection_is_not_visible(
        self,
        ctx: tuple[AsyncSession, TenantContext],
        _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        session, tenant = ctx
        _, _, other_company_id, _user = _schema
        other = TenantContext(company_id=other_company_id, user_id=tenant.user_id)

        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(NotFoundError):
            await service.get_inspection_detail(session, other, inspection.id)

        rows, _ = await service.list_inspections(
            session, other, InspectionFilters(), PageParams(), SortParams()
        )
        assert inspection.id not in {row.id for row in rows}

    async def test_another_companys_inspection_cannot_be_decided(
        self,
        ctx: tuple[AsyncSession, TenantContext],
        _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
    ) -> None:
        session, tenant = ctx
        _, _, other_company_id, _user = _schema
        other = TenantContext(company_id=other_company_id, user_id=tenant.user_id)

        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(NotFoundError):
            await service.decide_inspection(
                session,
                other,
                inspection.id,
                DecideRequest(accepted_quantity=Decimal("100"), reason="Not mine to decide"),
            )

        after = await balance(session, tenant, lot.item_id, lot.warehouse_id)
        assert after.qc_hold_quantity == Decimal("100.0000")


class TestCloseGuards:
    async def test_an_undecided_inspection_cannot_be_closed(
        self, ctx: tuple[AsyncSession, TenantContext]
    ) -> None:
        session, tenant = ctx
        lot = await seed_lot(session, tenant)
        inspection = await raise_incoming(session, tenant, lot)

        with pytest.raises(BusinessRuleError):
            await service.close_inspection(
                session, tenant, inspection.id, reason=CloseRequest(reason="tidy up").reason
            )
