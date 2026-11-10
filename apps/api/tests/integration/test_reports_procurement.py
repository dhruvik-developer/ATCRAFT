"""Procurement reports execute against PostgreSQL with checked totals and filters."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.pagination import PageParams
from src.modules.procurement.inbound_models import PurchaseOrderShipment
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.vendors.models import Transporter, TransporterType
from tests import seed_support
from tests.procurement_support import _schema as _schema
from tests.procurement_support import db as db
from tests.procurement_support import masters as masters
from tests.procurement_support import other_tenant as other_tenant
from tests.procurement_support import tenant as tenant

pytestmark = pytest.mark.integration

PERIOD = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


async def _run(
    session: AsyncSession,
    tenant: TenantContext,
    key: ReportKey,
    filters: ReportFilters | None = None,
):
    result = await reports_service.run_report(
        session,
        tenant,
        key=key,
        period=PERIOD,
        filters=filters or ReportFilters(),
        params=PageParams(limit=100),
        may_export=False,
        now=NOW,
    )
    assert isinstance(result, reports_service.InlineReport)
    return result.rows


async def test_procurement_reports_return_scoped_rows_with_checked_totals_and_filters(
    db: AsyncSession,
    tenant: TenantContext,
    other_tenant: TenantContext,
    masters,
) -> None:
    category = await seed_support.seed_category(
        db, company_id=tenant.company_id, code="REPORT-CAT", name="Report category"
    )
    masters["item"].category_id = category.id
    await db.flush()
    transporter = Transporter(
        company_id=tenant.company_id,
        code="REPORT-TRANS",
        name="Report Transport",
        transporter_type=TransporterType.TRANSPORT,
        is_active=True,
    )
    db.add(transporter)
    await db.flush()

    order = PurchaseOrder(
        company_id=tenant.company_id,
        code="REPORT-PO-001",
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        item_category_id=category.id,
        po_date=date(2026, 9, 10),
        expected_delivery_date=date(2026, 9, 20),
        vendor_committed_date=date(2026, 9, 20),
        status=PurchaseOrderStatus.ACKNOWLEDGED,
        subtotal=Decimal("800"),
        total_amount=Decimal("1000"),
        sent_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        acknowledged_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        duplicate_acknowledged=True,
        duplicate_acknowledgement_reason="The additional quantity is required.",
        created_by_id=tenant.user_id,
        created_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    db.add(order)
    await db.flush()
    line = PurchaseOrderLine(
        company_id=tenant.company_id,
        purchase_order_id=order.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("8"),
        received_quantity=Decimal("3"),
        accepted_quantity=Decimal("3"),
        unit_price=Decimal("100"),
        line_total=Decimal("800"),
    )
    db.add(line)
    db.add_all(
        [
            PurchaseOrderShipment(
                company_id=tenant.company_id,
                purchase_order_id=order.id,
                shipment_number=1,
                transporter_id=transporter.id,
                shipped_at=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
                expected_delivery_date=date(2026, 9, 20),
                delivered_at=datetime(2026, 9, 23, 8, 0, tzinfo=UTC),
                freight_amount=Decimal("100"),
            ),
            PurchaseOrderShipment(
                company_id=tenant.company_id,
                purchase_order_id=order.id,
                shipment_number=2,
                transporter_id=transporter.id,
                shipped_at=datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
                expected_delivery_date=date(2026, 9, 20),
                freight_amount=Decimal("200"),
            ),
            PurchaseOrderShipment(
                company_id=tenant.company_id,
                purchase_order_id=order.id,
                shipment_number=3,
                transporter_id=transporter.id,
                shipped_at=datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
                expected_delivery_date=date(2026, 9, 30),
                freight_amount=Decimal("50"),
            ),
        ]
    )

    foreign_vendor = await seed_support.seed_vendor(
        db, company_id=other_tenant.company_id, code="FOREIGN-REPORT-VENDOR"
    )
    foreign_warehouse = await seed_support.seed_warehouse(
        db, company_id=other_tenant.company_id, code="FOREIGN-REPORT-WH"
    )
    foreign_order = PurchaseOrder(
        company_id=other_tenant.company_id,
        code="FOREIGN-REPORT-PO",
        vendor_id=foreign_vendor.id,
        warehouse_id=foreign_warehouse.id,
        po_date=date(2026, 9, 10),
        status=PurchaseOrderStatus.SENT,
        total_amount=Decimal("9999"),
    )
    db.add(foreign_order)
    await db.flush()

    register = await _run(db, tenant, ReportKey.PURCHASE_ORDER_REGISTER)
    assert len(register) == 1
    assert register[0].purchase_order_id == order.id
    assert register[0].received_percent == Decimal("37.50")
    assert register[0].pending_quantity == Decimal("5")

    vendor_summary = await _run(db, tenant, ReportKey.PURCHASE_ORDER_SUMMARY)
    assert len(vendor_summary) == 1
    assert vendor_summary[0].group_total_amount == Decimal("1000")
    category_summary = await _run(
        db, tenant, ReportKey.PURCHASE_ORDER_SUMMARY, ReportFilters(group_by="category")
    )
    assert len(category_summary) == 1
    assert category_summary[0].category_id == category.id
    assert category_summary[0].group_total_amount == Decimal("800")

    delayed = await _run(db, tenant, ReportKey.DELAYED_PURCHASE_ORDERS)
    assert len(delayed) == 1
    assert delayed[0].pending_quantity == Decimal("5")
    assert delayed[0].days_late == 10
    duplicate = await _run(db, tenant, ReportKey.DUPLICATE_PO_OVERRIDES)
    assert len(duplicate) == 1
    assert duplicate[0].duplicate_acknowledgement_reason == ("The additional quantity is required.")

    acceptance = await _run(db, tenant, ReportKey.VENDOR_ACCEPTANCE)
    assert len(acceptance) == 1
    assert acceptance[0].sent_count == 1
    assert acceptance[0].acknowledged_within_24h_percent == Decimal("100.00")
    assert acceptance[0].average_hours_to_acknowledge == Decimal("24.0000000000")
    assert acceptance[0].median_hours_to_acknowledge == Decimal("24.0000000000")

    shipments = await _run(db, tenant, ReportKey.INBOUND_SHIPMENTS)
    assert len(shipments) == 3
    assert next(row for row in shipments if row.shipment_number == 1).days_late == 3
    in_transit = await _run(
        db, tenant, ReportKey.INBOUND_SHIPMENTS, ReportFilters(in_transit_only=True)
    )
    assert len(in_transit) == 2
    expected_today = await _run(
        db, tenant, ReportKey.INBOUND_SHIPMENTS, ReportFilters(expected_today=True)
    )
    assert [row.shipment_number for row in expected_today] == [3]
    late_only = await _run(db, tenant, ReportKey.INBOUND_SHIPMENTS, ReportFilters(late_only=True))
    assert [row.shipment_number for row in late_only] == [2]
    filtered = await _run(
        db,
        tenant,
        ReportKey.INBOUND_SHIPMENTS,
        ReportFilters(category_id=category.id, transporter_id=transporter.id),
    )
    assert len(filtered) == 3

    freight = await _run(db, tenant, ReportKey.INBOUND_FREIGHT)
    assert len(freight) == 1
    assert freight[0].shipment_count == 3
    assert freight[0].freight_amount == Decimal("350")
    filtered_freight = await _run(
        db,
        tenant,
        ReportKey.INBOUND_FREIGHT,
        ReportFilters(category_id=category.id, transporter_id=transporter.id),
    )
    assert len(filtered_freight) == 1
    assert filtered_freight[0].freight_amount == Decimal("350")
    history = await _run(db, tenant, ReportKey.VENDOR_INBOUND_HISTORY)
    assert len(history) == 1
    assert history[0].shipment_count == 3
    assert history[0].delivered_count == 1
    assert history[0].late_count == 2
    assert history[0].on_time_percent == Decimal("0.00")
    filtered_history = await _run(
        db,
        tenant,
        ReportKey.VENDOR_INBOUND_HISTORY,
        ReportFilters(category_id=category.id, transporter_id=transporter.id),
    )
    assert len(filtered_history) == 1
    assert filtered_history[0].shipment_count == 3
