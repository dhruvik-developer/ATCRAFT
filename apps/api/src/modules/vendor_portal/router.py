"""The vendor portal: every route a vendor account may call, and only vendor accounts.

Thin by construction: bind, call one service function, wrap the envelope.
Every route names its portal right literally and the router demands a vendor
principal (``require_vendor_user``): an internal administrator holds every
permission string, and without that gate could act "as" a vendor by naming an
order. The vendor is always the token's - no request carries a vendor id - and
another vendor's order, file, job or bill is "not found", never "forbidden".
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission, require_vendor_user
from src.core.messages import success_message
from src.core.pagination import PageParams, bind_query_filters, page_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.documents.service import DOWNLOAD_HEADERS, content_disposition, stream_file
from src.modules.procurement import inbound_service
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
from src.modules.reports.schemas import ReportJobRead
from src.modules.vendor_portal.schemas import (
    VendorPortalAccept,
    VendorPortalDashboard,
    VendorPortalDateChange,
    VendorPortalDecline,
    VendorPortalInvoiceCreate,
    VendorPortalInvoiceDetail,
    VendorPortalInvoiceSummary,
    VendorPortalMe,
    VendorPortalOrderActionRead,
    VendorPortalOrderDetail,
    VendorPortalPayment,
    VendorPortalPaymentSummary,
    VendorPortalPendingAmendmentRead,
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
OrderTab = Literal["to_accept", "accepted", "ready", "dispatched", "delivered", "all"]


@router.get(
    "/me",
    response_model=ApiResponse[VendorPortalMe],
    summary="The signed-in vendor account: vendor, roles, categories",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def me(
    session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[VendorPortalMe]:
    return ApiResponse[VendorPortalMe].ok(await VendorPortalService(session, tenant).me(principal))


@router.get(
    "/dashboard",
    response_model=ApiResponse[VendorPortalDashboard],
    summary="Tile counts: orders by tab, bills awaiting payment, recent QC rejections",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def dashboard(session: DbSession, tenant: Tenant) -> ApiResponse[VendorPortalDashboard]:
    return ApiResponse[VendorPortalDashboard].ok(
        await VendorPortalService(session, tenant).dashboard()
    )


@router.get(
    "/purchase-orders",
    response_model=PaginatedResponse[InboundTrackingRead],
    summary="The vendor's purchase orders, with tracking status",
    description=(
        "tab narrows to one vendor-app tab: to_accept (SENT), accepted (ACKNOWLEDGED, nothing "
        "dispatched), ready, dispatched (dispatched or in transit), delivered (delivered, "
        "received, QC pending or GRN completed); all is every order the account can see."
    ),
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    tab: OrderTab = "all",
    search: Annotated[str | None, Query(max_length=120)] = None,
    assigned_to_me: bool = False,
) -> PaginatedResponse[InboundTrackingRead]:
    rows, page = await inbound_service.tracking_board(
        session,
        tenant,
        bind_query_filters(
            InboundTrackingFilters, tab=tab, search=search, assigned_to_me=assigned_to_me
        ),
        params,
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/pending-amendments",
    response_model=PaginatedResponse[VendorPortalPendingAmendmentRead],
    summary="Previously sent orders under revision, without revised terms",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def pending_amendments(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorPortalPendingAmendmentRead]:
    rows, page = await VendorPortalService(session, tenant).pending_amendments(params)
    return PaginatedResponse[VendorPortalPendingAmendmentRead](data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}",
    response_model=ApiResponse[VendorPortalOrderDetail | VendorPortalPendingAmendmentRead],
    summary="One purchase order: lines, dates, timeline, shipments, files, receipts, bills",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalOrderDetail | VendorPortalPendingAmendmentRead]:
    return ApiResponse[VendorPortalOrderDetail | VendorPortalPendingAmendmentRead].ok(
        await VendorPortalService(session, tenant).order(order_id)
    )


@router.post(
    "/purchase-orders/{order_id}/accept",
    response_model=ApiResponse[VendorPortalOrderActionRead],
    summary="Accept a sent order, optionally committing a delivery date",
    description=(
        "SENT to ACKNOWLEDGED. The committed date is stored as vendor_committed_date; the "
        "buyer's expected_delivery_date (the planning date) is not changed."
    ),
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def accept_order(
    order_id: uuid.UUID,
    payload: VendorPortalAccept,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[VendorPortalOrderActionRead]:
    order = await VendorPortalService(session, tenant).accept(principal, order_id, payload)
    return ApiResponse[VendorPortalOrderActionRead].ok(order, f"{order.code} accepted")


@router.post(
    "/purchase-orders/{order_id}/decline",
    response_model=ApiResponse[VendorPortalOrderActionRead],
    summary="Decline a sent order, with the reason",
    description="SENT to VENDOR_DECLINED; the buyer then cancels the order or sends it again.",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def decline_order(
    order_id: uuid.UUID,
    payload: VendorPortalDecline,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[VendorPortalOrderActionRead]:
    order = await VendorPortalService(session, tenant).decline(principal, order_id, payload.reason)
    return ApiResponse[VendorPortalOrderActionRead].ok(order, f"{order.code} declined")


@router.post(
    "/purchase-orders/{order_id}/delivery-date",
    response_model=ApiResponse[VendorPortalOrderActionRead],
    summary="Change the vendor's committed delivery date",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_RESPOND)],
)
async def change_delivery_date(
    order_id: uuid.UUID, payload: VendorPortalDateChange, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalOrderActionRead]:
    order = await VendorPortalService(session, tenant).change_date(order_id, payload)
    return ApiResponse[VendorPortalOrderActionRead].ok(
        order, success_message(f"Committed delivery date of {order.code}", "updated")
    )


@router.post(
    "/purchase-orders/{order_id}/ready-for-dispatch",
    response_model=ApiResponse[VendorPortalOrderActionRead],
    summary="Mark an accepted order ready for dispatch",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def mark_ready(
    order_id: uuid.UUID, payload: VendorPortalReady, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalOrderActionRead]:
    order = await VendorPortalService(session, tenant).ready(order_id, payload)
    return ApiResponse[VendorPortalOrderActionRead].ok(
        order, f"{order.code} marked ready for dispatch"
    )


@router.get(
    "/shipments",
    response_model=PaginatedResponse[InboundShipmentRead],
    summary="Consignments across the vendor's visible purchase orders",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def all_shipments(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    status: Literal["all", "in_transit", "delivered"] = "all",
) -> PaginatedResponse[InboundShipmentRead]:
    rows, page = await VendorPortalService(session, tenant).shipments(params, status)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}/shipments",
    response_model=PaginatedResponse[InboundShipmentRead],
    summary="The order's consignments, newest first",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def list_shipments(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundShipmentRead]:
    rows, page = await inbound_service.list_shipments(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/shipments",
    response_model=ApiResponse[InboundShipmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Record a consignment (transporter or courier, tracking, LR, vehicle, freight)",
    description="Only on an accepted order with goods still due. Numbered 1..n per order.",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def record_shipment(
    order_id: uuid.UUID, payload: InboundShipmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[InboundShipmentRead]:
    shipment = await inbound_service.record_inbound_shipment(session, tenant, order_id, payload)
    return ApiResponse[InboundShipmentRead].ok(
        shipment, success_message(f"Shipment {shipment.shipment_number}", "recorded")
    )


@router.post(
    "/purchase-orders/{order_id}/shipments/{shipment_id}/delivered",
    response_model=ApiResponse[InboundShipmentRead],
    summary="Mark a consignment delivered",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def mark_delivered(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundDeliveredRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    shipment = await inbound_service.mark_delivered(
        session, tenant, order_id, shipment_id, payload.delivered_at
    )
    return ApiResponse[InboundShipmentRead].ok(
        shipment, f"Shipment {shipment.shipment_number} marked delivered"
    )


@router.get(
    "/transporters/options",
    response_model=PaginatedResponse[TransporterOption],
    summary="Active transporters, for the shipment form",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_SHIP)],
)
async def transporter_options(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[TransporterOption]:
    rows, page = await VendorPortalService(session, tenant).transporters(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/documents",
    response_model=ApiResponse[InboundDocumentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Upload an invoice, dispatch photo, LR copy, delivery challan or other file",
    description=(
        "Multipart: file, kind and an optional shipment_id. kind INVOICE needs "
        "vendor_portal.invoice; every other kind needs vendor_portal.ship."
    ),
    dependencies=[
        require_permission(
            Permission.VENDOR_PORTAL_SHIP, Permission.VENDOR_PORTAL_INVOICE, mode="any"
        )
    ],
)
async def upload_document(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    file: Annotated[UploadFile, File()],
    kind: Annotated[DocumentKind, Form()],
    shipment_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ApiResponse[InboundDocumentRead]:
    document = await upload_vendor_document(
        session, tenant, principal, order_id, file, kind, shipment_id
    )
    return ApiResponse[InboundDocumentRead].ok(
        document, success_message(f"{document.file_name}", "uploaded")
    )


@router.get(
    "/purchase-orders/{order_id}/documents",
    response_model=PaginatedResponse[InboundDocumentRead],
    summary="The files shared on one order (its typed uploads and the PO PDF)",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def list_documents(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundDocumentRead]:
    rows, page = await inbound_service.list_documents(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/documents/{document_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "File content; the Content-Type header identifies its actual media type",
        }
    },
    summary="Download a file shared on one of the vendor's orders",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def download_document(
    document_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> StreamingResponse:
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
    summary="Posted goods receipts on the vendor's orders, with QC results",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def list_receipts(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    rejected_only: bool = False,
) -> PaginatedResponse[VendorPortalReceiptRead]:
    rows, page = await VendorPortalService(session, tenant).receipts(
        params, rejected_only=rejected_only
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}/receipts",
    response_model=PaginatedResponse[VendorPortalReceiptRead],
    summary="Posted goods receipts on one order, with QC results",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def list_order_receipts(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorPortalReceiptRead]:
    rows, page = await VendorPortalService(session, tenant).receipts(params, order_id)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/invoices",
    response_model=PaginatedResponse[VendorPortalInvoiceSummary],
    summary="The vendor's bills: status, paid, balance, due date",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def list_invoices(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    unpaid_only: bool = False,
) -> PaginatedResponse[VendorPortalInvoiceSummary]:
    rows, page = await VendorPortalService(session, tenant).invoices(
        params, unpaid_only=unpaid_only
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/invoices",
    response_model=ApiResponse[VendorPortalInvoiceDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a bill against one of the vendor's own orders",
    description=(
        "Raised for the signed-in vendor and submitted for review. purchase_order_id is "
        "required; no vendor or cost-centre field is accepted. A file goes up first as an "
        "INVOICE document on the same order and is named by invoice_document_id."
    ),
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def create_invoice(
    payload: VendorPortalInvoiceCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalInvoiceDetail]:
    invoice = await VendorPortalService(session, tenant).create_invoice(payload)
    return ApiResponse[VendorPortalInvoiceDetail].ok(
        invoice, success_message(f"Invoice {invoice.vendor_invoice_number}", "submitted")
    )


@router.get(
    "/invoices/{invoice_id}",
    response_model=ApiResponse[VendorPortalInvoiceDetail],
    summary="One of the vendor's bills, with its lines",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def get_invoice(
    invoice_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[VendorPortalInvoiceDetail]:
    return ApiResponse[VendorPortalInvoiceDetail].ok(
        await VendorPortalService(session, tenant).invoice(invoice_id)
    )


@router.get(
    "/payments",
    response_model=PaginatedResponse[VendorPortalPayment],
    summary="Approved payments to the vendor and the bills each settled",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def list_payments(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[VendorPortalPayment]:
    rows, page = await VendorPortalService(session, tenant).payments(params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/payments/summary",
    response_model=ApiResponse[list[VendorPortalPaymentSummary]],
    summary="Payment dashboard per currency: outstanding, overdue, due soon, paid, last paid",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_INVOICE)],
)
async def payment_summary(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[VendorPortalPaymentSummary]]:
    return ApiResponse[list[VendorPortalPaymentSummary]].ok(
        await VendorPortalService(session, tenant).payment_summary()
    )


@router.get(
    "/rating",
    response_model=ApiResponse[VendorRatingRead],
    summary="The vendor's own rating: on-time and rejection percentages, score, sample",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def rating(session: DbSession, tenant: Tenant) -> ApiResponse[VendorRatingRead]:
    return ApiResponse[VendorRatingRead].ok(await vendor_rating(session, tenant))


@router.post(
    "/purchase-orders/{order_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate the purchase order PDF",
    description="Queued; poll the portal job route it returns, then follow its download_url.",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def order_pdf(order_id: uuid.UUID, session: DbSession, tenant: Tenant) -> AcceptedJobResponse:
    return await queue_purchase_order_pdf(session, tenant, order_id, portal=True)


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[ReportJobRead],
    summary="Status of one of the vendor's own PO PDF jobs",
    dependencies=[require_permission(Permission.VENDOR_PORTAL_ACCESS)],
)
async def get_job(
    job_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ReportJobRead]:
    return ApiResponse[ReportJobRead].ok(await VendorPortalService(session, tenant).job(job_id))
