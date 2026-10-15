"""Service-level tests for finance against a real PostgreSQL.

What is under test is the sentence master rules section 24 turns on: a payment
reduces what is outstanding, and every posting that gets it there balances.
Every assertion here is about the rows afterwards - the invoice's paid total and
status, the payment's allocated total, the ledger's debits against its credits,
and what the outstanding and ageing reports then say.

These need PostgreSQL because none of it is expressible without it: the CHECK
constraints that make an unbalanced invoice impossible, the partial unique index
behind the control accounts, ``SELECT ... FOR UPDATE`` on the invoice and
payment headers, the advisory lock that serialises chart provisioning, the
window function behind the statement's running balance, and the audit rows
written inside the same transaction as the money they describe.

Isolation follows ``test_quality_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import src.modules.organization.models
import src.modules.users.models  # noqa: F401 - registers `users`
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.models import Base
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.customers.models import Customer, CustomerType
from src.modules.finance import service
from src.modules.finance.models import (
    LedgerAccountType,
    LedgerEntry,
    MatchStatus,
    PartyType,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceStatus,
)
from src.modules.finance.repository import LedgerAccountRepository, LedgerEntryRepository
from src.modules.finance.schemas import (
    AllocationRequest,
    InvoiceApproval,
    LedgerAccountCreate,
    PaymentAllocationIn,
    PaymentCreate,
    StatementFilters,
    VendorInvoiceCreate,
    VendorInvoiceLineIn,
)
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
)
from src.modules.products.models import Item, ItemGroup, UnitOfMeasure, UomCategory
from src.modules.vendors.models import Vendor, VendorType
from src.modules.warehouses.models import Warehouse, WarehouseType
from tests import seed_support
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

# These modules own tables this one points at and are built beside it; prefer
# their real definitions and fall back to whatever the metadata already holds.
for _module_path, _table in (
    ("src.modules.inventory.models", "inventory_balances"),
    ("src.modules.sales.models", "sales_order_lines"),
    ("src.modules.dispatch.models", "dispatch_lines"),
):
    if _table not in Base.metadata.tables:
        with contextlib.suppress(ModuleNotFoundError):
            importlib.import_module(_module_path)


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with two companies and one accountant.

    The second company exists so tenant isolation can be asserted rather than
    assumed - a scoping rule that is never tested is a claim, not a property.
    """
    schema = f"it_finance_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                # Through the ORM builders, not a hand-written INSERT: the
                # column lists went stale twice in the files CLAUDE.md names.
                for identifier in (company_id, other_company_id):
                    await seed_support.seed_company(session, company_id=identifier, prefix="FIN")
                await seed_support.seed_user(
                    session,
                    company_id=company_id,
                    user_id=user_id,
                    email="accounts@atcraft.test",
                    full_name="Accountant",
                )
        finally:
            await engine.dispose()

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

    yield schema, company_id, other_company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, Principal]]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, _other, user_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    principal = Principal(
        user_id=user_id,
        company_id=company_id,
        email="accounts@atcraft.test",
        full_name="Accountant",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("ACCOUNTS",),
    )
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield session, tenant, principal
            await outer.rollback()
    finally:
        await engine.dispose()


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Fixture:
    """One vendor, one item and a purchase order received in full."""

    def __init__(self) -> None:
        self.vendor: Vendor
        self.item: Item
        self.order: PurchaseOrder
        self.order_line: PurchaseOrderLine
        self.receipt: GoodsReceipt
        self.receipt_line: GoodsReceiptLine
        self.uom: UnitOfMeasure


async def seed(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    ordered_quantity: str = "100",
    unit_price: str = "25",
    accepted_quantity: str = "100",
    line_discount: str = "0",
) -> Fixture:
    """A vendor, an item, an order for it and the receipt that took it in."""
    fixture = Fixture()

    fixture.uom = UnitOfMeasure(
        company_id=company_id, code=_code("U"), name="Numbers", uom_category=UomCategory.COUNT
    )
    fixture.vendor = Vendor(
        company_id=company_id,
        code=_code("V"),
        name="Hardware Supplies",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER],
        payment_terms_days=30,
    )
    warehouse = Warehouse(
        company_id=company_id,
        code=_code("W"),
        name="Raw store",
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    session.add_all([fixture.uom, fixture.vendor, warehouse])
    await session.flush()

    fixture.item = Item(
        company_id=company_id,
        code=_code("RM"),
        name="Brass rod 12mm",
        item_group=ItemGroup.RAW_MATERIAL,
        base_uom_id=fixture.uom.id,
        hsn_code="7407",
        gst_rate=Decimal("18"),
    )
    session.add(fixture.item)
    await session.flush()

    fixture.order = PurchaseOrder(
        company_id=company_id,
        code=_code("PO"),
        vendor_id=fixture.vendor.id,
        po_date=date(2026, 4, 1),
        warehouse_id=warehouse.id,
    )
    session.add(fixture.order)
    await session.flush()

    fixture.order_line = PurchaseOrderLine(
        company_id=company_id,
        purchase_order_id=fixture.order.id,
        line_number=1,
        item_id=fixture.item.id,
        quantity=Decimal(ordered_quantity),
        uom_id=fixture.uom.id,
        unit_price=Decimal(unit_price),
        discount=Decimal(line_discount),
        received_quantity=Decimal(accepted_quantity),
        accepted_quantity=Decimal(accepted_quantity),
    )
    fixture.receipt = GoodsReceipt(
        company_id=company_id,
        code=_code("GRN"),
        purchase_order_id=fixture.order.id,
        vendor_id=fixture.vendor.id,
        warehouse_id=warehouse.id,
        receipt_date=date(2026, 4, 5),
    )
    session.add_all([fixture.order_line, fixture.receipt])
    await session.flush()

    fixture.receipt_line = GoodsReceiptLine(
        company_id=company_id,
        goods_receipt_id=fixture.receipt.id,
        line_number=1,
        purchase_order_line_id=fixture.order_line.id,
        item_id=fixture.item.id,
        received_quantity=Decimal(accepted_quantity),
        accepted_quantity=Decimal(accepted_quantity),
        uom_id=fixture.uom.id,
        unit_cost=Decimal(unit_price),
    )
    session.add(fixture.receipt_line)
    await session.flush()
    return fixture


def invoice_payload(
    fixture: Fixture,
    *,
    quantity: str = "100",
    unit_price: str = "25",
    number: str | None = None,
    invoice_date: date = date(2026, 4, 6),
    link_receipt: bool = True,
    currency: str = "INR",
) -> VendorInvoiceCreate:
    return VendorInvoiceCreate(
        vendor_id=fixture.vendor.id,
        vendor_invoice_number=number or _code("INV"),
        invoice_date=invoice_date,
        currency=currency,
        purchase_order_id=fixture.order.id,
        goods_receipt_id=fixture.receipt.id if link_receipt else None,
        lines=[
            VendorInvoiceLineIn(
                item_id=fixture.item.id,
                purchase_order_line_id=fixture.order_line.id,
                goods_receipt_line_id=fixture.receipt_line.id if link_receipt else None,
                quantity=Decimal(quantity),
                uom_id=fixture.uom.id,
                unit_price=Decimal(unit_price),
                tax_rate=Decimal("18"),
            )
        ],
    )


async def approved_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    fixture: Fixture,
    **kwargs: object,
) -> VendorInvoice:
    """Record, submit and approve one matching invoice."""
    detail = await service.create_invoice(
        session,
        tenant,
        invoice_payload(fixture, **kwargs),  # type: ignore[arg-type]
    )
    await service.submit_invoice(session, tenant, detail.id, reason=None)
    await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())
    return await session.get(VendorInvoice, detail.id)  # type: ignore[return-value]


