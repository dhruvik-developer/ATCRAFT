"""Staff API and public signed webhook for courier tracking."""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from src.core.config import settings
from src.core.deps import (
    DbSession,
    Tenant,
    forbid_vendor_users,
    require_permission,
)
from src.core.errors import (
    AuthenticationError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from src.core.logging import resolve_client_ip
from src.core.pagination import CursorParams, PageParams, cursor_params, page_params
from src.core.permissions import Permission
from src.core.ratelimit import enforce_rate_limit
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.core.security import (
    constant_time_compare,
    decrypt_courier_webhook_secret,
    hash_secret_token,
)
from src.modules.courier_tracking import service
from src.modules.courier_tracking.models import MAX_WEBHOOK_BYTES, CourierProvider
from src.modules.courier_tracking.provider import (
    verify_generic_signature,
)
from src.modules.courier_tracking.schemas import (
    CourierAccountCreate,
    CourierAccountRead,
    CourierAccountUpdate,
    CourierEventFilters,
    CourierEventRead,
    CourierRefreshRequest,
    CourierTestRead,
    GenericWebhookPayload,
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
)
async def create_account(
    payload: CourierAccountCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[CourierAccountRead]:
    return ApiResponse.ok(await service.create_account(session, tenant, payload))


@router.patch(
    "/accounts/{account_id}",
    response_model=ApiResponse[CourierAccountRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
)
async def update_account(
    account_id: uuid.UUID,
    payload: CourierAccountUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CourierAccountRead]:
    return ApiResponse.ok(await service.update_account(session, tenant, account_id, payload))


@router.post(
    "/accounts/{account_id}/rotate-webhook",
    response_model=ApiResponse[RotateWebhookRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
)
async def rotate_webhook(
    account_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[RotateWebhookRead]:
    _, url, secret = await service.rotate_webhook(session, tenant, account_id)
    account = await service.get_account(session, tenant, account_id)
    header = (
        "X-Api-Key" if account.provider is CourierProvider.SHIPROCKET else "X-Courier-Signature"
    )
    return ApiResponse.ok(RotateWebhookRead(webhook_url=url, secret=secret, header_name=header))


@router.post(
    "/accounts/{account_id}/test",
    response_model=ApiResponse[CourierTestRead],
    dependencies=[
        require_permission(Permission.SETTINGS_MANAGE),
        Depends(forbid_vendor_users),
    ],
)
async def test_connection(
    account_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[CourierTestRead]:
    provider = await service.test_connection(session, tenant, account_id)
    return ApiResponse.ok(CourierTestRead(connected=True, provider=provider))


@router.post(
    "/refresh",
    response_model=ApiResponse[dict[str, int]],
    dependencies=[
        require_permission(Permission.DISPATCH_UPDATE),
        Depends(forbid_vendor_users),
    ],
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
    return ApiResponse.ok({"events_recorded": count})


@router.get(
    "/events",
    response_model=CursorPaginatedResponse[CourierEventRead],
    dependencies=[
        require_permission(Permission.DISPATCH_VIEW),
        Depends(forbid_vendor_users),
    ],
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
)
async def courier_webhook(
    token: str, request: Request, session: DbSession
) -> ApiResponse[dict[str, int]]:
    if not settings.courier_tracking_enabled:
        raise ValidationError("Courier tracking is disabled for this deployment.")
    client_ip = resolve_client_ip(
        request.headers.get("x-real-ip"),
        request.client.host if request.client is not None else None,
    )
    await enforce_rate_limit(
        f"courier-webhook:{client_ip or 'unknown'}",
        limit=60,
        window_seconds=60,
        scope="courier_webhook",
    )
    body = await _bounded_body(request)
    account = await service.account_for_webhook(session, hash_secret_token(token))
    if account is None or account.webhook_secret_ciphertext is None:
        raise NotFoundError("Courier webhook")
    secret = decrypt_courier_webhook_secret(account.webhook_secret_ciphertext)
    # provider is a String column: ORM loads return plain str, so == not is.
    if account.provider == CourierProvider.GENERIC_WEBHOOK:
        if not verify_generic_signature(
            secret=secret,
            body=body,
            signature=request.headers.get("x-courier-signature", ""),
            timestamp=request.headers.get("x-courier-timestamp", ""),
        ):
            raise AuthenticationError("Courier webhook signature is invalid.")
        try:
            payload = GenericWebhookPayload.model_validate_json(body)
        except ValueError as exc:
            raise ValidationError("Courier webhook payload is invalid.") from exc
        count = await service.ingest_generic_webhook(session, account, payload.events)
    else:
        supplied = request.headers.get("x-api-key", "")
        if not supplied or not constant_time_compare(supplied, secret):
            raise AuthenticationError("Courier webhook token is invalid.")
        try:
            payload_data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("Courier webhook payload is invalid.") from exc
        if not isinstance(payload_data, dict):
            raise ValidationError("Courier webhook payload must be an object.")
        count = await service.ingest_shiprocket_webhook(session, account, payload_data)
    return ApiResponse.ok({"events_recorded": count}, message="Webhook accepted.")


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
