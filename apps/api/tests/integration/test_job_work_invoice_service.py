"""Job-work vendor invoices against a real PostgreSQL.

A JOB_WORK invoice is a vendor invoice matched to a job-work order and its
receipt lines rather than to a purchase order and a goods receipt. Approving
one posts the processing charge to ``JOB_WORK_EXPENSE`` (not
``PURCHASE_EXPENSE``), moves the receipt lines' ``invoiced_amount``, and posts
the excess-loss recovery beside the bill. Cancelling one reverses both journals
and rolls both amounts back - and the same reversal now holds for a purchase
bill cancelled after a dispute, which used to cancel without reversing
anything.

Isolation follows ``test_finance_service.py``: one private schema for the
module, created once and dropped afterwards, with every test's writes rolled
back.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import src.modules.users.models  # noqa: F401 - registers `users`
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.finance import service
from src.modules.finance.models import (
    LedgerEntry,
    MatchStatus,
    PartyType,
    PaymentMethod,
    PaymentType,
    SystemAccount,
    VendorInvoice,
    VendorInvoiceStatus,
    VendorInvoiceType,
)
from src.modules.finance.repository import LedgerAccountRepository, LedgerEntryRepository
from src.modules.finance.schemas import (
    AllocationRequest,
    InvoiceApproval,
    PaymentAllocationIn,
    PaymentCreate,
    VendorInvoiceCreate,
    VendorInvoiceFilters,
    VendorInvoiceLineIn,
)
from src.modules.job_work import service as job_work_service
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkReceipt,
    JobWorkReceiptLine,
    RateBasis,
)
from src.modules.organization.models import CostCentre, CostCentreType
from src.modules.vendors.models import VendorType
from tests import seed_support
from tests.schema_support import report_database_setup_failure

pytestmark = pytest.mark.integration

for _module_path, _table in (
    ("src.modules.inventory.models", "inventory_balances"),
    ("src.modules.procurement.models", "purchase_orders"),
    ("src.modules.products.models", "items"),
    ("src.modules.warehouses.models", "warehouses"),
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
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    schema = f"it_job_work_invoice_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
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
                await seed_support.seed_company(session, company_id=company_id, prefix="JW")
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

    yield schema, company_id, user_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, Principal]]:
    schema, company_id, user_id = _schema
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


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Shop:
    """One plater, one lever, and an issued order with one receipt behind it."""

    def __init__(self) -> None:
        self.vendor_id: uuid.UUID
        self.item_id: uuid.UUID
        self.uom_id: uuid.UUID
        self.order_id: uuid.UUID
        self.receipt_line_id: uuid.UUID
        self.cost_centre_id: uuid.UUID | None = None


async def seed_shop(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    charges: str = "1073.00",
    vendor_debit: str = "250.00",
    output_quantity: str = "58",
    with_cost_centre: bool = False,
) -> Shop:
    """An issued job-work order with a single receipt line carrying charges."""
    shop = Shop()
    vendor = await seed_support.seed_vendor(
        session, company_id=company_id, vendor_types=(VendorType.JOB_WORK,)
    )
    uom = await seed_support.seed_uom(session, company_id=company_id)
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=_code("LEVER"),
        base_uom_id=uom.id,
        hsn_code="9988",
        gst_rate=Decimal("18"),
    )
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=_code("W"))
    shop.vendor_id, shop.item_id, shop.uom_id = vendor.id, item.id, uom.id

    if with_cost_centre:
        centre = CostCentre(
            company_id=company_id,
            code=_code("CC"),
            name="Plating",
            cost_centre_type=CostCentreType.PRODUCTION,
        )
        session.add(centre)
        await session.flush()
        shop.cost_centre_id = centre.id

    order = JobWorkOrder(
        company_id=company_id,
        code=_code("JW"),
        vendor_id=vendor.id,
        process_type=JobWorkProcess.COATING,
        expected_return_date=date(2026, 5, 20),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        cost_centre_id=shop.cost_centre_id,
        rate_per_unit=Decimal("18.50"),
        rate_basis=RateBasis.PER_PIECE,
        estimated_charges=Decimal(charges),
        status=JobWorkOrderStatus.ISSUED,
        tax_rate=Decimal("18"),
        sac_code="9988",
    )
    session.add(order)
    await session.flush()
    shop.order_id = order.id

    order_line = JobWorkOrderLine(
        company_id=company_id,
        job_work_order_id=order.id,
        line_number=1,
        item_id=item.id,
        uom_id=uom.id,
        quantity_to_issue=Decimal(output_quantity),
        expected_output_item_id=item.id,
        expected_output_quantity=Decimal(output_quantity),
    )
    receipt = JobWorkReceipt(
        company_id=company_id,
        code=_code("R"),
        receipt_number=1,
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        receipt_date=date(2026, 5, 10),
        total_output_quantity=Decimal(output_quantity),
        total_input_consumed=Decimal(output_quantity),
        total_charges=Decimal(charges),
        vendor_debit_amount=Decimal(vendor_debit),
        reason="Plated levers received back and counted",
    )
    session.add_all([order_line, receipt])
    await session.flush()

    receipt_line = JobWorkReceiptLine(
        company_id=company_id,
        receipt_id=receipt.id,
        job_work_order_line_id=order_line.id,
        line_number=1,
        output_item_id=item.id,
        output_quantity=Decimal(output_quantity),
        input_consumed_quantity=Decimal(output_quantity),
        charges=Decimal(charges),
        vendor_debit_amount=Decimal(vendor_debit),
    )
    session.add(receipt_line)
    await session.flush()
    shop.receipt_line_id = receipt_line.id
    return shop


def jw_payload(
    shop: Shop,
    *,
    quantity: str = "58",
    unit_price: str = "18.50",
    number: str | None = None,
) -> VendorInvoiceCreate:
    return VendorInvoiceCreate(
        vendor_id=shop.vendor_id,
        vendor_invoice_number=number or _code("CHR"),
        invoice_date=date(2026, 5, 12),
        currency="INR",
        invoice_type=VendorInvoiceType.JOB_WORK,
        job_work_order_id=shop.order_id,
        lines=[
            VendorInvoiceLineIn(
                job_work_receipt_line_id=shop.receipt_line_id,
                description="Antique brass plating - lever sets",
                hsn_code="9988",
                quantity=Decimal(quantity),
                unit_price=Decimal(unit_price),
                tax_rate=Decimal("18"),
            )
        ],
    )


async def approved_jw_invoice(session, tenant, principal, shop, **kwargs):
    detail = await service.create_invoice(session, tenant, jw_payload(shop, **kwargs))
    await service.submit_invoice(session, tenant, detail.id, reason=None)
    await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())
    invoice = await session.get(VendorInvoice, detail.id)
    assert invoice is not None
    return invoice


async def _entries(session, tenant, reference_type: str, reference_id: uuid.UUID):
    return await LedgerEntryRepository(session, tenant).entries_for_reference(
        reference_type, reference_id
    )


class TestApprovePostsExpenseTrackingAndRecovery:
    async def test_create_submit_and_approve(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)

        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        assert invoice.status == VendorInvoiceStatus.APPROVED.value
        assert invoice.match_status == MatchStatus.MATCHED.value
        assert invoice.total_amount == Decimal("1266.1400")
        assert invoice.recovered_amount == Decimal("250.0000")

        posted = await _entries(session, tenant, service.REFERENCE_VENDOR_INVOICE, invoice.id)
        names = await _account_names(session, tenant, posted)
        debits = {names[e.account_id]: e.debit_amount for e in posted if e.debit_amount}
        credits = {names[e.account_id]: e.credit_amount for e in posted if e.credit_amount}
        assert debits["Job Work Charges"] == Decimal("1073.0000")
        assert debits["Input GST Credit"] == Decimal("193.1400")
        assert credits["Accounts Payable"] == Decimal("1266.1400")
        assert "Purchases and Services" not in debits

        recovery = await _entries(session, tenant, service.REFERENCE_JOB_WORK_RECOVERY, invoice.id)
        assert len(recovery) == 2
        names = await _account_names(session, tenant, recovery)
        assert {(names[e.account_id], e.debit_amount, e.credit_amount) for e in recovery} == {
            ("Accounts Payable", Decimal("250.0000"), Decimal("0.0000")),
            ("Job Work Charges", Decimal("0.0000"), Decimal("250.0000")),
        }

        line = await session.get(JobWorkReceiptLine, shop.receipt_line_id)
        assert line is not None
        assert line.invoiced_amount == Decimal("1073.0000")
        assert line.debit_posted_amount == Decimal("250.0000")

    async def test_the_expense_account_is_provisioned_at_a_free_code(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)

        await approved_jw_invoice(session, tenant, principal, shop)

        accounts = await LedgerAccountRepository(session, tenant).by_system_codes(
            [SystemAccount.JOB_WORK_EXPENSE]
        )
        assert accounts[str(SystemAccount.JOB_WORK_EXPENSE)].code == "5150"

    async def test_expense_splits_by_effective_cost_centre(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id, with_cost_centre=True)

        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        posted = await _entries(session, tenant, service.REFERENCE_VENDOR_INVOICE, invoice.id)
        names = await _account_names(session, tenant, posted)
        expense_legs = [entry for entry in posted if names[entry.account_id] == "Job Work Charges"]
        assert len(expense_legs) == 1
        assert expense_legs[0].cost_centre_id == shop.cost_centre_id

    async def test_a_second_invoice_cannot_over_bill(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)

        await approved_jw_invoice(session, tenant, principal, shop)

        with pytest.raises(BusinessRuleError, match="unbilled"):
            await service.create_invoice(session, tenant, jw_payload(shop, quantity="1"))

    async def test_an_over_bill_at_approval_is_refused_despite_an_override(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)

        # Two drafts, each fine against the unbilled charges on its own.
        first = await service.create_invoice(
            session, tenant, jw_payload(shop, quantity="40", number=_code("CHR"))
        )
        second = await service.create_invoice(
            session, tenant, jw_payload(shop, quantity="58", number=_code("CHR"))
        )
        await service.submit_invoice(session, tenant, first.id, reason=None)
        await service.submit_invoice(session, tenant, second.id, reason=None)
        await service.approve_invoice(session, tenant, principal, first.id, InvoiceApproval())

        with pytest.raises(BusinessRuleError, match="already been billed"):
            await service.approve_invoice(
                session,
                tenant,
                principal,
                second.id,
                InvoiceApproval(override_reason="The vendor insists on the full lot charge"),
            )

    async def test_a_foreign_or_other_vendor_order_is_refused(self, ctx) -> None:
        session, tenant, _ = ctx
        shop = await seed_shop(session, tenant.company_id)
        other_vendor = await seed_support.seed_vendor(
            session, company_id=tenant.company_id, vendor_types=(VendorType.JOB_WORK,)
        )

        payload = jw_payload(shop)
        payload.vendor_id = other_vendor.id
        with pytest.raises(BusinessRuleError, match="another vendor"):
            await service.create_invoice(session, tenant, payload)

        payload = jw_payload(shop)
        payload.job_work_order_id = uuid.uuid4()
        with pytest.raises(NotFoundError):
            await service.create_invoice(session, tenant, payload)

    async def test_billable_lines_lists_only_the_unbilled(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)

        before = await job_work_service.billable_lines(session, tenant, shop.order_id)
        assert len(before) == 1
        assert before[0].unbilled_amount == Decimal("1073.0000")
        assert before[0].pending_vendor_debit == Decimal("250.0000")
        assert before[0].rate_per_unit == Decimal("18.500000")
        assert before[0].tax_rate == Decimal("18.0000")
        assert before[0].sac_code == "9988"

        await approved_jw_invoice(session, tenant, principal, shop, quantity="40")

        after = await job_work_service.billable_lines(session, tenant, shop.order_id)
        assert len(after) == 1
        assert after[0].unbilled_amount == Decimal("333.0000")

    async def test_the_list_filter_selects_job_work_bills(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)
        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        rows, _ = await service.list_invoices(
            session,
            tenant,
            filters=VendorInvoiceFilters(invoice_type=VendorInvoiceType.JOB_WORK),
            params=PageParams(limit=50),
            sort=SortParams(),
        )
        assert [row.id for row in rows] == [invoice.id]
        assert rows[0].job_work_order_id == shop.order_id

        rows, _ = await service.list_invoices(
            session,
            tenant,
            filters=VendorInvoiceFilters(invoice_type=VendorInvoiceType.PURCHASE),
            params=PageParams(limit=50),
            sort=SortParams(),
        )
        assert all(row.id != invoice.id for row in rows)


class TestCancelReversesAndRollsBack:
    async def test_cancelling_an_approved_job_work_invoice(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)
        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        cancelled = await service.cancel_invoice(
            session, tenant, invoice.id, reason="Duplicate bill from the plater"
        )

        assert cancelled.status is VendorInvoiceStatus.CANCELLED
        assert cancelled.recovered_amount == Decimal("0.0000")

        for reference in (
            service.REFERENCE_VENDOR_INVOICE,
            service.REFERENCE_JOB_WORK_RECOVERY,
        ):
            posted = await _entries(session, tenant, reference, invoice.id)
            originals = [e for e in posted if not e.is_reversal]
            reversals = [e for e in posted if e.is_reversal]
            assert originals and reversals
            debit = sum((e.debit_amount for e in posted), Decimal("0"))
            credit = sum((e.credit_amount for e in posted), Decimal("0"))
            assert debit == credit

        line = await session.get(JobWorkReceiptLine, shop.receipt_line_id)
        assert line is not None
        assert line.invoiced_amount == Decimal("0.0000")
        assert line.debit_posted_amount == Decimal("0.0000")

    async def test_dispute_then_cancel_reverses_a_purchase_bill(self, ctx) -> None:
        """The reported defect: APPROVED -> DISPUTED -> CANCELLED kept the journal."""
        session, tenant, principal = ctx
        fixture = await _seed_purchase(session, tenant.company_id)

        detail = await service.create_invoice(session, tenant, _po_payload(fixture))
        await service.submit_invoice(session, tenant, detail.id, reason=None)
        await service.approve_invoice(session, tenant, principal, detail.id, InvoiceApproval())
        await service.dispute_invoice(session, tenant, detail.id, reason="Rate under dispute")
        cancelled = await service.cancel_invoice(
            session, tenant, detail.id, reason="Vendor withdrew the bill"
        )

        assert cancelled.status is VendorInvoiceStatus.CANCELLED
        posted = await _entries(session, tenant, service.REFERENCE_VENDOR_INVOICE, detail.id)
        assert [e for e in posted if not e.is_reversal]
        assert [e for e in posted if e.is_reversal]

    async def test_dispute_then_cancel_reverses_a_job_work_bill(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)
        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        await service.dispute_invoice(session, tenant, invoice.id, reason="Short delivery claimed")
        cancelled = await service.cancel_invoice(
            session, tenant, invoice.id, reason="Settled for the delivered quantity"
        )

        assert cancelled.status is VendorInvoiceStatus.CANCELLED
        recovery = await _entries(session, tenant, service.REFERENCE_JOB_WORK_RECOVERY, invoice.id)
        assert [e for e in recovery if e.is_reversal]
        line = await session.get(JobWorkReceiptLine, shop.receipt_line_id)
        assert line is not None
        assert line.invoiced_amount == Decimal("0.0000")
        assert line.debit_posted_amount == Decimal("0.0000")


class TestRecoveryReducesWhatIsOwed:
    """The recovery already reduced the payable in the ledger, so every
    what-is-owed read is net of it (docs/assumptions.md §B3-2).

    The default shop bills 1073.00 in charges (1266.14 with GST) and recovers
    250.00 of excess-loss debit, so the net payable is 1016.14. Before this
    fix the summary, the outstanding list and the allocation bound all read
    the gross 1266.14, and the payment dialog's prefill guided the operator
    to overpay the vendor by exactly the recovery.
    """

    async def test_outstanding_is_net_of_the_recovery(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)
        invoice = await approved_jw_invoice(session, tenant, principal, shop)

        assert service.invoice_summary(invoice).outstanding_amount == Decimal("1016.14")

        rows, _ = await service.list_outstanding(
            session, tenant, params=PageParams(limit=50), vendor_id=shop.vendor_id
        )
        assert [row.invoice_id for row in rows] == [invoice.id]
        assert rows[0].outstanding_amount == Decimal("1016.14")

    async def test_allocation_is_capped_at_the_net_payable(self, ctx) -> None:
        session, tenant, principal = ctx
        shop = await seed_shop(session, tenant.company_id)
        invoice = await approved_jw_invoice(session, tenant, principal, shop)
        invoice_id = invoice.id

        payment = await service.create_payment(
            session,
            tenant,
            PaymentCreate(
                payment_type=PaymentType.PAYMENT,
                party_type=PartyType.VENDOR,
                party_id=shop.vendor_id,
                payment_date=date(2026, 5, 14),
                amount=Decimal("2000"),
                payment_method=PaymentMethod.BANK_TRANSFER,
            ),
        )
        await service.approve_payment(session, tenant, principal, payment.id)

        with pytest.raises(BusinessRuleError, match=r"owes 1016\.14"):
            await service.allocate_payment(
                session,
                tenant,
                payment.id,
                AllocationRequest(
                    allocations=[
                        PaymentAllocationIn(vendor_invoice_id=invoice_id, amount=Decimal("1266.14"))
                    ]
                ),
            )

        await service.allocate_payment(
            session,
            tenant,
            payment.id,
            AllocationRequest(
                allocations=[
                    PaymentAllocationIn(vendor_invoice_id=invoice_id, amount=Decimal("1016.14"))
                ]
            ),
        )
        await session.refresh(invoice)
        assert invoice.paid_amount == Decimal("1016.1400")
        assert VendorInvoiceStatus(invoice.status) is VendorInvoiceStatus.PAID


async def _account_names(
    session: AsyncSession, tenant: TenantContext, entries: list[LedgerEntry]
) -> dict[uuid.UUID, str]:
    from src.modules.finance.models import LedgerAccount

    ids = {entry.account_id for entry in entries}
    rows = (
        await session.execute(
            select(LedgerAccount.id, LedgerAccount.name).where(LedgerAccount.id.in_(ids))
        )
    ).all()
    return {row.id: row.name for row in rows}


class _Purchase:
    def __init__(self) -> None:
        self.vendor_id: uuid.UUID
        self.item_id: uuid.UUID
        self.uom_id: uuid.UUID
        self.order_id: uuid.UUID
        self.order_line_id: uuid.UUID
        self.receipt_id: uuid.UUID
        self.receipt_line_id: uuid.UUID


async def _seed_purchase(session: AsyncSession, company_id: uuid.UUID) -> _Purchase:
    """A vendor, an item, an order for it and the receipt that took it in."""
    from src.modules.procurement.models import (
        GoodsReceipt,
        GoodsReceiptLine,
        PurchaseOrder,
        PurchaseOrderLine,
    )
    from src.modules.warehouses.models import WarehouseType

    bought = _Purchase()
    vendor = await seed_support.seed_vendor(session, company_id=company_id)
    uom = await seed_support.seed_uom(session, company_id=company_id)
    item = await seed_support.seed_item(
        session, company_id=company_id, code=_code("RM"), base_uom_id=uom.id
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=company_id,
        code=_code("W"),
        warehouse_type=WarehouseType.RAW_MATERIAL,
    )
    bought.vendor_id, bought.item_id, bought.uom_id = vendor.id, item.id, uom.id

    order = PurchaseOrder(
        company_id=company_id,
        code=_code("PO"),
        vendor_id=vendor.id,
        po_date=date(2026, 4, 1),
        warehouse_id=warehouse.id,
    )
    session.add(order)
    await session.flush()
    bought.order_id = order.id

    order_line = PurchaseOrderLine(
        company_id=company_id,
        purchase_order_id=order.id,
        line_number=1,
        item_id=item.id,
        quantity=Decimal("100"),
        uom_id=uom.id,
        unit_price=Decimal("25"),
        discount=Decimal("0"),
        received_quantity=Decimal("100"),
        accepted_quantity=Decimal("100"),
    )
    receipt = GoodsReceipt(
        company_id=company_id,
        code=_code("GRN"),
        purchase_order_id=order.id,
        vendor_id=vendor.id,
        warehouse_id=warehouse.id,
        receipt_date=date(2026, 4, 5),
    )
    session.add_all([order_line, receipt])
    await session.flush()
    bought.order_line_id = order_line.id
    bought.receipt_id = receipt.id

    receipt_line = GoodsReceiptLine(
        company_id=company_id,
        goods_receipt_id=receipt.id,
        line_number=1,
        purchase_order_line_id=order_line.id,
        item_id=item.id,
        received_quantity=Decimal("100"),
        accepted_quantity=Decimal("100"),
        uom_id=uom.id,
        unit_cost=Decimal("25"),
    )
    session.add(receipt_line)
    await session.flush()
    bought.receipt_line_id = receipt_line.id
    return bought


def _po_payload(bought: _Purchase) -> VendorInvoiceCreate:
    return VendorInvoiceCreate(
        vendor_id=bought.vendor_id,
        vendor_invoice_number=_code("INV"),
        invoice_date=date(2026, 4, 6),
        currency="INR",
        purchase_order_id=bought.order_id,
        goods_receipt_id=bought.receipt_id,
        lines=[
            VendorInvoiceLineIn(
                item_id=bought.item_id,
                purchase_order_line_id=bought.order_line_id,
                goods_receipt_line_id=bought.receipt_line_id,
                quantity=Decimal("100"),
                uom_id=bought.uom_id,
                unit_price=Decimal("25"),
                tax_rate=Decimal("18"),
            )
        ],
    )
