"""HTTP surface for the audit trail and the approval queue.

**This router has no POST, PUT, PATCH or DELETE for audit logs, and never will.**
Audit entries are written by :func:`src.modules.audit.service.record_audit`
inside the transaction of the business change they describe. Ordinary users must
not be able to modify audit records (master rules section 41), which is why there
is no endpoint to try: the migration additionally installs a PostgreSQL trigger
on ``audit_logs`` that raises on UPDATE and DELETE, so the guarantee holds even
against a future route, a stray script or a psql session - not only against this
file staying as it is.

The two decision endpoints below are the only writes in the module, and they
write to ``approval_requests``, not to the log.

Permission gating comes in two flavours here, deliberately:

* the read endpoints require :data:`Permission.AUDIT_LOG_VIEW` - reading other
  people's activity is an auditor's privilege, not a side effect of being logged
  in;
* the approval endpoints require *any* approval permission
  (:data:`~src.modules.audit.query_service.DECIDABLE_PERMISSIONS`), because
  requiring ``audit_log.view`` there would lock every approver out of their own
  queue. Which specific permission each row needs is a property of the row, so
  the service re-checks it per request - see ``assert_may_decide``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from src.core.deps import CurrentUser, DbSession, Tenant, require_any_permission, require_permission
from src.core.pagination import CursorParams, PageParams, cursor_params, page_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.audit.models import AuditAction
from src.modules.audit.query_service import (
    DECIDABLE_PERMISSIONS,
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    ApprovalService,
    AuditQueryService,
)
from src.modules.audit.schemas import (
    ApprovalDecisionRequest,
    ApprovalFilters,
    ApprovalRequestRead,
    AuditLogFilters,
    AuditLogListItem,
    AuditLogRead,
)

router = APIRouter(prefix="/audit", tags=["Audit"], responses=COMMON_ERROR_RESPONSES)

CursorDep = Annotated[CursorParams, Depends(cursor_params)]
PageDep = Annotated[PageParams, Depends(page_params)]


def audit_log_filters(
    entity_type: Annotated[
        str | None, Query(description="Record type, e.g. `purchase_order`")
    ] = None,
    entity_id: Annotated[uuid.UUID | None, Query(description="Id of the record acted upon")] = None,
    action: Annotated[AuditAction | None, Query(description="Action recorded")] = None,
    user_id: Annotated[
        uuid.UUID | None, Query(description="Actor who performed the action")
    ] = None,
    request_id: Annotated[
        str | None, Query(description="Correlation id, to reconstruct one request")
    ] = None,
    date_from: Annotated[
        datetime | None,
        Query(
            description=(f"Start of the window (UTC). Defaults to {DEFAULT_WINDOW_DAYS} days ago.")
        ),
    ] = None,
    date_to: Annotated[
        datetime | None, Query(description="End of the window (UTC). Defaults to now.")
    ] = None,
) -> AuditLogFilters:
    return AuditLogFilters(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        user_id=user_id,
        request_id=request_id,
        date_from=date_from,
        date_to=date_to,
    )


def approval_filters(
    entity_type: Annotated[
        str | None, Query(description="Only requests against this record type")
    ] = None,
) -> ApprovalFilters:
    return ApprovalFilters(entity_type=entity_type)


LogFilterDep = Annotated[AuditLogFilters, Depends(audit_log_filters)]
ApprovalFilterDep = Annotated[ApprovalFilters, Depends(approval_filters)]


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


@router.get(
    "/logs",
    response_model=CursorPaginatedResponse[AuditLogListItem],
    summary="Search the audit trail",
    description=(
        "Newest first, keyset-paginated: pass the `next_cursor` from the previous page rather "
        "than a page number, so paging stays constant-cost however large the trail grows.\n\n"
        f"The date window is mandatory. Omitting it means the last {DEFAULT_WINDOW_DAYS} days; "
        f"a window wider than {MAX_WINDOW_DAYS} days is rejected, because an unbounded query "
        "against the largest table in the system is a denial of service rather than a slow "
        "page. The window actually used is echoed in `message`.\n\n"
        "Rows carry `changed_fields` but not the before/after values - fetch a single entry "
        "for those."
    ),
    dependencies=[require_permission(Permission.AUDIT_LOG_VIEW)],
)
async def list_audit_logs(
    session: DbSession,
    tenant: Tenant,
    filters: LogFilterDep,
    params: CursorDep,
) -> CursorPaginatedResponse[AuditLogListItem]:
    items, pagination, window = await AuditQueryService(session, tenant).list_logs(filters, params)
    return CursorPaginatedResponse[AuditLogListItem](
        data=items,
        pagination=pagination,
        message=f"Audit entries from {window.start.isoformat()} to {window.end.isoformat()}",
    )


@router.get(
    "/logs/{log_id}",
    response_model=ApiResponse[AuditLogRead],
    summary="One audit entry in full",
    description=(
        "Includes `old_values`, `new_values` and the request provenance (ip address, user "
        "agent, correlation id) that the list view omits."
    ),
    dependencies=[require_permission(Permission.AUDIT_LOG_VIEW)],
)
async def get_audit_log(
    log_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[AuditLogRead]:
    entry = await AuditQueryService(session, tenant).get_log(log_id)
    return ApiResponse[AuditLogRead].ok(entry)


@router.get(
    "/entity/{entity_type}/{entity_id}",
    response_model=CursorPaginatedResponse[AuditLogListItem],
    summary="Full history of one record",
    description=(
        "The traceability trail required by master rules section 42: every recorded action "
        "against one record, newest first.\n\n"
        "Not date-windowed, unlike `/audit/logs` - the point of this endpoint is the complete "
        "history, and pinning the query to one `entity_id` already bounds it."
    ),
    dependencies=[require_permission(Permission.AUDIT_LOG_VIEW)],
)
async def get_entity_history(
    entity_type: Annotated[
        str, Path(min_length=1, max_length=64, description="Record type, e.g. `purchase_order`")
    ],
    entity_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: CursorDep,
) -> CursorPaginatedResponse[AuditLogListItem]:
    items, pagination = await AuditQueryService(session, tenant).entity_history(
        entity_type, entity_id, params
    )
    return CursorPaginatedResponse[AuditLogListItem](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Approvals
# -----------------------------------------------------------------------------


@router.get(
    "/approvals",
    response_model=PaginatedResponse[ApprovalRequestRead],
    summary="Approvals waiting for me",
    description=(
        "Pending requests whose action this caller holds the permission for, oldest first. "
        "Requests the caller cannot decide are filtered out in SQL rather than merely "
        "disabled in the UI: their `payload` can name amounts and vendors.\n\n"
        "`can_decide` is false on a row the caller raised themselves - segregation of duties "
        "- with the reason in `blocked_reason`."
    ),
    dependencies=[require_any_permission(*DECIDABLE_PERMISSIONS)],
)
async def list_pending_approvals(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: ApprovalFilterDep,
    params: PageDep,
) -> PaginatedResponse[ApprovalRequestRead]:
    items, pagination = await ApprovalService(session, tenant, principal).list_pending(
        filters, params
    )
    return PaginatedResponse[ApprovalRequestRead](data=items, pagination=pagination)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApiResponse[ApprovalRequestRead],
    summary="Approve a pending request",
    description=(
        "Records the decision and writes an audit entry against the record the request "
        "targets, in one transaction.\n\n"
        "Refused with 403 when the caller does not hold the permission the requested action "
        "needs, and when the caller raised the request themselves - a request approved by its "
        "own author carries no second judgement. Refused with 422 when the request has "
        "already been decided."
    ),
    dependencies=[require_any_permission(*DECIDABLE_PERMISSIONS)],
)
async def approve_request(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[ApprovalRequestRead]:
    decided = await ApprovalService(session, tenant, principal).approve(approval_id, payload)
    return ApiResponse[ApprovalRequestRead].ok(decided, "Approval recorded")


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApiResponse[ApprovalRequestRead],
    summary="Reject a pending request",
    description=(
        "Same authority rules as approving. A `note` is required: a rejection without a "
        "stated reason is not evidence of a judgement, it is the start of a phone call."
    ),
    dependencies=[require_any_permission(*DECIDABLE_PERMISSIONS)],
)
async def reject_request(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[ApprovalRequestRead]:
    decided = await ApprovalService(session, tenant, principal).reject(approval_id, payload)
    return ApiResponse[ApprovalRequestRead].ok(decided, "Rejection recorded")


__all__ = ["router"]
