"""Requisition to purchase orders, the category-wise split, against a real PostgreSQL.

What only a database can prove: the locks taken in order, the one transaction
that either raises every order or none, the ordered quantity pushed back onto the
requisition lines, the requisition moving to CONVERTED, the audit rows written
beside the orders, the duplicate refusal seeing orders raised a moment earlier,
and the tenant and vendor checks that a UUID foreign key cannot make.

Isolation follows the module-scoped schema pattern from
``test_procurement_service.py``: one throwaway schema, built once, with each
test's writes rolled back. Master data is built through the ORM.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.organization.models import Company, Department
from src.modules.procurement import service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseRequisitionLine,
    PurchaseSourceType,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderFilters,
    PurchaseOrderLineIn,
    PurchaseOrderUpdate,
    RequisitionConversionLineOverride,
    RequisitionConversionRequest,
    RequisitionCreate,
    RequisitionFilters,
    RequisitionLineIn,
    RequisitionUpdate,
)
from src.modules.procurement.service_rules import (
    RequisitionPriceSource,
    RequisitionVendorSource,
    VendorAssigneeReason,
)
from src.modules.products.models import Item, ItemGroup, ItemVendorLink, ProductionType
from src.modules.users.models import Role, User
from src.modules.vendors.models import Vendor, VendorCategory, VendorType
from tests import seed_support
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

    schema = f"it_conv_{uuid.uuid4().hex[:10]}"
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
                            full_name=handle.title(),
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


# -----------------------------------------------------------------------------
# The world: three categories, two vendors, three vendor managers
# -----------------------------------------------------------------------------


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


async def _approved_vendor(db: AsyncSession, tenant: TenantContext, name: str) -> Vendor:
    return await seed_support.seed_vendor(
        db,
        company_id=tenant.company_id,
        code=f"V-{_suffix()}",
        name=name,
        vendor_types=(VendorType.COMPONENT_SUPPLIER,),
        payment_terms_days=30,
        lead_time_days=7,
        currency="INR",
        is_approved=True,
        # The database refuses an approved vendor with no approval timestamp.
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
    )


@pytest_asyncio.fixture
async def world(db: AsyncSession, tenant: TenantContext) -> dict[str, Any]:
    """The proposal's own example, built through the ORM.

    * Categories: LOCKS > LOCK_BODY, HANDLES > LEVER, and PACKING.
    * ``precision`` is qualified for LOCKS (the parent - so a lock body is
      bought under LOCKS) and LEVER; ``kraft`` for PACKING.
    * Each item's preferred vendor link names its vendor, with a last price.
    * Precision has a LOCKS manager, a LEVER manager and an unrestricted
      owner; Kraft has a PACKING manager. All hold ``purchase_order.update``.
    """
    company_id = tenant.company_id
    uom = await seed_support.seed_uom(db, company_id=company_id)
    warehouse = await seed_support.seed_warehouse(db, company_id=company_id, code=f"WA{_suffix()}")
    second_warehouse = await seed_support.seed_warehouse(
        db, company_id=company_id, code=f"WB{_suffix()}"
    )

    locks = await seed_support.seed_category(db, company_id=company_id, code=f"LOCKS-{_suffix()}")
    lock_body = await seed_support.seed_category(
        db, company_id=company_id, code=f"LOCKBODY-{_suffix()}", parent=locks
    )
    handles = await seed_support.seed_category(
        db, company_id=company_id, code=f"HANDLES-{_suffix()}"
    )
    lever = await seed_support.seed_category(
        db, company_id=company_id, code=f"LEVER-{_suffix()}", parent=handles
    )
    packing = await seed_support.seed_category(
        db, company_id=company_id, code=f"PACKING-{_suffix()}"
    )

    precision = await _approved_vendor(db, tenant, "Precision Fasteners")
    kraft = await _approved_vendor(db, tenant, "Kraft Pack")
    db.add_all(
        [
            VendorCategory(
                company_id=company_id, vendor_id=precision.id, item_category_id=locks.id
            ),
            VendorCategory(
                company_id=company_id, vendor_id=precision.id, item_category_id=lever.id
            ),
            VendorCategory(company_id=company_id, vendor_id=kraft.id, item_category_id=packing.id),
        ]
    )
    await db.flush()

    async def item(code: str, category_id: uuid.UUID | None, **extra: Any) -> Item:
        return await seed_support.seed_item(
            db,
            company_id=company_id,
            code=f"{code}-{_suffix()}",
            base_uom_id=uom.id,
            purchase_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            category_id=category_id,
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
            production_type=ProductionType.BUY,
            can_purchase=True,
            is_active=True,
            **extra,
        )

    lock_body_item = await item("LB", lock_body.id)
    lever_item = await item("LH", lever.id)
    box_item = await item("BOX", packing.id)
    orphan_item = await item("ORPHAN", None)

    for linked, vendor, price in (
        (lock_body_item, precision, "182.00"),
        (lever_item, precision, "96.00"),
        (box_item, kraft, "14.20"),
    ):
        db.add(
            ItemVendorLink(
                company_id=company_id,
                item_id=linked.id,
                vendor_id=vendor.id,
                is_preferred=True,
                lead_time_days=7,
                last_purchase_price=Decimal(price),
                currency="INR",
            )
        )
    await db.flush()

    vendor_role = Role(
        company_id=company_id,
        code=f"VMGR_{_suffix()}",
        name="Vendor Manager",
        permissions=[
            str(Permission.PURCHASE_ORDER_VIEW),
            str(Permission.PURCHASE_ORDER_UPDATE),
            # Vendor assignees act through the portal: lane C moved the
            # assignee right from PURCHASE_ORDER_UPDATE to VENDOR_PORTAL_RESPOND.
            str(Permission.VENDOR_PORTAL_RESPOND),
        ],
        is_system=False,
        is_vendor_role=True,
        is_active=True,
    )
    db.add(vendor_role)
    await db.flush()

    async def manager(vendor: Vendor, name: str, *scope: uuid.UUID) -> User:
        # The role goes in through the constructor: appending to ``roles`` on a
        # flushed row would be a lazy load, which an async session refuses.
        return await seed_support.seed_user(
            db,
            company_id=company_id,
            full_name=name,
            vendor_id=vendor.id,
            allowed_category_ids=list(scope) or None,
            is_active=True,
            roles=[vendor_role],
        )

    return {
        "uom": uom,
        "warehouse": warehouse,
        "second_warehouse": second_warehouse,
        "locks": locks,
        "lock_body": lock_body,
        "handles": handles,
        "lever": lever,
        "packing": packing,
        "precision": precision,
        "kraft": kraft,
        "lock_body_item": lock_body_item,
        "lever_item": lever_item,
        "box_item": box_item,
        "orphan_item": orphan_item,
        "locks_manager": await manager(precision, "Manager One", locks.id),
        "lever_manager": await manager(precision, "Manager Two", lever.id),
        "owner": await manager(precision, "Precision Owner"),
        "packing_manager": await manager(kraft, "Manager Three", packing.id),
        "vendor_role": vendor_role,
    }


async def approved_requisition(
    db: AsyncSession,
    tenant: TenantContext,
    buyer: Principal,
    approver: Principal,
    *,
    warehouse_id: uuid.UUID,
    lines: list[RequisitionLineIn],
    required_in: int = 14,
    priority: int = 5,
):
    requisition, requisition_lines = await service.create_requisition(
        db,
        tenant,
        buyer,
        RequisitionCreate(
            required_date=today() + timedelta(days=required_in),
            warehouse_id=warehouse_id,
            priority=priority,
            lines=lines,
        ),
    )
    await service.transition_requisition(
        db, tenant, buyer, requisition.id, RequisitionStatus.PENDING_APPROVAL, reason="submit"
    )
    await service.transition_requisition(
        db, tenant, approver, requisition.id, RequisitionStatus.APPROVED, reason="approve"
    )
    return requisition, requisition_lines


def line(number: int, item: Item, quantity: str, price: str | None = None, **extra: Any):
    return RequisitionLineIn(
        line_number=number,
        item_id=item.id,
        quantity=Decimal(quantity),
        estimated_unit_price=Decimal(price) if price is not None else None,
        **extra,
    )


async def three_category_requisition(db, tenant, buyer, approver, world):
    """100 lock bodies, 200 handles and 500 boxes - the proposal's example."""
    return await approved_requisition(
        db,
        tenant,
        buyer,
        approver,
        warehouse_id=world["warehouse"].id,
        lines=[
            line(1, world["lock_body_item"], "100", "180.00"),
            line(2, world["lever_item"], "200", "95.00"),
            line(3, world["box_item"], "500", "14.50"),
        ],
    )


