"""Customer-facing commerce delegates stock, tax and PDF work to their owners."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import AcceptedJobResponse, OffsetPagination, api_path
from src.core.types import ZERO
from src.modules.dealer_portal.commerce_repository import DealerCommerceRepository
from src.modules.dealer_portal.commerce_schemas import (
    DealerCreditNoteDetail,
    DealerCreditNoteSummary,
    DealerEligibleDispatch,
    DealerEligibleDispatchLine,
    DealerInvoiceDetail,
    DealerInvoiceSummary,
    DealerReturnCreate,
    DealerReturnDetail,
    DealerReturnSummary,
)
from src.modules.dealer_portal.service import DealerPortalService
from src.modules.dispatch.models import Dispatch
from src.modules.documents.service import DocumentDownload, DocumentService
from src.modules.finance import credit_note_service, sales_invoice_service
from src.modules.finance.models import CreditNote, SalesInvoice
from src.modules.finance.sales_invoice_schemas import SalesInvoiceFilters, SalesInvoiceSummary
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.notifications.service import notify
from src.modules.reports import service as reports
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.reports.schemas import ReportJobRead
from src.modules.sales_returns import service as returns
from src.modules.sales_returns.models import SalesReturn, SalesReturnSource
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn


def invoice_summary(row: SalesInvoiceSummary) -> DealerInvoiceSummary:
    return DealerInvoiceSummary.model_validate(
        {
            **row.model_dump(),
            "credited_amount": row.total_amount - row.paid_amount - row.balance_amount,
        }
    )


class DealerCommerceService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant
        self.repo = DealerCommerceRepository(session, tenant)
        self.portal = DealerPortalService(session, tenant)

    async def invoices(
        self, params: PageParams, *, search: str | None = None
    ) -> tuple[list[DealerInvoiceSummary], OffsetPagination]:
        customer = await self.portal._customer()
        rows, page = await sales_invoice_service.list_sales_invoices(
            self.session,
            self.tenant,
            SalesInvoiceFilters(customer_id=customer.id, search=search),
            params,
            SortParams(),
        )
        return [invoice_summary(row) for row in rows], page

    async def invoice(self, identity: uuid.UUID) -> DealerInvoiceDetail:
        await self.portal._customer()
        await self.repo.require_owned(SalesInvoice, identity, "Invoice")
        row = await sales_invoice_service.get_sales_invoice(self.session, self.tenant, identity)
        credits, page = await self.credit_notes(PageParams(limit=200), invoice_id=identity)
        return DealerInvoiceDetail.model_validate(
            {
                **row.model_dump(),
                **invoice_summary(row).model_dump(),
                "credit_notes": credits,
                "credit_notes_pagination": page,
            }
        )

    async def credit_notes(
        self, params: PageParams, *, invoice_id: uuid.UUID | None = None
    ) -> tuple[list[DealerCreditNoteSummary], OffsetPagination]:
        await self.portal._customer()
        if invoice_id is not None:
            await self.repo.require_owned(SalesInvoice, invoice_id, "Invoice")
        rows, page = await self.repo.credit_notes(params, invoice_id=invoice_id)
        return [DealerCreditNoteSummary.model_validate(row) for row in rows], page

    async def credit_note(self, identity: uuid.UUID) -> DealerCreditNoteDetail:
        await self.portal._customer()
        await self.repo.require_owned(CreditNote, identity, "Credit note")
        row = await credit_note_service.get_credit_note(self.session, self.tenant, identity)
        return DealerCreditNoteDetail.model_validate(row.model_dump())

    async def list_returns(
        self, params: PageParams
    ) -> tuple[list[DealerReturnSummary], OffsetPagination]:
        await self.portal._customer()
        rows, page = await self.repo.returns(params)
        amounts = await self.repo.return_credits([row.id for row in rows])
        return [
            DealerReturnSummary.model_validate(
                {
                    **DealerReturnSummary.model_validate(row).model_dump(),
                    "credited_amount": amounts.get(row.id, ZERO),
                }
            )
            for row in rows
        ], page

    async def sales_return(self, identity: uuid.UUID) -> DealerReturnDetail:
        await self.portal._customer()
        await self.repo.require_owned(SalesReturn, identity, "Sales return")
        row = await returns.get_return(self.session, self.tenant, identity)
        amounts = await self.repo.return_credits([identity])
        return DealerReturnDetail.model_validate(
            {
                **row.model_dump(),
                "credited_amount": amounts.get(identity, ZERO),
            }
        )

    async def eligible_dispatches(
        self, params: PageParams
    ) -> tuple[list[DealerEligibleDispatch], OffsetPagination]:
        await self.portal._customer()
        rows, page = await self.repo.eligible_dispatches(params)
        lines = await self.repo.eligible_lines([row.id for row in rows])
        grouped: dict[uuid.UUID, list[DealerEligibleDispatchLine]] = {}
        for line, code, name, remaining in lines:
            grouped.setdefault(line.dispatch_id, []).append(
                DealerEligibleDispatchLine(
                    dispatch_line_id=line.id,
                    item_id=line.item_id,
                    item_code=code,
                    item_name=name,
                    returnable_quantity=remaining,
                )
            )
        return [
            DealerEligibleDispatch(
                id=row.id,
                code=row.code,
                dispatched_at=row.dispatched_at,
                lines=grouped.get(row.id, []),
            )
            for row in rows
        ], page

    async def request_return(self, payload: DealerReturnCreate) -> DealerReturnDetail:
        customer = await self.portal._customer()
        async with transaction(self.session):
            await self.repo.require_owned(Dispatch, payload.dispatch_id, "Dispatch")
            eligible = {
                line.id for line, _, _, _ in await self.repo.eligible_lines([payload.dispatch_id])
            }
            if any(line.dispatch_line_id not in eligible for line in payload.lines):
                raise BusinessRuleError(
                    "Choose returnable, non-serial dispatch lines. "
                    "Contact sales for serial returns."
                )
            row = await returns.create_return(
                self.session,
                self.tenant,
                SalesReturnCreate(
                    dispatch_id=payload.dispatch_id,
                    notes=payload.notes,
                    source=SalesReturnSource.DEALER_PORTAL,
                    lines=[
                        SalesReturnLineIn(**line.model_dump(), requires_qc=True)
                        for line in payload.lines
                    ],
                ),
            )
            holders = await NotificationRepository(
                self.session, self.tenant
            ).user_ids_with_permission(str(Permission.SALES_RETURN_CREATE))
            recipients = await self.portal._live_internal_users(
                [
                    *holders,
                    *([customer.assigned_sales_user_id] if customer.assigned_sales_user_id else []),
                ]
            )
            await notify(
                self.session,
                company_id=self.tenant.company_id,
                user_ids=recipients,
                notification_type=NotificationType.DEALER_RETURN_REQUEST_RECEIVED,
                title=f"Return request from {customer.name}",
                body=f"{row.code} is awaiting receipt and inspection.",
                severity=NotificationSeverity.INFO,
                entity_type="sales_return",
                entity_id=row.id,
                action_url="/sales-returns",
                actor_id=self.tenant.user_id,
            )
        return await self.sales_return(row.id)

    async def queue_pdf(
        self, identity: uuid.UUID, *, credit_note: bool = False
    ) -> AcceptedJobResponse:
        await self.portal._customer()
        model = CreditNote if credit_note else SalesInvoice
        await self.repo.require_owned(model, identity, "Credit note" if credit_note else "Invoice")
        queue = (
            credit_note_service.queue_credit_note_pdf
            if credit_note
            else sales_invoice_service.queue_sales_invoice_pdf
        )
        job = await queue(self.session, self.tenant, identity)
        return AcceptedJobResponse.create(job, api_path(f"dealer-portal/jobs/{job}"))

    async def job(self, identity: uuid.UUID) -> ReportJobRead:
        await self.portal._customer()
        snapshot = await ReportSnapshotRepository(self.session, self.tenant).get(identity)
        if snapshot is None or snapshot.created_by_id != self.tenant.user_id:
            raise NotFoundError("Report job", identity)
        mapping: dict[str, tuple[Any, str]] = {
            ReportKey.SALES_INVOICE: (SalesInvoice, "sales_invoice_id"),
            ReportKey.CREDIT_NOTE: (CreditNote, "credit_note_id"),
        }
        if snapshot.report_key not in mapping:
            raise NotFoundError("Report job", identity)
        model, key = mapping[snapshot.report_key]
        try:
            entity_id = uuid.UUID(str((snapshot.parameters or {}).get(key)))
        except ValueError as exc:
            raise NotFoundError("Report job", identity) from exc
        await self.repo.require_owned(model, entity_id, "Report job")
        read = await reports.get_job(self.session, self.tenant, identity)
        if read.document_id:
            read.download_url = api_path(f"dealer-portal/documents/{read.document_id}/download")
        return read

    async def download(self, identity: uuid.UUID) -> DocumentDownload:
        await self.portal._customer()
        documents = DocumentService(self.session, self.tenant)
        document = await documents.get_document(identity)
        models = {"sales_invoice": SalesInvoice, "credit_note": CreditNote}
        model = models.get(document.entity_type or "")
        if model is None or document.entity_id is None or document.mime_type != "application/pdf":
            raise NotFoundError("Document", identity)
        owner = await self.repo.require_owned(model, document.entity_id, "Document")
        if owner.pdf_document_id != identity:
            raise NotFoundError("Document", identity)
        return await documents.open_download(identity)
