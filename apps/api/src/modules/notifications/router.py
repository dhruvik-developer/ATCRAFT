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

from src.core.deps import (
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.messages import success_message
from src.core.pagination import CursorParams, bind_query_filters, cursor_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
)
from src.modules.notifications.alert_service import NotificationAlertService
from src.modules.notifications.channel_service import NotificationChannelService
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.push_router import router as push_router
from src.modules.notifications.schemas import (
    MarkReadResult,
    NotificationAlertRuleRead,
    NotificationAlertRulesUpdate,
    NotificationChannelsRead,
    NotificationChannelTemplateRead,
    NotificationChannelTemplatesUpdate,
    NotificationChannelTestRequest,
    NotificationDeliveryRead,
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

#: Company messaging and smart-alert administration is staff work. The portal
#: allow-lists in ``core/deps.py`` admit the whole ``/notifications`` root,
#: because the bell, its preferences and push registration are a vendor's or a
#: dealer's too, so these routes refuse a portal account themselves - whatever a
#: mis-built portal role or a direct grant lets it hold.
STAFF_ONLY = (Depends(forbid_vendor_users), Depends(forbid_dealer_users))


def notification_filters(
    unread_only: Annotated[bool, Query(description="Only unacknowledged notifications")] = False,
    notification_type: Annotated[NotificationType | None, Query()] = None,
    severity: Annotated[NotificationSeverity | None, Query()] = None,
) -> NotificationFilters:
    return bind_query_filters(
        NotificationFilters,
        unread_only=unread_only,
        notification_type=notification_type,
        severity=severity,
    )


FilterDep = Annotated[NotificationFilters, Depends(notification_filters)]


@router.get(
    "/alert-rules",
    response_model=ApiResponse[list[NotificationAlertRuleRead]],
    summary="Effective smart-alert rules",
    dependencies=[require_permission(Permission.SETTINGS_VIEW), *STAFF_ONLY],
)
async def list_alert_rules(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[NotificationAlertRuleRead]]:
    rules = await NotificationAlertService(session, tenant).list_rules()
    return ApiResponse[list[NotificationAlertRuleRead]].ok(rules)


@router.put(
    "/alert-rules",
    response_model=ApiResponse[list[NotificationAlertRuleRead]],
    summary="Update smart-alert rules",
    dependencies=[require_permission(Permission.SETTINGS_MANAGE), *STAFF_ONLY],
)
async def update_alert_rules(
    session: DbSession,
    tenant: Tenant,
    payload: NotificationAlertRulesUpdate,
) -> ApiResponse[list[NotificationAlertRuleRead]]:
    rules = await NotificationAlertService(session, tenant).update_rules(payload)
    return ApiResponse[list[NotificationAlertRuleRead]].ok(
        rules, success_message("Smart alert rules", "saved")
    )


@router.get(
    "/channels",
    response_model=ApiResponse[NotificationChannelsRead],
    summary="Enabled external notification channels",
    dependencies=[require_permission(Permission.NOTIFICATION_VIEW)],
)
async def notification_channels(
    session: DbSession, tenant: Tenant
) -> ApiResponse[NotificationChannelsRead]:
    result = await NotificationChannelService(session, tenant).channel_status()
    return ApiResponse[NotificationChannelsRead].ok(result)


@router.get(
    "/channel-templates",
    response_model=ApiResponse[list[NotificationChannelTemplateRead]],
    summary="Messaging templates configured for this company",
    dependencies=[require_permission(Permission.SETTINGS_VIEW), *STAFF_ONLY],
)
async def list_channel_templates(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[NotificationChannelTemplateRead]]:
    result = await NotificationChannelService(session, tenant).list_templates()
    return ApiResponse[list[NotificationChannelTemplateRead]].ok(result)


@router.get(
    "/channel-status",
    response_model=ApiResponse[NotificationChannelsRead],
    summary="Provider status for messaging administration",
    dependencies=[require_permission(Permission.SETTINGS_VIEW), *STAFF_ONLY],
)
async def admin_channel_status(
    session: DbSession, tenant: Tenant
) -> ApiResponse[NotificationChannelsRead]:
    result = await NotificationChannelService(session, tenant).channel_status()
    return ApiResponse[NotificationChannelsRead].ok(result)


@router.put(
    "/channel-templates",
    response_model=ApiResponse[list[NotificationChannelTemplateRead]],
    summary="Create or update messaging templates",
    dependencies=[require_permission(Permission.SETTINGS_MANAGE), *STAFF_ONLY],
)
async def update_channel_templates(
    session: DbSession, tenant: Tenant, payload: NotificationChannelTemplatesUpdate
) -> ApiResponse[list[NotificationChannelTemplateRead]]:
    result = await NotificationChannelService(session, tenant).update_templates(payload)
    return ApiResponse[list[NotificationChannelTemplateRead]].ok(
        result, success_message("Messaging templates", "saved")
    )


@router.post(
    "/channel-templates/test",
    response_model=ApiResponse[dict[str, bool]],
    summary="Send a selected template to the signed-in user's verified number",
    dependencies=[require_permission(Permission.SETTINGS_MANAGE), *STAFF_ONLY],
)
async def test_channel_template(
    session: DbSession, tenant: Tenant, payload: NotificationChannelTestRequest
) -> ApiResponse[dict[str, bool]]:
    await NotificationChannelService(session, tenant).test_send(payload)
    return ApiResponse[dict[str, bool]].ok({"sent": True}, "Test message sent")


@router.get(
    "/deliveries",
    response_model=CursorPaginatedResponse[NotificationDeliveryRead],
    summary="Messaging delivery history",
    dependencies=[require_permission(Permission.SETTINGS_VIEW), *STAFF_ONLY],
)
async def list_messaging_deliveries(
    session: DbSession, tenant: Tenant, params: CursorDep
) -> CursorPaginatedResponse[NotificationDeliveryRead]:
    items, pagination = await NotificationChannelService(session, tenant).list_deliveries(params)
    return CursorPaginatedResponse[NotificationDeliveryRead](data=items, pagination=pagination)


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
    return ApiResponse[list[NotificationPreferenceRead]].ok(
        preferences, success_message("Preferences", "saved")
    )


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