async def order_count(db: AsyncSession, tenant: TenantContext) -> int:
    stmt = (
        select(func.count())
        .select_from(PurchaseOrder)
        .where(PurchaseOrder.company_id == tenant.company_id)
    )
    return int((await db.execute(stmt)).scalar_one())


async def send(db, tenant, buyer, approver, order_id, **kwargs: Any) -> PurchaseOrder:
    await service.transition_purchase_order(
        db, tenant, buyer, order_id, PurchaseOrderStatus.PENDING_APPROVAL, reason="submit"
    )
    await service.transition_purchase_order(
        db, tenant, approver, order_id, PurchaseOrderStatus.APPROVED, reason="approve"
    )
    return await service.transition_purchase_order(
        db, tenant, buyer, order_id, PurchaseOrderStatus.SENT, reason="send", **kwargs
    )


# =============================================================================
# The split
# =============================================================================


class TestTheSplit:
    async def test_a_three_category_requisition_becomes_three_draft_orders(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id

        result = await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
        )

        assert result.converted
        assert not result.unresolved_lines
        assert len(result.groups) == 3
        by_category = {group.item_category_id: group for group in result.groups}
        # The lock body is bought under LOCKS - the nearest category Precision
        # is qualified for - not under its own LOCK_BODY.
        assert set(by_category) == {world["locks"].id, world["lever"].id, world["packing"].id}

        locks = by_category[world["locks"].id]
        assert locks.vendor_id == world["precision"].id
        assert locks.assigned_vendor_user_id == world["locks_manager"].id
        assert locks.assignee_reason is VendorAssigneeReason.CATEGORY_MATCH
        assert locks.assigned_vendor_user_name == "Manager One"
        assert by_category[world["lever"].id].assigned_vendor_user_id == world["lever_manager"].id
        assert (
            by_category[world["packing"].id].assigned_vendor_user_id == world["packing_manager"].id
        )

        orders = (
            (
                await db.execute(
                    select(PurchaseOrder).where(
                        PurchaseOrder.id.in_([g.purchase_order_id for g in result.groups])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(orders) == 3
        for order in orders:
            assert PurchaseOrderStatus(order.status) is PurchaseOrderStatus.DRAFT
            assert PurchaseSourceType(order.source_type) is PurchaseSourceType.PURCHASE_REQUISITION
            assert order.source_id == requisition_id
            group = next(g for g in result.groups if g.purchase_order_id == order.id)
            assert order.item_category_id == group.item_category_id
            assert order.assigned_vendor_user_id == group.assigned_vendor_user_id
            assert order.code == group.purchase_order_code

    async def test_ordered_quantity_rises_and_the_requisition_is_converted(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id

        result = await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
        )

        assert result.requisitions[0].status is RequisitionStatus.CONVERTED
        detail = await service.get_requisition_detail(db, tenant, requisition_id)
        assert detail.status is RequisitionStatus.CONVERTED
        for requisition_line in detail.lines:
            assert requisition_line.ordered_quantity == requisition_line.quantity
            assert str(requisition_line.status) == "ORDERED"

    async def test_the_lines_carry_their_sources_and_the_requisition_line_link(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, requisition_lines = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[
                # No estimate: priced from Precision's last purchase price.
                line(1, world["lock_body_item"], "10"),
                # A suggested vendor wins over the item's preferred one.
                line(2, world["box_item"], "20", "15.00", suggested_vendor_id=world["kraft"].id),
            ],
        )
        result = await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition.id], RequisitionConversionRequest()
        )
        lines = {line.item_id: line for group in result.groups for line in group.lines}
        lock_line = lines[world["lock_body_item"].id]
        assert lock_line.unit_price == Decimal("182.00")
        assert lock_line.price_source is RequisitionPriceSource.LAST_PURCHASE
        assert lock_line.vendor_source is RequisitionVendorSource.ITEM_PREFERRED
        box_line = lines[world["box_item"].id]
        assert box_line.price_source is RequisitionPriceSource.ESTIMATE
        assert box_line.vendor_source is RequisitionVendorSource.SUGGESTED

        order_lines = (
            (
                await db.execute(
                    select(PurchaseOrderLine).where(
                        PurchaseOrderLine.purchase_order_id.in_(
                            [g.purchase_order_id for g in result.groups]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {ol.requisition_line_id for ol in order_lines} == {rl.id for rl in requisition_lines}

    async def test_the_category_preferred_vendor_is_the_last_resort(
        self, db, tenant, buyer, approver, world
    ) -> None:
        # An item with no link of its own, in a category whose PARENT names a
        # preferred vendor: the nearest ancestor that has one is used.
        handle_spare = await seed_support.seed_item(
            db,
            company_id=tenant.company_id,
            code=f"HS-{_suffix()}",
            base_uom_id=world["uom"].id,
            item_group=ItemGroup.COMPONENT,
            category_id=world["lever"].id,
            gst_rate=Decimal("18"),
            can_purchase=True,
            is_active=True,
        )
        db.add(
            VendorCategory(
                company_id=tenant.company_id,
                vendor_id=world["precision"].id,
                item_category_id=world["handles"].id,
                is_preferred=True,
            )
        )
        await db.flush()
        requisition, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[line(1, handle_spare, "5", "40.00")],
        )
        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition.id], RequisitionConversionRequest()
        )
        group = preview.groups[0]
        assert group.vendor_id == world["precision"].id
        assert group.lines[0].vendor_source is RequisitionVendorSource.CATEGORY_PREFERRED
        # Precision is qualified for LEVER itself, the item's own category.
        assert group.item_category_id == world["lever"].id

    async def test_the_audit_trail_names_the_split_on_the_requisition(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        result = await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
        )

        rows = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_id == requisition_id,
                        AuditLog.action == AuditAction.UPDATE,
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        split = [row for row in rows if row.summary.startswith("Split into")]
        assert len(split) == 1
        for group in result.groups:
            assert group.purchase_order_code in split[0].summary
        context_orders = split[0].context["purchase_orders"]
        assert {entry["purchase_order_code"] for entry in context_orders} == {
            group.purchase_order_code for group in result.groups
        }
        creates = (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.entity_id.in_([g.purchase_order_id for g in result.groups]),
                    AuditLog.action == AuditAction.CREATE,
                )
            )
        ).scalar_one()
        assert creates == 3


class TestOverrides:
    async def test_a_partial_quantity_a_price_and_an_exclusion(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        lock_line, _, box_line = lines

        result = await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [requisition_id],
            RequisitionConversionRequest(
                lines=[
                    RequisitionConversionLineOverride(
                        requisition_line_id=lock_line.id,
                        quantity=Decimal("40"),
                        unit_price=Decimal("170"),
                    ),
                    RequisitionConversionLineOverride(
                        requisition_line_id=box_line.id, exclude=True
                    ),
                ]
            ),
        )

        assert len(result.groups) == 2
        assert result.skipped_line_count == 1
        converted = {line.item_id: line for group in result.groups for line in group.lines}
        assert converted[world["lock_body_item"].id].quantity == Decimal("40")
        assert converted[world["lock_body_item"].id].unit_price == Decimal("170")
        assert converted[world["lock_body_item"].id].price_source is RequisitionPriceSource.OVERRIDE
        assert world["box_item"].id not in converted

        detail = await service.get_requisition_detail(db, tenant, requisition_id)
        by_number = {line.line_number: line for line in detail.lines}
        assert by_number[1].ordered_quantity == Decimal("40")
        assert str(by_number[1].status) == "PARTIALLY_ORDERED"
        assert by_number[3].ordered_quantity == Decimal("0")
        assert detail.status is RequisitionStatus.CONVERTED

    async def test_a_vendor_override_moves_the_line_to_another_order(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        preview = await service.preview_requisition_conversion(
            db,
            tenant,
            [requisition.id],
            RequisitionConversionRequest(
                lines=[
                    RequisitionConversionLineOverride(
                        requisition_line_id=lines[0].id, vendor_id=world["kraft"].id
                    )
                ]
            ),
        )
        kraft_groups = [g for g in preview.groups if g.vendor_id == world["kraft"].id]
        # Kraft is not qualified for anything above a lock body, so the line is
        # bought under the item's own category, in its own order.
        assert {g.item_category_id for g in kraft_groups} == {
            world["packing"].id,
            world["lock_body"].id,
        }
        moved = next(g for g in kraft_groups if g.item_category_id == world["lock_body"].id)
        assert moved.lines[0].vendor_source is RequisitionVendorSource.OVERRIDE
        # Kraft has no LOCK_BODY manager and no unrestricted user.
        assert moved.assigned_vendor_user_id is None
        assert moved.assignee_reason is VendorAssigneeReason.NO_ELIGIBLE_USER

    async def test_an_override_naming_a_line_of_another_requisition_is_refused(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        with pytest.raises(ValidationError):
            await service.preview_requisition_conversion(
                db,
                tenant,
                [requisition.id],
                RequisitionConversionRequest(
                    lines=[RequisitionConversionLineOverride(requisition_line_id=uuid.uuid4())]
                ),
            )


class TestRefusals:
    async def test_an_unresolved_line_refuses_everything_and_writes_nothing(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[
                line(1, world["lock_body_item"], "10", "180"),
                # No vendor anywhere and no price.
                line(2, world["orphan_item"], "5"),
            ],
        )
        requisition_id = requisition.id
        before = await order_count(db, tenant)

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition_id], RequisitionConversionRequest()
        )
        assert len(preview.unresolved_lines) == 1
        unresolved = preview.unresolved_lines[0]
        assert unresolved.item_id == world["orphan_item"].id
        assert len(unresolved.reasons) == 2  # no vendor, no price
        assert len(preview.groups) == 1

        with pytest.raises(BusinessRuleError) as refused:
            await service.convert_requisitions_to_orders(
                db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
            )
        assert refused.value.details["unresolved_lines"][0]["line_number"] == 2

        assert await order_count(db, tenant) == before
        detail = await service.get_requisition_detail(db, tenant, requisition_id)
        assert detail.status is RequisitionStatus.APPROVED
        assert all(line.ordered_quantity == 0 for line in detail.lines)

    async def test_a_vendor_that_is_not_approved_is_an_unresolved_line(
        self, db, tenant, buyer, approver, world
    ) -> None:
        pending = await seed_support.seed_vendor(
            db, company_id=tenant.company_id, code=f"VP-{_suffix()}", is_active=True
        )
        requisition, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[line(1, world["box_item"], "5", "15", suggested_vendor_id=pending.id)],
        )
        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition.id], RequisitionConversionRequest()
        )
        assert preview.unresolved_lines[0].vendor_id == pending.id
        assert "not approved" in preview.unresolved_lines[0].reasons[0]

    async def test_an_item_deactivated_since_approval_is_a_line_to_leave_out(
        self, db, tenant, buyer, approver, world
    ) -> None:
        # Approved while the lever handle was still bought in, deactivated since.
        # The proposal must still render - the other orders, plus this one line
        # with its reason - or the buyer has nothing on screen to leave out, and
        # the requisition can never be converted at all.
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        lever_line_id = lines[1].id
        lever_code = world["lever_item"].code
        world["lever_item"].is_active = False
        await db.flush()

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition_id], RequisitionConversionRequest()
        )
        assert len(preview.groups) == 2
        (unresolved,) = preview.unresolved_lines
        assert unresolved.requisition_line_id == lever_line_id
        assert unresolved.item_code == lever_code
        assert any("inactive" in reason for reason in unresolved.reasons)

        with pytest.raises(BusinessRuleError) as refused:
            await service.convert_requisitions_to_orders(
                db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
            )
        # Plain ids from here on: the refusal rolled its savepoint back.
        assert refused.value.details["unresolved_lines"][0]["requisition_line_id"] == str(
            lever_line_id
        )

        result = await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [requisition_id],
            RequisitionConversionRequest(
                lines=[
                    RequisitionConversionLineOverride(
                        requisition_line_id=lever_line_id, exclude=True
                    )
                ]
            ),
        )
        assert len(result.groups) == 2
        assert result.unresolved_lines == []
        detail = await service.get_requisition_detail(db, tenant, requisition_id)
        assert detail.status is RequisitionStatus.CONVERTED
        left_open = next(row for row in detail.lines if row.id == lever_line_id)
        assert left_open.ordered_quantity == 0

    async def test_an_item_retired_since_approval_is_still_named(
        self, db, tenant, buyer, approver, world
    ) -> None:
        # A retired item is absent from the purchasable-item read; the line must
        # still say which item it was, not fail the proposal with a bare 404.
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        box_line_id = lines[2].id
        box_code = world["box_item"].code
        world["box_item"].deleted_at = utcnow()
        await db.flush()

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition.id], RequisitionConversionRequest()
        )
        assert len(preview.groups) == 2
        (unresolved,) = preview.unresolved_lines
        assert unresolved.requisition_line_id == box_line_id
        assert unresolved.item_code == box_code
        assert any("retired" in reason for reason in unresolved.reasons)

    async def test_a_requisition_that_is_not_approved_is_refused(
        self, db, tenant, buyer, world
    ) -> None:
        draft, _ = await service.create_requisition(
            db,
            tenant,
            buyer,
            RequisitionCreate(
                required_date=today() + timedelta(days=7),
                warehouse_id=world["warehouse"].id,
                lines=[line(1, world["box_item"], "5", "15")],
            ),
        )
        with pytest.raises(BusinessRuleError):
            await service.convert_requisitions_to_orders(
                db, tenant, buyer, [draft.id], RequisitionConversionRequest()
            )

    async def test_another_tenants_requisition_is_not_found(
        self, db, tenant, other_tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        with pytest.raises(NotFoundError):
            await service.preview_requisition_conversion(
                db, other_tenant, [requisition.id], RequisitionConversionRequest()
            )

    async def test_an_unknown_override_vendor_is_not_found(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        with pytest.raises(NotFoundError):
            await service.convert_requisitions_to_orders(
                db,
                tenant,
                buyer,
                [requisition.id],
                RequisitionConversionRequest(
                    lines=[
                        RequisitionConversionLineOverride(
                            requisition_line_id=lines[0].id, vendor_id=uuid.uuid4()
                        )
                    ]
                ),
            )

    async def test_a_second_conversion_sees_what_the_first_ordered(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
        )
        before = await order_count(db, tenant)

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition_id], RequisitionConversionRequest()
        )
        assert preview.groups == []
        assert preview.skipped_line_count == 3
        with pytest.raises(BusinessRuleError) as refused:
            await service.convert_requisitions_to_orders(
                db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
            )
        assert "Nothing is left to order" in refused.value.message
        assert await order_count(db, tenant) == before


