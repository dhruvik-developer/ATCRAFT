"""Issuing and cancelling a credit note against an issued sales invoice.

A credit note gives part of an invoice back, line by line: a quantity of a line,
valued in proportion to what the line has not yet credited, or a taxable amount
with no quantity (a price adjustment, a discount). A ``RETURN`` note must name
the sales return the goods came back on and credits no more than quality
accepted. Issuing posts the reversing journal, raises the invoice's credited
figures and moves its status, lowers the customer's outstanding balance and
tells the dealer portal; cancelling (``ISSUED -> CANCELLED``, irreversible)
reverses each of those in one transaction.

It is a file of its own because a credit note is a document of its own - its
own number, lifecycle, PDF and repository - while the invoice it credits stays
``sales_invoice_service.py``'s: the invoice's lines and status move here only
through the invoice's repository locks and its own transition.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.common.document_access import register_document_entity_resolver
from src.common.repository import labels_by_id
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.ids import new_id
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
from src.modules.finance.sales_invoice_repository import SalesInvoiceRepository
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
from src.modules.products.models import Item
from src.modules.quality.repository import QcInspectionRepository
from src.modules.sales_returns.crediting import accepted_return_quantity
from src.modules.sales_returns.models import SalesReturn, SalesReturnLine, SalesReturnStatus
from src.modules.sales_returns.repository import SalesReturnRepository

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

#: ``documents.entity_type`` of a file filed against a credit note - the PDF
#: the report worker renders, and a customer's acknowledged copy.
CREDIT_NOTE_DOCUMENT_ENTITY = "credit_note"

# A file filed against a credit note is one of this company's notes - not
# another company's, and not an id naming none. docs/assumptions.md §DOC-2.
register_document_entity_resolver(
    CREDIT_NOTE_DOCUMENT_ENTITY,
    lambda session, tenant, note_id: CreditNoteRepository(session, tenant).get_or_404(note_id),
)


async def _summaries(
    session: AsyncSession, tenant: TenantContext, rows: Sequence[CreditNote]
) -> list[CreditNoteSummary]:
    """Project notes, each naming its invoice and its return - batched per page."""
    invoices = await labels_by_id(
        session, tenant, (row.sales_invoice_id for row in rows), SalesInvoice.code
    )
    returns = await labels_by_id(
        session, tenant, (row.sales_return_id for row in rows), SalesReturn.code
    )
    summaries: list[CreditNoteSummary] = []
    for row in rows:
        summary = CreditNoteSummary.model_validate(row)
        invoice = invoices.get(row.sales_invoice_id)
        summary.sales_invoice_code = invoice.code if invoice else None
        sales_return = returns.get(row.sales_return_id) if row.sales_return_id else None
        summary.sales_return_code = sales_return.code if sales_return else None
        summaries.append(summary)
    return summaries


async def _detail(
    session: AsyncSession, tenant: TenantContext, row: CreditNote, lines: list[CreditNoteLine]
) -> CreditNoteDetail:
    (summary,) = await _summaries(session, tenant, [row])
    items = await labels_by_id(
        session, tenant, (line.item_id for line in lines), Item.code, Item.name
    )
    reads: list[CreditNoteLineRead] = []
    for line in lines:
        read = CreditNoteLineRead.model_validate(line)
        item = items.get(line.item_id)
        read.item_code = item.code if item else None
        read.item_name = item.name if item else None
        reads.append(read)
    return CreditNoteDetail(**summary.model_dump(), lines=reads)


async def get_credit_note(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID
) -> CreditNoteDetail:
    repo = CreditNoteRepository(session, tenant)
    row = await repo.get_or_404(note_id)
    return await _detail(session, tenant, row, await repo.lines(row.id))


async def queue_credit_note_pdf(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID
) -> str:
    from src.modules.reports import service as reports_service
    from src.modules.reports.models import ReportKey
    from src.modules.reports.repository import ReportSnapshotRepository

    note = await CreditNoteRepository(session, tenant).get_or_404(note_id)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.CREDIT_NOTE, {"credit_note_id": str(note.id)}, now=utcnow()
        )
        job.expires_at = None
        ReportSnapshotRepository(session, tenant).add(job)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type="credit_note",
            entity_id=note.id,
            entity_code=note.code,
            summary=f"Queued PDF for {note.code}",
            user_id=tenant.user_id,
            context={"job_id": job.id},
        )
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
    return await _summaries(session, tenant, rows), page


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
    # the same order - through the owner's own lock reads - so a manual credit
    # cannot deadlock a simultaneous receipt.
    returns = SalesReturnRepository(session, tenant)
    row = await returns.lock_for_update(return_id)
    if row is None:
        raise NotFoundError("Sales return", return_id)
    return row, await returns.lock_lines(return_id)


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
    inspections_by_id = {
        inspection.id: inspection
        for inspection in await QcInspectionRepository(session, tenant).get_many(
            [line.qc_inspection_id for line in return_lines if line.qc_inspection_id]
        )
    }
    credited = await CreditNoteRepository(session, tenant).issued_return_quantities(return_row.id)
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
    """Issue a credit note against an issued invoice, in one transaction.

    Requires ``credit_note.create`` (checked by the route). Refused when the
    invoice is cancelled or fully credited; when it is paid and the note is not
    a return credit naming a sales return; when its currency is not the
    company's; when a line is not the invoice's, gives neither a quantity nor an
    amount, or asks for more quantity or value than the line has left; when the
    total is not positive or would take the invoice's credits past its ceiling -
    the whole invoice for a paid invoice or a return credit, the unpaid balance
    otherwise; and when a ``RETURN`` note names no return. A return credit also
    needs a received or closed return of the same customer and dispatch,
    received after the invoice was issued, and credits each line no more than
    quality accepted less what earlier notes credited.

    Tax follows each line in proportion (``credit_tax_parts``), and the note
    carries its share of the invoice's round-off in proportion to the value
    credited so far (``credit_round_off``). On success it posts the journal -
    revenue and output tax debited, the customer's receivable credited -
    numbers and stores the note and its lines, raises the invoice lines' and
    the return lines' credited figures, snapshots a return credit's cost, moves
    the invoice to its status after credit, lowers the customer's outstanding
    balance, audits the create and notifies the dealer portal. The return is
    locked before the invoice, the order its owner takes, so a credit cannot
    deadlock a receipt on the same return.
    """
    async with transaction(session):
        return_row = None
        return_lines: list[SalesReturnLine] = []
        if payload.sales_return_id is not None:
            return_row, return_lines = await _lock_return(session, tenant, payload.sales_return_id)
        invoices = SalesInvoiceRepository(session, tenant)
        invoice = await invoices.lock_header(payload.sales_invoice_id)
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
        source_lines = await invoices.lock_lines(invoice.id, line_ids)
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
        # The note's id and number exist before its journal does, so every leg
        # names the note it belongs to - the customer's statement shows the
        # number, and a reversal on cancel copies both from the original legs.
        note_id = new_id()
        note_code = await next_document_number(session, tenant.company_id, DocTypes.CREDIT_NOTE)
        journal = await post_journal(
            session,
            tenant,
            [line for line in journal_lines if line.debit or line.credit],
            entry_date=payload.note_date,
            reference_type="CREDIT_NOTE",
            reference_id=note_id,
            reference_code=note_code,
            narration=f"Credit note against {invoice.code}",
            currency=invoice.currency,
        )
        row = CreditNote(
            id=note_id,
            company_id=tenant.company_id,
            created_by_id=tenant.user_id,
            code=note_code,
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
        from src.core.permissions import Permission
        from src.modules.notifications.models import NotificationType
        from src.modules.notifications.service import notify_customer

        await notify_customer(
            session,
            company_id=tenant.company_id,
            customer_id=row.customer_id,
            permission=Permission.DEALER_PORTAL_ACCOUNT,
            notification_type=NotificationType.DEALER_CREDIT_NOTE_ISSUED,
            title=f"Credit note issued: {row.code}",
            body=f"Credit note {row.code} was issued for {row.currency} {row.total_amount}.",
            action_url="/dealer/account",
            entity_type="credit_note",
            entity_id=row.id,
            actor_id=tenant.user_id,
        )
        await session.flush()
    return await get_credit_note(session, tenant, row.id)


async def cancel_credit_note(
    session: AsyncSession, tenant: TenantContext, note_id: uuid.UUID, reason: str
) -> CreditNoteDetail:
    notes = CreditNoteRepository(session, tenant)
    async with transaction(session):
        existing = await notes.get_or_404(note_id)
        return_lines: list[SalesReturnLine] = []
        if existing.sales_return_id is not None:
            _, return_lines = await _lock_return(session, tenant, existing.sales_return_id)
        row = await notes.lock_for_update(note_id)
        if row is None:
            raise NotFoundError("Credit note", note_id)
        CREDIT_NOTE_LIFECYCLE.assert_transition(row.status, CreditNoteStatus.CANCELLED)
        invoices = SalesInvoiceRepository(session, tenant)
        invoice = await invoices.lock_header(row.sales_invoice_id)
        await reverse_journal(
            session,
            tenant,
            row.journal_id,
            reason=reason,
        )
        note_lines = await notes.lines(row.id)
        source_lines = await invoices.lock_lines(
            invoice.id, [line.sales_invoice_line_id for line in note_lines]
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
            credited_quantities = await notes.issued_return_quantities(row.sales_return_id)
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
