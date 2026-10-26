"""Read-only statutory documents and dealer return requests."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import PageParams, page_params
from src.core.permissions import Permission
from src.core.responses import AcceptedJobResponse, ApiResponse, PaginatedResponse
from src.modules.dealer_portal.commerce_schemas import (
    DealerCreditNoteDetail,
    DealerCreditNoteSummary,
    DealerEligibleDispatch,
    DealerInvoiceDetail,
    DealerInvoiceSummary,
    DealerReturnCreate,
    DealerReturnDetail,
    DealerReturnSummary,
)
from src.modules.dealer_portal.commerce_service import DealerCommerceService
from src.modules.dealer_portal.promotion_schemas import DealerBannerRead, DealerOfferRead
from src.modules.dealer_portal.promotion_service import PromotionService
from src.modules.documents.service import DOWNLOAD_HEADERS, content_disposition, stream_file
from src.modules.reports.schemas import ReportJobRead

router = APIRouter()
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get(
    "/banners",
    response_model=PaginatedResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def banners(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[DealerBannerRead]:
    rows, page = await PromotionService(session, tenant).dealer_banners(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/offers",
    response_model=PaginatedResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def offers(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[DealerOfferRead]:
    rows, page = await PromotionService(session, tenant).dealer_offers(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/images/{document_id}",
    response_class=StreamingResponse,
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
    responses={
        200: {
            "content": {
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            },
            "description": "An image of a visible product or promotion",
        }
    },
)
async def image(document_id: uuid.UUID, session: DbSession, tenant: Tenant) -> StreamingResponse:
    result = await PromotionService(session, tenant).image(document_id)
    return StreamingResponse(
        stream_file(result.path),
        media_type=result.document.mime_type,
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(result.document.file_name).replace(
                "attachment;", "inline;", 1
            ),
            "Cache-Control": "private, max-age=300",
            "Content-Length": str(result.document.size_bytes),
        },
    )


@router.get(
    "/invoices",
    response_model=PaginatedResponse[DealerInvoiceSummary],
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def list_invoices(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> PaginatedResponse[DealerInvoiceSummary]:
    rows, page = await DealerCommerceService(session, tenant).invoices(params, search=search)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/invoices/{invoice_id}",
    response_model=ApiResponse[DealerInvoiceDetail],
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def get_invoice(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerInvoiceDetail]:
    return ApiResponse[DealerInvoiceDetail].ok(
        await DealerCommerceService(session, tenant).invoice(invoice_id)
    )


@router.post(
    "/invoices/{invoice_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def invoice_pdf(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    return await DealerCommerceService(session, tenant).queue_pdf(invoice_id)


@router.get(
    "/credit-notes",
    response_model=PaginatedResponse[DealerCreditNoteSummary],
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def list_credit_notes(
    session: DbSession, tenant: Tenant, params: PageDep, invoice_id: uuid.UUID | None = None
) -> PaginatedResponse[DealerCreditNoteSummary]:
    rows, page = await DealerCommerceService(session, tenant).credit_notes(
        params, invoice_id=invoice_id
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/credit-notes/{note_id}",
    response_model=ApiResponse[DealerCreditNoteDetail],
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def get_credit_note(
    note_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerCreditNoteDetail]:
    return ApiResponse[DealerCreditNoteDetail].ok(
        await DealerCommerceService(session, tenant).credit_note(note_id)
    )


@router.post(
    "/credit-notes/{note_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def credit_note_pdf(
    note_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    return await DealerCommerceService(session, tenant).queue_pdf(note_id, credit_note=True)


@router.get(
    "/returns/eligible-dispatches",
    response_model=PaginatedResponse[DealerEligibleDispatch],
    dependencies=[require_permission(Permission.DEALER_PORTAL_RETURN)],
)
async def eligible_dispatches(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[DealerEligibleDispatch]:
    rows, page = await DealerCommerceService(session, tenant).eligible_dispatches(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/returns",
    response_model=PaginatedResponse[DealerReturnSummary],
    dependencies=[require_permission(Permission.DEALER_PORTAL_RETURN)],
)
async def list_returns(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[DealerReturnSummary]:
    rows, page = await DealerCommerceService(session, tenant).list_returns(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/returns/{return_id}",
    response_model=ApiResponse[DealerReturnDetail],
    dependencies=[require_permission(Permission.DEALER_PORTAL_RETURN)],
)
async def get_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerReturnDetail]:
    return ApiResponse[DealerReturnDetail].ok(
        await DealerCommerceService(session, tenant).sales_return(return_id)
    )


@router.post(
    "/returns",
    response_model=ApiResponse[DealerReturnDetail],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.DEALER_PORTAL_RETURN)],
)
async def request_return(
    payload: DealerReturnCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerReturnDetail]:
    row = await DealerCommerceService(session, tenant).request_return(payload)
    return ApiResponse[DealerReturnDetail].ok(row, f"Return request {row.code} submitted")


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[ReportJobRead],
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def get_job(
    job_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ReportJobRead]:
    return ApiResponse[ReportJobRead].ok(await DealerCommerceService(session, tenant).job(job_id))


@router.get(
    "/documents/{document_id}/download",
    response_class=StreamingResponse,
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
    responses={
        200: {
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The dealer's own statutory PDF",
        }
    },
)
async def download(document_id: uuid.UUID, session: DbSession, tenant: Tenant) -> StreamingResponse:
    result = await DealerCommerceService(session, tenant).download(document_id)
    return StreamingResponse(
        stream_file(result.path),
        media_type=result.document.mime_type,
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(result.document.file_name),
            "Content-Length": str(result.document.size_bytes),
        },
    )
