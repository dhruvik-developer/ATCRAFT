"""Staff companions to the vendor consignment API."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from src.core.deps import DbSession, Tenant, forbid_vendor_users, require_permission
from src.core.pagination import PageParams, page_params
from src.core.permissions import Permission
from src.core.responses import AcceptedJobResponse, ApiResponse, PaginatedResponse
from src.modules.procurement import inbound_service as service
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

router = APIRouter(dependencies=[Depends(forbid_vendor_users)])
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get(
    "/inbound-tracking",
    response_model=PaginatedResponse[InboundTrackingRead],
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def inbound_tracking(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    overdue: bool = False,
    expected_today: bool = False,
    in_transit: bool = False,
    not_acknowledged: bool = False,
    vendor_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    search: str | None = None,
) -> PaginatedResponse[InboundTrackingRead]:
    rows, page = await service.tracking_board(
        session,
        tenant,
        InboundTrackingFilters(
            overdue=overdue,
            expected_today=expected_today,
            in_transit=in_transit,
            not_acknowledged=not_acknowledged,
            vendor_id=vendor_id,
            category_id=category_id,
            search=search,
        ),
        params,
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}/shipments",
    response_model=PaginatedResponse[InboundShipmentRead],
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def list_shipments(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundShipmentRead]:
    rows, page = await service.list_shipments(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/shipments",
    response_model=ApiResponse[InboundShipmentRead],
    status_code=201,
    dependencies=[require_permission(Permission.PURCHASE_ORDER_UPDATE)],
)
async def create_shipment(
    order_id: uuid.UUID, payload: InboundShipmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[InboundShipmentRead]:
    return ApiResponse.ok(await service.record_inbound_shipment(session, tenant, order_id, payload))


@router.patch(
    "/purchase-orders/{order_id}/shipments/{shipment_id}",
    response_model=ApiResponse[InboundShipmentRead],
    dependencies=[require_permission(Permission.PURCHASE_ORDER_UPDATE)],
)
async def edit_shipment(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundShipmentCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    return ApiResponse.ok(
        await service.record_inbound_shipment(
            session, tenant, order_id, payload, shipment_id=shipment_id
        )
    )


@router.post(
    "/purchase-orders/{order_id}/shipments/{shipment_id}/delivered",
    response_model=ApiResponse[InboundShipmentRead],
    dependencies=[require_permission(Permission.GRN_CREATE)],
)
async def delivered(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundDeliveredRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    return ApiResponse.ok(
        await service.mark_delivered(session, tenant, order_id, shipment_id, payload.delivered_at)
    )


@router.post(
    "/purchase-orders/{order_id}/documents",
    response_model=ApiResponse[InboundDocumentRead],
    status_code=201,
    dependencies=[require_permission(Permission.DOCUMENT_UPLOAD)],
)
async def upload_document(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    file: Annotated[UploadFile, File()],
    kind: Annotated[DocumentKind, Form()],
    shipment_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ApiResponse[InboundDocumentRead]:
    return ApiResponse.ok(
        await service.upload_document(session, tenant, order_id, file, kind, shipment_id)
    )


@router.get(
    "/purchase-orders/{order_id}/documents",
    response_model=PaginatedResponse[InboundDocumentRead],
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def list_documents(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundDocumentRead]:
    rows, page = await service.list_documents(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/accept-vendor-date",
    response_model=ApiResponse[None],
    dependencies=[require_permission(Permission.PURCHASE_ORDER_UPDATE)],
)
async def accept_vendor_date(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[None]:
    await service.accept_vendor_date(session, tenant, order_id)
    return ApiResponse.ok()


@router.post(
    "/purchase-orders/{order_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=202,
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def pdf(order_id: uuid.UUID, session: DbSession, tenant: Tenant) -> AcceptedJobResponse:
    return await queue_purchase_order_pdf(session, tenant, order_id)
