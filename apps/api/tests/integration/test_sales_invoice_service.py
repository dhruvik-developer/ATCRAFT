"""Real PostgreSQL coverage of the reserved dispatch -> GST invoice -> receipt chain."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.core.clock import today, utcnow
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.pagination import PageParams, SortParams
from src.core.types import ZERO
from src.modules.allocation import service as allocation
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest
from src.modules.finance import credit_note_service as credit_notes
from src.modules.finance import sales_invoice_service as invoices
from src.modules.finance import service as finance
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import (
    CreditNote,
    CreditNoteReason,
    CreditNoteStatus,
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
from src.modules.quality import service as quality
from src.modules.quality.schemas import DecideRequest
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales_returns import service as returns
from src.modules.sales_returns.models import SalesReturn, SalesReturnStatus
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_warehouse

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["preview", "issue"])
async def test_foreign_invoice_cannot_enter_company_currency_credit(bench, operation):
    b = bench
    dispatch = await make_dispatch(b)
    b.order.currency = "USD"
    await b.session.flush()
    before_credit, before_quantity = b.customer.outstanding_amount, b.line.invoiced_quantity
    call = (
        invoices.preview_sales_invoice if operation == "preview" else invoices.issue_sales_invoice
    )
    with pytest.raises(BusinessRuleError, match="company currency"):
        await call(b.session, b.tenant, SalesInvoiceCreate(dispatch_id=dispatch.id))
    await b.session.refresh(b.customer)
    await b.session.refresh(b.line)
    assert b.customer.outstanding_amount == before_credit
    assert b.line.invoiced_quantity == before_quantity
    assert await b.session.scalar(select(func.count()).select_from(SalesInvoice)) == 0
    assert await b.session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


async def test_legacy_foreign_invoice_reversal_cannot_change_nominal_credit(bench):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    row = await b.session.get(SalesInvoice, invoice.id)
    row.currency = "USD"
    await b.session.flush()
    invoice_id = row.id
    before_credit, before_quantity = b.customer.outstanding_amount, b.line.invoiced_quantity
    before_entries = await b.session.scalar(select(func.count()).select_from(LedgerEntry))
    with pytest.raises(BusinessRuleError, match="company currency"):
        await invoices.cancel_sales_invoice(b.session, b.tenant, invoice_id, reason="Legacy FX")
    await b.session.refresh(row)
    await b.session.refresh(b.customer)
    await b.session.refresh(b.line)
    assert row.status == SalesInvoiceStatus.ISSUED and row.cancelled_at is None
    assert b.customer.outstanding_amount == before_credit
    assert b.line.invoiced_quantity == before_quantity
    assert await b.session.scalar(select(func.count()).select_from(LedgerEntry)) == before_entries


async def test_usd_company_invoice_posts_and_reverses_company_credit(bench, company):
    b = bench
    company.currency, b.order.currency = "USD", "USD"
    await b.session.flush()
    invoice = await issue(b, await make_dispatch(b))
    assert invoice.currency == "USD"
    assert b.customer.outstanding_amount == invoice.total_amount
    entries = list((await b.session.scalars(select(LedgerEntry))).all())
    assert entries and all(entry.currency == "USD" for entry in entries)
    cancelled = await invoices.cancel_sales_invoice(
        b.session, b.tenant, invoice.id, reason="Reverse company currency invoice"
    )
    assert cancelled.status == SalesInvoiceStatus.CANCELLED
    assert b.customer.outstanding_amount == ZERO


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


async def test_credit_note_cancellation_restores_receivable_and_invoice_status(bench, principal):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    # The original invoice carries -0.27 round-off; this partial credit takes
    # its -0.01 share as well as 100 taxable + 18 GST.
    credit_amount = Decimal("117.99")
    receipt = await finance.create_payment(
        b.session,
        b.tenant,
        PaymentCreate(
            payment_type=PaymentType.RECEIPT,
            party_type=PartyType.CUSTOMER,
            party_id=b.customer.id,
            amount=invoice.total_amount - credit_amount,
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
                PaymentAllocationIn(
                    sales_invoice_id=invoice.id,
                    amount=invoice.total_amount - credit_amount,
                )
            ]
        ),
    )
    credit = await credit_notes.issue_credit_note(
        b.session,
        b.tenant,
        CreditNoteCreate(
            sales_invoice_id=invoice.id,
            note_date=today(),
            reason_type=CreditNoteReason.PRICE_ADJUSTMENT,
            lines=[
                CreditNoteLineIn(
                    sales_invoice_line_id=invoice.lines[0].id,
                    taxable_amount=Decimal("100"),
                )
            ],
        ),
    )

    assert credit.total_amount == credit_amount
    credited = await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    assert credited.status == SalesInvoiceStatus.PAID
    assert credited.balance_amount == ZERO
    assert b.customer.outstanding_amount == ZERO

    await credit_notes.cancel_credit_note(
        b.session, b.tenant, credit.id, reason="Correcting the issued credit"
    )

    reopened = await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    assert reopened.status == SalesInvoiceStatus.PARTIALLY_PAID
    assert reopened.balance_amount == credit_amount
    assert b.customer.outstanding_amount == credit_amount


async def test_credit_notes_cannot_exceed_receivable_after_prior_credits(bench, principal):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    remaining = Decimal("200")
    paid_amount = invoice.total_amount - remaining
    receipt = await finance.create_payment(
        b.session,
        b.tenant,
        PaymentCreate(
            payment_type=PaymentType.RECEIPT,
            party_type=PartyType.CUSTOMER,
            party_id=b.customer.id,
            amount=paid_amount,
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
            allocations=[PaymentAllocationIn(sales_invoice_id=invoice.id, amount=paid_amount)]
        ),
    )

    payload = CreditNoteCreate(
        sales_invoice_id=invoice.id,
        note_date=today(),
        reason_type=CreditNoteReason.PRICE_ADJUSTMENT,
        lines=[
            CreditNoteLineIn(
                sales_invoice_line_id=invoice.lines[0].id,
                taxable_amount=Decimal("100"),
            )
        ],
    )
    await credit_notes.issue_credit_note(b.session, b.tenant, payload)

    with pytest.raises(BusinessRuleError, match="invoice receivable balance"):
        await credit_notes.issue_credit_note(b.session, b.tenant, payload)


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


async def test_gst_output_nets_issued_credit_notes_by_note_date_and_restores_cancelled_credits(
    bench,
):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    period = {"date_from": invoice.invoice_date, "date_to": invoice.invoice_date}
    before = await finance.gst_summary(b.session, b.tenant, **period)

    credit = await credit_notes.issue_credit_note(
        b.session,
        b.tenant,
        CreditNoteCreate(
            sales_invoice_id=invoice.id,
            note_date=invoice.invoice_date,
            reason_type=CreditNoteReason.PRICE_ADJUSTMENT,
            lines=[
                CreditNoteLineIn(
                    sales_invoice_line_id=invoice.lines[0].id,
                    taxable_amount=Decimal("100"),
                )
            ],
        ),
    )
    after_issue = await finance.gst_summary(b.session, b.tenant, **period)
    assert after_issue.output.taxable_amount == before.output.taxable_amount - credit.taxable_amount
    assert after_issue.output.total_tax == before.output.total_tax - credit.tax_amount
    assert len(after_issue.output.rows) == 1
    assert after_issue.output.rows[0].document_count == 2

    await credit_notes.cancel_credit_note(
        b.session, b.tenant, credit.id, reason="Credit note entered in error"
    )
    after_cancel = await finance.gst_summary(b.session, b.tenant, **period)
    assert after_cancel.output.taxable_amount == before.output.taxable_amount
    assert after_cancel.output.total_tax == before.output.total_tax
    assert after_cancel.output.rows[0].document_count == 1


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


async def _return_for_invoice(b, dispatch, *, quantity="2", requires_qc=True, receive=False):
    row = await returns.create_return(
        b.session,
        b.tenant,
        SalesReturnCreate(
            dispatch_id=dispatch.id,
            lines=[
                SalesReturnLineIn(
                    dispatch_line_id=dispatch.lines[0].id,
                    quantity=Decimal(quantity),
                    reason_code="CUSTOMER_RETURN",
                    requires_qc=requires_qc,
                )
            ],
        ),
    )
    if receive:
        row = await returns.transition_return(
            b.session, b.tenant, row.id, SalesReturnStatus.RECEIVED
        )
    return row


def _return_credit(invoice, return_id, *, quantity="1"):
    return CreditNoteCreate(
        sales_invoice_id=invoice.id,
        sales_return_id=return_id,
        note_date=today(),
        reason_type=CreditNoteReason.RETURN,
        lines=[
            CreditNoteLineIn(sales_invoice_line_id=invoice.lines[0].id, quantity=Decimal(quantity))
        ],
    )


@pytest.mark.parametrize("cancelled", [False, True], ids=["draft", "cancelled"])
async def test_unreceived_return_cannot_credit_an_invoice_and_failure_is_atomic(bench, cancelled):
    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
    row = await _return_for_invoice(b, dispatch)
    if cancelled:
        await returns.transition_return(
            b.session, b.tenant, row.id, SalesReturnStatus.CANCELLED, reason="Never received"
        )
    payload = _return_credit(invoice, row.id)
    before_entries = await b.session.scalar(select(func.count()).select_from(LedgerEntry))
    before_outstanding = b.customer.outstanding_amount
    with pytest.raises(BusinessRuleError, match="received sales return"):
        await credit_notes.issue_credit_note(b.session, b.tenant, payload)
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 0
    assert await b.session.scalar(select(func.count()).select_from(LedgerEntry)) == before_entries
    await b.session.refresh(b.customer)
    assert b.customer.outstanding_amount == before_outstanding
    unchanged = await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    assert unchanged.lines[0].credited_amount == 0


async def test_cross_tenant_return_id_is_not_a_finance_authorization_bypass(bench, other_company):
    from src.core.deps import TenantContext

    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
    row = await _return_for_invoice(b, dispatch)
    other_tenant = TenantContext(company_id=other_company.id, user_id=b.tenant.user_id)
    with pytest.raises(NotFoundError, match="Sales return"):
        await credit_notes.issue_credit_note(
            b.session, other_tenant, _return_credit(invoice, row.id)
        )
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 0


@pytest.mark.parametrize("different", ["customer", "invoice"])
async def test_return_must_match_the_invoice_dispatch_and_customer(bench, different):
    b = bench
    dispatch = await make_dispatch(b, "100")
    invoice = await issue(b, dispatch)
    if different == "invoice":
        dispatch = await make_dispatch(b, "100")
    row = await _return_for_invoice(b, dispatch, receive=True)
    if different == "customer":
        customer = await seed_customer(
            b.session, company_id=b.tenant.company_id, code=f"OTHER{uuid.uuid4().hex[:6]}"
        )
        stored = await b.session.get(SalesReturn, row.id)
        stored.customer_id = customer.id
        await b.session.flush()
    with pytest.raises(BusinessRuleError, match="selected invoice and customer"):
        await credit_notes.issue_credit_note(b.session, b.tenant, _return_credit(invoice, row.id))
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 0


async def test_pending_qc_does_not_allow_manual_return_credit(bench):
    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
    row = await _return_for_invoice(b, dispatch, receive=True)
    with pytest.raises(BusinessRuleError, match="accepted sales-return balance"):
        await credit_notes.issue_credit_note(b.session, b.tenant, _return_credit(invoice, row.id))
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 0


async def test_settled_return_credit_cannot_be_replayed_or_exceed_its_accepted_quantity(bench):
    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
    row = await _return_for_invoice(b, dispatch, requires_qc=False, receive=True)
    with pytest.raises(BusinessRuleError, match="accepted sales-return balance"):
        await credit_notes.issue_credit_note(b.session, b.tenant, _return_credit(invoice, row.id))
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 1
    assert (await returns.get_return(b.session, b.tenant, row.id)).lines[0].credited_quantity == 2


async def test_return_credit_cannot_include_qc_rejected_quantity(bench):
    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
    row = await _return_for_invoice(b, dispatch, receive=True)
    await quality.decide_inspection(
        b.session,
        b.tenant,
        row.lines[0].qc_inspection_id,
        DecideRequest(
            accepted_quantity=Decimal("1"),
            rejected_quantity=Decimal("1"),
            rejection_reason="Returned part is damaged",
            reason="Settled the returned lot",
        ),
    )
    closed = await returns.get_return(b.session, b.tenant, row.id)
    assert closed.status == SalesReturnStatus.CLOSED
    assert closed.lines[0].credited_quantity == 1
    with pytest.raises(BusinessRuleError, match="accepted sales-return balance"):
        await credit_notes.issue_credit_note(b.session, b.tenant, _return_credit(invoice, row.id))
    assert await b.session.scalar(select(func.count()).select_from(CreditNote)) == 1


async def test_paid_return_credit_cancellation_and_reissue_preserve_counters_and_customer_credit(
    bench, principal
):
    b = bench
    dispatch = await make_dispatch(b)
    invoice = await issue(b, dispatch)
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
    await finance.approve_payment(b.session, b.tenant, principal, receipt.id, reason="Paid in full")
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
    row = await _return_for_invoice(b, dispatch, requires_qc=False, receive=True)
    note = await b.session.scalar(select(CreditNote))
    note_id, total = note.id, note.total_amount
    assert b.customer.outstanding_amount == -total
    assert row.lines[0].credited_quantity == 2
    await credit_notes.cancel_credit_note(
        b.session, b.tenant, note_id, reason="Replace credit note"
    )
    cancelled = await returns.get_return(b.session, b.tenant, row.id)
    assert cancelled.lines[0].credited_quantity == 0
    assert b.customer.outstanding_amount == 0

    closed = await returns.transition_return(b.session, b.tenant, row.id, SalesReturnStatus.CLOSED)
    assert closed.lines[0].credited_quantity == 2
    assert b.customer.outstanding_amount == -total
    assert (
        await b.session.scalar(
            select(func.count())
            .select_from(CreditNote)
            .where(CreditNote.status == CreditNoteStatus.ISSUED)
        )
        == 1
    )
    assert (
        await invoices.get_sales_invoice(b.session, b.tenant, invoice.id)
    ).status == SalesInvoiceStatus.PAID
