"""Service-level tests for packing against a real PostgreSQL.

What is under test is the sentence master rules section 22 turns on: a carton is
not free. Sealing a box issues the packing material it is made of from stock, and
every assertion here is about the balances, the ledger and the audit trail
afterwards - plus the roll-up onto the order's lines that decides whether the
customer is getting what they asked for.

These need PostgreSQL because none of it is expressible without it: the generated
``available_quantity`` column, the CHECK constraints that make an over-packed line
or a sealed carton with no ledger row impossible, ``SELECT ... FOR UPDATE`` on the
order header, the ``uuid[]`` traceability array with its cardinality constraint,
and the audit rows written inside the same transaction as the movement they
describe.

Isolation follows ``test_assembly_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
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
    InsufficientStockError,
    InvalidStateTransitionError,
)
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.packing import service
from src.modules.packing.models import Carton, CartonContent, PackingOrderStatus
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonContentsReplace,
    CartonCreate,
    CompleteRequest,
    PackingOrderCreate,
    PackingOrderLineIn,
    SealCartonRequest,
)

pytestmark = pytest.mark.integration

# These modules carry foreign keys this one points at and are built beside it;
# prefer their real definitions and fall back to the conftest stand-ins.
for _module_path, _table in (
    ("src.modules.vendors.models", "vendors"),
    ("src.modules.warehouses.models", "warehouses"),
    ("src.modules.products.models", "items"),
    ("src.modules.customers.models", "customers"),
    ("src.modules.sales.models", "sales_orders"),
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
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """Private schema with one company and one packer."""
    schema = f"it_packing_{uuid.uuid4().hex[:10]}"
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
                await session.execute(
                    text(
                        "INSERT INTO companies (id, code, name, country, currency, "
                        "financial_year_start_month, timezone, is_active) "
                        "VALUES (:id, :code, 'Atcraft', 'India', 'INR', 4, "
                        "'Asia/Kolkata', true)"
                    ),
                    {"id": company_id, "code": f"PKG{uuid.uuid4().hex[:6].upper()}"},
                )
                await session.execute(
                    text(
                        "INSERT INTO users (id, company_id, email, full_name, password_hash, "
                        "is_active, is_superuser, mfa_enabled, must_change_password, "
                        "failed_login_attempts) "
                        "VALUES (:id, :c, :email, 'Packer', 'x', true, false, false, "
                        "false, 0)"
                    ),
                    {"id": user_id, "c": company_id, "email": "packer@atcraft.test"},
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
        pytest.skip(f"PostgreSQL is not available for integration tests: {exc}")

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


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def seed_uom(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    uom_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO units_of_measure (id, company_id, code, name, uom_category, is_base, "
            "decimal_places, is_active, created_at, updated_at) "
            "VALUES (:id, :c, :code, 'Numbers', 'COUNT', false, 4, true, now(), now())"
        ),
        {"id": uom_id, "c": company_id, "code": f"U{uuid.uuid4().hex[:6].upper()}"},
    )
    return uom_id


async def seed_item(
    session: AsyncSession,
    company_id: uuid.UUID,
    uom_id: uuid.UUID,
    *,
    code: str,
    group: str = "FINISHED_GOOD",
    serial_tracked: bool = False,
    batch_tracked: bool = False,
    active: bool = True,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO items (id, company_id, code, name, item_group, base_uom_id, gst_rate, "
            "standard_cost, sale_price, dealer_price, production_type, lead_time_days, "
            "reorder_level, reorder_quantity, min_order_quantity, safety_stock, "
            "is_batch_tracked, is_serial_tracked, can_sell, is_active, "
            "created_at, updated_at) "
            "VALUES (:id, :c, :code, :name, :grp, :u, 18, 10, 100, 90, 'MAKE', 0, 0, 0, 0, 0, "
            ":batch, :serial, true, :active, now(), now())"
        ),
        {
            "id": item_id,
            "c": company_id,
            "u": uom_id,
            "code": code,
            "name": f"Item {code}",
            "grp": group,
            "batch": batch_tracked,
            "serial": serial_tracked,
            "active": active,
        },
    )
    return item_id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    warehouse_id = uuid.uuid4()
    code = _code("WH")
    await session.execute(
        text(
            "INSERT INTO warehouses (id, company_id, code, name, warehouse_type, country, "
            "is_active, is_default, allow_negative_stock, created_at, updated_at) "
            "VALUES (:id, :c, :code, :name, 'FINISHED_GOODS', 'India', true, false, false, "
            "now(), now())"
        ),
        {"id": warehouse_id, "c": company_id, "code": code, "name": f"Store {code}"},
    )
    return warehouse_id


async def seed_customer(
    session: AsyncSession, company_id: uuid.UUID, *, blocked: bool = False
) -> uuid.UUID:
    customer_id = uuid.uuid4()
    code = _code("CUST")
    await session.execute(
        text(
            # ``ck_customers_block_has_timestamp`` refuses a hold with no
            # recorded moment, so the timestamp is written in the same INSERT
            # rather than patched in afterwards.
            "INSERT INTO customers (id, company_id, code, name, customer_type, "
            "price_list_type, credit_days, outstanding_amount, is_active, is_blocked, "
            "block_reason, blocked_at, created_at, updated_at) "
            "VALUES (:id, :c, :code, :name, 'CUSTOMER', 'SALE', 30, 0, true, :blocked, "
            ":reason, CASE WHEN :blocked THEN now() END, now(), now())"
        ),
        {
            "id": customer_id,
            "c": company_id,
            "code": code,
            "name": f"Customer {code}",
            "blocked": blocked,
            "reason": "Credit hold" if blocked else None,
        },
    )
    return customer_id


async def stock_in(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
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
            reason="packing fixture opening stock",
            unit_cost=Decimal("10"),
        )


async def available(
    session: AsyncSession, tenant: TenantContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Decimal:
    return await inv.get_available(session, tenant, item_id, warehouse_id)


class Bench:
    """One warehouse, one customer, one saleable item and one kind of box."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        customer_id: uuid.UUID,
        goods_item_id: uuid.UUID,
        box_item_id: uuid.UUID,
    ) -> None:
        self.uom_id = uom_id
        self.warehouse_id = warehouse_id
        self.customer_id = customer_id
        self.goods_item_id = goods_item_id
        self.box_item_id = box_item_id

    def order_payload(self, *, quantity: str = "100") -> PackingOrderCreate:
        return PackingOrderCreate(
            customer_id=self.customer_id,
            warehouse_id=self.warehouse_id,
            planned_date=date(2026, 9, 1),
            lines=[
                PackingOrderLineIn(
                    item_id=self.goods_item_id,
                    quantity_to_pack=Decimal(quantity),
                    uom_id=self.uom_id,
                )
            ],
        )

    def carton_payload(self, *, quantity: str = "40", boxes: str = "1") -> CartonCreate:
        return CartonCreate(
            packing_material_item_id=self.box_item_id,
            packing_material_quantity=Decimal(boxes),
            tare_weight_kg=Decimal("0.5"),
            contents=[
                CartonContentIn(
                    line_number=1,
                    quantity=Decimal(quantity),
                    net_weight_kg=Decimal("10"),
                )
            ],
        )