# -----------------------------------------------------------------------------
# Invoice lifecycle and the three-way match
# -----------------------------------------------------------------------------


class TestVendorInvoice:
    async def test_totals_are_derived_from_the_lines(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)

        detail = await service.create_invoice(session, tenant, invoice_payload(fixture))

        assert detail.subtotal == Decimal("2500.0000")
        assert detail.tax_amount == Decimal("450.0000")
        assert detail.total_amount == Decimal("2950.0000")
        assert detail.outstanding_amount == Decimal("2950.00")
        assert detail.status is VendorInvoiceStatus.DRAFT
        assert detail.lines[0].cgst_amount == Decimal("225.0000")
        assert detail.lines[0].sgst_amount == Decimal("225.0000")

    async def test_the_due_date_comes_from_the_vendors_credit_days(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)

        detail = await service.create_invoice(session, tenant, invoice_payload(fixture))

        assert detail.due_date == date(2026, 4, 6) + timedelta(days=30)

    async def test_a_vendor_cannot_bill_the_same_number_twice(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)
        number = _code("DUP")
        first = await service.create_invoice(
            session, tenant, invoice_payload(fixture, number=number)
        )

        with pytest.raises(BusinessRuleError) as caught:
            await service.create_invoice(session, tenant, invoice_payload(fixture, number=number))

        assert caught.value.details["existing_invoice_code"] == first.code

    async def test_a_matching_invoice_approves_and_posts_the_payable(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)

        invoice = await approved_invoice(session, tenant, principal, fixture)

        assert VendorInvoiceStatus(invoice.status) is VendorInvoiceStatus.APPROVED
        assert MatchStatus(invoice.match_status) is MatchStatus.MATCHED
        assert invoice.approved_by_id == principal.user_id

        entries = await LedgerEntryRepository(session, tenant).entries_for_reference(
            service.REFERENCE_VENDOR_INVOICE, invoice.id
        )
        assert len(entries) == 3
        assert sum(e.debit_amount for e in entries) == sum(e.credit_amount for e in entries)
        payable = next(e for e in entries if e.party_type == PartyType.VENDOR)
        assert payable.credit_amount == Decimal("2950.0000")
        assert payable.party_id == fixture.vendor.id

    async def test_an_overbilled_invoice_is_refused_without_an_override(self, ctx) -> None:
        session, tenant, principal = ctx
        # Accepted 95, billed 100: the classic overbill.
        fixture = await seed(session, tenant.company_id, accepted_quantity="95")
        detail = await service.create_invoice(session, tenant, invoice_payload(fixture))
        await service.submit_invoice(session, tenant, detail.id, reason=None)

        with pytest.raises(service.MatchVarianceError) as caught:
            await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())

        assert caught.value.details["blocking_reasons"]
        invoice = await session.get(VendorInvoice, detail.id)
        assert VendorInvoiceStatus(invoice.status) is VendorInvoiceStatus.PENDING_APPROVAL

    async def test_an_override_with_a_reason_approves_and_records_why(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id, accepted_quantity="95")
        detail = await service.create_invoice(session, tenant, invoice_payload(fixture))
        await service.submit_invoice(session, tenant, detail.id, reason=None)

        approved = await service.approve_invoice(
            session,
            tenant,
            principal,
            detail.id,
            InvoiceApproval(override_reason="Short delivery agreed with the vendor by email"),
        )

        assert approved.status is VendorInvoiceStatus.APPROVED
        assert approved.match_status is MatchStatus.OVERRIDDEN
        assert "agreed with the vendor" in (approved.variance_override_reason or "")

        rows = await session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == detail.id, AuditLog.action == AuditAction.APPROVE
            )
        )
        approvals = list(rows.scalars().all())
        assert approvals, "approving an invoice must leave an audit trail"
        assert any((entry.context or {}).get("override_reason") for entry in approvals), (
            "the override reason must reach the audit log"
        )

    async def test_a_price_beyond_tolerance_blocks_approval(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id, unit_price="25")
        # 25 -> 26 is 4%, twice the tolerance.
        detail = await service.create_invoice(
            session, tenant, invoice_payload(fixture, unit_price="26")
        )
        await service.submit_invoice(session, tenant, detail.id, reason=None)

        with pytest.raises(service.MatchVarianceError):
            await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())

    async def test_a_price_inside_tolerance_approves(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id, unit_price="25")
        # 25 -> 25.25 is 1%.
        detail = await service.create_invoice(
            session, tenant, invoice_payload(fixture, unit_price="25.25")
        )
        await service.submit_invoice(session, tenant, detail.id, reason=None)

        approved = await service.approve_invoice(
            session, tenant, principal, detail.id, InvoiceApproval()
        )

        assert approved.status is VendorInvoiceStatus.APPROVED
        assert approved.match_status is MatchStatus.MATCHED

    async def test_editing_an_invoice_invalidates_its_match(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)
        detail = await service.create_invoice(session, tenant, invoice_payload(fixture))
        await service.submit_invoice(session, tenant, detail.id, reason=None)

        from src.modules.finance.schemas import VendorInvoiceUpdate

        amended = await service.update_invoice(
            session, tenant, detail.id, VendorInvoiceUpdate(notes="Chased the vendor")
        )

        assert amended.match_status is MatchStatus.NOT_MATCHED
        assert amended.matched_at is None

    async def test_the_match_reports_the_receipts_accepted_quantity(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id, accepted_quantity="90")
        detail = await service.create_invoice(
            session, tenant, invoice_payload(fixture, quantity="90")
        )

        result = await service.match_invoice(session, tenant, detail.id)

        assert result.matched is True
        assert result.lines[0].accepted_quantity == Decimal("90.0000")
        assert result.total_expected == Decimal("2250.00")


