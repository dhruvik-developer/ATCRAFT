"""HTTP surface for the notification bell.

Every route here addresses the *caller's own* notifications. The recipient is
never a parameter - it is taken from the verified principal, the same way
``company_id`` is - so there is no request shape that reads another employee's
approval queue, and no permission that could accidentally grant it.

ASSUMPTION: all routes are gated on ``notification.view``, including the ones
that write. ``notification.view`` is the only notification permission in the
registry, and this is the conservative option: marking one's own notification
read and choosing where one's own alerts are delivered are not privileged
operations, and inventing a second permission for them would mean every role in
the seed needs it before anyone can dismiss a bell.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import CursorParams, cursor_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
)
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.push_router import router as push_router
from src.modules.notifications.schemas import (
    MarkReadResult,
    NotificationFilters,
    NotificationPreferenceRead,
    NotificationRead,
    PreferencesUpdateRequest,
    UnreadCountRead,
)
from src.modules.notifications.service import NotificationService

router = APIRouter(
    prefix="/notifications", tags=["Notifications"], responses=COMMON_ERROR_RESPONSES
)
router.include_router(push_router)

CursorDep = Annotated[CursorParams, Depends(cursor_params)]


def notification_filters(
    unread_only: Annotated[bool, Query(description="Only unacknowledged notifications")] = False,
    notification_type: Annotated[NotificationType | None, Query()] = None,
    severity: Annotated[NotificationSeverity | None, Query()] = None,
) -> NotificationFilters:
    return NotificationFilters(
        unread_only=unread_only, notification_type=notification_type, severity=severity
    )


FilterDep = Annotated[NotificationFilters, Depends(notification_filters)]


@router.get(
    "",
    response_model=CursorPaginatedResponse[NotificationRead],
    summary="List my notifications",
    description=(
        "Unread first, then newest first within each group. Keyset-paginated: pass the "
        "`next_cursor` from the previous page rather than a page number, so paging stays "
        "constant-cost however large the history grows."
    ),
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def list_notifications(
    session: DbSession,
    tenant: Tenant,
    filters: FilterDep,
    params: CursorDep,
) -> CursorPaginatedResponse[NotificationRead]:
    items, pagination = await NotificationService(session, tenant).list_feed(filters, params)
    return CursorPaginatedResponse[NotificationRead](data=items, pagination=pagination)


@router.get(
    "/unread-count",
    response_model=ApiResponse[UnreadCountRead],
    summary="Unread badge count",
    description=(
        "Counts up to a ceiling and reports whether it was reached, so the badge can render "
        "`500+` without counting an unbounded backlog on every poll."
    ),
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def unread_count(session: DbSession, tenant: Tenant) -> ApiResponse[UnreadCountRead]:
    count = await NotificationService(session, tenant).unread_count()
    return ApiResponse[UnreadCountRead].ok(count)


@router.get(
    "/preferences",
    response_model=ApiResponse[list[NotificationPreferenceRead]],
    summary="My delivery preferences",
    description=(
        "Returns every notification type with the setting that will actually be applied, "
        "including types the user has never configured."
    ),
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def get_preferences(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[NotificationPreferenceRead]]:
    preferences = await NotificationService(session, tenant).get_preferences()
    return ApiResponse[list[NotificationPreferenceRead]].ok(preferences)


@router.put(
    "/preferences",
    response_model=ApiResponse[list[NotificationPreferenceRead]],
    summary="Update my delivery preferences",
    description=(
        "Submits the changed rows in one request. CRITICAL alerts are still delivered in-app "
        "regardless of the in-app setting."
    ),
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def update_preferences(
    session: DbSession, tenant: Tenant, payload: PreferencesUpdateRequest
) -> ApiResponse[list[NotificationPreferenceRead]]:
    preferences = await NotificationService(session, tenant).update_preferences(payload)
    return ApiResponse[list[NotificationPreferenceRead]].ok(preferences, "Preferences saved")


@router.post(
    "/read-all",
    response_model=ApiResponse[MarkReadResult],
    summary="Mark my notifications read",
    description=(
        "Acknowledges one bounded batch. When `remaining_unread` is non-zero the backlog "
        "exceeded a single batch and the client should repeat the call."
    ),
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def mark_all_read(session: DbSession, tenant: Tenant) -> ApiResponse[MarkReadResult]:
    result = await NotificationService(session, tenant).mark_all_read()
    return ApiResponse[MarkReadResult].ok(result, "Notifications marked read")


@router.post(
    "/{notification_id}/read",
    response_model=ApiResponse[NotificationRead],
    summary="Mark one notification read",
    description="Idempotent: acknowledging an already-read notification returns it unchanged.",
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def mark_read(
    notification_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[NotificationRead]:
    notification = await NotificationService(session, tenant).mark_read(notification_id)
    return ApiResponse[NotificationRead].ok(notification, "Notification marked read")


__all__ = ["router"]
