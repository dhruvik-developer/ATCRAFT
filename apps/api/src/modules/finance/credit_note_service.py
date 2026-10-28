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
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.customers.schemas import OutstandingAdjustment
from src.modules.customers.service import apply_outstanding_delta
from src.modules.finance.credit_cost_service import snapshot_return_credit_costs
from src.modules.finance.credit_note_repository import CreditNoteRepository
from src.modules.finance.credit_note_rounding import (
    CreditedLineTotals,
    credit_round_off,
    credit_tax_parts,
    quantity_credit,
)
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
from src.modules.finance.sales_invoice_service import (
    issued_credit_total,
    sales_invoice_status_after_payment,
    transition_sales_invoice,
)
from src.modules.finance.service import (
    JournalLine,
    company_currency,
    ensure_system_accounts,
    post_journal,
    reverse_journal,
)
from src.modules.quality.models import QcInspection
from src.modules.sales_returns.crediting import accepted_return_quantity
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine, SalesReturnStatus

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


async def queue_credit_note_pdf(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID
) -> str:
    from src.modules.reports import service as reports_service
    from src.modules.reports.models import ReportKey
    from src.modules.reports.repository import ReportSnapshotRepository

    note = await session.scalar(
        select(CreditNote).where(
            CreditNote.company_id == tenant.company_id,
            CreditNote.id == note_id,
        )
    )
    if note is None:
        raise NotFoundError("Credit note", note_id)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.CREDIT_NOTE, {"credit_note_id": str(note.id)}, now=utcnow()
        )
        job.expires_at = None
        ReportSnapshotRepository(session, tenant).add(job)
        await session.flush()
    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name="reports.generate_credit_note",
        queue="reports",
        task_kwargs={"credit_note_id": str(note.id)},
        subject="The credit note PDF",
    )
    return str(job.id)


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


def sales_invoice_status_after_credit(
    paid_amount: Decimal, credited_amount: Decimal, total_amount: Decimal
) -> SalesInvoiceStatus:
    return sales_invoice_status_after_payment(
        total_amount=total_amount,
        paid_amount=paid_amount,
        credited_amount=credited_amount,
    )