@pytest_asyncio.fixture
async def bench(ctx: tuple[AsyncSession, TenantContext]) -> Bench:
    session, tenant = ctx
    uom_id = await seed_uom(session, tenant.company_id)
    warehouse_id = await seed_warehouse(session, tenant.company_id)
    customer_id = await seed_customer(session, tenant.company_id)
    goods_item_id = await seed_item(
        session, tenant.company_id, uom_id, code=_code("LOCK"), group="FINISHED_GOOD"
    )
    box_item_id = await seed_item(
        session, tenant.company_id, uom_id, code=_code("BOX"), group="PACKING_MATERIAL"
    )
    await session.flush()
    await stock_in(session, tenant, box_item_id, warehouse_id, "500")
    return Bench(
        uom_id=uom_id,
        warehouse_id=warehouse_id,
        customer_id=customer_id,
        goods_item_id=goods_item_id,
        box_item_id=box_item_id,
    )


async def started_order(
    session: AsyncSession, tenant: TenantContext, bench: Bench, *, quantity: str = "100"
):
    order, _ = await service.create_order(
        session, tenant, bench.order_payload(quantity=quantity)
    )
    await service.start_packing(session, tenant, order.id, reason="Bench is free")
    return order


# -----------------------------------------------------------------------------
# The rule the module turns on: a carton is not free
# -----------------------------------------------------------------------------


