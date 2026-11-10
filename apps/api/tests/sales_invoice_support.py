"""A confirmed sales order with its stock reserved, and the invoice chain from it.

``bench`` is that order: five hundred units ordered by a customer of an
intra-state Maharashtra company, two hundred on hand and all two hundred
reserved, with the invoice PDF job stubbed so nothing reaches a broker.
``make_dispatch`` loads and dispatches it, ``issue`` invoices a dispatch, and
``_return_for_invoice`` raises a return against one. ``bench`` asks for the
conftest ``session``, ``tenant`` and ``company`` by name.

The invoice service, credit-note rounding and dealer commerce suites and several
HTTP suites all start from this bench; it lives here so none of those files is
another's library.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.clock import today
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest
from src.modules.finance import sales_invoice_service as invoices
from src.modules.finance.sales_invoice_schemas import SalesInvoiceCreate
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales_returns import service as returns
from src.modules.sales_returns.models import SalesReturnStatus
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_warehouse


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


__all__ = ["_return_for_invoice", "bench", "issue", "make_dispatch"]
