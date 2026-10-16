from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity
from src.modules.audit.service import record_status_change
from src.modules.finance.credit_note_repository import CreditNoteRepository
from src.modules.finance.credit_note_schemas import (
    CreditNoteCreate,
    CreditNoteDetail,
    CreditNoteLineRead,
    CreditNoteSummary,
)
from src.modules.finance.models import (
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    PartyType,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
    SystemAccount,
)
from src.modules.finance.service import (
    JournalLine,
    company_currency,
    ensure_system_accounts,
    post_journal,
    reverse_journal,
)

CREDIT_NOTE_LIFECYCLE: StateMachine[CreditNoteStatus] = StateMachine(
    entity="Credit note",
    initial=CreditNoteStatus.ISSUED,
    transitions=(
        Transition(
            CreditNoteStatus.ISSUED,
            CreditNoteStatus.CANCELLED,
            permission=Permission.CREDIT_NOTE_CANCEL,
            label="Cancel",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset(),
)


def _detail(row: CreditNote, lines: list[CreditNoteLine]) -> CreditNoteDetail:
    return CreditNoteDetail(
        **CreditNoteSummary.model_validate(row).model_dump(),
        lines=[CreditNoteLineRead.model_validate(line) for line in lines],
    )


async def _lines(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID
) -> list[CreditNoteLine]:
    stmt = (
        select(CreditNoteLine)
        .where(
            CreditNoteLine.company_id == tenant.company_id,
            CreditNoteLine.credit_note_id == note_id,
        )
        .order_by(CreditNoteLine.created_at, CreditNoteLine.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_credit_note(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID
) -> CreditNoteDetail:
    row = (
        await session.execute(
            select(CreditNote).where(
                CreditNote.company_id == tenant.company_id,
                CreditNote.id == note_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Credit note", note_id)
    return _detail(row, await _lines(session, tenant, row.id))


async def list_credit_notes(
    session: AsyncSession, tenant: TenantContext, params: PageParams, sort: SortParams
) -> tuple[list[CreditNoteSummary], OffsetPagination]:
    repo = CreditNoteRepository(session, tenant)
    rows, page = await repo.list_page(
        params,
        stmt=repo._scoped(),
        order_by=sort.resolve(repo.sortable_fields, CreditNote.note_date.desc()),
    )
    return [CreditNoteSummary.model_validate(row) for row in rows], page


def _tax_parts(
    taxable: Decimal, source: SalesInvoiceLine
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if source.taxable_amount <= ZERO:
        return ZERO, ZERO, ZERO, ZERO
    ratio = taxable / source.taxable_amount
    cgst = to_money(source.cgst_amount * ratio)
    sgst = to_money(source.sgst_amount * ratio)
    igst = to_money(source.igst_amount * ratio)
    return cgst, sgst, igst, to_money(cgst + sgst + igst)


def sales_invoice_status_after_credit(
    paid_amount: Decimal, credited_amount: Decimal, total_amount: Decimal
) -> SalesInvoiceStatus:
    if paid_amount + credited_amount >= total_amount:
        return SalesInvoiceStatus.CREDITED
    if paid_amount > ZERO:
        return SalesInvoiceStatus.PARTIALLY_PAID
    return SalesInvoiceStatus.ISSUED


async def issue_credit_note(
    session: AsyncSession, tenant: TenantContext, payload: CreditNoteCreate
) -> CreditNoteDetail:
    async with transaction(session):
        invoice = (
            await session.execute(
                select(SalesInvoice)
                .where(
                    SalesInvoice.company_id == tenant.company_id,
                    SalesInvoice.id == payload.sales_invoice_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise NotFoundError("Sales invoice", payload.sales_invoice_id)
        if invoice.status not in (
            SalesInvoiceStatus.ISSUED,
            SalesInvoiceStatus.PARTIALLY_PAID,
        ):
            raise BusinessRuleError(
                "Credit notes are supported only for issued or partially paid invoices."
            )
        if invoice.currency != await company_currency(session, tenant):
            raise BusinessRuleError("The invoice currency does not match the company currency.")

        line_ids = [line.sales_invoice_line_id for line in payload.lines]
        source_lines = list(
            (
                await session.execute(
                    select(SalesInvoiceLine)
                    .where(
                        SalesInvoiceLine.company_id == tenant.company_id,
                        SalesInvoiceLine.sales_invoice_id == invoice.id,
                        SalesInvoiceLine.id.in_(line_ids),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        by_id = {line.id: line for line in source_lines}
        if len(by_id) != len(set(line_ids)):
            raise BusinessRuleError("Every credit-note line must belong to the selected invoice.")

        computed: list[
            tuple[SalesInvoiceLine, Decimal | None, Decimal, Decimal, Decimal, Decimal, Decimal]
        ] = []
        taxable_total = ZERO
        tax_total = ZERO
        for requested in payload.lines:
            source = by_id[requested.sales_invoice_line_id]
            if requested.quantity is not None:
                if source.quantity <= ZERO:
                    raise BusinessRuleError("The invoice line has no creditable quantity.")
                if source.credited_quantity + requested.quantity > source.quantity:
                    raise BusinessRuleError("Credit quantity exceeds the invoice line balance.")
                taxable = to_money(source.taxable_amount * requested.quantity / source.quantity)
            elif requested.taxable_amount is not None:
                taxable = to_money(requested.taxable_amount)
            else:
                raise BusinessRuleError("Each credit-note line needs quantity or taxable amount.")
            if source.credited_amount + taxable > source.taxable_amount:
                raise BusinessRuleError("Credit amount exceeds the invoice line balance.")
            cgst, sgst, igst, tax = _tax_parts(taxable, source)
            computed.append((source, requested.quantity, taxable, cgst, sgst, igst, tax))
            taxable_total += taxable
            tax_total += tax

        total = to_money(taxable_total + tax_total)
        if total <= ZERO or invoice.paid_amount + total > invoice.total_amount:
            raise BusinessRuleError("The credit note exceeds the invoice receivable balance.")
        if payload.reason_type.value == "RETURN" and payload.sales_return_id is None:
            raise BusinessRuleError("A return credit note must reference a sales return.")

        accounts = await ensure_system_accounts(session, tenant)
        journal = await post_journal(
            session,
            tenant,
            [
                JournalLine(
                    account_id=accounts[str(SystemAccount.SALES_REVENUE)].id,
                    debit=taxable_total,
                ),
                JournalLine(
                    account_id=accounts[str(SystemAccount.OUTPUT_TAX)].id,
                    debit=tax_total,
                ),
                JournalLine(
                    account_id=accounts[str(SystemAccount.ACCOUNTS_RECEIVABLE)].id,
                    credit=total,
                    party_type=PartyType.CUSTOMER,
                    party_id=invoice.customer_id,
                ),
            ],
            entry_date=payload.note_date,
            reference_type="CREDIT_NOTE",
            reference_code=None,
            narration=f"Credit note against {invoice.code}",
            currency=invoice.currency,
        )
        row = CreditNote(
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            code=await next_document_number(session, tenant.company_id, DocTypes.CREDIT_NOTE),
            sales_invoice_id=invoice.id,
            sales_return_id=payload.sales_return_id,
            customer_id=invoice.customer_id,
            note_date=payload.note_date,
            reason_type=payload.reason_type,
            status=CreditNoteStatus.ISSUED,
            currency=invoice.currency,
            subtotal=taxable_total,
            taxable_amount=taxable_total,
            cgst_amount=sum(item[3] for item in computed),
            sgst_amount=sum(item[4] for item in computed),
            igst_amount=sum(item[5] for item in computed),
            tax_amount=tax_total,
            round_off_amount=ZERO,
            total_amount=total,
            buyer_name=invoice.buyer_name,
            buyer_gstin=invoice.buyer_gstin,
            seller_name=invoice.seller_name,
            seller_gstin=invoice.seller_gstin,
            seller_address=invoice.seller_address,
            billing_address=invoice.billing_address,
            place_of_supply_state_code=invoice.place_of_supply_state_code,
            is_interstate=invoice.is_interstate,
            issued_at=utcnow(),
            issued_by_id=tenant.user_id,
            journal_id=journal[0].journal_id,
        )
        session.add(row)
        await session.flush()
        for source, quantity, taxable, cgst, sgst, igst, tax in computed:
            session.add(
                CreditNoteLine(
                    company_id=tenant.company_id,
                    created_by_id=tenant.user_id,
                    credit_note_id=row.id,
                    sales_invoice_line_id=source.id,
                    item_id=source.item_id,
                    hsn_code=source.hsn_code,
                    quantity=to_quantity(quantity) if quantity is not None else None,
                    taxable_amount=taxable,
                    tax_rate=source.tax_rate,
                    cgst_amount=cgst,
                    sgst_amount=sgst,
                    igst_amount=igst,
                    tax_amount=tax,
                    line_total=to_money(taxable + tax),
                )
            )
            source.credited_quantity += quantity or ZERO
            source.credited_amount += taxable
        credited_total = await session.scalar(
            select(func.coalesce(func.sum(CreditNote.total_amount), 0)).where(
                CreditNote.company_id == tenant.company_id,
                CreditNote.sales_invoice_id == invoice.id,
                CreditNote.status == CreditNoteStatus.ISSUED,
            )
        )
        credited_total = Decimal(str(credited_total or ZERO))
        invoice.status = (
            SalesInvoiceStatus.CREDITED
            if invoice.paid_amount + credited_total >= invoice.total_amount
            else sales_invoice_status_after_credit(
                invoice.paid_amount, credited_total, invoice.total_amount
            )
        )
        await session.flush()
    return await get_credit_note(session, tenant, row.id)


async def cancel_credit_note(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID, reason: str
) -> CreditNoteDetail:
    async with transaction(session):
        row = (
            await session.execute(
                select(CreditNote)
                .where(
                    CreditNote.company_id == tenant.company_id,
                    CreditNote.id == note_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Credit note", note_id)
        CREDIT_NOTE_LIFECYCLE.assert_transition(row.status, CreditNoteStatus.CANCELLED)
        invoice = (
            await session.execute(
                select(SalesInvoice)
                .where(
                    SalesInvoice.company_id == tenant.company_id,
                    SalesInvoice.id == row.sales_invoice_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        await reverse_journal(
            session,
            tenant,
            row.journal_id,
            reason=reason,
        )
        note_lines = await _lines(session, tenant, row.id)
        source_lines = list(
            (
                await session.execute(
                    select(SalesInvoiceLine)
                    .where(
                        SalesInvoiceLine.company_id == tenant.company_id,
                        SalesInvoiceLine.id.in_(
                            [line.sales_invoice_line_id for line in note_lines]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        source_by_id = {line.id: line for line in source_lines}
        for line in note_lines:
            source = source_by_id[line.sales_invoice_line_id]
            source.credited_quantity -= line.quantity or ZERO
            source.credited_amount -= line.taxable_amount
        previous = row.status
        row.status = CreditNoteStatus.CANCELLED
        row.cancelled_at = utcnow()
        row.cancellation_reason = reason
        row.cancelled_by_id = tenant.user_id
        credited_total = await session.scalar(
            select(func.coalesce(func.sum(CreditNote.total_amount), 0)).where(
                CreditNote.company_id == tenant.company_id,
                CreditNote.sales_invoice_id == invoice.id,
                CreditNote.status == CreditNoteStatus.ISSUED,
            )
        )
        credited_total = Decimal(str(credited_total or ZERO))
        invoice.status = (
            SalesInvoiceStatus.CREDITED
            if invoice.paid_amount + credited_total >= invoice.total_amount
            else sales_invoice_status_after_credit(
                invoice.paid_amount, credited_total, invoice.total_amount
            )
        )
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type="credit_note",
            entity_id=row.id,
            entity_code=row.code,
            from_status=str(previous),
            to_status=str(row.status),
            user_id=tenant.user_id,
            reason=reason,
        )
        await session.flush()
    return await get_credit_note(session, tenant, note_id)