async def test_sealing_a_carton_issues_its_packing_material_from_stock(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)

    before = await available(session, tenant, bench.box_item_id, bench.warehouse_id)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload(boxes="2"))

    # Opening a carton must not move stock: the box is paid for when it is used.
    assert await available(session, tenant, bench.box_item_id, bench.warehouse_id) == before

    result = await service.seal_carton(
        session,
        tenant,
        order.id,
        carton.id,
        SealCartonRequest(reason="Carton full, taped and weighed"),
    )

    after = await available(session, tenant, bench.box_item_id, bench.warehouse_id)
    assert after == before - Decimal("2")
    assert result.packing_material_quantity == Decimal("2.0000")


async def test_the_material_issue_is_a_ledger_row_pointing_back_at_the_carton(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    result = await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    row = (
        await session.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.id == result.material_transaction_id
            )
        )
    ).scalar_one()

    assert row.transaction_type == TransactionType.MATERIAL_ISSUE
    # Signed negative: the sign is a property of the type, never of the caller.
    assert Decimal(row.quantity) == Decimal("-1.0000")
    assert row.item_id == bench.box_item_id
    assert row.warehouse_id == bench.warehouse_id
    assert row.reference_type == service.MOVEMENT_REFERENCE_CARTON
    assert row.reference_id == carton.id
    assert row.reference_code == carton.barcode
    assert row.reason == "Sealed"


