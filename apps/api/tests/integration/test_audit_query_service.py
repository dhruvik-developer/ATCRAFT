"""Service-level tests for the audit query API, against a real PostgreSQL.

These cover what only a database can prove: that the entity trail comes back in
the right order and contains exactly the right rows, that the mandatory date
window really is applied in SQL, that the list projection does not fetch the wide
JSONB columns, that one tenant cannot read another's history, and that a decision
on an approval request and its audit entry commit or roll back together.

Fixtures (``session``, ``tenant``, ``principal``, ``company``, ``other_company``,
``user``) come from ``tests/conftest.py``; the outer transaction is rolled back
after each test.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.pagination import CursorParams, PageParams
from src.core.permissions import Permission
from src.core.security import hash_password
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.audit.query_service import (
    MAX_WINDOW_DAYS,
    ApprovalService,
    AuditQueryService,
    resolve_window,
)
from src.modules.audit.repository import AuditLogRepository
from src.modules.audit.schemas import (
    ApprovalDecisionRequest,
    ApprovalFilters,
    ApprovalStatus,
    AuditLogFilters,
)
from src.modules.audit.service import record_audit

pytestmark = pytest.mark.integration

PO_APPROVE = str(Permission.PURCHASE_ORDER_APPROVE)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _make_user(session: AsyncSession, company_id: uuid.UUID):
    from src.modules.users.models import User

    record = User(
        company_id=company_id,
        email=f"a-{uuid.uuid4().hex[:10]}@test.local",
        full_name="Second Person",
        password_hash=hash_password("TestPassword!2026"),
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


def _principal_for(
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    *permissions: Permission,
    is_superuser: bool = False,
) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email="approver@test.local",
        full_name="Approver Person",
        is_superuser=is_superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


async def _log(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: AuditAction,
    summary: str,
    created_at=None,
    user_id: uuid.UUID | None = None,
) -> AuditLog:
    """Insert one audit row directly, at a timestamp the test chooses."""
    entry = AuditLog(
        company_id=company_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        created_by_id=user_id,
    )
    # Always stamped explicitly. The column default is `now()`, which is the
    # *transaction* clock, so every row a test wrote would share one timestamp -
    # and the date-window assertions would depend on the database clock agreeing
    # with the Python one to the millisecond.
    entry.created_at = created_at if created_at is not None else utcnow() - timedelta(minutes=1)
    session.add(entry)
    await session.flush()
    return entry


async def _approval(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    requested_by: uuid.UUID | None,
    requested_action: str = PO_APPROVE,
    entity_type: str = "purchase_order",
    entity_id: uuid.UUID | None = None,
    entity_code: str = "PO-0001",
    status: str = "PENDING",
) -> ApprovalRequest:
    record = ApprovalRequest(
        company_id=company_id,
        entity_type=entity_type,
        entity_id=entity_id or uuid.uuid4(),
        entity_code=entity_code,
        requested_action=requested_action,
        reason="Above the delegated limit",
        payload={"total": "125000.00"},
        status=status,
        created_by_id=requested_by,
    )
    session.add(record)
    await session.flush()
    return record


# -----------------------------------------------------------------------------
# The traceability trail (master rules section 42)
# -----------------------------------------------------------------------------


async def test_entity_history_returns_the_full_trail_newest_first(
    session: AsyncSession, tenant: TenantContext, company, user
):
    order_id = uuid.uuid4()
    base = utcnow() - timedelta(days=3)

    # A realistic lifecycle, written out of order on purpose so a test that
    # merely returned insertion order would pass by accident.
    for offset_minutes, action, summary in (
        (30, AuditAction.STATUS_CHANGE, "DRAFT -> PENDING_APPROVAL"),
        (0, AuditAction.CREATE, "purchase order raised"),
        (90, AuditAction.APPROVE, "approved"),
        (60, AuditAction.UPDATE, "quantity amended"),
    ):
        await _log(
            session,
            company.id,
            entity_type="purchase_order",
            entity_id=order_id,
            action=action,
            summary=summary,
            created_at=base + timedelta(minutes=offset_minutes),
            user_id=user.id,
        )

    # Noise that must not appear: another record of the same type, and the same
    # id under a different type.
    await _log(
        session,
        company.id,
        entity_type="purchase_order",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="a different order",
        created_at=base + timedelta(minutes=45),
    )
    await _log(
        session,
        company.id,
        entity_type="sales_order",
        entity_id=order_id,
        action=AuditAction.CREATE,
        summary="same id, different type",
        created_at=base + timedelta(minutes=45),
    )

    items, pagination = await AuditQueryService(session, tenant).entity_history(
        "purchase_order", order_id, CursorParams(limit=50)
    )

    assert [i.summary for i in items] == [
        "approved",
        "quantity amended",
        "DRAFT -> PENDING_APPROVAL",
        "purchase order raised",
    ]
    assert [i.action for i in items] == [
        AuditAction.APPROVE,
        AuditAction.UPDATE,
        AuditAction.STATUS_CHANGE,
        AuditAction.CREATE,
    ]
    assert pagination.has_next is False
    assert pagination.next_cursor is None


async def test_entity_history_accepts_the_entity_type_in_any_case(
    session: AsyncSession, tenant: TenantContext, company
):
    order_id = uuid.uuid4()
    await _log(
        session,
        company.id,
        entity_type="purchase_order",
        entity_id=order_id,
        action=AuditAction.CREATE,
        summary="raised",
    )

    items, _ = await AuditQueryService(session, tenant).entity_history(
        " Purchase_Order ", order_id, CursorParams(limit=10)
    )

    assert len(items) == 1


async def test_entity_history_pages_without_losing_or_repeating_rows(
    session: AsyncSession, tenant: TenantContext, company
):
    order_id = uuid.uuid4()
    base = utcnow() - timedelta(days=1)
    for index in range(7):
        await _log(
            session,
            company.id,
            entity_type="purchase_order",
            entity_id=order_id,
            action=AuditAction.UPDATE,
            summary=f"change {index}",
            created_at=base + timedelta(minutes=index),
        )

    service = AuditQueryService(session, tenant)
    seen: list[str] = []
    cursor = None
    for _ in range(5):  # bounded, so a paging bug fails rather than hangs
        items, pagination = await service.entity_history(
            "purchase_order", order_id, CursorParams(cursor=cursor, limit=3)
        )
        seen.extend(i.summary for i in items)
        if not pagination.has_next:
            break
        cursor = pagination.next_cursor

    assert seen == [f"change {i}" for i in reversed(range(7))]
    assert len(set(seen)) == 7


async def test_entity_history_is_tenant_scoped(
    session: AsyncSession, tenant: TenantContext, company, other_company
):
    order_id = uuid.uuid4()
    await _log(
        session,
        company.id,
        entity_type="purchase_order",
        entity_id=order_id,
        action=AuditAction.CREATE,
        summary="ours",
    )
    await _log(
        session,
        other_company.id,
        entity_type="purchase_order",
        entity_id=order_id,
        action=AuditAction.CREATE,
        summary="theirs",
    )

    items, _ = await AuditQueryService(session, tenant).entity_history(
        "purchase_order", order_id, CursorParams(limit=50)
    )

    assert [i.summary for i in items] == ["ours"]


# -----------------------------------------------------------------------------
# The feed, its window and its filters
# -----------------------------------------------------------------------------


async def test_the_default_window_excludes_rows_older_than_thirty_days(
    session: AsyncSession, tenant: TenantContext, company
):
    entity_id = uuid.uuid4()
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=entity_id,
        action=AuditAction.CREATE,
        summary="recent",
        created_at=utcnow() - timedelta(days=2),
    )
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=entity_id,
        action=AuditAction.CREATE,
        summary="ancient",
        created_at=utcnow() - timedelta(days=200),
    )

    items, _, window = await AuditQueryService(session, tenant).list_logs(
        AuditLogFilters(entity_type="item"), CursorParams(limit=50)
    )

    assert [i.summary for i in items] == ["recent"]
    assert window.days == pytest.approx(30, abs=0.01)


async def test_an_explicit_window_reaches_older_rows(
    session: AsyncSession, tenant: TenantContext, company
):
    entity_id = uuid.uuid4()
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=entity_id,
        action=AuditAction.CREATE,
        summary="ancient",
        created_at=utcnow() - timedelta(days=200),
    )

    items, _, _ = await AuditQueryService(session, tenant).list_logs(
        AuditLogFilters(entity_type="item", date_from=utcnow() - timedelta(days=300)),
        CursorParams(limit=50),
    )

    assert [i.summary for i in items] == ["ancient"]


async def test_an_over_wide_window_is_refused_before_any_query_runs(
    session: AsyncSession, tenant: TenantContext
):
    with pytest.raises(ValidationError) as excinfo:
        await AuditQueryService(session, tenant).list_logs(
            AuditLogFilters(date_from=utcnow() - timedelta(days=MAX_WINDOW_DAYS + 10)),
            CursorParams(limit=50),
        )

    assert excinfo.value.details["max_days"] == MAX_WINDOW_DAYS


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "login"),
        ("entity_type", "item"),
        ("request_id", "login"),
    ],
)
async def test_the_feed_filters_narrow_the_result(
    session: AsyncSession, tenant: TenantContext, company, user, field, expected
):
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        summary="item",
        user_id=user.id,
    )
    signin = AuditLog(
        company_id=company.id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        summary="login",
        request_id="req-abc-123",
        created_by_id=user.id,
    )
    session.add(signin)
    await session.flush()

    filters = {
        "action": AuditLogFilters(action=AuditAction.LOGIN),
        "entity_type": AuditLogFilters(entity_type="item"),
        "request_id": AuditLogFilters(request_id="req-abc-123"),
    }[field]

    items, _, _ = await AuditQueryService(session, tenant).list_logs(
        filters, CursorParams(limit=50)
    )

    assert [i.summary for i in items] == [expected]


async def test_the_actor_filter_selects_one_users_activity(
    session: AsyncSession, tenant: TenantContext, company, user
):
    other = await _make_user(session, company.id)
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        summary="by me",
        user_id=user.id,
    )
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        summary="by them",
        user_id=other.id,
    )

    items, _, _ = await AuditQueryService(session, tenant).list_logs(
        AuditLogFilters(user_id=other.id), CursorParams(limit=50)
    )

    assert [i.summary for i in items] == ["by them"]


async def test_the_feed_is_tenant_scoped(
    session: AsyncSession, tenant: TenantContext, company, other_company
):
    await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="ours",
    )
    await _log(
        session,
        other_company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="theirs",
    )

    items, _, _ = await AuditQueryService(session, tenant).list_logs(
        AuditLogFilters(), CursorParams(limit=50)
    )

    assert [i.summary for i in items] == ["ours"]


# -----------------------------------------------------------------------------
# Projection and detail
# -----------------------------------------------------------------------------


async def test_the_listing_does_not_fetch_the_wide_jsonb_columns(
    session: AsyncSession, tenant: TenantContext, company, user
):
    await record_audit(
        session,
        company_id=company.id,
        action=AuditAction.UPDATE,
        entity_type="item",
        entity_id=uuid.uuid4(),
        summary="repriced",
        user_id=user.id,
        old_values={"standard_cost": "10.00"},
        new_values={"standard_cost": "12.50"},
    )
    # Detach everything first: an object still in the identity map would report
    # its columns as loaded and the projection would go unmeasured.
    session.expunge_all()

    rows, _ = await AuditLogRepository(session, tenant).list_logs(
        AuditLogFilters(entity_type="item"),
        resolve_window(None, utcnow() + timedelta(minutes=5)),
        CursorParams(limit=50),
    )

    unloaded = inspect(rows[0]).unloaded
    # The three widest columns in the table are not fetched for a listing, and
    # raiseload=True means a caller that touches one fails loudly rather than
    # emitting a lazy SELECT per row.
    assert {"old_values", "new_values", "context"} <= unloaded
    assert rows[0].changed_fields == ["standard_cost"]


async def test_the_detail_read_returns_the_before_and_after_values(
    session: AsyncSession, tenant: TenantContext, company, user
):
    entry = await record_audit(
        session,
        company_id=company.id,
        action=AuditAction.UPDATE,
        entity_type="item",
        entity_id=uuid.uuid4(),
        summary="repriced",
        user_id=user.id,
        old_values={"standard_cost": "10.00"},
        new_values={"standard_cost": "12.50"},
    )

    detail = await AuditQueryService(session, tenant).get_log(entry.id)

    assert detail.old_values == {"standard_cost": "10.00"}
    assert detail.new_values == {"standard_cost": "12.50"}
    assert detail.changed_fields == ["standard_cost"]


async def test_another_tenants_entry_is_a_not_found_not_a_forbidden(
    session: AsyncSession, tenant: TenantContext, other_company
):
    entry = await _log(
        session,
        other_company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="theirs",
    )

    # Confirming that an id exists in another company is itself a disclosure.
    with pytest.raises(NotFoundError):
        await AuditQueryService(session, tenant).get_log(entry.id)


async def test_the_audit_repository_refuses_to_mutate_the_log(
    session: AsyncSession, tenant: TenantContext, company
):
    repo = AuditLogRepository(session, tenant)
    entry = await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="written once",
    )

    # Second line of defence only - the migration installs a trigger that blocks
    # UPDATE and DELETE on audit_logs at the database.
    with pytest.raises(TypeError):
        repo.add(entry)
    with pytest.raises(TypeError):
        await repo.hard_delete(entry)
    with pytest.raises(TypeError):
        await repo.bulk_delete([entry.id])


# -----------------------------------------------------------------------------
# The approval queue
# -----------------------------------------------------------------------------


async def test_the_queue_shows_only_requests_the_caller_can_decide(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    await _approval(session, company.id, requested_by=requester.id)
    await _approval(
        session,
        company.id,
        requested_by=requester.id,
        requested_action=str(Permission.PAYMENT_APPROVE),
        entity_type="payment",
        entity_code="PAY-0001",
    )

    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    items, pagination = await ApprovalService(session, tenant, principal).list_pending(
        ApprovalFilters(), PageParams(page=1, limit=50)
    )

    assert [i.entity_code for i in items] == ["PO-0001"]
    assert pagination.total == 1
    assert items[0].can_decide is True
    assert items[0].required_permission == PO_APPROVE


async def test_a_caller_with_no_approval_rights_sees_an_empty_queue(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    await _approval(session, company.id, requested_by=requester.id)

    principal = _principal_for(company.id, user.id, Permission.ITEM_VIEW)
    items, pagination = await ApprovalService(session, tenant, principal).list_pending(
        ApprovalFilters(), PageParams(page=1, limit=50)
    )

    # An empty permission set must not degenerate into "no filter".
    assert items == []
    assert pagination.total == 0


async def test_the_queue_flags_the_callers_own_request_as_undecidable(
    session: AsyncSession, tenant: TenantContext, company, user
):
    from src.modules.audit.query_service import SELF_APPROVAL_BLOCKED

    await _approval(session, company.id, requested_by=user.id)

    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    items, _ = await ApprovalService(session, tenant, principal).list_pending(
        ApprovalFilters(), PageParams(page=1, limit=50)
    )

    assert items[0].can_decide is False
    assert items[0].blocked_reason == SELF_APPROVAL_BLOCKED


async def test_the_queue_holds_only_pending_requests_oldest_first(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    first = await _approval(session, company.id, requested_by=requester.id, entity_code="PO-0001")
    second = await _approval(session, company.id, requested_by=requester.id, entity_code="PO-0002")
    await _approval(
        session,
        company.id,
        requested_by=requester.id,
        entity_code="PO-0003",
        status="APPROVED",
    )

    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    items, _ = await ApprovalService(session, tenant, principal).list_pending(
        ApprovalFilters(), PageParams(page=1, limit=50)
    )

    assert [i.id for i in items] == [first.id, second.id]


async def test_the_queue_is_tenant_scoped(
    session: AsyncSession, tenant: TenantContext, company, other_company, user
):
    requester = await _make_user(session, other_company.id)
    await _approval(session, other_company.id, requested_by=requester.id)

    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    items, _ = await ApprovalService(session, tenant, principal).list_pending(
        ApprovalFilters(), PageParams(page=1, limit=50)
    )

    assert items == []


# -----------------------------------------------------------------------------
# Decisions
# -----------------------------------------------------------------------------


async def test_approving_records_the_decision_and_its_audit_entry(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    request = await _approval(session, company.id, requested_by=requester.id)
    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)

    decided = await ApprovalService(session, tenant, principal).approve(
        request.id, ApprovalDecisionRequest(note="Within the revised budget")
    )

    assert decided.status is ApprovalStatus.APPROVED
    assert decided.decided_by_id == user.id
    assert decided.decision_note == "Within the revised budget"
    # Terminal: the row is no longer decidable by anyone.
    assert decided.can_decide is False

    trail = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.company_id == company.id,
                    AuditLog.entity_id == request.entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(trail) == 1
    entry = trail[0]
    # Written against the *target* record, so the decision shows up in the
    # purchase order's own trail rather than only in the approval row's.
    assert entry.action == AuditAction.APPROVE
    assert entry.entity_type == "purchase_order"
    assert entry.created_by_id == user.id
    assert entry.old_values == {"status": "PENDING"}
    assert entry.new_values == {"status": "APPROVED"}
    assert entry.context["approval_request_id"] == str(request.id)
    assert entry.context["requested_by_id"] == str(requester.id)


async def test_rejecting_requires_a_reason(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    request = await _approval(session, company.id, requested_by=requester.id)
    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    service = ApprovalService(session, tenant, principal)

    with pytest.raises(ValidationError):
        await service.reject(request.id, ApprovalDecisionRequest(note="   "))

    decided = await service.reject(request.id, ApprovalDecisionRequest(note="Quote is out of date"))

    assert decided.status is ApprovalStatus.REJECTED
    audit_actions = (
        (
            await session.execute(
                select(AuditLog.action).where(AuditLog.entity_id == request.entity_id)
            )
        )
        .scalars()
        .all()
    )
    # Exactly one entry: the refused attempt rolled back with its transaction.
    assert list(audit_actions) == [AuditAction.REJECT]


async def test_a_decided_request_cannot_be_decided_again(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    request = await _approval(session, company.id, requested_by=requester.id)
    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)
    service = ApprovalService(session, tenant, principal)

    await service.approve(request.id, ApprovalDecisionRequest())

    with pytest.raises(InvalidStateTransitionError):
        await service.reject(request.id, ApprovalDecisionRequest(note="changed my mind"))

    count = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == request.entity_id)
    )
    assert count == 1


async def test_the_requester_cannot_approve_their_own_request(
    session: AsyncSession, tenant: TenantContext, company, user
):
    request = await _approval(session, company.id, requested_by=user.id)
    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)

    with pytest.raises(AuthorizationError):
        await ApprovalService(session, tenant, principal).approve(
            request.id, ApprovalDecisionRequest()
        )

    await session.refresh(request)
    assert request.status == "PENDING"
    assert request.decided_by_id is None
    count = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == request.entity_id)
    )
    assert count == 0


async def test_a_superuser_may_decide_their_own_request(
    session: AsyncSession, tenant: TenantContext, company, user
):
    request = await _approval(session, company.id, requested_by=user.id)
    principal = _principal_for(company.id, user.id, is_superuser=True)

    decided = await ApprovalService(session, tenant, principal).approve(
        request.id, ApprovalDecisionRequest(note="Emergency override")
    )

    assert decided.status is ApprovalStatus.APPROVED


async def test_deciding_without_the_underlying_permission_is_refused(
    session: AsyncSession, tenant: TenantContext, company, user
):
    requester = await _make_user(session, company.id)
    request = await _approval(session, company.id, requested_by=requester.id)
    principal = _principal_for(company.id, user.id, Permission.PAYMENT_APPROVE)

    with pytest.raises(PermissionDeniedError) as excinfo:
        await ApprovalService(session, tenant, principal).approve(
            request.id, ApprovalDecisionRequest()
        )

    assert excinfo.value.details["required_permission"] == PO_APPROVE
    await session.refresh(request)
    assert request.status == "PENDING"


async def test_another_tenants_approval_request_is_not_found(
    session: AsyncSession, tenant: TenantContext, company, other_company, user
):
    requester = await _make_user(session, other_company.id)
    request = await _approval(session, other_company.id, requested_by=requester.id)
    principal = _principal_for(company.id, user.id, Permission.PURCHASE_ORDER_APPROVE)

    with pytest.raises(NotFoundError):
        await ApprovalService(session, tenant, principal).approve(
            request.id, ApprovalDecisionRequest()
        )


async def test_an_entry_recorded_with_an_ip_address_can_be_opened(
    session: AsyncSession, tenant: TenantContext, company, user
):
    """``ip_address`` is an ``INET`` column, and the driver does not return a string.

    asyncpg hands back an ``ipaddress.IPv4Address`` for ``INET``, so
    ``AuditLogRead.model_validate`` raised on a field declared ``str | None`` -
    and ``GET /audit/logs/{id}`` answered 500 for every entry the API had ever
    written, since the middleware records the caller's address on all of them.
    The list endpoint was unaffected, so the trail looked healthy and no entry
    could be opened.

    The fix is the ``IpAddress`` annotation in ``src.core.types``; this pins the
    behaviour at the read boundary where the failure actually appeared.
    """
    entry = await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.UPDATE,
        summary="Recorded from a real request",
        user_id=user.id,
    )
    entry.ip_address = "203.0.113.7"
    await session.flush()
    # Re-read through the driver rather than trusting the value we just set: the
    # string only becomes an IPv4Address on the way back out of Postgres.
    # `refresh` and not `expire`: expiring leaves the load to happen during
    # `model_validate`, which is synchronous, and that is a MissingGreenlet.
    await session.refresh(entry)

    read = await AuditQueryService(session, tenant).get_log(entry.id)

    assert read.ip_address == "203.0.113.7"
    assert isinstance(read.ip_address, str)


async def test_an_entry_with_no_ip_address_still_reads_as_none(
    session: AsyncSession, tenant: TenantContext, company, user
):
    """The renderer must not turn a missing address into the string "None"."""
    entry = await _log(
        session,
        company.id,
        entity_type="item",
        entity_id=uuid.uuid4(),
        action=AuditAction.CREATE,
        summary="Recorded by a background job, which has no request behind it",
        user_id=user.id,
    )
    await session.refresh(entry)

    read = await AuditQueryService(session, tenant).get_log(entry.id)
    assert read.ip_address is None