class TestDuplicates:
    async def test_an_open_order_for_the_same_demand_is_refused_with_409(
        self, db, tenant, buyer, approver, world
    ) -> None:
        # Already on order: lock bodies from Precision into the same store.
        await service.create_purchase_order(
            db,
            tenant,
            buyer,
            PurchaseOrderCreate(
                vendor_id=world["precision"].id,
                warehouse_id=world["warehouse"].id,
                expected_delivery_date=today() + timedelta(days=14),
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=world["lock_body_item"].id,
                        quantity=Decimal("50"),
                        unit_price=Decimal("180"),
                    )
                ],
            ),
        )
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        before = await order_count(db, tenant)

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition_id], RequisitionConversionRequest()
        )
        assert preview.requires_acknowledgement
        locks = next(g for g in preview.groups if g.item_category_id == world["locks"].id)
        assert len(locks.duplicate_candidates) == 1

        with pytest.raises(BusinessRuleError) as refused:
            await service.convert_requisitions_to_orders(
                db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
            )
        assert refused.value.code is ErrorCode.DUPLICATE_DOCUMENT
        assert refused.value.status_code == 409
        # All or nothing: the two orders that had no duplicate were not raised.
        assert await order_count(db, tenant) == before

        acknowledged = await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [requisition_id],
            RequisitionConversionRequest(
                acknowledge_duplicates=True,
                duplicate_acknowledgement_reason="Second build lot; both are needed.",
            ),
        )
        assert len(acknowledged.groups) == 3
        order = (
            await db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.id
                    == next(
                        g.purchase_order_id
                        for g in acknowledged.groups
                        if g.item_category_id == world["locks"].id
                    )
                )
            )
        ).scalar_one()
        assert order.duplicate_acknowledged
        assert order.duplicate_acknowledgement_reason == "Second build lot; both are needed."

    async def test_acknowledging_needs_the_approve_right(
        self, db, tenant, approver, world, _schema
    ) -> None:
        _, company_id, _, buyer_id, _ = _schema
        clerk = _principal(
            company_id,
            buyer_id,
            Permission.PURCHASE_ORDER_CREATE,
            Permission.PURCHASE_REQUISITION_CREATE,
        )
        await service.create_purchase_order(
            db,
            tenant,
            approver,
            PurchaseOrderCreate(
                vendor_id=world["kraft"].id,
                warehouse_id=world["warehouse"].id,
                expected_delivery_date=today() + timedelta(days=14),
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=world["box_item"].id,
                        quantity=Decimal("50"),
                        unit_price=Decimal("14"),
                    )
                ],
            ),
        )
        requisition, _ = await approved_requisition(
            db,
            tenant,
            clerk,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[line(1, world["box_item"], "10", "14.5")],
        )
        with pytest.raises(PermissionDeniedError):
            await service.convert_requisitions_to_orders(
                db,
                tenant,
                clerk,
                [requisition.id],
                RequisitionConversionRequest(
                    acknowledge_duplicates=True, duplicate_acknowledgement_reason="Needed twice"
                ),
            )

    async def test_the_same_pending_requisition_line_is_an_exact_duplicate(
        self, db, tenant, buyer, approver, world
    ) -> None:
        """A partial conversion followed by a second one: the open order already
        claims that requisition line, which is EXACT whatever the dates say."""
        requisition, lines = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[line(1, world["lock_body_item"], "100", "180")],
        )
        requisition_id = requisition.id
        await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [requisition_id],
            RequisitionConversionRequest(
                lines=[
                    RequisitionConversionLineOverride(
                        requisition_line_id=lines[0].id, quantity=Decimal("40")
                    )
                ]
            ),
        )
        preview = await service.preview_requisition_conversion(
            db,
            tenant,
            [requisition_id],
            RequisitionConversionRequest(expected_delivery_date=today() + timedelta(days=90)),
        )
        candidates = preview.groups[0].duplicate_candidates
        assert [c.confidence for c in candidates] == ["EXACT"]
        assert preview.groups[0].lines[0].quantity == Decimal("60")