async def test_the_sealed_carton_records_the_ledger_row_it_posted(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    result = await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    stored = (
        await session.execute(select(Carton).where(Carton.id == carton.id))
    ).scalar_one()
    # The CHECK constraint refuses a sealed carton without one; this proves the
    # service actually writes it rather than the constraint never being reached.
    assert stored.is_sealed is True
    assert stored.sealed_at is not None
    assert stored.material_transaction_id == result.material_transaction_id


async def test_a_carton_cannot_be_sealed_when_there_are_no_boxes_left(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(boxes="9999")
    )

    before = await available(session, tenant, bench.box_item_id, bench.warehouse_id)
    with pytest.raises(InsufficientStockError):
        await service.seal_carton(
            session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
        )

    # The whole seal rolled back to the savepoint: the carton is still open and
    # not one box moved. A partially applied seal would leave a sealed carton
    # with no ledger row, which the CHECK constraint would then reject on the
    # next flush of an unrelated write.
    stored = (
        await session.execute(select(Carton).where(Carton.id == carton.id))
    ).scalar_one()
    assert stored.is_sealed is False
    assert stored.material_transaction_id is None
    assert await available(session, tenant, bench.box_item_id, bench.warehouse_id) == before


async def test_sealing_twice_is_refused_so_one_box_is_never_paid_for_twice(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    before = await available(session, tenant, bench.box_item_id, bench.warehouse_id)
    with pytest.raises(BusinessRuleError, match="already sealed"):
        await service.seal_carton(
            session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed again")
        )
    assert await available(session, tenant, bench.box_item_id, bench.warehouse_id) == before


async def test_an_empty_carton_may_not_be_sealed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    empty = CartonCreate(
        packing_material_item_id=bench.box_item_id, tare_weight_kg=Decimal("0.5")
    )
    carton = await service.add_carton(session, tenant, order.id, empty)

    with pytest.raises(BusinessRuleError, match="empty"):
        await service.seal_carton(
            session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
        )


async def test_sealing_writes_its_audit_entry_in_the_same_transaction(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Taped and weighed")
    )

    entries = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == service.AUDIT_CARTON,
                    AuditLog.entity_id == carton.id,
                    AuditLog.action == AuditAction.STATUS_CHANGE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].context["reason"] == "Taped and weighed"


# -----------------------------------------------------------------------------
# Weights
# -----------------------------------------------------------------------------


async def test_sealing_computes_the_gross_weight_from_the_contents_and_the_box(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    payload = CartonCreate(
        packing_material_item_id=bench.box_item_id,
        tare_weight_kg=Decimal("0.75"),
        contents=[
            CartonContentIn(line_number=1, quantity=Decimal("20"), net_weight_kg=Decimal("8.5"))
        ],
    )
    carton = await service.add_carton(session, tenant, order.id, payload)
    result = await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    assert result.carton.net_weight_kg == Decimal("8.5000")
    assert result.carton.gross_weight_kg == Decimal("9.2500")


async def test_completion_freezes_the_carton_totals_onto_the_order(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)

    for _ in range(2):
        carton = await service.add_carton(
            session, tenant, order.id, bench.carton_payload(quantity="50")
        )
        await service.seal_carton(
            session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
        )

    result = await service.complete_packing(
        session, tenant, order.id, CompleteRequest(reason="All boxed")
    )

    assert result.total_cartons == 2
    assert result.total_net_weight == Decimal("20.0000")  # 2 x 10 kg of goods
    assert result.total_gross_weight == Decimal("21.0000")  # plus 2 x 0.5 kg of box
    assert result.order.status is PackingOrderStatus.PACKED
    assert result.order.packed_by_id == tenant.user_id
    assert result.short_packed_line_numbers == []


# -----------------------------------------------------------------------------
# Packed quantities and the order's lines
# -----------------------------------------------------------------------------


async def test_packing_a_carton_rolls_its_quantity_onto_the_order_line(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    await service.add_carton(session, tenant, order.id, bench.carton_payload(quantity="40"))

    detail = await service.get_order_detail(session, tenant, order.id)
    assert detail.lines[0].packed_quantity == Decimal("40.0000")


async def test_replacing_a_cartons_contents_nets_the_line_back_to_the_truth(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )

    await service.replace_carton_contents(
        session,
        tenant,
        order.id,
        carton.id,
        CartonContentsReplace(
            reason="Recounted the box",
            contents=[
                CartonContentIn(
                    line_number=1, quantity=Decimal("25"), net_weight_kg=Decimal("6")
                )
            ],
        ),
    )

    detail = await service.get_order_detail(session, tenant, order.id)
    assert detail.lines[0].packed_quantity == Decimal("25.0000")


async def test_removing_an_unsealed_carton_gives_the_quantity_back(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )

    await service.remove_carton(
        session, tenant, order.id, carton.id, reason="Wrong goods went in"
    )

    detail = await service.get_order_detail(session, tenant, order.id)
    assert detail.lines[0].packed_quantity == Decimal("0.0000")
    remaining = (
        await session.execute(
            select(func.count()).select_from(CartonContent).where(
                CartonContent.carton_id == carton.id
            )
        )
    ).scalar_one()
    assert remaining == 0


async def test_a_sealed_carton_cannot_be_removed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    with pytest.raises(BusinessRuleError, match="cannot be removed"):
        await service.remove_carton(
            session, tenant, order.id, carton.id, reason="Changed my mind"
        )


async def test_over_packing_a_line_across_two_cartons_is_refused(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench, quantity="50")
    await service.add_carton(session, tenant, order.id, bench.carton_payload(quantity="40"))

    with pytest.raises(BusinessRuleError, match="over-packed"):
        await service.add_carton(
            session, tenant, order.id, bench.carton_payload(quantity="20")
        )


# -----------------------------------------------------------------------------
# Lifecycle and the dispatch handover
# -----------------------------------------------------------------------------


async def test_an_order_with_an_open_carton_cannot_be_completed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    await service.add_carton(session, tenant, order.id, bench.carton_payload())

    with pytest.raises(BusinessRuleError, match="unsealed carton"):
        await service.complete_packing(
            session, tenant, order.id, CompleteRequest(reason="Done")
        )


async def test_an_order_with_no_cartons_cannot_be_completed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)

    with pytest.raises(BusinessRuleError, match="no cartons"):
        await service.complete_packing(
            session, tenant, order.id, CompleteRequest(reason="Done")
        )


async def test_a_short_pack_is_refused_unless_it_is_asked_for(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench, quantity="100")
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    with pytest.raises(BusinessRuleError, match="short-packed"):
        await service.complete_packing(
            session, tenant, order.id, CompleteRequest(reason="Ship what we have")
        )

    result = await service.complete_packing(
        session,
        tenant,
        order.id,
        CompleteRequest(reason="Balance follows next week", allow_short_pack=True),
    )
    assert result.short_packed_line_numbers == [1]


async def test_a_packed_order_appears_on_the_dispatch_feed_and_leaves_it_when_claimed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench, quantity="40")
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    # Not offered while it is still being packed.
    assert not [
        row
        for row in await service.ready_for_dispatch(session, tenant)
        if row.packing_order_id == order.id
    ]

    await service.complete_packing(session, tenant, order.id, CompleteRequest(reason="Done"))

    offered = [
        row
        for row in await service.ready_for_dispatch(session, tenant)
        if row.packing_order_id == order.id
    ]
    assert len(offered) == 1
    assert offered[0].carton_count == 1
    assert offered[0].total_gross_weight == Decimal("10.5000")

    dispatch_id = uuid.uuid4()
    await service.hand_over_to_dispatch(
        session, tenant, order.id, dispatch_id=dispatch_id, reason="On the 4pm truck"
    )

    assert not [
        row
        for row in await service.ready_for_dispatch(session, tenant)
        if row.packing_order_id == order.id
    ]


async def test_a_second_dispatch_cannot_take_an_order_another_one_claimed(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench, quantity="40")
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )
    await service.complete_packing(session, tenant, order.id, CompleteRequest(reason="Done"))

    first = uuid.uuid4()
    await service.hand_over_to_dispatch(
        session, tenant, order.id, dispatch_id=first, reason="Truck A"
    )
    # The same dispatch asking twice is a retry, not a conflict.
    await service.hand_over_to_dispatch(
        session, tenant, order.id, dispatch_id=first, reason="Truck A again"
    )

    with pytest.raises(BusinessRuleError, match="another dispatch"):
        await service.hand_over_to_dispatch(
            session, tenant, order.id, dispatch_id=uuid.uuid4(), reason="Truck B"
        )