# -----------------------------------------------------------------------------
# Payments - the deliverable: a payment reduces what is outstanding
# -----------------------------------------------------------------------------


class TestPaymentReducesOutstanding:
    async def test_a_part_payment_reduces_the_outstanding_and_moves_the_status(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)
        assert invoice.total_amount == Decimal("2950.0000")

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("1000"),
                payment_method=PaymentMethod.BANK_TRANSFER,
                reference_number="UTR-0001",
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        settled = await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=invoice.id, amount=Decimal("1000"))
                ]
            ),
        )

        await session.refresh(invoice)
        assert invoice.paid_amount == Decimal("1000.0000")
        assert VendorInvoiceStatus(invoice.status) is VendorInvoiceStatus.PARTIALLY_PAID
        assert settled.allocated_amount == Decimal("1000.0000")
        assert settled.unallocated_amount == Decimal("0.00")
        assert settled.status is PaymentStatus.ALLOCATED

        rows, _ = await service.list_outstanding(
            session, tenant, params=PageParams(page=1, limit=50), vendor_id=fixture.vendor.id
        )
        assert len(rows) == 1
        assert rows[0].outstanding_amount == Decimal("1950.00")

    async def test_settling_an_invoice_in_full_marks_it_paid_and_clears_the_report(
        self, ctx
    ) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("2950"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)
        await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=invoice.id, amount=Decimal("2950"))
                ]
            ),
        )

        await session.refresh(invoice)
        assert VendorInvoiceStatus(invoice.status) is VendorInvoiceStatus.PAID

        rows, _ = await service.list_outstanding(
            session, tenant, params=PageParams(page=1, limit=50), vendor_id=fixture.vendor.id
        )
        assert rows == []

    async def test_allocating_more_than_the_invoice_owes_is_refused(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("5000"),
                payment_method=PaymentMethod.CHEQUE,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        with pytest.raises(BusinessRuleError) as caught:
            await service.allocate_payment(
                session,
                tenant,
                payment.id,
                AllocationRequest(
                    allocations=[
                        PaymentAllocationIn(vendor_invoice_id=invoice.id, amount=Decimal("4000"))
                    ]
                ),
            )

        assert caught.value.details["outstanding_amount"] == "2950.00"
        await session.refresh(invoice)
        assert invoice.paid_amount == Decimal("0.0000")

    async def test_allocating_more_than_the_payment_is_worth_is_refused(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        first = await approved_invoice(session, tenant, principal, fixture)
        second = await approved_invoice(session, tenant, principal, fixture)

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("3000"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        with pytest.raises(BusinessRuleError) as caught:
            await service.allocate_payment(
                session,
                tenant,
                payment.id,
                AllocationRequest(
                    allocations=[
                        PaymentAllocationIn(vendor_invoice_id=first.id, amount=Decimal("2000")),
                        PaymentAllocationIn(vendor_invoice_id=second.id, amount=Decimal("2000")),
                    ]
                ),
            )

        assert caught.value.details["payment_amount"] == "3000.0000"

    async def test_re_sending_the_same_allocation_does_not_pay_twice(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("1000"),
                payment_method=PaymentMethod.UPI,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)
        request = AllocationRequest(
            allocations=[PaymentAllocationIn(vendor_invoice_id=invoice.id, amount=Decimal("1000"))]
        )

        await service.allocate_payment(session, tenant, payment.id, request)
        again = await service.allocate_payment(session, tenant, payment.id, request)

        await session.refresh(invoice)
        assert invoice.paid_amount == Decimal("1000.0000")
        assert again.allocated_amount == Decimal("1000.0000")
        assert len(again.allocations) == 1

    async def test_replacing_an_allocation_un_settles_what_it_dropped(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        first = await approved_invoice(session, tenant, principal, fixture)
        second = await approved_invoice(session, tenant, principal, fixture)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("2950"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)
        await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=first.id, amount=Decimal("2950"))
                ]
            ),
        )
        await session.refresh(first)
        assert VendorInvoiceStatus(first.status) is VendorInvoiceStatus.PAID

        # The clerk allocated to the wrong invoice; replacing must un-settle it.
        result = await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=second.id, amount=Decimal("2950"))
                ],
                replace=True,
            ),
        )

        await session.refresh(first)
        await session.refresh(second)
        assert first.paid_amount == Decimal("0.0000")
        assert VendorInvoiceStatus(first.status) is VendorInvoiceStatus.APPROVED
        assert second.paid_amount == Decimal("2950.0000")
        assert VendorInvoiceStatus(second.status) is VendorInvoiceStatus.PAID
        assert result.allocated_amount == Decimal("2950.0000")
        assert [a.vendor_invoice_id for a in result.allocations] == [second.id]

    async def test_an_invoice_that_has_been_paid_cannot_be_cancelled(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("500"),
                payment_method=PaymentMethod.CASH,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)
        await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=invoice.id, amount=Decimal("500"))
                ]
            ),
        )

        with pytest.raises(BusinessRuleError):
            await service.cancel_invoice(session, tenant, invoice.id, reason="Raised in error")

    async def test_a_payment_posts_a_balanced_settlement_journal(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("1000"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        entries = await LedgerEntryRepository(session, tenant).entries_for_reference(
            service.REFERENCE_PAYMENT, payment.id
        )
        assert len(entries) == 2
        debit, credit = await LedgerEntryRepository(session, tenant).journal_balances(
            entries[0].journal_id
        )
        assert debit == credit == Decimal("1000.0000")

    async def test_cancelling_an_approved_payment_reverses_it_rather_than_deleting(
        self, ctx
    ) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("750"),
                payment_method=PaymentMethod.CHEQUE,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        cancelled = await service.cancel_payment(
            session, tenant, payment.id, reason="Cheque never presented"
        )

        assert cancelled.status is PaymentStatus.CANCELLED
        entries = await LedgerEntryRepository(session, tenant).entries_for_reference(
            service.REFERENCE_PAYMENT, payment.id
        )
        # Original two legs plus their mirror image; nothing was deleted.
        assert len(entries) == 4
        assert sum(1 for entry in entries if entry.is_reversal) == 2
        assert sum(e.debit_amount for e in entries) == sum(e.credit_amount for e in entries)


class TestCustomerReceipt:
    async def test_a_receipt_reduces_the_customers_outstanding_balance(self, ctx) -> None:
        session, tenant, principal = ctx
        customer = Customer(
            company_id=tenant.company_id,
            code=_code("C"),
            name="Retail Doors Ltd",
            customer_type=CustomerType.CUSTOMER,
            outstanding_amount=Decimal("5000"),
        )
        session.add(customer)
        await session.flush()

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.RECEIPT,
                party_type=PartyType.CUSTOMER,
                party_id=customer.id,
                payment_date=date(2026, 5, 10),
                amount=Decimal("2000"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        await session.refresh(customer)
        assert customer.outstanding_amount == Decimal("3000.0000")

        entries = await LedgerEntryRepository(session, tenant).entries_for_reference(
            service.REFERENCE_PAYMENT, payment.id
        )
        receivable = next(e for e in entries if e.party_type == PartyType.CUSTOMER)
        assert receivable.credit_amount == Decimal("2000.0000")

    async def test_a_customer_receipt_refuses_a_vendor_invoice_reference(self, ctx) -> None:
        session, tenant, principal = ctx
        customer = Customer(
            company_id=tenant.company_id,
            code=_code("C"),
            name="Retail Doors Ltd",
            customer_type=CustomerType.CUSTOMER,
        )
        session.add(customer)
        await session.flush()
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.RECEIPT,
                party_type=PartyType.CUSTOMER,
                party_id=customer.id,
                payment_date=date(2026, 5, 10),
                amount=Decimal("100"),
                payment_method=PaymentMethod.CASH,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        with pytest.raises(ValidationError, match="sales invoices"):
            await service.allocate_payment(
                session,
                tenant,
                payment.id,
                AllocationRequest(
                    allocations=[
                        PaymentAllocationIn(vendor_invoice_id=uuid.uuid4(), amount=Decimal("100"))
                    ]
                ),
            )


# -----------------------------------------------------------------------------
# Ledger, statements and reports
# -----------------------------------------------------------------------------


class TestLedger:
    async def test_an_unbalanced_journal_writes_nothing(self, ctx) -> None:
        session, tenant, _ = ctx
        accounts = await service.ensure_system_accounts(session, tenant)
        before = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.company_id == tenant.company_id)
            )
        ).scalar_one()

        with pytest.raises(service.UnbalancedJournalError):
            await service.post_journal(
                session,
                tenant,
                [
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.BANK)].id, debit=Decimal("100")
                    ),
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.CASH)].id, credit=Decimal("90")
                    ),
                ],
                entry_date=date(2026, 4, 1),
                reference_type="manual",
            )

        after = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.company_id == tenant.company_id)
            )
        ).scalar_one()
        assert after == before

    async def test_a_journal_against_an_unknown_account_is_refused(self, ctx) -> None:
        session, tenant, _ = ctx
        accounts = await service.ensure_system_accounts(session, tenant)

        with pytest.raises(NotFoundError):
            await service.post_journal(
                session,
                tenant,
                [
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.BANK)].id, debit=Decimal("100")
                    ),
                    service.JournalLine(account_id=uuid.uuid4(), credit=Decimal("100")),
                ],
                entry_date=date(2026, 4, 1),
                reference_type="manual",
            )

    async def test_provisioning_the_chart_is_idempotent(self, ctx) -> None:
        session, tenant, _ = ctx

        first = await service.ensure_system_accounts(session, tenant)
        second = await service.ensure_system_accounts(session, tenant)

        assert set(first) == set(second)
        assert {account.id for account in first.values()} == {
            account.id for account in second.values()
        }

    async def test_the_vendor_statement_carries_a_running_balance(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 6),
                amount=Decimal("950"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        statement, _ = await service.party_statement(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            party_id=fixture.vendor.id,
            filters=StatementFilters(date_to=date(2026, 12, 31)),
            params=PageParams(page=1, limit=50),
        )

        assert [row.running_balance for row in statement.rows] == [
            Decimal("2950.00"),
            Decimal("2000.00"),
        ]
        assert statement.closing_balance == Decimal("2000.00")
        assert statement.total_credit == Decimal("2950.00")
        assert statement.total_debit == Decimal("950.00")
        assert invoice.total_amount == Decimal("2950.0000")

    async def test_the_statement_opening_balance_carries_into_a_window(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        await approved_invoice(session, tenant, principal, fixture)
        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 6, 1),
                amount=Decimal("950"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        statement, _ = await service.party_statement(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            party_id=fixture.vendor.id,
            filters=StatementFilters(date_from=date(2026, 5, 1), date_to=date(2026, 12, 31)),
            params=PageParams(page=1, limit=50),
        )

        # The invoice is before the window; only the payment is in it, and the
        # balance still starts from what was carried in.
        assert statement.opening_balance == Decimal("2950.00")
        assert len(statement.rows) == 1
        assert statement.rows[0].running_balance == Decimal("2000.00")
        assert statement.closing_balance == Decimal("2000.00")


class TestReports:
    async def test_ageing_buckets_a_debt_by_how_late_it_is(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        # Due 6 May 2026; read the report 45 days later.
        report = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            as_of=invoice.due_date + timedelta(days=45),
        )

        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.party_id == fixture.vendor.id
        assert row.bucket_31_60 == Decimal("2950.00")
        assert row.bucket_0_30 == Decimal("0.00")
        assert row.total_outstanding == Decimal("2950.00")
        assert report.total_outstanding == Decimal("2950.00")

    async def test_a_debt_not_yet_due_lands_in_the_not_due_column(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        report = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            as_of=invoice.due_date - timedelta(days=5),
        )

        assert report.rows[0].not_due == Decimal("2950.00")
        assert report.rows[0].bucket_0_30 == Decimal("0.00")

    async def test_the_outstanding_row_reports_the_days_overdue(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        rows, pagination = await service.list_outstanding(
            session,
            tenant,
            params=PageParams(page=1, limit=50),
            as_of=invoice.due_date + timedelta(days=10),
        )

        assert pagination.total == 1
        assert rows[0].days_overdue == 10
        assert rows[0].ageing_bucket == "0-30"

    async def test_the_gst_summary_reports_input_tax_from_approved_invoices(self, ctx) -> None:
        session, tenant, principal = ctx
        fixture = await seed(session, tenant.company_id)
        await approved_invoice(session, tenant, principal, fixture)

        summary = await service.gst_summary(
            session, tenant, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30)
        )

        assert summary.input.taxable_amount == Decimal("2500.00")
        assert summary.input.cgst_amount == Decimal("225.00")
        assert summary.input.sgst_amount == Decimal("225.00")
        assert summary.input.total_tax == Decimal("450.00")
        assert summary.input.rows[0].hsn_code == "7407"
        # Nothing has been dispatched, so there is no output tax and the whole
        # input credit is carried forward.
        assert summary.output.total_tax == Decimal("0.00")
        assert summary.net_payable == Decimal("-450.00")

    async def test_a_draft_invoice_is_not_an_input_tax_credit(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)
        await service.create_invoice(session, tenant, invoice_payload(fixture))

        summary = await service.gst_summary(
            session, tenant, date_from=date(2026, 4, 1), date_to=date(2026, 4, 30)
        )

        assert summary.input.total_tax == Decimal("0.00")


class TestReceivableAgeing:
    """The FIFO netting behind customer ageing.

    There is no customer-invoice document in the schema, so an open receivable
    is a ledger debit the party's credits have not yet covered. Getting the
    netting wrong would age the wrong money: applying a receipt to the newest
    charge instead of the oldest moves a debt from the 90+ column to the 0-30
    one, which is the column somebody decides whether to chase from.
    """

    async def _seed_receivable(
        self, session, tenant, principal, *, charges: list[tuple[date, str]], received: str
    ):
        customer = Customer(
            company_id=tenant.company_id,
            code=_code("C"),
            name="Retail Doors Ltd",
            customer_type=CustomerType.CUSTOMER,
        )
        session.add(customer)
        await session.flush()

        accounts = await service.ensure_system_accounts(session, tenant)
        sales = await service.create_account(
            session,
            tenant,
            LedgerAccountCreate(
                code=_code("40")[:32], name="Sales", account_type=LedgerAccountType.INCOME
            ),
        )

        for when, amount in charges:
            await service.post_journal(
                session,
                tenant,
                [
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                        debit=Decimal(amount),
                        party_type=PartyType.CUSTOMER,
                        party_id=customer.id,
                    ),
                    service.JournalLine(account_id=sales.id, credit=Decimal(amount)),
                ],
                entry_date=when,
                reference_type="sales_charge",
                reference_code=f"SALE/{when.isoformat()}",
            )

        if received != "0":
            payment = await service.create_payment(
                session,
                tenant,
                PaymentCreate(
                    payment_type=PaymentType.RECEIPT,
                    party_type=PartyType.CUSTOMER,
                    party_id=customer.id,
                    payment_date=date(2026, 6, 15),
                    amount=Decimal(received),
                    payment_method=PaymentMethod.BANK_TRANSFER,
                ),
            )
            await service.approve_payment(session, tenant, principal, payment.id)
        return customer

    async def test_a_receipt_is_applied_to_the_oldest_charge_first(self, ctx) -> None:
        session, tenant, principal = ctx
        customer = await self._seed_receivable(
            session,
            tenant,
            principal,
            charges=[(date(2026, 4, 1), "1000"), (date(2026, 6, 1), "500")],
            received="800",
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.party_id == customer.id
        # 800 settled the April charge down to 200; June's 500 is untouched.
        assert row.bucket_90_plus == Decimal("200.00")
        assert row.bucket_0_30 == Decimal("500.00")
        assert row.bucket_31_60 == Decimal("0.00")
        assert row.total_outstanding == Decimal("700.00")
        assert row.oldest_due_date == date(2026, 4, 1)

    async def test_a_fully_settled_customer_leaves_the_report(self, ctx) -> None:
        session, tenant, principal = ctx
        await self._seed_receivable(
            session,
            tenant,
            principal,
            charges=[(date(2026, 4, 1), "1000")],
            received="1000",
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert report.rows == []
        assert report.total_outstanding == Decimal("0.00")

    async def test_an_unsettled_customer_ages_the_whole_charge(self, ctx) -> None:
        session, tenant, principal = ctx
        await self._seed_receivable(
            session,
            tenant,
            principal,
            charges=[(date(2026, 6, 1), "1500")],
            received="0",
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert report.rows[0].bucket_0_30 == Decimal("1500.00")
        assert report.total_outstanding == Decimal("1500.00")


class TestReceivableAgeingWithoutAControlAccount:
    """The tenant whose chart of accounts has never been provisioned.

    ``ageing_report`` asks the chart for the ``ACCOUNTS_RECEIVABLE`` control
    account and hands what comes back to the query as *the* set of accounts a
    receivable may live on. For a company that has not run
    ``ensure_system_accounts`` that set is empty, and an empty restriction has
    exactly one safe reading: nothing matches.

    The query was written the other way round - ``if account_ids:`` around the
    ``IN`` - so an empty set removed the account predicate altogether and the
    report widened from one control account to the whole general ledger. Every
    party-tagged debit in the company then aged as customer debt. It is not an
    exotic state either: provisioning the chart is a separate, explicit step,
    and the report is readable before anyone has taken it.
    """

    async def _customer(self, session: AsyncSession, tenant: TenantContext) -> Customer:
        customer = Customer(
            company_id=tenant.company_id,
            code=_code("C"),
            name="Retail Doors Ltd",
            customer_type=CustomerType.CUSTOMER,
        )
        session.add(customer)
        await session.flush()
        return customer

    async def _party_debit_on_another_account(
        self, session: AsyncSession, tenant: TenantContext, customer: Customer, amount: str
    ) -> None:
        """A customer-tagged debit that is emphatically not a receivable.

        A refundable deposit lodged on the customer's behalf: it carries the
        party, it is a debit, it is not money the customer owes.
        """
        deposits = await service.create_account(
            session,
            tenant,
            LedgerAccountCreate(
                code=_code("18")[:32],
                name="Refundable deposits",
                account_type=LedgerAccountType.ASSET,
            ),
        )
        bank = await service.create_account(
            session,
            tenant,
            LedgerAccountCreate(
                code=_code("10")[:32], name="Bank", account_type=LedgerAccountType.ASSET
            ),
        )
        await service.post_journal(
            session,
            tenant,
            [
                service.JournalLine(
                    account_id=deposits.id,
                    debit=Decimal(amount),
                    party_type=PartyType.CUSTOMER,
                    party_id=customer.id,
                ),
                service.JournalLine(account_id=bank.id, credit=Decimal(amount)),
            ],
            entry_date=date(2026, 4, 1),
            reference_type="deposit",
            reference_code="DEP/2026/1",
        )

    async def test_an_unprovisioned_company_is_owed_nothing_rather_than_everything(
        self, ctx
    ) -> None:
        session, tenant, _principal = ctx
        customer = await self._customer(session, tenant)
        await self._party_debit_on_another_account(session, tenant, customer, "9000")

        assert (
            await LedgerAccountRepository(session, tenant).system_account_ids(
                SystemAccount.ACCOUNTS_RECEIVABLE
            )
            == []
        ), "this test is only meaningful while no receivable control account exists"

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert report.rows == [], (
            "a debit on an account that is not the receivable control account "
            "was reported as customer debt"
        )
        assert report.total_outstanding == Decimal("0.00")
        assert report.total_bucket_90_plus == Decimal("0.00")
        assert [block.total_outstanding for block in report.currencies] == [Decimal("0.00")]

    async def test_narrowing_to_one_customer_does_not_reopen_the_gap(self, ctx) -> None:
        """``party_id`` adds a predicate; it must not stand in for the missing one."""
        session, tenant, _principal = ctx
        customer = await self._customer(session, tenant)
        await self._party_debit_on_another_account(session, tenant, customer, "9000")

        report = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.CUSTOMER,
            party_id=customer.id,
            as_of=date(2026, 7, 1),
        )

        assert report.rows == []
        assert report.total_outstanding == Decimal("0.00")

    async def test_the_same_charge_on_the_control_account_is_reported(self, ctx) -> None:
        """The other half: the empty answer above is the missing account, not a broken seed.

        Same company, same customer, same amount - posted to the receivable
        control account this time, which now exists.
        """
        session, tenant, _principal = ctx
        customer = await self._customer(session, tenant)
        accounts = await service.ensure_system_accounts(session, tenant)
        sales = await service.create_account(
            session,
            tenant,
            LedgerAccountCreate(
                code=_code("40")[:32], name="Sales", account_type=LedgerAccountType.INCOME
            ),
        )
        await service.post_journal(
            session,
            tenant,
            [
                service.JournalLine(
                    account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                    debit=Decimal("9000"),
                    party_type=PartyType.CUSTOMER,
                    party_id=customer.id,
                ),
                service.JournalLine(account_id=sales.id, credit=Decimal("9000")),
            ],
            entry_date=date(2026, 4, 1),
            reference_type="sales_charge",
            reference_code="SALE/2026/1",
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert [row.party_id for row in report.rows] == [customer.id]
        assert report.total_outstanding == Decimal("9000.00")


class TestTenantIsolation:
    async def test_another_companys_invoice_is_not_visible(
        self, ctx, _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]
    ) -> None:
        session, tenant, principal = ctx
        _schema_name, _company, other_company_id, user_id = _schema
        fixture = await seed(session, tenant.company_id)
        invoice = await approved_invoice(session, tenant, principal, fixture)

        intruder = TenantContext(company_id=other_company_id, user_id=user_id)

        with pytest.raises(NotFoundError):
            await service.get_invoice_detail(session, intruder, invoice.id)

        rows, _ = await service.list_outstanding(
            session, intruder, params=PageParams(page=1, limit=50)
        )
        assert rows == []


class TestMultiCurrencyReporting:
    """A company transacting in two currencies must never be handed one scalar
    that silently mixes them.

    This schema has no exchange-rate table and multi-currency is still an open
    question for the business (``docs/assumptions.md``), so the honest answer is
    a narrow one: an ageing report is drawn in a single denomination, says which
    one, and names the denominations it therefore left out. The broad answer -
    rupees added to dollars, and vendors then *ranked* by the meaningless total,
    which puts a vendor owed 1,000 USD above one owed 50,000 INR - is what these
    tests exist to keep out.
    """

    async def _open_payable(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        *,
        unit_price: str,
        currency: str,
    ) -> VendorInvoice:
        """One approved, unpaid invoice from a vendor of its own."""
        fixture = await seed(session, tenant.company_id, unit_price=unit_price)
        # The order is denominated the way the bill is. An invoice priced in
        # dollars against a rupee order is a separate defect and is deliberately
        # not what is under test here.
        fixture.order.currency = currency
        await session.flush()
        return await approved_invoice(
            session, tenant, principal, fixture, unit_price=unit_price, currency=currency
        )

    async def _customer_ledger(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        *,
        charges: list[tuple[date, str, str]],
        receipt: tuple[str, str] | None = None,
    ) -> Customer:
        """A customer with charges, and optionally one receipt, per currency."""
        customer = Customer(
            company_id=tenant.company_id,
            code=_code("C"),
            name="Export Doors Ltd",
            customer_type=CustomerType.CUSTOMER,
        )
        session.add(customer)
        await session.flush()

        accounts = await service.ensure_system_accounts(session, tenant)
        sales = await service.create_account(
            session,
            tenant,
            LedgerAccountCreate(
                code=_code("40")[:32], name="Sales", account_type=LedgerAccountType.INCOME
            ),
        )

        for when, amount, currency in charges:
            await service.post_journal(
                session,
                tenant,
                [
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                        debit=Decimal(amount),
                        party_type=PartyType.CUSTOMER,
                        party_id=customer.id,
                    ),
                    service.JournalLine(account_id=sales.id, credit=Decimal(amount)),
                ],
                entry_date=when,
                reference_type="sales_charge",
                reference_code=f"SALE/{when.isoformat()}/{currency}",
                currency=currency,
            )

        if receipt is not None:
            amount, currency = receipt
            # Historical multi-currency ledger rows remain reportable, without
            # mutating the company-currency customer credit cache via a receipt.
            await service.post_journal(
                session,
                tenant,
                [
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.BANK)].id, debit=Decimal(amount)
                    ),
                    service.JournalLine(
                        account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                        credit=Decimal(amount),
                        party_type=PartyType.CUSTOMER,
                        party_id=customer.id,
                    ),
                ],
                entry_date=date(2026, 6, 15),
                reference_type="historical_receipt",
                reference_code=_code("RCPT"),
                currency=currency,
            )
        return customer

    async def test_the_payables_ageing_never_adds_two_currencies_together(self, ctx) -> None:
        session, tenant, principal = ctx
        # 59,000 INR, 11,800 INR and 118,000 USD. The dollar figure is the
        # largest number in the table, so a report that mixed denominations
        # would rank that vendor first and total 188,800 of nothing.
        larger = await self._open_payable(
            session, tenant, principal, unit_price="500", currency="INR"
        )
        smaller = await self._open_payable(
            session, tenant, principal, unit_price="100", currency="INR"
        )
        await self._open_payable(session, tenant, principal, unit_price="1000", currency="USD")

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.VENDOR, as_of=date(2026, 6, 1)
        )

        assert report.currency == "INR"
        assert [row.party_id for row in report.rows] == [larger.vendor_id, smaller.vendor_id]
        assert [row.currency for row in report.rows] == ["INR", "INR"]
        assert report.rows[0].total_outstanding == Decimal("59000.00")
        assert report.rows[1].total_outstanding == Decimal("11800.00")
        assert report.total_outstanding == Decimal("70800.00")
        assert report.total_bucket_0_30 == Decimal("70800.00")
        # The dollar debt is not in the total, so the report says where it went.
        assert report.other_currencies == ["USD"]

    async def test_asking_for_the_other_currency_reports_that_one_alone(self, ctx) -> None:
        session, tenant, principal = ctx
        await self._open_payable(session, tenant, principal, unit_price="500", currency="INR")
        dollars = await self._open_payable(
            session, tenant, principal, unit_price="1000", currency="USD"
        )

        # Lower case on purpose: a currency is a code, not a spelling.
        report = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            as_of=date(2026, 6, 1),
            currency="usd",
        )

        assert report.currency == "USD"
        assert [row.party_id for row in report.rows] == [dollars.vendor_id]
        assert report.total_outstanding == Decimal("118000.00")
        assert report.other_currencies == ["INR"]

    async def test_every_outstanding_row_states_the_currency_it_is_owed_in(self, ctx) -> None:
        session, tenant, principal = ctx
        await self._open_payable(session, tenant, principal, unit_price="500", currency="INR")
        await self._open_payable(session, tenant, principal, unit_price="1000", currency="USD")

        # The payables list is documents rather than an aggregate: nothing is
        # summed, so every denomination is returned - each row saying which.
        rows, _ = await service.list_outstanding(
            session, tenant, params=PageParams(page=1, limit=50)
        )
        assert sorted(row.currency for row in rows) == ["INR", "USD"]

        # Narrowing it is how the list is reconciled against the ageing report.
        rows, pagination = await service.list_outstanding(
            session, tenant, params=PageParams(page=1, limit=50), currency="usd"
        )
        assert pagination.total == 1
        assert rows[0].currency == "USD"
        assert rows[0].outstanding_amount == Decimal("118000.00")

    async def test_a_dollar_receipt_does_not_settle_a_rupee_debt(self, ctx) -> None:
        session, tenant, principal = ctx
        # 1,000 charged in rupees and 500 in dollars, then 800 dollars received.
        # Netted across currencies the receipt would eat 800 of the rupee debt
        # and report 700 outstanding; netted per currency it settles the dollar
        # charge and leaves the rupee debt untouched.
        await self._customer_ledger(
            session,
            tenant,
            principal,
            charges=[(date(2026, 4, 1), "1000", "INR"), (date(2026, 4, 1), "500", "USD")],
            receipt=("800", "USD"),
        )

        rupees = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert rupees.currency == "INR"
        assert rupees.rows[0].total_outstanding == Decimal("1000.00")
        assert rupees.rows[0].bucket_90_plus == Decimal("1000.00")
        assert rupees.total_outstanding == Decimal("1000.00")
        # Nothing is owed in dollars any more, so nothing was left out.
        assert rupees.other_currencies == []

        dollars = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.CUSTOMER,
            as_of=date(2026, 7, 1),
            currency="USD",
        )
        assert dollars.rows == []
        assert dollars.total_outstanding == Decimal("0.00")
        assert dollars.other_currencies == ["INR"]

    async def test_an_unsettled_foreign_charge_is_named_not_absorbed(self, ctx) -> None:
        session, tenant, principal = ctx
        await self._customer_ledger(
            session,
            tenant,
            principal,
            charges=[(date(2026, 6, 1), "1000", "INR"), (date(2026, 6, 1), "500", "USD")],
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert report.total_outstanding == Decimal("1000.00")
        assert report.other_currencies == ["USD"]

    async def test_a_payables_book_kept_entirely_in_another_currency_is_not_empty(
        self, ctx
    ) -> None:
        """The state the narrow fix reached: real debt, reported as none.

        ``companies.currency`` is INR here and every open bill is in USD. No
        write path in this backend derives an invoice's currency from that
        column, so the two disagreeing is not a corrupt database - it is a
        company that imports. Drawing the report in the company's currency and
        stopping hands it a confident, empty page.
        """
        session, tenant, principal = ctx
        dollars = await self._open_payable(
            session, tenant, principal, unit_price="1000", currency="USD"
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.VENDOR, as_of=date(2026, 6, 1)
        )

        assert report.currency == "USD", "the report answered in a currency with no debt in it"
        assert [row.party_id for row in report.rows] == [dollars.vendor_id]
        assert report.total_outstanding == Decimal("118000.00")
        # Nothing was left out, because nothing was narrowed away.
        assert report.other_currencies == []
        assert [block.currency for block in report.currencies] == ["USD"]

    async def test_a_receivable_book_kept_entirely_in_another_currency_is_not_empty(
        self, ctx
    ) -> None:
        """The same defect on the ledger side, where the netting is FIFO."""
        session, tenant, principal = ctx
        customer = await self._customer_ledger(
            session,
            tenant,
            principal,
            charges=[(date(2026, 6, 1), "500", "USD")],
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.CUSTOMER, as_of=date(2026, 7, 1)
        )

        assert report.currency == "USD"
        assert [row.party_id for row in report.rows] == [customer.id]
        assert report.total_outstanding == Decimal("500.00")
        assert report.rows[0].bucket_0_30 == Decimal("500.00")

    async def test_the_ranking_runs_inside_a_denomination_not_across_them(self, ctx) -> None:
        """A dollar figure must not be wedged between two rupee ones.

        On the raw numbers the order would be Import (118,000 USD), the larger
        rupee vendor (59,000) and then the smaller (11,800) - a ranking built on
        a comparison that has no meaning. Each block is ranked on its own.
        """
        session, tenant, principal = ctx
        larger = await self._open_payable(
            session, tenant, principal, unit_price="500", currency="INR"
        )
        smaller = await self._open_payable(
            session, tenant, principal, unit_price="100", currency="INR"
        )
        dollars = await self._open_payable(
            session, tenant, principal, unit_price="1000", currency="USD"
        )

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.VENDOR, as_of=date(2026, 6, 1)
        )

        blocks = {block.currency: block for block in report.currencies}
        assert [row.party_id for row in blocks["INR"].rows] == [
            larger.vendor_id,
            smaller.vendor_id,
        ]
        assert [row.party_id for row in blocks["USD"].rows] == [dollars.vendor_id]
        assert blocks["INR"].total_outstanding == Decimal("70800.00")
        assert blocks["USD"].total_outstanding == Decimal("118000.00")
        # 70,800 + 118,000. No figure anywhere may be this number.
        mixed = Decimal("188800.00")
        assert all(block.total_outstanding != mixed for block in report.currencies)
        assert report.total_outstanding != mixed

    async def test_narrowing_chooses_what_is_restated_not_what_is_covered(self, ctx) -> None:
        """Asking for dollars must not lose sight of a rupee debt."""
        session, tenant, principal = ctx
        await self._open_payable(session, tenant, principal, unit_price="500", currency="INR")
        await self._open_payable(session, tenant, principal, unit_price="1000", currency="USD")

        report = await service.ageing_report(
            session,
            tenant,
            party_type=PartyType.VENDOR,
            as_of=date(2026, 6, 1),
            currency="USD",
        )

        blocks = {block.currency: block for block in report.currencies}
        assert report.currency == "USD"
        assert report.currencies[0].currency == "USD"
        assert blocks["INR"].total_outstanding == Decimal("59000.00")
        assert blocks["INR"].rows[0].currency == "INR"

    async def test_a_block_says_how_many_parties_are_behind_its_totals(self, ctx) -> None:
        """``limit`` caps rows per denomination; the totals stay the block's."""
        session, tenant, principal = ctx
        larger = await self._open_payable(
            session, tenant, principal, unit_price="500", currency="INR"
        )
        await self._open_payable(session, tenant, principal, unit_price="100", currency="INR")

        report = await service.ageing_report(
            session, tenant, party_type=PartyType.VENDOR, as_of=date(2026, 6, 1), limit=1
        )

        assert [row.party_id for row in report.rows] == [larger.vendor_id]
        assert report.currencies[0].party_count == 2
        assert report.total_outstanding == Decimal("70800.00"), (
            "the totals came from the capped rows rather than the whole block"
        )