class TestBatch:
    async def test_several_requisitions_across_two_warehouses(
        self, db, tenant, buyer, approver, world
    ) -> None:
        first, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[
                line(1, world["lock_body_item"], "10", "180"),
                line(2, world["box_item"], "100", "14.5"),
            ],
        )
        second, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["second_warehouse"].id,
            lines=[line(1, world["lock_body_item"], "20", "180")],
        )
        third, _ = await approved_requisition(
            db,
            tenant,
            buyer,
            approver,
            warehouse_id=world["warehouse"].id,
            lines=[line(1, world["lock_body_item"], "5", "181")],
            priority=2,
        )
        ids = [first.id, second.id, third.id]

        result = await service.convert_requisitions_to_orders(
            db, tenant, buyer, ids, RequisitionConversionRequest()
        )

        keys = {(g.vendor_id, g.item_category_id, g.warehouse_id) for g in result.groups}
        assert keys == {
            (world["precision"].id, world["locks"].id, world["warehouse"].id),
            (world["precision"].id, world["locks"].id, world["second_warehouse"].id),
            (world["kraft"].id, world["packing"].id, world["warehouse"].id),
        }
        pooled = next(
            g
            for g in result.groups
            if g.warehouse_id == world["warehouse"].id and g.item_category_id == world["locks"].id
        )
        # Two requisitions in one order: no single header source, lines from both.
        assert pooled.source_requisition_id is None
        assert {line.requisition_id for line in pooled.lines} == {first.id, third.id}
        assert pooled.priority == 2
        alone = next(g for g in result.groups if g.warehouse_id == world["second_warehouse"].id)
        assert alone.source_requisition_id == second.id
        assert all(r.status is RequisitionStatus.CONVERTED for r in result.requisitions)

    async def test_the_groups_are_ordered_deterministically(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition.id], RequisitionConversionRequest()
        )
        keys = [
            (str(g.vendor_id), str(g.item_category_id or ""), str(g.warehouse_id))
            for g in preview.groups
        ]
        assert keys == sorted(keys)


