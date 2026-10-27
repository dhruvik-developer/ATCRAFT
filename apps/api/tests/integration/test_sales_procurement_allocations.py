"""The sales trail reports allocated quantities without inventing shared root shares."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import get_db
from src.core.deps import get_current_principal
from src.main import app
from src.modules.mrp.models import MrpRun, MrpRunStatus
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderLineStatus,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    PurchaseSourceType,
    RequisitionLineStatus,
    RequisitionStatus,
)
from src.modules.sales.fulfilment import _procurement_documents, order_fulfilment
from src.modules.sales.models import SalesOrder, SalesOrderLine
from tests import seed_support
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import buyer as buyer
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import other_tenant as other_tenant
from tests.integration.test_procurement_service import tenant as tenant

pytestmark = pytest.mark.integration


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _sales_orders(db, tenant, masters):
    customer = await seed_support.seed_customer(db, company_id=tenant.company_id)
    orders = [
        SalesOrder(
            company_id=tenant.company_id,
            code=_code("SO"),
            customer_id=customer.id,
            warehouse_id=masters["warehouse"].id,
            order_date=today(),
            required_date=today() + timedelta(days=10),
        )
        for _ in range(2)
    ]
    db.add_all(orders)
    await db.flush()
    lines = [
        SalesOrderLine(
            company_id=tenant.company_id,
            sales_order_id=order.id,
            line_number=1,
            item_id=masters["item"].id,
            quantity=Decimal("50"),
            uom_id=masters["uom"].id,
            unit_price=Decimal("100"),
        )
        for order in orders
    ]
    db.add_all(lines)
    await db.flush()
    return orders, lines


def _root(order, line):
    return {
        "demand_type": "SALES_ORDER",
        "demand_id": str(order.id),
        "demand_line_id": str(line.id),
    }


async def _requisition(
    db,
    tenant,
    masters,
    order,
    line,
    *,
    quantity="10",
    refs=None,
    mrp=False,
):
    source_type, source_id = PurchaseSourceType.SALES_ORDER, order.id
    if mrp:
        run = MrpRun(
            company_id=tenant.company_id,
            code=_code("MRP"),
            horizon_days=30,
            status=MrpRunStatus.COMPLETED,
        )
        db.add(run)
        await db.flush()
        source_type, source_id = PurchaseSourceType.MRP_RUN, run.id
    header = PurchaseRequisition(
        company_id=tenant.company_id,
        code=_code("PR"),
        requisition_date=today(),
        required_date=today() + timedelta(days=10),
        warehouse_id=masters["warehouse"].id,
        source_type=source_type,
        source_id=source_id,
        status=RequisitionStatus.CONVERTED,
    )
    db.add(header)
    await db.flush()
    detail = PurchaseRequisitionLine(
        company_id=tenant.company_id,
        requisition_id=header.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal(quantity),
        ordered_quantity=Decimal(quantity),
        status=RequisitionLineStatus.ORDERED,
        source_demand_refs=[_root(order, line)] if refs is None else refs,
    )
    db.add(detail)
    await db.flush()
    return header, detail


async def _physical_line(db, tenant, masters, allocations, *, scalar=None):
    order = PurchaseOrder(
        company_id=tenant.company_id,
        code=_code("PO"),
        po_date=today(),
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
        sent_at=utcnow(),
        subtotal=Decimal("2000"),
        tax_amount=Decimal("360"),
        total_amount=Decimal("2360"),
    )
    db.add(order)
    await db.flush()
    line = PurchaseOrderLine(
        company_id=tenant.company_id,
        purchase_order_id=order.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("40"),
        unit_price=Decimal("50"),
        received_quantity=Decimal("8"),
        accepted_quantity=Decimal("8"),
        tax_rate=Decimal("18"),
        tax_amount=Decimal("360"),
        line_total=Decimal("2360"),
        status=PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
        requisition_line_id=scalar,
    )
    db.add(line)
    await db.flush()
    db.add_all(
        [
            PurchaseOrderSourceAllocation(
                company_id=tenant.company_id,
                purchase_order_line_id=line.id,
                requisition_line_id=source.id,
                quantity=Decimal(quantity),
                is_active=active,
            )
            for source, quantity, active in allocations
        ]
    )
    await db.flush()
    return order, line


async def _trail(db, tenant, order, line):
    rows, _ = await _procurement_documents(db, tenant, order.id, frozenset({line.id}))
    return rows


def _purchases(rows):
    return [row for row in rows if row.kind == "PURCHASE_ORDER"]


async def test_each_sales_trail_reports_its_allocation_of_a_pooled_physical_line(
    db, tenant, masters
):
    orders, lines = await _sales_orders(db, tenant, masters)
    _, first = await _requisition(db, tenant, masters, orders[0], lines[0], quantity="10")
    _, second = await _requisition(db, tenant, masters, orders[1], lines[1], quantity="30")
    po, _ = await _physical_line(db, tenant, masters, [(first, "10", True), (second, "30", True)])
    for order, line, expected in zip(orders, lines, ("10", "30"), strict=True):
        purchase = _purchases(await _trail(db, tenant, order, line))
        assert len(purchase) == 1
        assert purchase[0].id == po.id
        assert purchase[0].line_id == line.id
        assert purchase[0].quantity == Decimal(expected)
        assert purchase[0].secondary_quantity is None
        assert "full PO line" in purchase[0].detail


async def test_shared_mrp_roots_remain_aggregate_at_document_level(db, tenant, masters):
    orders, lines = await _sales_orders(db, tenant, masters)
    roots = [_root(order, line) for order, line in zip(orders, lines, strict=True)]
    _, source = await _requisition(
        db, tenant, masters, orders[0], lines[0], quantity="40", refs=roots, mrp=True
    )
    await _physical_line(db, tenant, masters, [(source, "40", True)])
    for order, line in zip(orders, lines, strict=True):
        documents = await _trail(db, tenant, order, line)
        assert len(documents) == 2
        assert {document.kind for document in documents} == {
            "PURCHASE_REQUISITION",
            "PURCHASE_ORDER",
        }
        assert all(document.line_id is None for document in documents)
        assert all(document.quantity == Decimal("40") for document in documents)
        assert all("aggregate" in document.detail for document in documents)


async def test_mrp_requisition_is_discovered_from_roots_without_sales_header_source(
    db, tenant, masters
):
    orders, lines = await _sales_orders(db, tenant, masters)
    header, source = await _requisition(db, tenant, masters, orders[0], lines[0], mrp=True)
    await _physical_line(db, tenant, masters, [(source, "10", True)])
    result = await order_fulfilment(db, tenant, orders[0].id)
    assert [row.id for row in result.requisitions] == [header.id]
    assert result.requisitions[0].line_id == lines[0].id
    assert result.purchase_orders[0].quantity == Decimal("10")


async def test_inactive_mapping_keeps_history_without_falling_back_to_scalar_quantity(
    db, tenant, masters
):
    orders, lines = await _sales_orders(db, tenant, masters)
    _, source = await _requisition(db, tenant, masters, orders[0], lines[0])
    source.ordered_quantity = Decimal("0")
    source.status = RequisitionLineStatus.PENDING
    await _physical_line(db, tenant, masters, [(source, "10", False)], scalar=source.id)
    purchase = _purchases(await _trail(db, tenant, orders[0], lines[0]))
    assert len(purchase) == 1
    assert purchase[0].quantity == Decimal("10")
    assert purchase[0].secondary_quantity is None
    assert "released" in purchase[0].detail
    assert "historical" in purchase[0].detail


@pytest.mark.parametrize("malformed", [None, "bad-uuid"])
async def test_legacy_damaged_references_remain_readable_and_unattributed(
    db, tenant, masters, malformed
):
    orders, lines = await _sales_orders(db, tenant, masters)
    raw = [None] if malformed is None else [{"demand_type": "SALES_ORDER", "demand_id": malformed}]
    _, source = await _requisition(db, tenant, masters, orders[0], lines[0], refs=raw)
    await _physical_line(db, tenant, masters, [(source, "10", True)])
    documents = await _trail(db, tenant, orders[0], lines[0])
    assert len(documents) == 2
    assert all(document.line_id is None for document in documents)
    assert all("aggregate" in document.detail for document in documents)


async def test_multiple_requisitions_for_one_sales_order_are_grouped_without_duplicate_po_quantity(
    db, tenant, masters
):
    orders, lines = await _sales_orders(db, tenant, masters)
    first_header, first = await _requisition(
        db, tenant, masters, orders[0], lines[0], quantity="10"
    )
    second_header, second = await _requisition(
        db, tenant, masters, orders[0], lines[0], quantity="3"
    )
    await _physical_line(db, tenant, masters, [(first, "10", True), (second, "3", True)])
    documents = await _trail(db, tenant, orders[0], lines[0])
    assert {row.id for row in documents if row.kind == "PURCHASE_REQUISITION"} == {
        first_header.id,
        second_header.id,
    }
    purchases = _purchases(documents)
    assert len(purchases) == 1
    assert purchases[0].quantity == Decimal("13")


async def test_canonical_roots_supersede_a_conflicting_legacy_sales_header(db, tenant, masters):
    orders, lines = await _sales_orders(db, tenant, masters)
    _, source = await _requisition(
        db, tenant, masters, orders[0], lines[0], refs=[_root(orders[1], lines[1])]
    )
    await _physical_line(db, tenant, masters, [(source, "10", True)])
    assert await _trail(db, tenant, orders[0], lines[0]) == []
    assert len(_purchases(await _trail(db, tenant, orders[1], lines[1]))) == 1


async def test_another_tenant_cannot_see_the_allocation_or_requisition(
    db, tenant, masters, other_tenant
):
    orders, lines = await _sales_orders(db, tenant, masters)
    _, source = await _requisition(db, tenant, masters, orders[0], lines[0])
    await _physical_line(db, tenant, masters, [(source, "10", True)])
    documents, receipts = await _procurement_documents(
        db, other_tenant, orders[0].id, frozenset({lines[0].id})
    )
    assert documents == []
    assert receipts == []


async def test_http_trail_exposes_only_the_allocated_quantity(
    db: AsyncSession, tenant, buyer, masters
):
    orders, lines = await _sales_orders(db, tenant, masters)
    _, first = await _requisition(db, tenant, masters, orders[0], lines[0], quantity="10")
    _, second = await _requisition(db, tenant, masters, orders[1], lines[1], quantity="30")
    await _physical_line(db, tenant, masters, [(first, "10", True), (second, "30", True)])
    prior = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_principal] = lambda: buyer
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1"
        ) as client:
            response = await client.get(f"/sales/{orders[0].id}/fulfilment")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(prior)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["purchase_orders"]) == 1
    assert Decimal(data["purchase_orders"][0]["quantity"]) == Decimal("10")
    assert data["purchase_orders"][0]["secondary_quantity"] is None