async def _lock_return(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> tuple[SalesReturn, list[SalesReturnLine]]:
    # The return owner locks its document before its invoice. Finance must use
    # the same order so a manual credit cannot deadlock a simultaneous receipt.
    row = await session.scalar(
        select(SalesReturn)
        .where(SalesReturn.company_id == tenant.company_id, SalesReturn.id == return_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("Sales return", return_id)
    lines = list(
        (
            await session.scalars(
                select(SalesReturnLine)
                .where(
                    SalesReturnLine.company_id == tenant.company_id,
                    SalesReturnLine.sales_return_id == return_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return row, lines


async def _issued_return_quantities(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    rows = await session.execute(
        select(SalesInvoiceLine.dispatch_line_id, func.sum(CreditNoteLine.quantity))
        .join(CreditNoteLine, CreditNoteLine.sales_invoice_line_id == SalesInvoiceLine.id)
        .join(CreditNote, CreditNote.id == CreditNoteLine.credit_note_id)
        .where(
            CreditNote.company_id == tenant.company_id,
            CreditNoteLine.company_id == tenant.company_id,
            SalesInvoiceLine.company_id == tenant.company_id,
            CreditNote.sales_return_id == return_id,
            CreditNote.status == CreditNoteStatus.ISSUED,
        )
        .group_by(SalesInvoiceLine.dispatch_line_id)
    )
    return {dispatch_line_id: Decimal(quantity or ZERO) for dispatch_line_id, quantity in rows}


async def _validate_return_credit(
    session: AsyncSession,
    tenant: TenantContext,
    payload: CreditNoteCreate,
    invoice: SalesInvoice,
    return_row: SalesReturn,
    return_lines: list[SalesReturnLine],
    source_by_id: dict[uuid.UUID, SalesInvoiceLine],
) -> dict[uuid.UUID, Decimal]:
    if return_row.status not in (SalesReturnStatus.RECEIVED, SalesReturnStatus.CLOSED):
        raise BusinessRuleError("Only a received sales return can be credited.")
    if (
        return_row.customer_id != invoice.customer_id
        or return_row.dispatch_id != invoice.dispatch_id
    ):
        raise BusinessRuleError(
            "The sales return must belong to the selected invoice and customer."
        )
    if return_row.received_at is None or return_row.received_at < invoice.issued_at:
        # Invoice issue already subtracts quantities physically received before
        # issue. Later QC acceptance must not refund those unbilled goods again.
        raise BusinessRuleError(
            "Only goods returned after invoice issue can receive a return credit."
        )
    by_dispatch = {line.dispatch_line_id: line for line in return_lines}
    inspection_ids = {line.qc_inspection_id for line in return_lines if line.qc_inspection_id}
    inspections = (
        (
            await session.scalars(
                select(QcInspection).where(
                    QcInspection.company_id == tenant.company_id,
                    QcInspection.id.in_(inspection_ids),
                )
            )
        ).all()
        if inspection_ids
        else []
    )
    inspections_by_id = {inspection.id: inspection for inspection in inspections}
    credited = await _issued_return_quantities(session, tenant, return_row.id)
    for requested in payload.lines:
        source = source_by_id[requested.sales_invoice_line_id]
        return_line = by_dispatch.get(source.dispatch_line_id)
        if return_line is None:
            raise BusinessRuleError(
                "Every credit-note line must belong to the selected sales return."
            )
        accepted = accepted_return_quantity(
            return_line,
            inspections_by_id.get(return_line.qc_inspection_id)
            if return_line.qc_inspection_id is not None
            else None,
        )
        quantity = requested.quantity
        if quantity is None or credited.get(source.dispatch_line_id, ZERO) + quantity > accepted:
            raise BusinessRuleError("Credit quantity exceeds the accepted sales-return balance.")
    return credited


async def issue_credit_note(
    session: AsyncSession, tenant: TenantContext, payload: CreditNoteCreate
) -> CreditNoteDetail:
    async with transaction(session):
        return_row = None
        return_lines: list[SalesReturnLine] = []
        if payload.sales_return_id is not None:
            return_row, return_lines = await _lock_return(session, tenant, payload.sales_return_id)
        invoice = (
            await session.execute(
                select(SalesInvoice)
                .where(
                    SalesInvoice.company_id == tenant.company_id,
                    SalesInvoice.id == payload.sales_invoice_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise NotFoundError("Sales invoice", payload.sales_invoice_id)
        if invoice.status in (SalesInvoiceStatus.CANCELLED, SalesInvoiceStatus.CREDITED):
            raise BusinessRuleError(
                "Credit notes are not supported for cancelled or fully credited invoices."
            )
        if invoice.status == SalesInvoiceStatus.PAID and not (
            payload.reason_type.value == "RETURN" and payload.sales_return_id is not None
        ):
            raise BusinessRuleError(
                "A paid invoice can only receive a credit for a linked customer return."
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
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        by_id = {line.id: line for line in source_lines}
        if len(by_id) != len(set(line_ids)):
            raise BusinessRuleError("Every credit-note line must belong to the selected invoice.")
        return_credited: dict[uuid.UUID, Decimal] = {}
        if return_row is not None:
            return_credited = await _validate_return_credit(
                session, tenant, payload, invoice, return_row, return_lines, by_id
            )

        credit_repo = CreditNoteRepository(session, tenant)
        prior_lines = await credit_repo.issued_line_totals(line_ids)
        computed: list[
            tuple[SalesInvoiceLine, Decimal | None, Decimal, Decimal, Decimal, Decimal, Decimal]
        ] = []
        taxable_total = ZERO
        tax_total = ZERO
        for requested in payload.lines:
            source = by_id[requested.sales_invoice_line_id]
            prior = prior_lines.get(source.id, CreditedLineTotals())
            if requested.quantity is not None:
                if source.quantity <= ZERO:
                    raise BusinessRuleError("The invoice line has no creditable quantity.")
                if source.credited_quantity + requested.quantity > source.quantity:
                    raise BusinessRuleError("Credit quantity exceeds the invoice line balance.")
                taxable = quantity_credit(
                    source.quantity, source.taxable_amount, prior, requested.quantity
                )
            elif requested.taxable_amount is not None:
                taxable = to_money(requested.taxable_amount)
            else:
                raise BusinessRuleError("Each credit-note line needs quantity or taxable amount.")
            if source.credited_amount + taxable > source.taxable_amount:
                raise BusinessRuleError("Credit amount exceeds the invoice line balance.")
            cgst, sgst, igst, tax = credit_tax_parts(
                taxable,
                source.taxable_amount,
                (source.cgst_amount, source.sgst_amount, source.igst_amount),
                prior,
            )
            computed.append((source, requested.quantity, taxable, cgst, sgst, igst, tax))
            taxable_total += taxable
            tax_total += tax

        existing_credited_total, issued_round_off = await credit_repo.issued_invoice_totals(
            invoice.id
        )
        round_off = credit_round_off(
            invoice.taxable_amount + invoice.tax_amount,
            invoice.round_off_amount,
            existing_credited_total,
            issued_round_off,
            taxable_total + tax_total,
        )
        total = to_money(taxable_total + tax_total + round_off)
        return_credit = (
            payload.reason_type.value == "RETURN" and payload.sales_return_id is not None
        )
        credit_ceiling = (
            invoice.total_amount
            if invoice.status == SalesInvoiceStatus.PAID or return_credit
            else invoice.total_amount - invoice.paid_amount
        )
        if total <= ZERO or existing_credited_total + total > credit_ceiling:
            raise BusinessRuleError("The credit note exceeds the invoice receivable balance.")
        if payload.reason_type.value == "RETURN" and payload.sales_return_id is None:
            raise BusinessRuleError("A return credit note must reference a sales return.")

        accounts = await ensure_system_accounts(session, tenant)
        journal_lines = [
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
        ]
        if round_off:
            journal_lines.append(
                JournalLine(
                    account_id=accounts[str(SystemAccount.ROUND_OFF)].id,
                    debit=round_off if round_off > ZERO else ZERO,
                    credit=-round_off if round_off < ZERO else ZERO,
                )
            )
        journal = await post_journal(
            session,
            tenant,
            [line for line in journal_lines if line.debit or line.credit],
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
            round_off_amount=round_off,
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
        created_lines: list[CreditNoteLine] = []
        for source, quantity, taxable, cgst, sgst, igst, tax in computed:
            note_line = CreditNoteLine(
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
            session.add(note_line)
            created_lines.append(note_line)
            source.credited_quantity += quantity or ZERO
            source.credited_amount += taxable
            if return_row is not None:
                return_credited[source.dispatch_line_id] = return_credited.get(
                    source.dispatch_line_id, ZERO
                ) + (quantity or ZERO)
        for return_line in return_lines:
            return_line.credited_quantity = return_credited.get(return_line.dispatch_line_id, ZERO)
        await session.flush()
        pending_cost_lines = await snapshot_return_credit_costs(
            session, tenant, row, created_lines, source_lines, return_lines
        )
        credited_total = await issued_credit_total(session, tenant, invoice.id)
        await transition_sales_invoice(
            session,
            tenant,
            invoice,
            sales_invoice_status_after_credit(
                invoice.paid_amount, credited_total, invoice.total_amount
            ),
            reason=f"Credit note {row.code} issued",
        )
        await apply_outstanding_delta(
            session,
            tenant,
            invoice.customer_id,
            OutstandingAdjustment(
                delta=-total,
                reason=f"Credit note {row.code} issued",
                reference_type="credit_note",
                reference_id=row.id,
                reference_code=row.code,
            ),
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="credit_note",
            entity_id=row.id,
            entity_code=row.code,
            summary=f"Issued credit note {row.code}",
            user_id=tenant.user_id,
            context={
                "sales_invoice_id": str(invoice.id),
                "total_amount": str(total),
                "cost_pending_line_ids": pending_cost_lines,
            },
        )
        await session.flush()
    return await get_credit_note(session, tenant, row.id)


async def cancel_credit_note(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID, reason: str
) -> CreditNoteDetail:
    async with transaction(session):
        existing = await session.scalar(
            select(CreditNote).where(
                CreditNote.company_id == tenant.company_id, CreditNote.id == note_id
            )
        )
        if existing is None:
            raise NotFoundError("Credit note", note_id)
        return_lines: list[SalesReturnLine] = []
        if existing.sales_return_id is not None:
            _, return_lines = await _lock_return(session, tenant, existing.sales_return_id)
        row = (
            await session.execute(
                select(CreditNote)
                .where(
                    CreditNote.company_id == tenant.company_id,
                    CreditNote.id == note_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
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
                .execution_options(populate_existing=True)
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
                    .execution_options(populate_existing=True)
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
        await session.flush()
        if row.sales_return_id is not None:
            credited_quantities = await _issued_return_quantities(
                session, tenant, row.sales_return_id
            )
            for return_line in return_lines:
                return_line.credited_quantity = credited_quantities.get(
                    return_line.dispatch_line_id, ZERO
                )
        credited_total = await issued_credit_total(session, tenant, invoice.id)
        await transition_sales_invoice(
            session,
            tenant,
            invoice,
            sales_invoice_status_after_payment(
                total_amount=invoice.total_amount,
                paid_amount=invoice.paid_amount,
                credited_amount=credited_total,
            ),
            reason=f"Credit note {row.code} cancelled",
        )
        await apply_outstanding_delta(
            session,
            tenant,
            invoice.customer_id,
            OutstandingAdjustment(
                delta=row.total_amount,
                reason=f"Credit note {row.code} cancelled",
                reference_type="credit_note",
                reference_id=row.id,
                reference_code=row.code,
            ),
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