class TestPreview:
    async def test_the_preview_writes_nothing_and_proposes_what_convert_raises(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        before = await order_count(db, tenant)

        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition_id], RequisitionConversionRequest()
        )
        assert not preview.converted
        assert await order_count(db, tenant) == before
        assert all(group.purchase_order_id is None for group in preview.groups)
        detail = await service.get_requisition_detail(db, tenant, requisition_id)
        assert detail.status is RequisitionStatus.APPROVED

        converted = await service.convert_requisitions_to_orders(
            db, tenant, buyer, [requisition_id], RequisitionConversionRequest()
        )
        assert [
            (g.vendor_id, g.item_category_id, g.total_amount, g.assigned_vendor_user_id)
            for g in preview.groups
        ] == [
            (g.vendor_id, g.item_category_id, g.total_amount, g.assigned_vendor_user_id)
            for g in converted.groups
        ]

    async def test_the_preview_prices_the_proposed_orders(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        preview = await service.preview_requisition_conversion(
            db, tenant, [requisition.id], RequisitionConversionRequest()
        )
        locks = next(g for g in preview.groups if g.item_category_id == world["locks"].id)
        # 100 x 180 = 18 000, plus 18% GST.
        assert locks.subtotal == Decimal("18000.0000")
        assert locks.total_amount == Decimal("21240.0000")
        assert locks.item_category_code == world["locks"].code


# =============================================================================
# The order's category and its assignee, on the buyer's own order
# =============================================================================


def _order_payload(world, item: Item, **extra: Any) -> PurchaseOrderCreate:
    return PurchaseOrderCreate(
        vendor_id=extra.pop("vendor_id", world["precision"].id),
        warehouse_id=world["warehouse"].id,
        expected_delivery_date=today() + timedelta(days=30),
        lines=[
            PurchaseOrderLineIn(
                line_number=1, item_id=item.id, quantity=Decimal("5"), unit_price=Decimal("10")
            )
        ],
        **extra,
    )


class TestOrderCategoryAndAssignee:
    async def test_a_buyers_order_derives_its_category_from_its_lines(
        self, db, tenant, buyer, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        assert order.item_category_id == world["locks"].id

        updated, _ = await service.update_purchase_order(
            db,
            tenant,
            buyer,
            order.id,
            PurchaseOrderUpdate(
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=world["lever_item"].id,
                        quantity=Decimal("5"),
                        unit_price=Decimal("10"),
                    ),
                    PurchaseOrderLineIn(
                        line_number=2,
                        item_id=world["lock_body_item"].id,
                        quantity=Decimal("5"),
                        unit_price=Decimal("10"),
                    ),
                ]
            ),
        )
        # Two categories: none is claimed.
        assert updated.item_category_id is None

    async def test_an_assignee_from_another_vendor_is_refused(
        self, db, tenant, buyer, world
    ) -> None:
        with pytest.raises(ValidationError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(
                    world,
                    world["lock_body_item"],
                    assigned_vendor_user_id=world["packing_manager"].id,
                ),
            )

    async def test_an_assignee_outside_the_orders_category_is_refused(
        self, db, tenant, buyer, world
    ) -> None:
        with pytest.raises(ValidationError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(
                    world,
                    world["lock_body_item"],
                    assigned_vendor_user_id=world["lever_manager"].id,
                ),
            )

    async def test_an_unknown_assignee_is_not_found(self, db, tenant, buyer, world) -> None:
        with pytest.raises(NotFoundError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(
                    world, world["lock_body_item"], assigned_vendor_user_id=uuid.uuid4()
                ),
            )

    async def test_an_internal_user_cannot_be_the_vendor_assignee(
        self, db, tenant, buyer, world, _schema
    ) -> None:
        _, _, _, buyer_id, _ = _schema
        with pytest.raises(ValidationError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(world, world["lock_body_item"], assigned_vendor_user_id=buyer_id),
            )

    async def test_a_deactivated_assignee_is_refused(self, db, tenant, buyer, world) -> None:
        world["locks_manager"].is_active = False
        await db.flush()
        with pytest.raises(ValidationError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(
                    world,
                    world["lock_body_item"],
                    assigned_vendor_user_id=world["locks_manager"].id,
                ),
            )

    async def test_send_defaults_the_assignee_by_the_rule(
        self, db, tenant, buyer, approver, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        assert order.assigned_vendor_user_id is None
        sent = await send(db, tenant, buyer, approver, order.id)
        assert sent.assigned_vendor_user_id == world["locks_manager"].id

    async def test_send_validates_a_named_assignee(
        self, db, tenant, buyer, approver, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        order_id = order.id
        with pytest.raises(ValidationError):
            await send(
                db,
                tenant,
                buyer,
                approver,
                order_id,
                assigned_vendor_user_id=world["lever_manager"].id,
            )

    async def test_send_replaces_an_assignee_who_can_no_longer_take_the_order(
        self, db, tenant, buyer, approver, world
    ) -> None:
        """Deactivated between raising and Send: chosen again, never refused.

        An APPROVED order cannot be edited, so refusing the stale assignee would
        leave it with no way to the vendor. With the lock-body manager gone,
        the rule falls back to the one unrestricted user.
        """
        order, _, _ = await service.create_purchase_order(
            db,
            tenant,
            buyer,
            _order_payload(
                world, world["lock_body_item"], assigned_vendor_user_id=world["locks_manager"].id
            ),
        )
        world["locks_manager"].is_active = False
        await db.flush()

        sent = await send(db, tenant, buyer, approver, order.id)
        assert PurchaseOrderStatus(sent.status) is PurchaseOrderStatus.SENT
        assert sent.assigned_vendor_user_id == world["owner"].id

    async def test_send_still_refuses_a_named_assignee_who_cannot_take_the_order(
        self, db, tenant, buyer, approver, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        order_id = order.id
        world["locks_manager"].is_active = False
        await db.flush()
        with pytest.raises(ValidationError):
            await send(
                db,
                tenant,
                buyer,
                approver,
                order_id,
                assigned_vendor_user_id=world["locks_manager"].id,
            )

    async def test_send_accepts_the_unrestricted_owner(
        self, db, tenant, buyer, approver, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        sent = await send(
            db, tenant, buyer, approver, order.id, assigned_vendor_user_id=world["owner"].id
        )
        assert sent.assigned_vendor_user_id == world["owner"].id

    async def test_an_edit_that_leaves_the_assignees_categories_clears_the_assignee(
        self, db, tenant, buyer, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db,
            tenant,
            buyer,
            _order_payload(
                world, world["lock_body_item"], assigned_vendor_user_id=world["locks_manager"].id
            ),
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
                        item_id=world["lever_item"].id,
                        quantity=Decimal("5"),
                        unit_price=Decimal("10"),
                    )
                ]
            ),
        )
        assert updated.item_category_id == world["lever"].id
        assert updated.assigned_vendor_user_id is None

    async def test_the_vendor_assignee_list_offers_who_send_accepts(
        self, db, tenant, buyer, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        assignees = await service.list_vendor_assignees(db, tenant, order.id)
        offered = {a.id: a for a in assignees}
        assert set(offered) == {world["locks_manager"].id, world["owner"].id}
        assert offered[world["locks_manager"].id].is_suggested
        assert not offered[world["owner"].id].is_suggested
        assert offered[world["locks_manager"].id].allowed_category_codes == [world["locks"].code]

    async def test_the_detail_names_the_category_and_the_assignee(
        self, db, tenant, buyer, world
    ) -> None:
        order, _, _ = await service.create_purchase_order(
            db,
            tenant,
            buyer,
            _order_payload(
                world, world["lock_body_item"], assigned_vendor_user_id=world["locks_manager"].id
            ),
        )
        detail = await service.get_purchase_order_detail(db, tenant, order.id)
        assert detail.item_category_id == world["locks"].id
        assert detail.item_category_code == world["locks"].code
        assert detail.assigned_vendor_user_name == "Manager One"

    async def test_the_order_list_filters_by_category_subtree(
        self, db, tenant, buyer, world
    ) -> None:
        lever_order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lever_item"])
        )
        lock_order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        rows, _ = await service.list_purchase_orders(
            db,
            tenant,
            PurchaseOrderFilters(item_category_id=world["handles"].id),
            PageParams(),
            SortParams(),
        )
        ids = {row.id for row in rows}
        # The lever order sits beneath HANDLES; the lock order does not.
        assert lever_order.id in ids
        assert lock_order.id not in ids
        assert next(row for row in rows if row.id == lever_order.id).item_category_id == (
            world["lever"].id
        )

    async def test_the_order_list_names_each_rows_category(self, db, tenant, buyer, world) -> None:
        """The book's Category column is read by buyers who hold no ``category.view``."""
        lever_order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lever_item"])
        )
        lock_order, _, _ = await service.create_purchase_order(
            db, tenant, buyer, _order_payload(world, world["lock_body_item"])
        )
        mixed_order, _, _ = await service.create_purchase_order(
            db,
            tenant,
            buyer,
            PurchaseOrderCreate(
                vendor_id=world["precision"].id,
                warehouse_id=world["warehouse"].id,
                expected_delivery_date=today() + timedelta(days=30),
                lines=[
                    PurchaseOrderLineIn(
                        line_number=number,
                        item_id=item.id,
                        quantity=Decimal("5"),
                        unit_price=Decimal("10"),
                    )
                    for number, item in enumerate(
                        (world["lever_item"], world["lock_body_item"]), start=1
                    )
                ],
                # The same items on the same vendor and warehouse as the two
                # above - a duplicate by section 16, raised on purpose here.
                acknowledge_duplicates=True,
                duplicate_acknowledgement_reason="A third order spanning both categories",
            ),
        )
        ids = (lever_order.id, lock_order.id, mixed_order.id)

        rows, _ = await service.list_purchase_orders(
            db, tenant, PurchaseOrderFilters(), PageParams(), SortParams()
        )
        by_id = {row.id: row for row in rows if row.id in ids}

        assert (
            by_id[lever_order.id].item_category_code,
            by_id[lever_order.id].item_category_name,
        ) == (
            world["lever"].code,
            world["lever"].name,
        )
        assert by_id[lock_order.id].item_category_code == world["locks"].code
        # Lines in two categories: no category, so nothing to name.
        assert by_id[mixed_order.id].item_category_id is None
        assert by_id[mixed_order.id].item_category_code is None
        assert by_id[mixed_order.id].item_category_name is None


