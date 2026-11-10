"""A kit, its two components, an approved BOM and routing, and the orders on them.

``masters`` builds the plant through the ORM: a store, a unit, a work centre, the
kit with its body and screw components, and an approved default structure and
routing for it. ``stocked`` adds a thousand of each component through the real
engine (``stock``). ``make_order`` raises a production order for the kit,
``approve`` approves it as a second person, ``release`` releases it, and
``issue_everything`` drafts and posts one material issue covering every
component (``components_of``).

``masters`` and ``stocked`` ask for ``db`` and ``tenant`` by name: the production
service suite supplies its private schema's, and the production HTTP suite
aliases ``db`` to the conftest ``session``. Both suites build on them; they live
here so neither is the other's library.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.inventory.models import TransactionType
from src.modules.inventory.service import post_movement
from src.modules.production import service
from src.modules.production.models import MaterialIssueType, ProductionOrderComponent
from src.modules.production.schemas import (
    ApproveRequest,
    MaterialIssueCreate,
    MaterialIssueLineIn,
    MaterialIssuePostRequest,
    ProductionOrderCreate,
    ReleaseRequest,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.routing.models import Routing, RoutingOperation, RoutingStatus, WorkCenter
from src.modules.warehouses.models import Warehouse, WarehouseType


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


async def make_order(db, tenant, masters, *, quantity: str = "100", **kwargs):
    kwargs.setdefault("planned_start_date", today())
    kwargs.setdefault("planned_end_date", today() + timedelta(days=5))
    payload = ProductionOrderCreate(
        item_id=masters["kit"].id,
        planned_quantity=Decimal(quantity),
        warehouse_id=masters["warehouse"].id,
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


__all__ = [
    "approve",
    "components_of",
    "issue_everything",
    "make_order",
    "masters",
    "release",
    "stock",
    "stocked",
]
