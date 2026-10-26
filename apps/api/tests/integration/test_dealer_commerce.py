"""Real invoice/return services remain private to the signed-in dealer."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from src.core.clock import today
from src.core.errors import AuthorizationError, BusinessRuleError, NotFoundError
from src.core.pagination import PageParams
from src.modules.customers.models import CustomerType
from src.modules.dealer_portal.commerce_schemas import DealerReturnCreate
from src.modules.dealer_portal.commerce_service import DealerCommerceService
from src.modules.finance import credit_note_service
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import CreditNoteReason
from src.modules.products.models import PriceType
from src.modules.sales_returns.models import SalesReturn, SalesReturnSource, SalesReturnStatus
from tests.integration.test_sales_invoice_service import bench as bench
from tests.integration.test_sales_invoice_service import issue, make_dispatch

pytestmark = pytest.mark.integration


@pytest.fixture
async def portal(bench):
    b = bench
    b.customer.customer_type = CustomerType.DEALER
    b.customer.price_list_type = PriceType.DEALER
    await b.session.flush()
    context = replace(b.tenant, customer_id=b.customer.id)
    return DealerCommerceService(b.session, context)


async def test_invoice_projection_nets_issued_credits_without_internal_fields(bench, portal):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    await credit_note_service.issue_credit_note(
        b.session,
        b.tenant,
        CreditNoteCreate(
            sales_invoice_id=invoice.id,
            note_date=today(),
            reason_type=CreditNoteReason.DISCOUNT,
            lines=[
                CreditNoteLineIn(
                    sales_invoice_line_id=invoice.lines[0].id, taxable_amount=Decimal("100")
                )
            ],
        ),
    )
    rows, page = await portal.invoices(PageParams())
    assert page.total == 1
    assert rows[0].credited_amount == Decimal("118")
    assert rows[0].balance_amount == invoice.total_amount - Decimal("118")
    detail = await portal.invoice(invoice.id)
    assert len(detail.credit_notes) == 1 and len(detail.lines) == 1
    assert {"journal_id", "issued_by_id", "cancelled_by_id", "company_id", "notes"}.isdisjoint(
        detail.model_dump()
    )


@pytest.mark.parametrize(
    "operation", ["invoices", "invoice", "credit_note", "sales_return", "download", "job"]
)
async def test_foreign_or_missing_ids_have_no_owner_disclosure(bench, portal, operation):
    if operation == "invoices":
        foreign = DealerCommerceService(
            bench.session, replace(portal.tenant, customer_id=uuid.uuid4())
        )
        with pytest.raises(AuthorizationError):
            await foreign.invoices(PageParams())
    else:
        with pytest.raises(NotFoundError):
            await getattr(portal, operation)(uuid.uuid4())


async def test_invoice_id_cannot_be_read_or_queued_by_another_dealer(bench, portal):
    from tests.api.dealer_portal_support import dealer_customer

    b = bench
    invoice = await issue(b, await make_dispatch(b))
    other = await dealer_customer(b.session, b.tenant, "Other dealer")
    foreign = DealerCommerceService(b.session, replace(portal.tenant, customer_id=other.id))
    with pytest.raises(NotFoundError):
        await foreign.invoice(invoice.id)
    with pytest.raises(NotFoundError):
        await foreign.queue_pdf(invoice.id)
    rows, _ = await foreign.invoices(PageParams())
    assert rows == []


async def test_dealer_return_request_is_draft_and_reserves_returnable_quantity(bench, portal):
    b = bench
    dispatch = await make_dispatch(b)
    eligible, _ = await portal.eligible_dispatches(PageParams())
    assert len(eligible) == 1 and eligible[0].id == dispatch.id
    line = eligible[0].lines[0]
    payload = DealerReturnCreate(
        dispatch_id=dispatch.id,
        lines=[
            {"dispatch_line_id": line.dispatch_line_id, "quantity": "2", "reason_code": "DAMAGED"}
        ],
    )
    result = await portal.request_return(payload)
    assert result.source == SalesReturnSource.DEALER_PORTAL
    assert result.status == SalesReturnStatus.DRAFT and result.received_at is None
    assert result.lines[0].requires_qc and result.credited_amount == 0
    row = await b.session.get(SalesReturn, result.id)
    assert row.requested_by_user_id == b.tenant.user_id
    refreshed, _ = await portal.eligible_dispatches(PageParams())
    assert refreshed[0].lines[0].returnable_quantity == line.returnable_quantity - Decimal("2")


async def test_dealer_cannot_request_another_customers_return(bench, portal):
    from tests.api.dealer_portal_support import dealer_customer

    b = bench
    dispatch = await make_dispatch(b)
    eligible, _ = await portal.eligible_dispatches(PageParams())
    other = await dealer_customer(b.session, b.tenant, "Other dealer")
    foreign = DealerCommerceService(b.session, replace(portal.tenant, customer_id=other.id))
    with pytest.raises(NotFoundError):
        await foreign.request_return(
            DealerReturnCreate(
                dispatch_id=dispatch.id,
                lines=[
                    {
                        "dispatch_line_id": eligible[0].lines[0].dispatch_line_id,
                        "quantity": "1",
                        "reason_code": "DAMAGED",
                    }
                ],
            )
        )


async def test_returnable_list_excludes_serial_units_and_over_return_is_refused(bench, portal):
    b = bench
    dispatch = await make_dispatch(b)
    eligible, _ = await portal.eligible_dispatches(PageParams())
    line = eligible[0].lines[0]
    with pytest.raises(BusinessRuleError):
        await portal.request_return(
            DealerReturnCreate(
                dispatch_id=dispatch.id,
                lines=[
                    {
                        "dispatch_line_id": line.dispatch_line_id,
                        "quantity": line.returnable_quantity + 1,
                        "reason_code": "DAMAGED",
                    }
                ],
            )
        )
    b.item.is_serial_tracked = True
    await b.session.flush()
    rows, _ = await portal.eligible_dispatches(PageParams())
    assert rows == []


async def test_outstanding_invoices_include_net_balance_and_exclude_other_customers(
    bench, portal, monkeypatch
):
    b = bench
    invoice = await issue(b, await make_dispatch(b))
    monkeypatch.setattr(
        "src.modules.dealer_portal.service.today", lambda: invoice.due_date + timedelta(days=1)
    )
    result = await portal.portal.outstanding()
    assert result.overdue_invoices[0].document_id == invoice.id
    assert result.overdue_invoices[0].outstanding_amount == invoice.total_amount