# =============================================================================
# Links the buyer names on a hand-raised order (T3)
# =============================================================================


class TestSourceLinks:
    async def test_another_tenants_requisition_line_is_not_found(
        self, db, tenant, other_tenant, buyer, approver, world
    ) -> None:
        _, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        other_uom = await seed_support.seed_uom(db, company_id=other_tenant.company_id)
        other_item = await seed_support.seed_item(
            db,
            company_id=other_tenant.company_id,
            code=f"OT-{_suffix()}",
            base_uom_id=other_uom.id,
            can_purchase=True,
        )
        other_vendor = await _approved_vendor(db, other_tenant, "Other")
        other_warehouse = await seed_support.seed_warehouse(
            db, company_id=other_tenant.company_id, code=f"OW{_suffix()}"
        )
        with pytest.raises(NotFoundError):
            await service.create_purchase_order(
                db,
                other_tenant,
                _principal(other_tenant.company_id, other_tenant.user_id),
                PurchaseOrderCreate(
                    vendor_id=other_vendor.id,
                    warehouse_id=other_warehouse.id,
                    lines=[
                        PurchaseOrderLineIn(
                            line_number=1,
                            item_id=other_item.id,
                            quantity=Decimal("1"),
                            unit_price=Decimal("1"),
                            requisition_line_id=lines[0].id,
                        )
                    ],
                ),
            )

    async def test_a_line_of_an_unapproved_requisition_is_refused(
        self, db, tenant, buyer, world
    ) -> None:
        _, draft_lines = await service.create_requisition(
            db,
            tenant,
            buyer,
            RequisitionCreate(
                required_date=today() + timedelta(days=7),
                warehouse_id=world["warehouse"].id,
                lines=[line(1, world["lock_body_item"], "5", "15")],
            ),
        )
        with pytest.raises(BusinessRuleError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                PurchaseOrderCreate(
                    vendor_id=world["precision"].id,
                    warehouse_id=world["warehouse"].id,
                    lines=[
                        PurchaseOrderLineIn(
                            line_number=1,
                            item_id=world["lock_body_item"].id,
                            quantity=Decimal("5"),
                            unit_price=Decimal("15"),
                            requisition_line_id=draft_lines[0].id,
                        )
                    ],
                ),
            )

    async def test_a_line_ordering_a_different_item_is_refused(
        self, db, tenant, buyer, approver, world
    ) -> None:
        _, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        with pytest.raises(ValidationError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                PurchaseOrderCreate(
                    vendor_id=world["precision"].id,
                    warehouse_id=world["warehouse"].id,
                    lines=[
                        PurchaseOrderLineIn(
                            line_number=1,
                            item_id=world["lever_item"].id,
                            quantity=Decimal("5"),
                            unit_price=Decimal("15"),
                            # The lock-body line, ordering a lever handle.
                            requisition_line_id=lines[0].id,
                        )
                    ],
                ),
            )

    async def test_a_foreign_department_on_a_requisition_is_not_found(
        self, db, tenant, other_tenant, buyer, world
    ) -> None:
        foreign = Department(
            company_id=other_tenant.company_id, code=f"D{_suffix()}", name="Elsewhere"
        )
        db.add(foreign)
        await db.flush()
        with pytest.raises(NotFoundError):
            await service.create_requisition(
                db,
                tenant,
                buyer,
                RequisitionCreate(
                    required_date=today() + timedelta(days=7),
                    warehouse_id=world["warehouse"].id,
                    department_id=foreign.id,
                    lines=[line(1, world["box_item"], "5", "15")],
                ),
            )

    async def test_a_suggested_vendor_must_be_one_of_this_tenants(
        self, db, tenant, other_tenant, buyer, world
    ) -> None:
        # The split reads the suggestion as a vendor source, so a foreign id is
        # refused where it is typed - on raising and on amending the draft.
        foreign_id = (await _approved_vendor(db, other_tenant, "Elsewhere Supplies")).id
        warehouse_id = world["warehouse"].id
        box_id = world["box_item"].id
        kraft_id = world["kraft"].id

        def suggesting(vendor_id: uuid.UUID) -> RequisitionLineIn:
            return RequisitionLineIn(
                line_number=1,
                item_id=box_id,
                quantity=Decimal("5"),
                estimated_unit_price=Decimal("15"),
                suggested_vendor_id=vendor_id,
            )

        with pytest.raises(NotFoundError):
            await service.create_requisition(
                db,
                tenant,
                buyer,
                RequisitionCreate(
                    required_date=today() + timedelta(days=7),
                    warehouse_id=warehouse_id,
                    lines=[suggesting(foreign_id)],
                ),
            )

        draft, _ = await service.create_requisition(
            db,
            tenant,
            buyer,
            RequisitionCreate(
                required_date=today() + timedelta(days=7),
                warehouse_id=warehouse_id,
                lines=[suggesting(kraft_id)],
            ),
        )
        draft_id = draft.id
        with pytest.raises(NotFoundError):
            await service.update_requisition(
                db, tenant, draft_id, RequisitionUpdate(lines=[suggesting(foreign_id)])
            )

    async def test_a_foreign_branch_on_an_order_is_not_found(
        self, db, tenant, buyer, world
    ) -> None:
        with pytest.raises(NotFoundError):
            await service.create_purchase_order(
                db,
                tenant,
                buyer,
                _order_payload(world, world["lock_body_item"], branch_id=uuid.uuid4()),
            )


