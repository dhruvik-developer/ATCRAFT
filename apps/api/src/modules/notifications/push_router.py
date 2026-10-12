"""Own-device API, shared by internal and vendor principals."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import CursorParams, cursor_params
from src.core.permissions import Permission
from src.core.responses import ApiResponse, CursorPaginatedResponse
from src.modules.notifications.push_schemas import (
    PushConfigRead,
    PushDeleteRead,
    PushEndpointRequest,
    PushSubscribeRequest,
    PushSubscriptionRead,
    PushTestRead,
)
from src.modules.notifications.push_service import PushService

router = APIRouter(prefix="/push", dependencies=[require_permission(Permission.NOTIFICATION_VIEW)])
CursorDep = Annotated[CursorParams, Depends(cursor_params)]


@router.get("/config", response_model=ApiResponse[PushConfigRead])
async def push_config(session: DbSession, tenant: Tenant) -> ApiResponse[PushConfigRead]:
    return ApiResponse.ok(PushService.config())


@router.post("/subscriptions", response_model=ApiResponse[PushSubscriptionRead])
async def subscribe(
    payload: PushSubscribeRequest, request: Request, session: DbSession, tenant: Tenant
) -> ApiResponse[PushSubscriptionRead]:
    return ApiResponse.ok(
        await PushService(session, tenant).subscribe(payload, request.headers.get("user-agent"))
    )


@router.get("/subscriptions", response_model=CursorPaginatedResponse[PushSubscriptionRead])
async def list_subscriptions(
    session: DbSession, tenant: Tenant, params: CursorDep
) -> CursorPaginatedResponse[PushSubscriptionRead]:
    rows, pagination = await PushService(session, tenant).list_devices(params)
    return CursorPaginatedResponse(data=rows, pagination=pagination)


@router.delete("/subscriptions/{subscription_id}", response_model=ApiResponse[PushDeleteRead])
async def delete_subscription(
    subscription_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PushDeleteRead]:
    await PushService(session, tenant).remove(subscription_id)
    return ApiResponse.ok(PushDeleteRead(removed=True))


@router.post("/subscriptions/unregister", response_model=ApiResponse[PushDeleteRead])
async def unregister(
    payload: PushEndpointRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[PushDeleteRead]:
    removed = await PushService(session, tenant).unregister(payload.endpoint)
    return ApiResponse.ok(PushDeleteRead(removed=removed))


@router.post("/test", response_model=ApiResponse[PushTestRead])
async def test_push(session: DbSession, tenant: Tenant) -> ApiResponse[PushTestRead]:
    return ApiResponse.ok(PushTestRead(queued=await PushService(session, tenant).test()))