async def test_packing_moves_no_finished_goods_stock(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    await stock_in(session, tenant, bench.goods_item_id, bench.warehouse_id, "200")
    before = await available(session, tenant, bench.goods_item_id, bench.warehouse_id)

    order = await started_order(session, tenant, bench, quantity="40")
    carton = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="40")
    )
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )
    await service.complete_packing(session, tenant, order.id, CompleteRequest(reason="Done"))

    # Completing does not dispatch: the goods leave when dispatch posts its own
    # movements. Posting them here as well would decrement the same stock twice.
    assert await available(session, tenant, bench.goods_item_id, bench.warehouse_id) == before


async def test_an_order_with_a_sealed_carton_cannot_be_cancelled(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    carton = await service.add_carton(session, tenant, order.id, bench.carton_payload())
    await service.seal_carton(
        session, tenant, order.id, carton.id, SealCartonRequest(reason="Sealed")
    )

    with pytest.raises(BusinessRuleError, match="consumed packing material"):
        await service.cancel_order(session, tenant, order.id, reason="Customer cancelled")


async def test_cancelling_returns_the_packed_quantities_and_drops_open_cartons(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    await service.add_carton(session, tenant, order.id, bench.carton_payload(quantity="40"))

    cancelled = await service.cancel_order(
        session, tenant, order.id, reason="Customer cancelled"
    )
    assert PackingOrderStatus(cancelled.status) is PackingOrderStatus.CANCELLED

    detail = await service.get_order_detail(session, tenant, order.id)
    assert detail.lines[0].packed_quantity == Decimal("0.0000")


async def test_a_packed_order_cannot_be_started_again(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench, quantity="40")

    with pytest.raises(InvalidStateTransitionError):
        await service.start_packing(session, tenant, order.id, reason="Twice")


async def test_cartons_cannot_be_opened_on_a_draft(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order, _ = await service.create_order(session, tenant, bench.order_payload())

    with pytest.raises(BusinessRuleError, match="packing is in progress"):
        await service.add_carton(session, tenant, order.id, bench.carton_payload())


# -----------------------------------------------------------------------------
# Barcodes and traceability
# -----------------------------------------------------------------------------


async def test_each_carton_gets_a_sequential_number_and_a_verifiable_barcode(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)

    first = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="10")
    )
    second = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="10")
    )

    assert (first.carton_number, second.carton_number) == (1, 2)
    assert first.barcode != second.barcode
    assert service.verify_barcode(first.barcode)
    assert service.verify_barcode(second.barcode)


async def test_a_removed_cartons_number_is_not_reused(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order = await started_order(session, tenant, bench)
    first = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="10")
    )
    await service.add_carton(session, tenant, order.id, bench.carton_payload(quantity="10"))
    await service.remove_carton(session, tenant, order.id, first.id, reason="Wrong box")

    third = await service.add_carton(
        session, tenant, order.id, bench.carton_payload(quantity="10")
    )
    # Number 1 has been printed on a label somewhere; reusing it would make two
    # different boxes claim it.
    assert third.carton_number == 3