# =============================================================================
# Closing a requisition, and the list the conversion starts from
# =============================================================================


class TestCloseAndList:
    async def test_an_approved_requisition_can_be_closed(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        closed = await service.transition_requisition(
            db, tenant, approver, requisition.id, RequisitionStatus.CLOSED, reason="Not needed"
        )
        assert RequisitionStatus(closed.status) is RequisitionStatus.CLOSED

    async def test_a_converted_requisition_can_be_closed(
        self, db, tenant, buyer, approver, world
    ) -> None:
        requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
        requisition_id = requisition.id
        await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [requisition_id],
            RequisitionConversionRequest(
                lines=[
                    RequisitionConversionLineOverride(requisition_line_id=lines[2].id, exclude=True)
                ]
            ),
        )
        closed = await service.transition_requisition(
            db, tenant, approver, requisition_id, RequisitionStatus.CLOSED, reason="Boxes later"
        )
        assert RequisitionStatus(closed.status) is RequisitionStatus.CLOSED
        # A closed requisition is no longer convertible.
        with pytest.raises(BusinessRuleError):
            await service.preview_requisition_conversion(
                db, tenant, [requisition_id], RequisitionConversionRequest()
            )

    async def test_a_draft_cannot_be_closed(self, db, tenant, buyer, world) -> None:
        draft, _ = await service.create_requisition(
            db,
            tenant,
            buyer,
            RequisitionCreate(
                required_date=today() + timedelta(days=7),
                warehouse_id=world["warehouse"].id,
                lines=[line(1, world["box_item"], "5", "15")],
            ),
        )
        with pytest.raises(InvalidStateTransitionError):
            await service.transition_requisition(
                db, tenant, buyer, draft.id, RequisitionStatus.CLOSED, reason="Never mind"
            )

    async def test_the_list_counts_open_lines_and_filters_convertible_ones(
        self, db, tenant, buyer, approver, world
    ) -> None:
        open_one, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        done, _ = await three_category_requisition(db, tenant, buyer, approver, world)
        await service.convert_requisitions_to_orders(
            db,
            tenant,
            buyer,
            [done.id],
            RequisitionConversionRequest(
                acknowledge_duplicates=True, duplicate_acknowledgement_reason="Separate build"
            ),
        )

        rows, _ = await service.list_requisitions(
            db, tenant, RequisitionFilters(), PageParams(), SortParams()
        )
        by_id = {row.id: row for row in rows}
        assert by_id[open_one.id].open_line_count == 3
        assert by_id[done.id].open_line_count == 0

        convertible, _ = await service.list_requisitions(
            db, tenant, RequisitionFilters(convertible_only=True), PageParams(), SortParams()
        )
        ids = {row.id for row in convertible}
        assert open_one.id in ids
        assert done.id not in ids


async def test_requisition_lines_are_left_untouched_by_a_refused_preview(
    db, tenant, buyer, approver, world
) -> None:
    """The preview takes no locks and writes nothing, even on the refusal path."""
    requisition, lines = await three_category_requisition(db, tenant, buyer, approver, world)
    line_ids = [line.id for line in lines]
    with pytest.raises(ValidationError):
        await service.preview_requisition_conversion(
            db,
            tenant,
            [requisition.id],
            RequisitionConversionRequest(
                lines=[RequisitionConversionLineOverride(requisition_line_id=uuid.uuid4())]
            ),
        )
    ordered = (
        await db.execute(
            select(PurchaseRequisitionLine.ordered_quantity).where(
                PurchaseRequisitionLine.id.in_(line_ids)
            )
        )
    ).scalars()
    assert all(quantity == 0 for quantity in ordered)
