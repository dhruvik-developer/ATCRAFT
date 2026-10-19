"""Issue immutable GST invoices, post receivables and reverse cancellations."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.currency import assert_customer_currency
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError, ValidationError
from src.core.ids import new_id
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.customers.schemas import OutstandingAdjustment
from src.modules.customers.service import apply_outstanding_delta, get_customer_for_order
from src.modules.dispatch.models import DispatchStatus, DispatchType
from src.modules.finance.models import (
    CreditNote,
    CreditNoteStatus,
    PartyType,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
    SystemAccount,
)
from src.modules.finance.repository import FinancialYearRepository
from src.modules.finance.sales_invoice_repository import (
    SalesInvoiceRepository,
    SalesInvoiceSourceRepository,
)
from src.modules.finance.sales_invoice_schemas import (
    SalesInvoiceCreate,
    SalesInvoiceDetail,
    SalesInvoiceFilters,
    SalesInvoiceLineRead,
    SalesInvoicePreview,
    SalesInvoiceSummary,
)
from src.modules.finance.service import (
    JournalLine,
    company_currency,
    ensure_system_accounts,
    post_journal,
    reverse_journal,
)
from src.modules.sales.models import SalesOrderLineStatus, SalesOrderStatus
from src.modules.sales.repository import SalesOrderRepository
from src.modules.sales.service import _transition as transition_order
from src.modules.sales.service import compute_line_amounts

log = get_logger(__name__)
# ASSUMPTION: mirrors the e-invoice cancellation window (§A2-4).
INVOICE_CANCEL_WINDOW_HOURS = 24
# ASSUMPTION: nearest rupee, HALF_UP (§A2-5).
ROUND_INVOICE_TOTAL_TO = Decimal("1")
SALES_INVOICE_LIFECYCLE: StateMachine[SalesInvoiceStatus] = StateMachine(
    entity="Sales invoice",
    initial=SalesInvoiceStatus.ISSUED,
    transitions=(
        Transition(
            SalesInvoiceStatus.ISSUED,
            SalesInvoiceStatus.PARTIALLY_PAID,
            permission=Permission.PAYMENT_RECORD,
            label="Receipt allocated",
        ),
        Transition(
            SalesInvoiceStatus.ISSUED,
            SalesInvoiceStatus.CREDITED,
            permission=Permission.CREDIT_NOTE_CREATE,
            label="Credited",
        ),
        Transition(
            SalesInvoiceStatus.ISSUED,
            SalesInvoiceStatus.PAID,
            permission=Permission.PAYMENT_RECORD,
            label="Paid",
        ),
        Transition(
            SalesInvoiceStatus.PARTIALLY_PAID,
            SalesInvoiceStatus.PAID,
            permission=Permission.PAYMENT_RECORD,
            label="Paid",
        ),
        Transition(
            SalesInvoiceStatus.PARTIALLY_PAID,
            SalesInvoiceStatus.CREDITED,
            permission=Permission.CREDIT_NOTE_CREATE,
            label="Credited",
        ),
        Transition(
            SalesInvoiceStatus.PAID,
            SalesInvoiceStatus.CREDITED,
            permission=Permission.CREDIT_NOTE_CREATE,
            label="Credited",
        ),
        Transition(
            SalesInvoiceStatus.PARTIALLY_PAID,
            SalesInvoiceStatus.ISSUED,
            permission=Permission.PAYMENT_RECORD,
            label="Receipt released",
        ),
        Transition(
            SalesInvoiceStatus.PAID,
            SalesInvoiceStatus.PARTIALLY_PAID,
            permission=Permission.PAYMENT_RECORD,
            label="Receipt released",
        ),
        Transition(
            SalesInvoiceStatus.PAID,
            SalesInvoiceStatus.ISSUED,
            permission=Permission.PAYMENT_RECORD,
            label="Receipt released",
        ),
        Transition(
            SalesInvoiceStatus.CREDITED,
            SalesInvoiceStatus.ISSUED,
            permission=Permission.CREDIT_NOTE_CANCEL,
            label="Credit cancelled",
        ),
        Transition(
            SalesInvoiceStatus.CREDITED,
            SalesInvoiceStatus.PARTIALLY_PAID,
            permission=Permission.CREDIT_NOTE_CANCEL,
            label="Credit cancelled",
        ),
        Transition(
            SalesInvoiceStatus.CREDITED,
            SalesInvoiceStatus.PAID,
            permission=Permission.CREDIT_NOTE_CANCEL,
            label="Credit cancelled",
        ),
        Transition(
            SalesInvoiceStatus.ISSUED,
            SalesInvoiceStatus.CANCELLED,
            permission=Permission.SALES_INVOICE_CANCEL,
            label="Cancel",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({SalesInvoiceStatus.CANCELLED, SalesInvoiceStatus.CREDITED}),
)


def sales_invoice_status_after_payment(
    *, total_amount: Decimal, paid_amount: Decimal, credited_amount: Decimal = ZERO
) -> SalesInvoiceStatus:
    if paid_amount + credited_amount >= total_amount:
        return SalesInvoiceStatus.PAID if paid_amount > ZERO else SalesInvoiceStatus.CREDITED
    if paid_amount > ZERO:
        return SalesInvoiceStatus.PARTIALLY_PAID
    return SalesInvoiceStatus.ISSUED


async def issued_credit_total(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(CreditNote.total_amount), 0)).where(
            CreditNote.company_id == tenant.company_id,
            CreditNote.sales_invoice_id == invoice_id,
            CreditNote.status == CreditNoteStatus.ISSUED,
        )
    )
    return Decimal(str(value or ZERO))


def apportion_discount(
    *,
    total_discount: Decimal,
    ordered: Decimal,
    quantity: Decimal,
    invoiced_before: Decimal,
    discount_before: Decimal,
) -> Decimal:
    """Last invoice takes the exact remainder, after paisa rounding earlier shares."""
    if invoiced_before + quantity >= ordered:
        return to_money(max(total_discount - discount_before, ZERO))
    return to_money(total_discount * quantity / ordered)


def sales_invoice_journal_lines(
    invoice: SalesInvoice, accounts: dict[str, Any]
) -> list[JournalLine]:
    lines = [
        JournalLine(
            account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
            debit=invoice.total_amount,
            party_type=PartyType.CUSTOMER,
            party_id=invoice.customer_id,
        )
    ]
    if invoice.taxable_amount:
        lines.append(
            JournalLine(
                account_id=accounts[str(SystemAccount.SALES_REVENUE)].id,
                credit=invoice.taxable_amount,
            )
        )
    if invoice.tax_amount:
        lines.append(
            JournalLine(
                account_id=accounts[str(SystemAccount.OUTPUT_TAX)].id, credit=invoice.tax_amount
            )
        )
    if invoice.round_off_amount:
        lines.append(
            JournalLine(
                account_id=accounts[str(SystemAccount.ROUND_OFF)].id,
                debit=-invoice.round_off_amount if invoice.round_off_amount < ZERO else ZERO,
                credit=invoice.round_off_amount if invoice.round_off_amount > ZERO else ZERO,
            )
        )
    return lines


def _address(row: Any) -> str:
    if row is None:
        return ""
    return ", ".join(
        str(value)
        for field in ("address_line1", "address_line2", "city", "state", "postal_code", "country")
        if (value := getattr(row, field, None))
    )


async def preview_sales_invoice(
    session: AsyncSession, tenant: TenantContext, payload: SalesInvoiceCreate, *, lock: bool = False
) -> SalesInvoicePreview:
    source = SalesInvoiceSourceRepository(session, tenant)
    repo = SalesInvoiceRepository(session, tenant)
    dispatch = await source.dispatch(payload.dispatch_id)
    if dispatch.customer_id is None:
        raise BusinessRuleError("A sales invoice requires a customer SALES dispatch.")
    customer = await get_customer_for_order(session, tenant, dispatch.customer_id, lock=lock)
    dispatch = await source.dispatch(payload.dispatch_id, lock=lock)
    if dispatch.dispatch_type != DispatchType.SALES or dispatch.status in (
        DispatchStatus.NOT_DISPATCHED,
        DispatchStatus.CANCELLED,
    ):
        raise BusinessRuleError("Only a SALES dispatch that is ready or has left can be invoiced.")
    if dispatch.sales_order_id is None:
        # ASSUMPTION: no agreed pricing exists for an unlinked dispatch (§A2-7).
        raise BusinessRuleError(
            "Link the SALES dispatch to its priced sales order before raising an invoice."
        )
    if await repo.live_for_dispatch(dispatch.id):
        raise BusinessRuleError("This dispatch already has a live sales invoice.")
    order = await source.order(dispatch.sales_order_id, lock=lock)
    assert_customer_currency(order.currency, await company_currency(session, tenant))
    order_lines = await source.order_lines(order.id, lock=lock)
    by_id = {line.id: line for line in order_lines}
    dispatch_lines = await source.dispatch_lines(dispatch.id)
    date = payload.invoice_date or dispatch.dispatch_date
    if date is None:
        date = today()
    if date < order.order_date:
        raise ValidationError("The invoice date cannot be before the sales-order date.")
    year = await FinancialYearRepository(session, tenant).containing(date)
    if year is not None and year.is_closed:
        raise BusinessRuleError("The invoice date belongs to a closed financial year.")
    facts = await source.item_facts([line.item_id for line in dispatch_lines])
    units = await source.units([line.uom_id for line in order_lines])
    if dispatch.status == DispatchStatus.READY_FOR_DISPATCH:
        from src.modules.dispatch.service import open_dispatch_quantities
        from src.modules.sales.service import dispatchable_quantity, live_reservations

        live = await live_reservations(session, tenant, order_lines)
        planned = await open_dispatch_quantities(
            session, tenant, order.id, exclude_dispatch_id=dispatch.id
        )
        for dl in dispatch_lines:
            ol = by_id.get(dl.sales_order_line_id) if dl.sales_order_line_id else None
            if ol is None:
                raise ValidationError(
                    f"Line {dl.line_number}: the dispatch needs sales-order linkage."
                )
            cap = dispatchable_quantity(
                ordered=Decimal(ol.quantity),
                dispatched=Decimal(ol.dispatched_quantity),
                reserved=live.get(ol.reservation_id, ZERO) if ol.reservation_id else ZERO,
                planned=planned.get(ol.id, ZERO),
            )
            if Decimal(dl.quantity) > cap:
                raise BusinessRuleError(
                    f"Line {dl.line_number}: its staged goods no longer have a live reservation. "
                    "Allocate stock before issuing."
                )

    discounts = await repo.previous_discount(list(by_id))
    lines: list[SalesInvoiceLineRead] = []
    for dl in dispatch_lines:
        ol = by_id.get(dl.sales_order_line_id) if dl.sales_order_line_id else None
        if ol is None or ol.item_id != dl.item_id:
            raise ValidationError(
                f"Dispatch line {dl.line_number} has no matching priced sales-order line."
            )
        qty = to_quantity(Decimal(dl.quantity) - Decimal(getattr(dl, "returned_quantity", ZERO)))
        if qty <= ZERO:
            continue
        allowance = Decimal(ol.dispatched_quantity)
        if dispatch.status == DispatchStatus.READY_FOR_DISPATCH:
            # ASSUMPTION: pre-removal invoice uses staged goods as its cap.
            # See docs/assumptions.md §A2-8.
            allowance += qty
        if Decimal(ol.invoiced_quantity) + qty > min(Decimal(ol.quantity), allowance):
            raise BusinessRuleError(
                f"Line {ol.line_number}: invoicing would exceed the goods "
                "dispatched or staged for this dispatch."
            )
        discount = apportion_discount(
            total_discount=Decimal(ol.discount_amount),
            ordered=Decimal(ol.quantity),
            quantity=qty,
            invoiced_before=Decimal(ol.invoiced_quantity),
            discount_before=discounts.get(ol.id, ZERO),
        )
        amounts = compute_line_amounts(
            quantity=qty,
            unit_price=Decimal(ol.unit_price),
            tax_rate=Decimal(ol.tax_rate),
            is_interstate=order.is_interstate,
            discount_amount=discount,
        )
        lines.append(
            SalesInvoiceLineRead(
                id=dl.id,
                dispatch_line_id=dl.id,
                sales_order_line_id=ol.id,
                item_id=ol.item_id,
                uom_id=ol.uom_id,
                line_number=len(lines) + 1,
                description=ol.description or facts[ol.item_id][0],
                hsn_code=facts[ol.item_id][1],
                uom_code=units[ol.uom_id],
                quantity=qty,
                credited_quantity=ZERO,
                credited_amount=ZERO,
                unit_price=Decimal(ol.unit_price),
                tax_rate=Decimal(ol.tax_rate),
                subtotal=amounts.gross_amount,
                discount_amount=amounts.discount_amount,
                taxable_amount=amounts.taxable_amount,
                cgst_amount=amounts.cgst_amount,
                sgst_amount=amounts.sgst_amount,
                igst_amount=amounts.igst_amount,
                tax_amount=amounts.tax_amount,
                line_total=amounts.line_total,
            )
        )
    if not lines:
        raise BusinessRuleError("This dispatch has no uninvoiced goods remaining.")
    company, branch = await source.seller(order.branch_id)
    addresses = await source.addresses(
        [value for value in (order.billing_address_id, order.shipping_address_id) if value]
    )
    billing = addresses.get(order.billing_address_id) if order.billing_address_id else None
    shipping = addresses.get(order.shipping_address_id) if order.shipping_address_id else None
    totals = {
        field: to_money(sum((getattr(line, field) for line in lines), ZERO))
        for field in (
            "subtotal",
            "discount_amount",
            "taxable_amount",
            "cgst_amount",
            "sgst_amount",
            "igst_amount",
            "tax_amount",
        )
    }
    raw_total = totals["taxable_amount"] + totals["tax_amount"]
    total = raw_total.quantize(ROUND_INVOICE_TOTAL_TO, rounding=ROUND_HALF_UP)
    if total <= ZERO:
        raise BusinessRuleError(
            "The invoice total must remain positive after rounding. "
            "Review its agreed prices and discounts."
        )
    return SalesInvoicePreview(
        dispatch_id=dispatch.id,
        sales_order_id=order.id,
        customer_id=customer.id,
        invoice_date=date,
        due_date=date + timedelta(days=customer.credit_days),
        currency=order.currency,
        is_interstate=order.is_interstate,
        place_of_supply_state_code=order.place_of_supply_state_code,
        seller_name=company.legal_name or company.name,
        seller_gstin=(branch.gstin if branch else None) or company.gstin,
        seller_address=_address(branch or company),
        seller_state_code=(branch.state_code if branch else None) or company.state_code,
        seller_state_name=(branch.state if branch else None) or company.state,
        buyer_name=customer.legal_name or customer.name,
        buyer_gstin=(billing.gstin if billing else None) or customer.gstin,
        buyer_state_code=(billing.state_code if billing else None)
        or (customer.gstin[:2] if customer.gstin else None)
        or (shipping.state_code if shipping else None),
        buyer_state_name=(billing.state if billing else None)
        or (shipping.state if shipping else None),
        billing_address=_address(billing or shipping or customer),
        shipping_address=_address(shipping or billing or customer),
        transporter_name=payload.transporter_name
        or await source.transporter_name(dispatch.transporter_id)
        or dispatch.courier_name
        or dispatch.transport_name,
        vehicle_number=payload.vehicle_number or dispatch.vehicle_number,
        lr_number=payload.lr_number or dispatch.lr_number,
        eway_bill_number=payload.eway_bill_number or dispatch.eway_bill_number,
        notes=payload.notes,
        bank_name=company.bank_name,
        bank_account_name=company.bank_account_name,
        bank_account_number=company.bank_account_number,
        bank_ifsc=company.bank_ifsc,
        bank_branch=company.bank_branch,
        invoice_terms=company.invoice_terms,
        **totals,
        round_off_amount=to_money(total - raw_total),
        total_amount=to_money(total),
        lines=lines,
    )


async def transition_sales_invoice(
    session: AsyncSession,
    tenant: TenantContext,
    invoice: SalesInvoice,
    target: SalesInvoiceStatus,
    *,
    reason: str,
) -> None:
    current = SalesInvoiceStatus(invoice.status)
    if current is target:
        return
    SALES_INVOICE_LIFECYCLE.assert_transition(current, target)
    invoice.status = target
    SalesInvoiceRepository(session, tenant).touch(invoice)
    await session.flush()
    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type="sales_invoice",
        entity_id=invoice.id,
        entity_code=invoice.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        reason=reason,
    )


async def issue_sales_invoice(
    session: AsyncSession, tenant: TenantContext, payload: SalesInvoiceCreate
) -> SalesInvoiceDetail:
    repo = SalesInvoiceRepository(session, tenant)
    async with transaction(session):
        preview = await preview_sales_invoice(session, tenant, payload, lock=True)
        code = await next_document_number(
            session, tenant.company_id, DocTypes.SALES_INVOICE, on=preview.invoice_date
        )
        # ASSUMPTION: number allocation and issue share one transaction (§A2-2).
        invoice = repo.add(
            SalesInvoice(
                **preview.model_dump(exclude={"lines"}),
                code=code,
                status=SalesInvoiceStatus.ISSUED,
                issued_at=utcnow(),
                issued_by_id=tenant.user_id,
                paid_amount=ZERO,
                journal_id=new_id(),
            )
        )
        await session.flush()
        for data in preview.lines:
            line = SalesInvoiceLine(
                **data.model_dump(),
                sales_invoice_id=invoice.id,
                company_id=tenant.company_id,
                created_by_id=tenant.user_id,
            )
            session.add(line)
        source = SalesInvoiceSourceRepository(session, tenant)
        order = await source.order(invoice.sales_order_id, lock=True)
        order_lines = await source.order_lines(order.id, lock=True)
        by_id = {line.id: line for line in order_lines}
        for data in preview.lines:
            ol = by_id[data.sales_order_line_id]
            ol.invoiced_quantity = to_quantity(Decimal(ol.invoiced_quantity) + data.quantity)
        accounts = await ensure_system_accounts(session, tenant)
        await post_journal(
            session,
            tenant,
            sales_invoice_journal_lines(invoice, accounts),
            entry_date=invoice.invoice_date,
            reference_type="sales_invoice",
            reference_id=invoice.id,
            reference_code=invoice.code,
            currency=invoice.currency,
            journal_id=invoice.journal_id,
            narration=f"Tax invoice {invoice.code}",
        )
        await apply_outstanding_delta(
            session,
            tenant,
            invoice.customer_id,
            OutstandingAdjustment(
                delta=invoice.total_amount,
                reason=f"Invoice {invoice.code}",
                reference_type="sales_invoice",
                reference_id=invoice.id,
                reference_code=invoice.code,
            ),
        )
        if order.status == SalesOrderStatus.DISPATCHED and all(
            line.status == SalesOrderLineStatus.CANCELLED or line.invoiced_quantity >= line.quantity
            for line in order_lines
        ):
            await transition_order(
                session,
                tenant,
                order,
                SalesOrderStatus.INVOICED,
                reason=f"Fully invoiced by {invoice.code}",
                repo=SalesOrderRepository(session, tenant),
            )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=f"Issued tax invoice {invoice.code}",
            user_id=tenant.user_id,
            new_values={"dispatch_id": invoice.dispatch_id, "total_amount": invoice.total_amount},
        )
    try:
        await queue_sales_invoice_pdf(session, tenant, invoice.id)
    except AppError:
        # Issue has committed. A failed job is visible and may be retried from detail.
        log.error("sales_invoice.automatic_pdf_failed", invoice_id=str(invoice.id))
    return await get_sales_invoice(session, tenant, invoice.id)


def _summary(
    invoice: SalesInvoice, blocked: bool, credited_amount: Decimal = ZERO
) -> SalesInvoiceSummary:
    balance = to_money(invoice.total_amount - invoice.paid_amount - credited_amount)
    within_window = utcnow() - invoice.issued_at <= timedelta(hours=INVOICE_CANCEL_WINDOW_HOURS)
    return SalesInvoiceSummary(
        **{
            field: getattr(invoice, field)
            for field in SalesInvoiceSummary.model_fields
            if field not in {"balance_amount", "overdue_days", "can_cancel"}
        },
        balance_amount=balance,
        overdue_days=max((today() - invoice.due_date).days, 0)
        if balance > ZERO and invoice.status != SalesInvoiceStatus.CANCELLED
        else 0,
        can_cancel=invoice.status == SalesInvoiceStatus.ISSUED
        and invoice.paid_amount == ZERO
        and within_window
        and not blocked,
    )


async def list_sales_invoices(
    session: AsyncSession,
    tenant: TenantContext,
    filters: SalesInvoiceFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[SalesInvoiceSummary], OffsetPagination]:
    repo = SalesInvoiceRepository(session, tenant)
    rows, pagination = await repo.list_filtered(filters, params, sort)
    ids = [row.id for row in rows]
    credited_rows = await session.execute(
        select(CreditNote.sales_invoice_id, func.sum(CreditNote.total_amount))
        .where(
            CreditNote.company_id == tenant.company_id,
            CreditNote.sales_invoice_id.in_(ids),
            CreditNote.status == CreditNoteStatus.ISSUED,
        )
        .group_by(CreditNote.sales_invoice_id)
    )
    credited = {invoice_id: Decimal(str(total or ZERO)) for invoice_id, total in credited_rows}
    blocked = (
        await repo.allocation_ids(ids)
        | await repo.credited_ids(ids)
        | await repo.closed_order_ids(ids)
    )
    return [
        _summary(row, row.id in blocked, credited.get(row.id, ZERO)) for row in rows
    ], pagination


async def get_sales_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> SalesInvoiceDetail:
    repo = SalesInvoiceRepository(session, tenant)
    invoice = await repo.get_or_404(invoice_id)
    payments = await repo.payments(invoice.id)
    blocked = (
        bool(payments)
        or bool(await repo.credited_ids([invoice.id]))
        or bool(await repo.closed_order_ids([invoice.id]))
    )
    credited_amount = await issued_credit_total(session, tenant, invoice.id)
    values = {
        field: getattr(invoice, field)
        for field in SalesInvoiceDetail.model_fields
        if field not in {"lines", "payments", "balance_amount", "overdue_days", "can_cancel"}
    }
    values.update(_summary(invoice, blocked, credited_amount).model_dump())
    return SalesInvoiceDetail(
        **values,
        lines=[SalesInvoiceLineRead.model_validate(line) for line in await repo.lines(invoice.id)],
        payments=payments,
    )


async def cancel_sales_invoice(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID, *, reason: str
) -> SalesInvoiceDetail:
    repo = SalesInvoiceRepository(session, tenant)
    source = SalesInvoiceSourceRepository(session, tenant)
    initial = await repo.get_or_404(invoice_id)
    customer_id, dispatch_id = initial.customer_id, initial.dispatch_id
    async with transaction(session):
        await get_customer_for_order(session, tenant, customer_id, lock=True)
        await source.dispatch(dispatch_id, lock=True)
        invoice = await repo.lock_header(invoice_id)
        # Legacy foreign invoices require reconciliation before a currency-less
        # customer credit delta can be reversed (ADDITIONAL11-2).
        assert_customer_currency(invoice.currency, await company_currency(session, tenant))
        if (
            invoice.status != SalesInvoiceStatus.ISSUED
            or await repo.allocation_ids([invoice.id])
            or await repo.credited_ids([invoice.id])
            or utcnow() - invoice.issued_at > timedelta(hours=INVOICE_CANCEL_WINDOW_HOURS)
        ):
            raise BusinessRuleError(
                "This invoice cannot be cancelled. Issue a credit note instead."
            )
        order = await source.order(invoice.sales_order_id, lock=True)
        if order.status == SalesOrderStatus.CLOSED:
            raise BusinessRuleError("This sales order is closed. Issue a credit note instead.")
        order_lines = await source.order_lines(order.id, lock=True)
        lines = await repo.lines(invoice.id)
        by_id = {line.id: line for line in order_lines}
        for line in lines:
            ol = by_id[line.sales_order_line_id]
            ol.invoiced_quantity = to_quantity(
                Decimal(ol.invoiced_quantity) - Decimal(line.quantity)
            )
        year = await FinancialYearRepository(session, tenant).containing(invoice.invoice_date)
        entries = await reverse_journal(
            session,
            tenant,
            invoice.journal_id,
            reason=f"Invoice cancelled: {reason}",
            entry_date=today() if year and year.is_closed else invoice.invoice_date,
        )
        invoice.reversal_journal_id = entries[0].journal_id
        invoice.cancelled_at, invoice.cancelled_by_id, invoice.cancellation_reason = (
            utcnow(),
            tenant.user_id,
            reason,
        )
        await transition_sales_invoice(
            session, tenant, invoice, SalesInvoiceStatus.CANCELLED, reason=reason
        )
        await apply_outstanding_delta(
            session,
            tenant,
            invoice.customer_id,
            OutstandingAdjustment(
                delta=-invoice.total_amount,
                reason=f"Invoice {invoice.code} cancelled",
                reference_type="sales_invoice",
                reference_id=invoice.id,
                reference_code=invoice.code,
            ),
        )
        if order.status == SalesOrderStatus.INVOICED:
            await transition_order(
                session,
                tenant,
                order,
                SalesOrderStatus.DISPATCHED,
                reason=f"Invoice {invoice.code} cancelled",
                repo=SalesOrderRepository(session, tenant),
            )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CANCEL,
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=f"Cancelled tax invoice {invoice.code}",
            user_id=tenant.user_id,
            context={"reason": reason},
        )
    return await get_sales_invoice(session, tenant, invoice.id)


async def sales_invoice_print_data(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> dict[str, Any]:
    return (await get_sales_invoice(session, tenant, invoice_id)).model_dump(mode="json")


async def queue_sales_invoice_pdf(
    session: AsyncSession, tenant: TenantContext, invoice_id: uuid.UUID
) -> str:
    from src.modules.reports import service as reports_service
    from src.modules.reports.models import ReportKey
    from src.modules.reports.repository import ReportSnapshotRepository

    invoice = await SalesInvoiceRepository(session, tenant).get_or_404(invoice_id)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.SALES_INVOICE, {"sales_invoice_id": str(invoice.id)}, now=utcnow()
        )
        # Statutory invoice document jobs never expire (§A2-6).
        job.expires_at = None
        ReportSnapshotRepository(session, tenant).add(job)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_code=invoice.code,
            summary=f"Queued PDF for {invoice.code}",
            user_id=tenant.user_id,
            context={"job_id": job.id},
        )
    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name="reports.generate_sales_invoice",
        queue="reports",
        task_kwargs={"sales_invoice_id": str(invoice.id)},
        subject="The sales invoice PDF",
    )
    return str(job.id)


async def allocate_sales_receipt(
    session: AsyncSession, tenant: TenantContext, payment: Any, payload: Any
) -> None:
    if any(row.vendor_invoice_id is not None for row in payload.allocations):
        raise ValidationError("A customer receipt must allocate to sales invoices.")
    requested = {row.sales_invoice_id: to_money(row.amount) for row in payload.allocations}
    notes = {row.sales_invoice_id: row.notes for row in payload.allocations}
    await _set_sales_receipt_allocations(
        session, tenant, payment, requested, notes, replace=payload.replace
    )


async def release_sales_receipt(session: AsyncSession, tenant: TenantContext, payment: Any) -> None:
    await _set_sales_receipt_allocations(session, tenant, payment, {}, {}, replace=True)


async def _set_sales_receipt_allocations(
    session: AsyncSession,
    tenant: TenantContext,
    payment: Any,
    requested: dict[uuid.UUID, Decimal],
    notes: dict[uuid.UUID, str | None],
    *,
    replace: bool,
) -> None:
    from src.modules.finance.models import PaymentAllocation
    from src.modules.finance.repository import PaymentAllocationRepository, PaymentRepository
    from src.modules.finance.service import (
        _transition_payment,
        payment_status_after_allocation,
        plan_allocation,
    )

    repo = SalesInvoiceRepository(session, tenant)
    allocations = PaymentAllocationRepository(session, tenant)
    existing = {
        row.sales_invoice_id: row
        for row in await allocations.list_for_payment(payment.id)
        if row.sales_invoice_id is not None
    }
    amounts = {key: to_money(row.amount) for key, row in existing.items()}
    plan = plan_allocation(existing=amounts, requested=requested, replace=replace)
    if plan.total > to_money(payment.amount):
        raise BusinessRuleError("Allocations exceed the receipt amount.")
    affected = set(plan.final) | set(amounts)
    invoices = {row.id: row for row in await repo.lock_many(sorted(affected, key=str))}
    if affected - set(invoices):
        from src.core.errors import NotFoundError

        raise NotFoundError("Sales invoice", next(iter(affected - set(invoices))))
    credited_rows = await session.execute(
        select(CreditNote.sales_invoice_id, func.sum(CreditNote.total_amount))
        .where(
            CreditNote.company_id == tenant.company_id,
            CreditNote.sales_invoice_id.in_(affected),
            CreditNote.status == CreditNoteStatus.ISSUED,
        )
        .group_by(CreditNote.sales_invoice_id)
    )
    credited = {invoice_id: Decimal(str(total or ZERO)) for invoice_id, total in credited_rows}
    for key, amount in requested.items():
        invoice = invoices[key]
        if invoice.customer_id != payment.party_id:
            raise BusinessRuleError("The sales invoice belongs to a different customer.")
        if invoice.currency != payment.currency:
            raise BusinessRuleError("The receipt and invoice must use the same currency.")
        if invoice.status == SalesInvoiceStatus.CANCELLED or (
            invoice.status == SalesInvoiceStatus.PAID and key not in amounts
        ):
            raise BusinessRuleError(
                "Only issued or partially paid sales invoices can receive an allocation."
            )
        if amount > to_money(
            invoice.total_amount
            - invoice.paid_amount
            - credited.get(invoice.id, ZERO)
            + amounts.get(key, ZERO)
        ):
            raise BusinessRuleError(f"{invoice.code}: allocation exceeds its outstanding balance.")
    for key in affected:
        invoice = invoices[key]
        invoice.paid_amount = to_money(
            invoice.paid_amount - amounts.get(key, ZERO) + plan.final.get(key, ZERO)
        )
    if replace:
        await allocations.delete_for_payment(payment.id)
        existing = {}
    for key, amount in requested.items():
        if key in existing:
            existing[key].amount, existing[key].notes = amount, notes.get(key)
        else:
            allocations.add(
                PaymentAllocation(
                    payment_id=payment.id, sales_invoice_id=key, amount=amount, notes=notes.get(key)
                )
            )
    await session.flush()
    for key in sorted(affected, key=str):
        invoice = invoices[key]
        await transition_sales_invoice(
            session,
            tenant,
            invoice,
            sales_invoice_status_after_payment(
                total_amount=invoice.total_amount,
                paid_amount=invoice.paid_amount,
                credited_amount=credited.get(invoice.id, ZERO),
            ),
            reason=f"Receipt allocation {payment.code}",
        )
    payment.allocated_amount = plan.total
    PaymentRepository(session, tenant).touch(payment)
    await _transition_payment(
        session,
        tenant,
        payment,
        payment_status_after_allocation(
            amount=payment.amount, allocated_amount=payment.allocated_amount
        ),
        reason="Sales invoice allocations changed",
    )
    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.PAYMENT,
        entity_type="payment",
        entity_id=payment.id,
        entity_code=payment.code,
        user_id=tenant.user_id,
        summary=f"Allocated receipt {payment.code}",
        new_values={"allocated_amount": payment.allocated_amount},
    )