class TestDocumentCurrencyDefault:
    """Where a document's denomination comes from when nobody names one.

    The ageing report defaults to ``companies.currency``; until now no write
    path did, so the report's idea of "the ordinary denomination" and the
    documents' could differ with nothing keeping them in step. They now come
    from one function.
    """

    async def test_an_invoice_with_no_currency_takes_the_companys(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)
        payload = invoice_payload(fixture).model_copy(update={"currency": None})

        detail = await service.create_invoice(session, tenant, payload)

        assert detail.currency == await service.company_currency(session, tenant)

    async def test_a_payment_with_no_currency_takes_the_companys(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=fixture.vendor.id,
                payment_date=date(2026, 5, 1),
                amount=Decimal("100"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )

        assert payment.currency == await service.company_currency(session, tenant)

    async def test_a_company_keeping_books_in_another_currency_gets_that_one(self, ctx) -> None:
        """The point of the change: change the column, and writes follow it.

        A module constant would have gone on minting INR invoices for a company
        whose books - and whose ageing report - are in dollars.
        """
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)
        await session.execute(
            text("UPDATE companies SET currency = 'USD' WHERE id = :id"),
            {"id": tenant.company_id},
        )
        accounts = await service.ensure_system_accounts(session, tenant)

        detail = await service.create_invoice(
            session, tenant, invoice_payload(fixture).model_copy(update={"currency": None})
        )
        entries = await service.post_journal(
            session,
            tenant,
            [
                service.JournalLine(
                    account_id=accounts[str(SystemAccount.ACCOUNTS_PAYABLE)].id,
                    credit=Decimal("100"),
                ),
                service.JournalLine(
                    account_id=accounts[str(SystemAccount.PURCHASE_EXPENSE)].id,
                    debit=Decimal("100"),
                ),
            ],
            entry_date=date(2026, 5, 1),
            reference_type="manual_journal",
            reference_code=_code("JV"),
        )

        assert detail.currency == "USD"
        assert {entry.currency for entry in entries} == {"USD"}

    async def test_an_explicit_currency_is_still_honoured(self, ctx) -> None:
        session, tenant, _ = ctx
        fixture = await seed(session, tenant.company_id)

        detail = await service.create_invoice(
            session, tenant, invoice_payload(fixture, currency="USD")
        )

        assert detail.currency == "USD"
