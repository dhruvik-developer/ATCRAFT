"""Thin vendor-only routes. Staff rights never bypass the vendor identity gate."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission, require_vendor_user
from src.core.pagination import PageParams, page_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.documents.service import DOWNLOAD_HEADERS, content_disposition, stream_file
from src.modules.finance import service as finance
from src.modules.finance.schemas import PaymentDetail, VendorInvoiceDetail, VendorInvoiceSummary
from src.modules.procurement import inbound_service as inbound
from src.modules.procurement.inbound_schemas import (
    DocumentKind,
    InboundDeliveredRequest,
    InboundDocumentRead,
    InboundShipmentCreate,
    InboundShipmentRead,
    InboundTrackingFilters,
    InboundTrackingRead,
)
from src.modules.procurement.purchase_order_pdf import queue_purchase_order_pdf
from src.modules.procurement.schemas import PurchaseOrderSummary, ReasonRequest
from src.modules.reports.schemas import ReportJobRead
from src.modules.vendor_portal.schemas import (
    VendorPortalAccept,
    VendorPortalDashboard,
    VendorPortalDateChange,
    VendorPortalInvoiceCreate,
    VendorPortalMe,
    VendorPortalOrderDetail,
    VendorPortalPaymentSummary,
    VendorPortalReady,
    VendorPortalReceiptRead,
)
from src.modules.vendor_portal.service import (
    VendorPortalService,
    upload_vendor_document,
    vendor_rating,
)
from src.modules.vendors.schemas import TransporterOption, VendorRatingRead

router = APIRouter(
    prefix="/vendor-portal",
    tags=["Vendor Portal"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[Depends(require_vendor_user)],
)
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get(
    "/me",
    response_model=ApiResponse[VendorPortalMe],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def me(
    session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[VendorPortalMe]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).me(principal))


@router.get(
    "/dashboard",
    response_model=ApiResponse[VendorPortalDashboard],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def dashboard(session: DbSession, tenant: Tenant) -> ApiResponse[VendorPortalDashboard]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).dashboard())


@router.get(
    "/purchase-orders",
    response_model=PaginatedResponse[InboundTrackingRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def orders(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    tab: Annotated[
        str, Query(pattern="^(to_accept|accepted|ready|dispatched|delivered|all)$")
    ] = "all",
    search: str | None = None,
    assigned_to_me: bool = False,
) -> PaginatedResponse[InboundTrackingRead]:
    rows, page = await inbound.tracking_board(
        session,
        tenant,
        InboundTrackingFilters(tab=tab, search=search, assigned_to_me=assigned_to_me),
        params,
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}",
    response_model=ApiResponse[VendorPortalOrderDetail],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalOrderDetail]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).order(order_id))


@router.post(
    "/purchase-orders/{order_id}/accept",
    response_model=ApiResponse[PurchaseOrderSummary],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def accept(
    order_id: uuid.UUID,
    payload: VendorPortalAccept,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    return ApiResponse.ok(
        await VendorPortalService(session, tenant).accept(principal, order_id, payload)
    )


@router.post(
    "/purchase-orders/{order_id}/decline",
    response_model=ApiResponse[PurchaseOrderSummary],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def decline(
    order_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    return ApiResponse.ok(
        await VendorPortalService(session, tenant).decline(principal, order_id, payload.reason)
    )


@router.post(
    "/purchase-orders/{order_id}/delivery-date",
    response_model=ApiResponse[PurchaseOrderSummary],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def delivery_date(
    order_id: uuid.UUID,
    payload: VendorPortalDateChange,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).change_date(order_id, payload))


@router.post(
    "/purchase-orders/{order_id}/ready-for-dispatch",
    response_model=ApiResponse[PurchaseOrderSummary],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def ready_for_dispatch(
    order_id: uuid.UUID,
    payload: VendorPortalReady,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).ready(order_id, payload))


@router.get(
    "/purchase-orders/{order_id}/shipments",
    response_model=PaginatedResponse[InboundShipmentRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def shipments(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundShipmentRead]:
    rows, page = await inbound.list_shipments(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/shipments",
    response_model=ApiResponse[InboundShipmentRead],
    status_code=201,
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def shipment(
    order_id: uuid.UUID, payload: InboundShipmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[InboundShipmentRead]:
    return ApiResponse.ok(await inbound.record_inbound_shipment(session, tenant, order_id, payload))


@router.post(
    "/purchase-orders/{order_id}/shipments/{shipment_id}/delivered",
    response_model=ApiResponse[InboundShipmentRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def delivered(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundDeliveredRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    return ApiResponse.ok(
        await inbound.mark_delivered(session, tenant, order_id, shipment_id, payload.delivered_at)
    )


@router.get(
    "/transporters/options",
    response_model=PaginatedResponse[TransporterOption],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def transporters(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[TransporterOption]:
    rows, page = await VendorPortalService(session, tenant).transporters(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/documents",
    response_model=ApiResponse[InboundDocumentRead],
    status_code=201,
    dependencies=[
        require_permission(
            Permission.VENDOR_PORTAL_SHIP, Permission.VENDOR_PORTAL_INVOICE, mode="any"
        )
    ],
)
async def upload(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    file: Annotated[UploadFile, File()],
    kind: Annotated[DocumentKind, Form()],
    shipment_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ApiResponse[InboundDocumentRead]:
    return ApiResponse.ok(
        await upload_vendor_document(session, tenant, principal, order_id, file, kind, shipment_id)
    )


@router.get(
    "/purchase-orders/{order_id}/documents",
    response_model=PaginatedResponse[InboundDocumentRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def documents(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundDocumentRead]:
    rows, page = await inbound.list_documents(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/documents/{document_id}/download",
    response_class=StreamingResponse,
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def download(document_id: uuid.UUID, session: DbSession, tenant: Tenant) -> StreamingResponse:
    result = await VendorPortalService(session, tenant).download(document_id)
    return StreamingResponse(
        stream_file(result.path),
        media_type=result.document.mime_type,
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(result.document.file_name),
            "Content-Length": str(result.document.size_bytes),
        },
    )


@router.get(
    "/receipts",
    response_model=PaginatedResponse[VendorPortalReceiptRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def receipts(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorPortalReceiptRead]:
    rows, page = await VendorPortalService(session, tenant).receipts(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}/receipts",
    response_model=PaginatedResponse[VendorPortalReceiptRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def order_receipts(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorPortalReceiptRead]:
    rows, page = await VendorPortalService(session, tenant).receipts(params, order_id)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/invoices",
    response_model=PaginatedResponse[VendorInvoiceSummary],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def invoices(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorInvoiceSummary]:
    rows, page = await VendorPortalService(session, tenant).invoices(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/invoices",
    response_model=ApiResponse[VendorInvoiceDetail],
    status_code=201,
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def invoice(
    payload: VendorPortalInvoiceCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorInvoiceDetail]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).create_invoice(payload))


@router.get(
    "/invoices/{invoice_id}",
    response_model=ApiResponse[VendorInvoiceDetail],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def invoice_detail(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorInvoiceDetail]:
    return ApiResponse.ok(await finance.get_invoice_detail(session, tenant, invoice_id))


@router.get(
    "/payments",
    response_model=PaginatedResponse[PaymentDetail],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def payments(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[PaymentDetail]:
    rows, page = await VendorPortalService(session, tenant).payments(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/payments/summary",
    response_model=ApiResponse[list[VendorPortalPaymentSummary]],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def payment_summary(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[VendorPortalPaymentSummary]]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).payment_summary())


@router.get(
    "/rating",
    response_model=ApiResponse[VendorRatingRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def rating(session: DbSession, tenant: Tenant) -> ApiResponse[VendorRatingRead]:
    return ApiResponse.ok(await vendor_rating(session, tenant))


@router.post(
    "/purchase-orders/{order_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=202,
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def pdf(order_id: uuid.UUID, session: DbSession, tenant: Tenant) -> AcceptedJobResponse:
    return await queue_purchase_order_pdf(session, tenant, order_id, portal=True)


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[ReportJobRead],
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def job(job_id: uuid.UUID, session: DbSession, tenant: Tenant) -> ApiResponse[ReportJobRead]:
    return ApiResponse.ok(await VendorPortalService(session, tenant).job(job_id))
