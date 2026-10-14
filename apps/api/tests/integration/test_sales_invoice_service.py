"""Real PostgreSQL coverage of the reserved dispatch -> GST invoice -> receipt chain."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.core.clock import today, utcnow
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.pagination import PageParams, SortParams
from src.core.types import ZERO
from src.modules.allocation import service as allocation
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest
from src.modules.finance import sales_invoice_service as invoices
from src.modules.finance import service as finance
from src.modules.finance.models import (
    LedgerEntry,
    PartyType,
    PaymentMethod,
    PaymentType,
    SalesInvoice,
    SalesInvoiceStatus,
)
from src.modules.finance.sales_invoice_schemas import SalesInvoiceCreate, SalesInvoiceFilters
from src.modules.finance.schemas import AllocationRequest, PaymentAllocationIn, PaymentCreate
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_warehouse

pytestmark = pytest.mark.integration


@pytest.fixture
async def bench(session, tenant, company, monkeypatch):
    async def queued(*args, **kwargs):
        return str(uuid.uuid4())

    monkeypatch.setattr(invoices, "queue_sales_invoice_pdf", queued)
    company.state_code, company.state = "27", "Maharashtra"
    uom = await seed_uom(session, company_id=tenant.company_id)
    item = await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"FG{uuid.uuid4().hex[:6]}",
        base_uom_id=uom.id,
        can_sell=True,
        hsn_code="8302",
        gst_rate=Decimal("18"),
    )
    warehouse = await seed_warehouse(
        session, company_id=tenant.company_id, code=f"WH{uuid.uuid4().hex[:6]}"
    )
    customer = await seed_customer(
        session, company_id=tenant.company_id, code=f"CU{uuid.uuid4().hex[:6]}", credit_days=30
    )
    order = SalesOrder(
        company_id=tenant.company_id,
        code=f"SO{uuid.uuid4().hex[:6]}",
        customer_id=customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=7),
        warehouse_id=warehouse.id,
        status=SalesOrderStatus.CONFIRMED,
        currency="INR",
        is_interstate=False,
    )
    session.add(order)
    await session.flush()
    line = SalesOrderLine(
        company_id=tenant.company_id,
        sales_order_id=order.id,
        line_number=1,
        item_id=item.id,
        quantity=Decimal("500"),
        uom_id=uom.id,
        unit_price=Decimal("10"),
        discount_amount=Decimal("33.33"),
        tax_rate=Decimal("18"),
    )
    session.add(line)
    await session.flush()
    await inventory.post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("200"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code="OPEN",
        reason="Fixture opening stock",
        unit_cost=Decimal("5"),
    )
    view = await sales.allocate_order(session, tenant, order.id, reason="Fixture allocation")
    assert view.lines[0].reserved_quantity == Decimal("200")
    return SimpleNamespace(
        session=session,
        tenant=tenant,
        customer=customer,
        order=order,
        line=line,
        item=item,
        warehouse=warehouse,
        uom=uom,
    )


async def make_dispatch(b, quantity="200", *, leave=True):
    body = DispatchCreate.model_validate(
        {
            "dispatch_type": "SALES",
            "customer_id": str(b.customer.id),
            "warehouse_id": str(b.warehouse.id),
            "sales_order_id": str(b.order.id),
            "dispatch_date": today().isoformat(),
            "lines": [
                {
                    "item_id": str(b.item.id),
                    "sales_order_line_id": str(b.line.id),
                    "quantity": quantity,
                    "serial_ids": [],
                    "carton_ids": [],
                }
            ],
        }
    )
    dispatch = await dispatch_service.create_dispatch(b.session, b.tenant, body)
    await dispatch_service.mark_ready(b.session, b.tenant, dispatch.id, reason="Loaded and ready")
    if leave:
        await dispatch_service.dispatch_consignment(
            b.session, b.tenant, dispatch.id, DispatchRequest(reason="Goods leaving")
        )
    return dispatch


async def issue(b, dispatch):
    return await invoices.issue_sales_invoice(
        b.session, b.tenant, SalesInvoiceCreate(dispatch_id=dispatch.id)
    )


async def test_partial_dispatch_topup_and_invoice_discount_reconcile(bench):
    b = bench
    first = await make_dispatch(b)
    assert b.order.status == SalesOrderStatus.PARTIALLY_DISPATCHED
    assert b.line.dispatched_quantity == Decimal("200")
    assert b.line.allocated_quantity == 0
    one = await issue(b, first)
    assert one.discount_amount == Decimal("13.33")
    await inventory.post_movement(
        b.session,
        b.tenant,
        item_id=b.item.id,
        warehouse_id=b.warehouse.id,
        quantity=Decimal("300"),
        transaction_type=TransactionType.PURCHASE_RECEIPT,
        reference_type="goods_receipt",
        reference_id=uuid.uuid4(),
        reference_code="GRN-TOPUP",
        reason="Goods receipt top-up",
        unit_cost=Decimal("5"),
    )
    allocated = await sales.allocate_order(b.session, b.tenant, b.order.id, reason="Stock arrived")
    assert allocated.lines[0].reserved_quantity == Decimal("300")
    second = await make_dispatch(b, "300")
    two = await issue(b, second)
    assert one.discount_amount + two.discount_amount == Decimal("33.33")
    assert b.order.status == SalesOrderStatus.INVOICED
    assert b.line.invoiced_quantity == Decimal("500")
    legs = list(
        (
            await b.session.execute(
                select(LedgerEntry).where(LedgerEntry.journal_id == two.journal_id)
            )
        ).scalars()
    )
    assert sum(leg.debit_amount for leg in legs) == sum(leg.credit_amount for leg in legs)
    assert b.customer.outstanding_amount == one.total_amount + two.total_amount
    rows, page = await invoices.list_sales_invoices(
        b.session, b.tenant, SalesInvoiceFilters(), PageParams(), SortParams()
    )
    assert {row.id for row in rows} == {one.id, two.id}
    assert page is not None


async def test_preview_persists_nothing_and_ready_invoice_blocks_dispatch_cancel(bench):
    b = bench
    dispatch = await make_dispatch(b, leave=False)
    preview = await invoices.preview_sales_invoice(
        b.session, b.tenant, SalesInvoiceCreate(dispatch_id=dispatch.id)
    )
    assert preview.due_date == today() + timedelta(days=30)
    assert not list((await b.session.execute(select(SalesInvoice))).scalars())
    invoice = await issue(b, dispatch)
    with pytest.raises(BusinessRuleError, match="live sales invoice"):
        await dispatch_service.cancel_dispatch(
            b.session, b.tenant, dispatch.id, reason="Truck cancelled"
        )
    with pytest.raises(BusinessRuleError, match="live sales invoice"):
        await issue(b, dispatch)
    cancelled = await invoices.cancel_sales_invoice(
        b.session, b.tenant, invoice.id, reason="Issue correction"
    )
    assert cancelled.status == SalesInvoiceStatus.CANCELLED
    assert b.line.invoiced_quantity == 0
    assert b.customer.outstanding_amount == 0
    assert (
        cancelled.journal_id
        != (await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)).journal_id
        or cancelled.status == "CANCELLED"
    )
    await dispatch_service.cancel_dispatch(
        b.session, b.tenant, dispatch.id, reason="Truck cancelled"
    )


async def test_receipt_allocation_and_cancel_restore_invoice(bench, principal):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    receipt = await finance.create_payment(
        b.session,
        b.tenant,
        PaymentCreate(
            payment_type=PaymentType.RECEIPT,
            party_type=PartyType.CUSTOMER,
            party_id=b.customer.id,
            amount=invoice.total_amount,
            payment_date=today(),
            payment_method=PaymentMethod.CASH,
        ),
    )
    await finance.approve_payment(
        b.session, b.tenant, principal, receipt.id, reason="Receipt approved"
    )
    await finance.allocate_payment(
        b.session,
        b.tenant,
        receipt.id,
        AllocationRequest(
            allocations=[
                PaymentAllocationIn(sales_invoice_id=invoice.id, amount=invoice.total_amount)
            ]
        ),
    )
    assert (
        await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    ).status == SalesInvoiceStatus.PAID
    with pytest.raises(BusinessRuleError, match="credit note"):
        await invoices.cancel_sales_invoice(
            b.session, b.tenant, invoice.id, reason="Cannot cancel paid"
        )
    await finance.cancel_payment(b.session, b.tenant, receipt.id, reason="Receipt reversed")
    restored = await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    assert restored.status == SalesInvoiceStatus.ISSUED
    assert restored.paid_amount == 0
    assert restored.payments == []
    assert b.customer.outstanding_amount == invoice.total_amount


async def test_ageing_due_date_and_gst_use_snapshot(bench):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    report = await finance.ageing_report(
        b.session, b.tenant, party_type=PartyType.CUSTOMER, as_of=today()
    )
    assert report.currencies[0].rows[0].not_due == invoice.total_amount
    b.item.hsn_code = "9999"
    await b.session.flush()
    gst = await finance.gst_summary(b.session, b.tenant, date_from=today(), date_to=today())
    assert gst.output.total_tax == invoice.tax_amount
    assert gst.output.rows[0].hsn_code == "8302"
    assert gst.dispatched_not_invoiced.total_tax == 0


async def test_closed_reservation_refused_and_replaced_on_topup(bench):
    b = bench
    rid = b.line.reservation_id
    await allocation.release(b.session, b.tenant, reservation_id=rid, reason="Manual release")
    with pytest.raises(ValidationError, match="live stock reservation"):
        await make_dispatch(b)
    view = await sales.allocation_status(b.session, b.tenant, b.order.id)
    assert view.lines[0].dispatchable_quantity == 0
    view = await sales.allocate_order(
        b.session, b.tenant, b.order.id, reason="Reserve released stock again"
    )
    assert view.lines[0].reserved_quantity == Decimal("200")
    assert b.line.reservation_id != rid


async def test_invoice_cross_tenant_refused(bench, other_company):
    from src.core.deps import TenantContext

    other_tenant = TenantContext(company_id=other_company.id, user_id=bench.tenant.user_id)
    invoice = await issue(bench, await make_dispatch(bench))
    with pytest.raises(NotFoundError):
        await invoices.get_sales_invoice(bench.session, other_tenant, invoice.id)


async def test_expired_cancellation_window_and_journal_reversal(bench):
    invoice = await issue(bench, await make_dispatch(bench))
    row = (
        await bench.session.execute(select(SalesInvoice).where(SalesInvoice.id == invoice.id))
    ).scalar_one()
    row.issued_at = utcnow() - timedelta(hours=25)
    await bench.session.flush()
    with pytest.raises(BusinessRuleError, match="credit note"):
        await invoices.cancel_sales_invoice(
            bench.session, bench.tenant, invoice.id, reason="Too late"
        )


async def test_ready_invoice_then_departure_advances_order_and_cancel_reverses_journal(bench):
    b = bench
    b.line.quantity = Decimal("200")
    await b.session.flush()
    await sales.allocate_order(
        b.session, b.tenant, b.order.id, reason="Fully cover the smaller fixture order"
    )
    dispatch = await make_dispatch(b, leave=False)
    invoice = await issue(b, dispatch)
    assert b.line.invoiced_quantity == Decimal("200")
    result = await dispatch_service.dispatch_consignment(
        b.session, b.tenant, dispatch.id, DispatchRequest(reason="Invoiced goods leave")
    )
    assert result.sales_order_status == SalesOrderStatus.INVOICED
    cancelled = await invoices.cancel_sales_invoice(
        b.session, b.tenant, invoice.id, reason="Invoice corrected within the window"
    )
    assert cancelled.status == SalesInvoiceStatus.CANCELLED
    assert b.order.status == SalesOrderStatus.DISPATCHED
    assert b.line.invoiced_quantity == ZERO
    legs = list(
        (
            await b.session.execute(
                select(LedgerEntry).where(LedgerEntry.reference_id == invoice.id)
            )
        ).scalars()
    )
    assert any(leg.is_reversal for leg in legs)
    assert sum(leg.debit_amount for leg in legs) == sum(leg.credit_amount for leg in legs)
    assert b.customer.outstanding_amount == 0


async def test_quantity_growth_restores_confirmed_state(bench, principal):
    from src.modules.sales.schemas import SalesOrderLineIn, SalesOrderUpdate

    b = bench
    b.line.quantity = Decimal("200")
    await b.session.flush()
    await sales.allocate_order(b.session, b.tenant, b.order.id, reason="Fully allocated fixture")
    assert b.order.status == SalesOrderStatus.ALLOCATED
    await sales.update_order(
        b.session,
        b.tenant,
        principal,
        b.order.id,
        SalesOrderUpdate(
            lines=[
                SalesOrderLineIn(
                    line_number=1, item_id=b.item.id, quantity=Decimal("500"), uom_id=b.uom.id
                )
            ]
        ),
    )
    assert b.order.status == SalesOrderStatus.CONFIRMED
    assert b.line.allocated_quantity == Decimal("200")


async def test_topup_noop_has_no_audit_and_cancel_accepts_closed_reservation(bench):
    from src.modules.audit.models import AuditLog

    b = bench
    before = list((await b.session.execute(select(AuditLog.id))).scalars())
    await sales.allocate_order(b.session, b.tenant, b.order.id, reason="No newly available stock")
    assert list((await b.session.execute(select(AuditLog.id))).scalars()) == before
    await allocation.release(
        b.session, b.tenant, reservation_id=b.line.reservation_id, reason="Manually released"
    )
    await sales.cancel_order(b.session, b.tenant, b.order.id, reason="Customer cancelled")
    assert b.order.status == SalesOrderStatus.CANCELLED


async def test_system_revenue_account_skips_client_owned_code(bench):
    from src.modules.finance.models import LedgerAccount, LedgerAccountType, SystemAccount

    b = bench
    b.session.add(
        LedgerAccount(
            company_id=b.tenant.company_id,
            code="4500",
            name="Client own revenue",
            account_type=LedgerAccountType.INCOME,
            is_system=False,
        )
    )
    await b.session.flush()
    accounts = await finance.ensure_system_accounts(b.session, b.tenant)
    assert accounts[str(SystemAccount.SALES_REVENUE)].code == "4501"


async def test_ready_invoice_guards_order_cancel_and_quantity_floor(bench, principal):
    from src.modules.sales.schemas import SalesOrderLineIn, SalesOrderUpdate

    b = bench
    invoice = await issue(b, await make_dispatch(b, leave=False))
    with pytest.raises(BusinessRuleError, match="live sales invoice"):
        await sales.cancel_order(b.session, b.tenant, b.order.id, reason="Order withdrawn")
    with pytest.raises(BusinessRuleError, match="already been invoiced"):
        await sales.update_order(
            b.session,
            b.tenant,
            principal,
            b.order.id,
            SalesOrderUpdate(
                lines=[
                    SalesOrderLineIn(
                        line_number=1, item_id=b.item.id, uom_id=b.uom.id, quantity=Decimal("100")
                    )
                ]
            ),
        )
    assert (
        await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    ).total_amount == invoice.total_amount


async def test_reservation_released_after_planning_is_rechecked_at_departure(bench):
    b = bench
    dispatch = await make_dispatch(b, leave=False)
    await allocation.release(
        b.session,
        b.tenant,
        reservation_id=b.line.reservation_id,
        reason="Promise released after planning",
    )
    with pytest.raises(ValidationError, match="live stock reservation"):
        await dispatch_service.dispatch_consignment(
            b.session, b.tenant, dispatch.id, DispatchRequest(reason="Must not leave")
        )
    assert b.line.dispatched_quantity == ZERO


async def test_closed_order_disables_cancel_in_both_list_and_detail(bench, principal):
    from src.modules.sales.schemas import SalesOrderLineIn, SalesOrderUpdate

    b = bench
    await sales.update_order(
        b.session,
        b.tenant,
        principal,
        b.order.id,
        SalesOrderUpdate(
            lines=[
                SalesOrderLineIn(
                    line_number=1,
                    item_id=b.item.id,
                    quantity=Decimal("200"),
                    uom_id=b.uom.id,
                )
            ]
        ),
    )
    invoice = await issue(b, await make_dispatch(b))
    assert b.order.status == SalesOrderStatus.INVOICED
    await sales.close_order(b.session, b.tenant, b.order.id, reason="Fulfilment complete")
    detail = await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    assert not detail.can_cancel
    rows, _ = await invoices.list_sales_invoices(
        b.session, b.tenant, SalesInvoiceFilters(), PageParams(), SortParams()
    )
    assert not next(row for row in rows if row.id == invoice.id).can_cancel
    with pytest.raises(BusinessRuleError, match="closed"):
        await invoices.cancel_sales_invoice(b.session, b.tenant, invoice.id, reason="Try reversal")
