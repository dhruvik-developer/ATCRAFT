"""Staff API and public signed webhook for courier tracking."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from src.core.deps import (
    DbSession,
    Tenant,
    forbid_vendor_users,
    require_permission,
)
from src.core.errors import PayloadTooLargeError, ValidationError
from src.core.logging import resolve_client_ip
from src.core.messages import success_message
from src.core.pagination import CursorParams, PageParams, cursor_params, page_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.courier_tracking import service
from src.modules.courier_tracking.models import MAX_WEBHOOK_BYTES
from src.modules.courier_tracking.provider import (
    GENERIC_SIGNATURE_HEADER,
    GENERIC_TIMESTAMP_HEADER,
    SHIPROCKET_KEY_HEADER,
)
from src.modules.courier_tracking.schemas import (
    CourierAccountCreate,
    CourierAccountRead,
    CourierAccountUpdate,
    CourierEventFilters,
    CourierEventRead,
    CourierRefreshRequest,
    CourierTestRead,
    RotateWebhookRead,
)

router = APIRouter(
    prefix="/integrations/courier",
    tags=["Courier Integration"],
    responses=COMMON_ERROR_RESPONSES,
)
CursorDep = Annotated[CursorParams, Depends(cursor_params)]
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get(
    "/accounts",
    response_model=PaginatedResponse[CourierAccountRead],
    dependencies=[
        require_permission(Permission.SETTINGS_VIEW),
        Depends(forbid_vendor_users),
    ],
    summary="List courier accounts",
)
async def list_accounts(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[CourierAccountRead]:
    rows, pagination = await service.list_accounts(session, tenant, params)
    return PaginatedResponse[CourierAccountRead](data=rows, pagination=pagination)


@router.post(
    "/accounts",
    response_model=ApiResponse[CourierAccountRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
    summary="Connect a courier account",
)
async def create_account(
    payload: CourierAccountCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[CourierAccountRead]:
    return ApiResponse.ok(
        await service.create_account(session, tenant, payload),
        success_message("Courier account", "created"),
    )


@router.patch(
    "/accounts/{account_id}",
    response_model=ApiResponse[CourierAccountRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
    summary="Edit or deactivate a courier account",
)
async def update_account(
    account_id: uuid.UUID,
    payload: CourierAccountUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CourierAccountRead]:
    return ApiResponse.ok(
        await service.update_account(session, tenant, account_id, payload),
        success_message("Courier account", "updated"),
    )


@router.post(
    "/accounts/{account_id}/rotate-webhook",
    response_model=ApiResponse[RotateWebhookRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
    summary="Reset a courier account's webhook secret",
)
async def rotate_webhook(
    account_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[RotateWebhookRead]:
    return ApiResponse.ok(
        await service.rotate_webhook(session, tenant, account_id),
        success_message("Webhook secret", "reset"),
    )


@router.post(
    "/accounts/{account_id}/test",
    response_model=ApiResponse[CourierTestRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
    summary="Test a courier account's connection",
)
async def test_connection(
    account_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[CourierTestRead]:
    provider = await service.test_connection(session, tenant, account_id)
    return ApiResponse.ok(
        CourierTestRead(connected=True, provider=provider),
        success_message("Courier connection", "verified"),
    )


@router.post(
    "/refresh",
    response_model=ApiResponse[dict[str, int]],
    dependencies=[
        require_permission(Permission.DISPATCH_UPDATE),
        Depends(forbid_vendor_users),
    ],
    summary="Pull the latest courier tracking now",
)
async def refresh_tracking(
    payload: CourierRefreshRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[dict[str, int]]:
    count = await service.refresh_tracking(
        session,
        tenant,
        dispatch_id=payload.dispatch_id,
        shipment_id=payload.shipment_id,
    )
    return ApiResponse.ok({"events_recorded": count}, success_message("Tracking", "refreshed"))


@router.get(
    "/events",
    response_model=CursorPaginatedResponse[CourierEventRead],
    dependencies=[
        require_permission(Permission.DISPATCH_VIEW),
        Depends(forbid_vendor_users),
    ],
    summary="List received courier tracking events",
)
async def list_events(
    session: DbSession,
    tenant: Tenant,
    params: CursorDep,
    tracking_number: Annotated[str | None, Query(max_length=120)] = None,
    dispatch_id: uuid.UUID | None = None,
    shipment_id: uuid.UUID | None = None,
) -> CursorPaginatedResponse[CourierEventRead]:
    rows, pagination = await service.list_events(
        session,
        tenant,
        params=params,
        filters=CourierEventFilters(
            tracking_number=tracking_number,
            dispatch_id=dispatch_id,
            shipment_id=shipment_id,
        ),
    )
    return CursorPaginatedResponse[CourierEventRead](data=rows, pagination=pagination)


@router.post(
    "/webhook/{token}",
    response_model=ApiResponse[dict[str, int]],
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=True,
    summary="Receive a signed courier tracking webhook",
)
async def courier_webhook(
    token: str, request: Request, session: DbSession
) -> ApiResponse[dict[str, int]]:
    await service.admit_webhook(
        resolve_client_ip(
            request.headers.get("x-real-ip"),
            request.client.host if request.client is not None else None,
        )
    )
    count = await service.receive_webhook(
        session,
        token,
        await _bounded_body(request),
        signature=request.headers.get(GENERIC_SIGNATURE_HEADER, ""),
        timestamp=request.headers.get(GENERIC_TIMESTAMP_HEADER, ""),
        api_key=request.headers.get(SHIPROCKET_KEY_HEADER, ""),
    )
    return ApiResponse.ok({"events_recorded": count}, message="Webhook accepted")


async def _bounded_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError as exc:
            raise ValidationError("Content-Length is invalid.") from exc
        if declared < 0:
            raise ValidationError("Content-Length is invalid.")
        if declared > MAX_WEBHOOK_BYTES:
            raise PayloadTooLargeError("Courier webhook payload exceeds 64 KiB.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise PayloadTooLargeError("Courier webhook payload exceeds 64 KiB.")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["router"]