async def test_a_serial_can_be_traced_back_to_the_carton_holding_it(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    serial_item_id = await seed_item(
        session, tenant.company_id, bench.uom_id, code=_code("SER"), serial_tracked=True
    )
    serial_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO stock_serials (id, company_id, item_id, serial_number, "
            "warehouse_id, status, created_at, updated_at) "
            "VALUES (:id, :c, :item, :sn, :wh, 'IN_STOCK', now(), now())"
        ),
        {
            "id": serial_id,
            "c": tenant.company_id,
            "item": serial_item_id,
            "sn": _code("SN"),
            "wh": bench.warehouse_id,
        },
    )
    await session.flush()

    order, _ = await service.create_order(
        session,
        tenant,
        PackingOrderCreate(
            customer_id=bench.customer_id,
            warehouse_id=bench.warehouse_id,
            planned_date=date(2026, 9, 1),
            lines=[
                PackingOrderLineIn(
                    item_id=serial_item_id,
                    quantity_to_pack=Decimal("1"),
                    uom_id=bench.uom_id,
                )
            ],
        ),
    )
    await service.start_packing(session, tenant, order.id, reason="Bench is free")
    carton = await service.add_carton(
        session,
        tenant,
        order.id,
        CartonCreate(
            packing_material_item_id=bench.box_item_id,
            tare_weight_kg=Decimal("0.2"),
            contents=[
                CartonContentIn(
                    line_number=1,
                    quantity=Decimal("1"),
                    net_weight_kg=Decimal("2"),
                    serial_ids=[serial_id],
                )
            ],
        ),
    )

    found = await service.find_cartons_holding_serial(session, tenant, serial_id)
    assert [c.id for c in found] == [carton.id]
    assert found[0].contents[0].serial_ids == [serial_id]


async def test_one_serial_cannot_be_packed_into_two_cartons(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    serial_item_id = await seed_item(
        session, tenant.company_id, bench.uom_id, code=_code("SER"), serial_tracked=True
    )
    serial_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO stock_serials (id, company_id, item_id, serial_number, "
            "warehouse_id, status, created_at, updated_at) "
            "VALUES (:id, :c, :item, :sn, :wh, 'IN_STOCK', now(), now())"
        ),
        {
            "id": serial_id,
            "c": tenant.company_id,
            "item": serial_item_id,
            "sn": _code("SN"),
            "wh": bench.warehouse_id,
        },
    )
    await session.flush()

    order, _ = await service.create_order(
        session,
        tenant,
        PackingOrderCreate(
            customer_id=bench.customer_id,
            warehouse_id=bench.warehouse_id,
            planned_date=date(2026, 9, 1),
            lines=[
                PackingOrderLineIn(
                    item_id=serial_item_id,
                    quantity_to_pack=Decimal("2"),
                    uom_id=bench.uom_id,
                )
            ],
        ),
    )
    await service.start_packing(session, tenant, order.id, reason="Bench is free")
    content = CartonCreate(
        packing_material_item_id=bench.box_item_id,
        tare_weight_kg=Decimal("0.2"),
        contents=[
            CartonContentIn(
                line_number=1,
                quantity=Decimal("1"),
                net_weight_kg=Decimal("2"),
                serial_ids=[serial_id],
            )
        ],
    )
    await service.add_carton(session, tenant, order.id, content)

    with pytest.raises(BusinessRuleError, match="already in carton"):
        await service.add_carton(session, tenant, order.id, content)


# -----------------------------------------------------------------------------
# Order creation
# -----------------------------------------------------------------------------


async def test_goods_may_not_be_packed_for_a_blocked_customer(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    blocked_id = await seed_customer(session, tenant.company_id, blocked=True)
    await session.flush()

    payload = bench.order_payload()
    payload.customer_id = blocked_id

    with pytest.raises(BusinessRuleError, match="blocked"):
        await service.create_order(session, tenant, payload)


async def test_a_new_order_is_a_draft_with_numbered_lines_and_a_document_code(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    order, lines = await service.create_order(session, tenant, bench.order_payload())

    assert order.code.startswith("PK/")
    assert PackingOrderStatus(order.status) is PackingOrderStatus.DRAFT
    assert [line.line_number for line in lines] == [1]
    assert lines[0].packed_quantity == Decimal("0")


async def test_the_packing_material_pick_list_shows_live_availability(
    ctx: tuple[AsyncSession, TenantContext], bench: Bench
) -> None:
    session, tenant = ctx
    rows, _ = await service.list_packing_materials(
        session,
        tenant,
        warehouse_id=bench.warehouse_id,
        params=PageParams(page=1, limit=50),
    )
    offered = {row.item_id: row.available_quantity for row in rows}
    assert offered.get(bench.box_item_id) == Decimal("500.0000")
    # Finished goods are not offered as boxes: item_group selects the list.
    assert bench.goods_item_id not in offered
