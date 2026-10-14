"""Foreign nominal amounts cannot change company-denominated customer credit."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.errors import BusinessRuleError
from src.modules.customers.models import Customer, CustomerType
from src.modules.finance import service
from src.modules.finance.models import (
    LedgerEntry,
    PartyType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
)
from src.modules.finance.schemas import PaymentCreate, PaymentUpdate
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderStatus
from src.modules.sales.repository import OrderPartyRepository
from src.modules.sales.schemas import SalesOrderCreate
from src.modules.vendors.models import Vendor, VendorType

pytestmark = pytest.mark.integration


async def customer(session, tenant):
    row = Customer(
        company_id=tenant.company_id,
        code=f"C-{uuid.uuid4().hex[:10]}",
        name="Credit Customer",
        customer_type=CustomerType.CUSTOMER,
        credit_limit=Decimal("1000"),
        outstanding_amount=Decimal("1000"),
    )
    session.add(row)
    await session.flush()
    return row


def receipt(row, currency=None):
    return PaymentCreate(
        payment_type=PaymentType.RECEIPT,
        party_type=PartyType.CUSTOMER,
        party_id=row.id,
        payment_date=date(2026, 10, 14),
        amount=Decimal("100"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        currency=currency,
    )


async def test_foreign_receipt_creation_leaves_cache_and_ledger_unchanged(session, tenant):
    row = await customer(session, tenant)
    row_id = row.id
    with pytest.raises(BusinessRuleError, match="company currency"):
        await service.create_payment(session, tenant, receipt(row, "USD"))
    await session.refresh(row)
    assert row.outstanding_amount == Decimal("1000")
    assert (
        await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.party_id == row_id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.party_id == row_id)
        )
        == 0
    )


@pytest.mark.parametrize("operation", ["update", "approve"])
async def test_legacy_foreign_draft_cannot_reach_credit_posting(
    session, tenant, principal, operation
):
    row = await customer(session, tenant)
    payment = Payment(
        company_id=tenant.company_id,
        code=f"P-{uuid.uuid4().hex[:10]}",
        payment_type=PaymentType.RECEIPT,
        party_type=PartyType.CUSTOMER,
        party_id=row.id,
        payment_date=date(2026, 10, 14),
        amount=Decimal("100"),
        allocated_amount=Decimal("0"),
        currency="USD",
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=PaymentStatus.DRAFT,
    )
    session.add(payment)
    await session.flush()
    payment_id = payment.id
    with pytest.raises(BusinessRuleError, match="company currency"):
        if operation == "update":
            await service.update_payment(
                session, tenant, payment_id, PaymentUpdate(amount=Decimal("120"))
            )
        else:
            await service.approve_payment(session, tenant, principal, payment_id)
    await session.refresh(payment)
    await session.refresh(row)
    assert payment.status == PaymentStatus.DRAFT and payment.approved_at is None
    assert row.outstanding_amount == Decimal("1000")
    cancelled = await service.cancel_payment(
        session, tenant, payment_id, reason="Unsupported legacy denomination"
    )
    assert cancelled.status == PaymentStatus.CANCELLED


async def test_omitted_receipt_currency_uses_non_inr_company(session, tenant, principal, company):
    company.currency = "USD"
    await session.flush()
    row = await customer(session, tenant)
    payment = await service.create_payment(session, tenant, receipt(row))
    assert payment.currency == "USD"
    await service.approve_payment(session, tenant, principal, payment.id)
    await session.refresh(row)
    assert row.outstanding_amount == Decimal("900")
    cancelled = await service.cancel_payment(session, tenant, payment.id, reason="Reversed receipt")
    assert cancelled.status == PaymentStatus.CANCELLED
    await session.refresh(row)
    assert row.outstanding_amount == Decimal("1000")


async def test_foreign_vendor_payment_is_still_supported(session, tenant, principal):
    vendor = Vendor(
        company_id=tenant.company_id,
        code=f"V-{uuid.uuid4().hex[:10]}",
        name="Foreign Vendor",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER],
        is_active=True,
    )
    session.add(vendor)
    await session.flush()
    payment = await service.create_payment(
        session,
        tenant,
        PaymentCreate(
            payment_type=PaymentType.PAYMENT,
            party_type=PartyType.VENDOR,
            party_id=vendor.id,
            payment_date=date(2026, 10, 14),
            amount=Decimal("100"),
            currency="USD",
            payment_method=PaymentMethod.BANK_TRANSFER,
        ),
    )
    approved = await service.approve_payment(session, tenant, principal, payment.id)
    assert approved.status == PaymentStatus.APPROVED
    entries = await service.LedgerEntryRepository(session, tenant).entries_for_reference(
        service.REFERENCE_PAYMENT, payment.id
    )
    assert entries and all(entry.currency == "USD" for entry in entries)


@pytest.mark.parametrize("operation", ["create", "confirm", "update"])
async def test_foreign_sales_order_cannot_enter_credit_flow(session, tenant, principal, operation):
    row = await customer(session, tenant)
    if operation == "create":
        payload = SalesOrderCreate(
            customer_id=row.id,
            warehouse_id=uuid.uuid4(),
            required_date=date(2026, 10, 20),
            currency="USD",
            lines=[{"line_number": 1, "item_id": uuid.uuid4(), "quantity": "1"}],
        )
        with pytest.raises(BusinessRuleError, match="company currency"):
            await sales.create_order(session, tenant, principal, payload)
    else:
        from src.modules.sales.schemas import SalesOrderUpdate
        from tests.seed_support import seed_warehouse

        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
        )
        order = SalesOrder(
            company_id=tenant.company_id,
            code=f"SO-{uuid.uuid4().hex[:10]}",
            customer_id=row.id,
            warehouse_id=warehouse.id,
            order_date=date(2026, 10, 14),
            required_date=date(2026, 10, 20),
            currency="USD",
            status=SalesOrderStatus.DRAFT,
        )
        session.add(order)
        await session.flush()
        order_id = order.id
        with pytest.raises(BusinessRuleError, match="company currency"):
            if operation == "confirm":
                await sales.confirm_order(
                    session, tenant, principal, order_id, reason="Check currency boundary"
                )
            else:
                await sales.update_order(
                    session, tenant, principal, order_id, SalesOrderUpdate(notes="Edit")
                )
        await session.refresh(order)
        assert order.status == SalesOrderStatus.DRAFT


async def test_sales_company_denomination_lookup(session, tenant, company):
    company.currency = "USD"
    await session.flush()
    assert await OrderPartyRepository(session, tenant).company_currency() == "USD"
