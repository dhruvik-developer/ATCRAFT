"""A private procurement schema, its people and masters, and the order builders.

``_schema`` builds a throwaway PostgreSQL schema once per module - two companies,
a buyer and an approver - and ``db`` hands each test a session in it whose writes
roll back. ``tenant``, ``other_tenant``, ``buyer``, ``approver`` and
``buyer_without_approval`` are the callers, and ``masters`` a store, a unit, an
approved vendor and a plain, a batch-tracked and a serialised item. A module that
wants the schema imports every fixture it asks for, each as ``name as name``.

The builders take whatever session and callers they are handed, so the HTTP
suites drive them over the conftest database as well: ``make_order``,
``send_order`` and ``receive`` take a purchase order from draft to a posted
receipt, ``make_requisition`` and ``make_rfq`` raise the other two documents, and
``manual_payload``, ``manual_resolution``, ``approved_request``, ``linked_order``
and ``fresh`` stage a duplicate-order decision.

They lived in ``test_procurement_service.py`` and
``test_purchase_order_changes.py``, and the suites around those imported them
from there; they live here so no test file is another's library.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.models import Base
from src.core.permissions import Permission
from src.modules.organization.models import Company
from src.modules.procurement import service
from src.modules.procurement.models import (
    PurchaseOrderStatus,
    PurchaseSourceType,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderCreate,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderLineIn,
    RequisitionCreate,
    RequisitionLineIn,
    RfqCreate,
    RfqLineIn,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.users.models import User
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests.schema_support import prepare_metadata, report_database_setup_failure


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


async def make_requisition(
    db: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters,
    *,
    quantity: str = "10",
    unit_price: str | None = None,
):
    payload = RequisitionCreate(
        required_date=today() + timedelta(days=14),
        warehouse_id=masters["warehouse"].id,
        lines=[
            RequisitionLineIn(
                line_number=1,
                item_id=masters["item"].id,
                quantity=Decimal(quantity),
                estimated_unit_price=None if unit_price is None else Decimal(unit_price),
            )
        ],
    )
    return await service.create_requisition(db, tenant, principal, payload)


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


def manual_payload(masters: dict[str, Any], quantity: str = "25", **overrides: Any):
    values = {
        "vendor_id": masters["vendor"].id,
        "warehouse_id": masters["warehouse"].id,
        "expected_delivery_date": today() + timedelta(days=10),
        "lines": [
            PurchaseOrderLineIn(
                line_number=1,
                item_id=masters["item"].id,
                quantity=Decimal(quantity),
                unit_price=Decimal("50"),
            )
        ],
    }
    values.update(overrides)
    return PurchaseOrderCreate.model_validate(values)


def manual_resolution(order, draft, *, action="MERGE", line_id=None, version=None):
    return PurchaseOrderDuplicateResolutionRequest(
        action=action,
        candidate_purchase_order_id=order.id,
        candidate_line_id=line_id,
        expected_candidate_version=order.version if version is None else version,
        reason="Additional approved demand",
        draft={"kind": "MANUAL", "purchase_order": draft},
    )


async def fresh(db, model, identifier):
    return (
        await db.execute(
            select(model).where(model.id == identifier).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def approved_request(db, tenant, buyer, approver, masters, *quantities):
    payload = RequisitionCreate(
        required_date=today() + timedelta(days=10),
        warehouse_id=masters["warehouse"].id,
        lines=[
            RequisitionLineIn(
                line_number=number,
                item_id=masters["item"].id,
                quantity=Decimal(quantity),
                suggested_vendor_id=masters["vendor"].id,
                estimated_unit_price=Decimal("50"),
            )
            for number, quantity in enumerate(quantities or ("20",), 1)
        ],
    )
    header, lines = await service.create_requisition(db, tenant, buyer, payload)
    for status in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await service.transition_requisition(
            db, tenant, approver, header.id, status, reason="Demand approved"
        )
    return header, lines


async def linked_order(db, tenant, buyer, masters, allocations, *, quantity=None):
    total = sum((Decimal(value) for _, value in allocations), Decimal("0"))
    payload = manual_payload(
        masters,
        lines=[
            PurchaseOrderLineIn(
                line_number=1,
                item_id=masters["item"].id,
                uom_id=masters["uom"].id,
                quantity=Decimal(quantity) if quantity else total,
                unit_price=Decimal("50"),
                source_allocations=[
                    {"requisition_line_id": identifier, "quantity": value}
                    for identifier, value in allocations
                ],
            )
        ],
    )
    return await service.create_purchase_order(db, tenant, buyer, payload)


__all__ = [
    "_principal",
    "_schema",
    "_schema_engine",
    "approved_request",
    "approver",
    "buyer",
    "buyer_without_approval",
    "db",
    "fresh",
    "linked_order",
    "make_order",
    "make_requisition",
    "make_rfq",
    "manual_payload",
    "manual_resolution",
    "masters",
    "other_tenant",
    "receive",
    "send_order",
    "tenant",
]
