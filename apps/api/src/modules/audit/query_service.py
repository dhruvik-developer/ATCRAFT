"""Business logic for the audit query API and the approval queue.

Why this lives beside ``service.py`` rather than inside it: ``service.py`` is the
*recording* half of the module - ``record_audit`` and ``record_status_change`` -
which every other module imports and which must stay exactly as it is. This file
is the *reading* half. Routes must stay thin (section 74), so the rules below
cannot live in ``router.py``; keeping them in their own module leaves the write
path untouched while still putting a service layer between the routes and the
repositories.

Three rules are enforced here and nowhere else.

**Every log query is bounded in time.** ``audit_logs`` is the largest table in
the system and never shrinks. An unbounded query against it is not "a slow
endpoint", it is a denial of service that any authenticated auditor could trigger
by accident, so the window is mandatory: :func:`resolve_window` fills in a
default and rejects anything wider than the ceiling. The one read that is exempt
is the single-record trail, which is already pinned to one ``entity_id``.

**A decision is a state transition, not a field assignment.** ``PENDING`` moves
to ``APPROVED`` or ``REJECTED`` and to nothing else, ever. Routing it through
:data:`APPROVAL_DECISION_MACHINE` is what makes a second approve of an
already-approved request a 422 with a clear code instead of a silently
overwritten decision (sections 18/73).

**Nobody approves their own request.** Segregation of duties is the entire point
of an approval workflow: a purchase order approved by the person who raised it
carries no second judgement and is worthless as the evidence section 42 asks for.
The only exception is a superuser, who can already do anything and for whom the
audit entry is the control.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.actors import actor_id
from src.core.clock import utcnow
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    NotFoundError,
    PermissionDeniedError,
    SelfApprovalError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.messages import date_before, self_approval_refused, too_long
from src.core.pagination import CursorParams, PageParams
from src.core.permissions import Permission
from src.core.responses import CursorPagination, OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import ApprovalRequest
from src.modules.audit.repository import ApprovalRequestRepository, AuditLogRepository
from src.modules.audit.schemas import (
    MAX_ENTITY_TYPE_LENGTH,
    ApprovalDecisionRequest,
    ApprovalFilters,
    ApprovalRequestRead,
    ApprovalStatus,
    AuditLogFilters,
    AuditLogListItem,
    AuditLogRead,
    AuditWindow,
)
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.repository import NotificationRepository

log = get_logger(__name__)

# --- the time-window guard ----------------------------------------------------
#
# ASSUMPTION: a log query with no dates means "the last 30 days", and no query
# may span more than 366 days. Neither number is stated in the master rules; 30
# days is what an operational investigation actually looks at, and 366 covers a
# full financial year plus a leap day, which is the widest range a statutory
# audit legitimately needs in one request. Anything longer is an export, and an
# export is a background job (section 35), not a synchronous page. Change the two
# constants here and nowhere else.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366

_DEFAULT_WINDOW = timedelta(days=DEFAULT_WINDOW_DAYS)
_MAX_WINDOW = timedelta(days=MAX_WINDOW_DAYS)

# ASSUMPTION: a rejection must carry a note and an approval need not. A rejection
# is an instruction to someone else to change something, and "rejected, no reason
# given" is not evidence of a judgement - it is the start of a phone call. This
# is the conservative reading of section 42.
MIN_REJECTION_NOTE_LENGTH = 3

#: The inbox shows it beside a request its own raiser cannot decide, and
#: :func:`assert_may_decide` refuses in it - the one segregation-of-duties sentence.
SELF_APPROVAL_BLOCKED = self_approval_refused("This request", decision="decided")
UNKNOWN_ACTION_BLOCKED = (
    "This request names an action the permission registry does not know, so the "
    "authority it needs cannot be verified. Only a platform administrator may decide it."
)
ALREADY_DECIDED = "This request has already been decided and cannot be decided again."


# -----------------------------------------------------------------------------
# Pure helpers - no session, no I/O, unit-testable
# -----------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Interpret a client-supplied timestamp as UTC.

    ASSUMPTION: a naive datetime on the query string means UTC rather than the
    caller's local time. The API stores and returns UTC everywhere (section 28),
    so reading a bare ``2026-01-01T00:00:00`` as anything else would silently
    shift the window - and there is no timezone on the request to shift it by.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_window(
    date_from: datetime | None,
    date_to: datetime | None,
    *,
    now: datetime | None = None,
) -> AuditWindow:
    """Turn the optional date filters into the bounded range the query will use.

    Missing bounds are defaulted rather than dropped, and an over-wide range is
    rejected rather than clamped: silently narrowing a 5-year request to 1 year
    would return a page that looks complete and is not, which in an audit context
    is worse than an error.
    """
    reference = _as_utc(now) if now is not None else utcnow()

    start = _as_utc(date_from) if date_from is not None else None
    end = _as_utc(date_to) if date_to is not None else None

    # Order matters: date_to defaults to now, and date_from then defaults to one
    # default window before whichever date_to ended up being. With neither
    # supplied that yields the trailing 30 days.
    if end is None:
        end = reference
    if start is None:
        start = end - _DEFAULT_WINDOW

    if start > end:
        raise ValidationError(
            date_before("end of the period", "start of the period") + " The end defaults to now.",
            details={"date_from": start.isoformat(), "date_to": end.isoformat()},
        )

    span = end - start
    if span > _MAX_WINDOW:
        raise ValidationError(
            f"An audit query may span at most {MAX_WINDOW_DAYS} days. "
            "Narrow the range, or request an export for anything longer.",
            details={
                "requested_days": round(span.total_seconds() / 86_400, 2),
                "max_days": MAX_WINDOW_DAYS,
            },
        )

    return AuditWindow(start=start, end=end)


def normalise_entity_type(value: str) -> str:
    """Fold a path-supplied entity type to the form the recorders write."""
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValidationError("Name the record type to read its history.")
    if len(cleaned) > MAX_ENTITY_TYPE_LENGTH:
        raise ValidationError(too_long("The record type", MAX_ENTITY_TYPE_LENGTH))
    return cleaned


#: Permissions that gate an approval. Derived from the registry rather than
#: hand-listed so a new ``*.approve`` permission is picked up by the queue
#: without a second edit here - a permission that exists but is missing from this
#: tuple would make its requests invisible to everyone who holds it.
DECIDABLE_PERMISSIONS: tuple[Permission, ...] = tuple(
    p for p in Permission if p.value.endswith((".approve", "_approve"))
)

_PERMISSION_BY_VALUE: dict[str, Permission] = {p.value: p for p in Permission}


def permission_for_action(requested_action: str) -> Permission | None:
    """The permission an approval request needs, or ``None`` if unrecognised.

    ASSUMPTION: ``approval_requests.requested_action`` holds the permission
    string of the action awaiting approval, e.g. ``"purchase_order.approve"``.
    Nothing in the schema documentation fixes the vocabulary, and this is the
    conservative choice: it means the approver's authority is checked against the
    same registry the underlying action will be checked against, instead of
    against a second mapping table that could drift out of step with it. An
    action outside the registry is not guessed at - it becomes superuser-only.
    """
    return _PERMISSION_BY_VALUE.get(requested_action.strip().lower())


@dataclass(frozen=True, slots=True)
class DecisionAuthority:
    """Whether one principal may decide one approval request, and why not."""

    permission: Permission | None
    allowed: bool
    reason: str | None
    is_self_request: bool


def evaluate_decision_authority(
    principal: Principal,
    *,
    requested_by_id: uuid.UUID | None,
    requested_action: str,
) -> DecisionAuthority:
    """Non-raising authority check, used to annotate the queue.

    The permission is checked before the segregation-of-duties rule so that a
    caller with no authority over this kind of request learns nothing about who
    raised it.
    """
    permission = permission_for_action(requested_action)
    is_self = requested_by_id is not None and requested_by_id == principal.user_id

    if permission is None:
        allowed = principal.is_superuser
        return DecisionAuthority(
            permission=None,
            allowed=allowed,
            reason=None if allowed else UNKNOWN_ACTION_BLOCKED,
            is_self_request=is_self,
        )

    if not principal.has(permission):
        return DecisionAuthority(
            permission=permission,
            allowed=False,
            reason=f"Deciding this request requires the '{permission}' permission.",
            is_self_request=is_self,
        )

    # A superuser is exempt: they can already perform the underlying action
    # directly, so blocking them here would only push the work somewhere that
    # leaves less evidence. The audit entry is the control in that case.
    if is_self and not principal.is_superuser:
        return DecisionAuthority(
            permission=permission,
            allowed=False,
            reason=SELF_APPROVAL_BLOCKED,
            is_self_request=True,
        )

    return DecisionAuthority(
        permission=permission, allowed=True, reason=None, is_self_request=is_self
    )


def assert_may_decide(
    principal: Principal,
    *,
    requested_by_id: uuid.UUID | None,
    requested_action: str,
) -> DecisionAuthority:
    """Raising form of :func:`evaluate_decision_authority`.

    Missing authority is a :class:`PermissionDeniedError` so the frontend can
    switch on the code and name the permission; a self-approval is a plain
    :class:`AuthorizationError`, because the caller *does* hold the permission
    and telling them to go and get it would be wrong advice.
    """
    authority = evaluate_decision_authority(
        principal, requested_by_id=requested_by_id, requested_action=requested_action
    )
    if authority.allowed:
        return authority

    if authority.permission is not None and not principal.has(authority.permission):
        raise PermissionDeniedError(str(authority.permission))
    if authority.permission is not None and authority.is_self_request:
        raise SelfApprovalError("This request", decision="decided")
    raise AuthorizationError(authority.reason or UNKNOWN_ACTION_BLOCKED)


def decidable_actions_for(principal: Principal) -> frozenset[str] | None:
    """The ``requested_action`` values this principal may act on.

    ``None`` means "no restriction" and is returned only for a superuser, who
    must also see requests naming an action outside the registry - nobody else
    can decide those, so nobody else needs them in their queue.
    """
    if principal.is_superuser:
        return None
    return frozenset(str(p) for p in DECIDABLE_PERMISSIONS if principal.has(p))


#: PENDING is the only state a decision can be made from, and both decisions are
#: terminal: an approval that could be un-approved is not evidence of anything.
#: The transitions carry no ``permission`` because the authority a decision needs
#: is a property of the *row* - its ``requested_action`` - not of the edge; that
#: check is :func:`assert_may_decide`.
APPROVAL_DECISION_MACHINE: StateMachine[ApprovalStatus] = StateMachine(
    entity="Approval request",
    initial=ApprovalStatus.PENDING,
    transitions=(
        Transition(
            source=ApprovalStatus.PENDING,
            target=ApprovalStatus.APPROVED,
            label="Approve",
            is_irreversible=True,
        ),
        Transition(
            source=ApprovalStatus.PENDING,
            target=ApprovalStatus.REJECTED,
            label="Reject",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}),
)


def current_status(record: ApprovalRequest) -> ApprovalStatus:
    """Read the stored status as a machine state.

    The column is ``String(24)``, so an unrecognised value is possible in
    principle - from a future status this build does not know about. Refusing to
    decide such a row is the only safe answer: guessing that it is still pending
    could approve something that was already withdrawn.
    """
    try:
        return ApprovalStatus(record.status)
    except ValueError as exc:
        raise BusinessRuleError(
            f"Approval request is in an unrecognised state '{record.status}' and "
            "cannot be decided by this version of the API.",
            details={"status": record.status},
        ) from exc


# -----------------------------------------------------------------------------
# Services
# -----------------------------------------------------------------------------


class AuditQueryService:
    """Read side of the audit trail.

    There is no write method on this class by design. Audit rows are produced by
    :func:`src.modules.audit.service.record_audit` in the transaction of the
    change they describe; a caller that could write one through the API could
    forge history (section 41).
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.logs = AuditLogRepository(session, tenant)

    async def list_logs(
        self, filters: AuditLogFilters, params: CursorParams
    ) -> tuple[list[AuditLogListItem], CursorPagination, AuditWindow]:
        """The filtered feed. The resolved window is returned so it can be echoed.

        Echoing it matters: the client asked for "no dates" and got 30 days, and
        an investigator who cannot see which window they are looking at will read
        an absence of rows as an absence of activity.
        """
        window = resolve_window(filters.date_from, filters.date_to)
        rows, pagination = await self.logs.list_logs(filters, window, params)
        return [AuditLogListItem.model_validate(row) for row in rows], pagination, window

    async def get_log(self, log_id: uuid.UUID) -> AuditLogRead:
        record = await self.logs.get_full(log_id)
        if record is None:
            # Identical to "belongs to another tenant" - see BaseRepository.
            raise NotFoundError("Audit entry", log_id)
        return AuditLogRead.model_validate(record)

    async def entity_history(
        self, entity_type: str, entity_id: uuid.UUID, params: CursorParams
    ) -> tuple[list[AuditLogListItem], CursorPagination]:
        """Every recorded action against one record - the section 42 trail.

        Deliberately not time-windowed. The point of this endpoint is "show me
        everything that ever happened to this purchase order", and the
        ``(company_id, entity_type, entity_id)`` predicate is selective enough on
        its own that no additional bound is needed to keep it cheap.
        """
        rows, pagination = await self.logs.entity_history(
            normalise_entity_type(entity_type), entity_id, params
        )
        return [AuditLogListItem.model_validate(row) for row in rows], pagination


async def open_approval(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    entity_type: str,
    entity_id: uuid.UUID,
    entity_code: str | None,
    approve_permission: Permission,
    requested_by: uuid.UUID | None,
    summary: str,
    action_url: str,
    amount: Decimal | None = None,
) -> ApprovalRequest:
    """Create one pending request and notify its eligible approvers atomically."""
    if not action_url.startswith("/"):
        raise ValidationError("Approval action_url must be an application-relative path.")

    repo = ApprovalRequestRepository(session, tenant)
    async with transaction(session):
        await advisory_xact_lock(
            session,
            "approval_request",
            tenant.company_id,
            entity_type,
            entity_id,
            str(approve_permission),
        )
        payload = {"amount": str(amount)} if amount is not None else None
        existing = await repo.get_open(
            entity_type=entity_type,
            entity_id=entity_id,
            requested_action=str(approve_permission),
        )
        if existing is not None:
            if (
                existing.entity_code != entity_code
                or existing.reason != summary
                or existing.payload != payload
            ):
                existing.entity_code = entity_code
                existing.reason = summary
                existing.payload = payload
                await session.flush()
            return existing

        record = ApprovalRequest(
            company_id=tenant.company_id,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_code=entity_code,
            requested_action=str(approve_permission),
            reason=summary,
            payload=payload,
            status=ApprovalStatus.PENDING.value,
            created_by_id=actor_id(requested_by),
        )
        repo.add(record)
        await session.flush()

        recipients = await NotificationRepository(session, tenant).user_ids_with_permission(
            str(approve_permission)
        )
        requester_id = actor_id(requested_by)
        recipients = [user_id for user_id in recipients if user_id != requester_id]
        from src.modules.notifications.service import NotificationDraft, notify_many

        await notify_many(
            session,
            company_id=tenant.company_id,
            notification_type=NotificationType.APPROVAL_PENDING,
            drafts=[
                NotificationDraft(
                    user_ids=recipients,
                    title="Approval pending",
                    body=summary,
                    severity=NotificationSeverity.WARNING,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action_url=action_url,
                    group_key=f"approval-pending:{record.id}",
                )
            ],
            actor_id=requested_by,
            write_audit=False,
        )
        return record


async def close_approval(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    entity_type: str,
    entity_id: uuid.UUID,
    requested_action: Permission,
    outcome: ApprovalStatus,
    decided_by: uuid.UUID | None,
    decision_note: str | None = None,
    action_url: str,
    notify_requester: bool = True,
) -> ApprovalRequest | None:
    """Close the pending queue row alongside the owning workflow's decision."""
    if outcome not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise ValidationError("An approval outcome must be APPROVED or REJECTED.")
    if not action_url.startswith("/"):
        raise ValidationError("Approval action_url must be an application-relative path.")

    repo = ApprovalRequestRepository(session, tenant)
    async with transaction(session):
        record = await repo.get_open(
            entity_type=entity_type,
            entity_id=entity_id,
            requested_action=str(requested_action),
            for_update=True,
        )
        if record is None:
            return None

        APPROVAL_DECISION_MACHINE.assert_transition(current_status(record), outcome)
        record.status = outcome.value
        record.decided_by_id = actor_id(decided_by)
        record.decision_note = decision_note
        await session.flush()

        if (
            notify_requester
            and record.created_by_id is not None
            and record.created_by_id != actor_id(decided_by)
        ):
            from src.modules.notifications.service import NotificationDraft, notify_many

            notification_type = (
                NotificationType.APPROVAL_GRANTED
                if outcome is ApprovalStatus.APPROVED
                else NotificationType.APPROVAL_REJECTED
            )
            await notify_many(
                session,
                company_id=tenant.company_id,
                notification_type=notification_type,
                drafts=[
                    NotificationDraft(
                        user_ids=[record.created_by_id],
                        title=f"Approval {outcome.value.lower()}",
                        body=(
                            decision_note
                            or f"{entity_code_for_approval(record)} was {outcome.value.lower()}."
                        ),
                        severity=(
                            NotificationSeverity.SUCCESS
                            if outcome is ApprovalStatus.APPROVED
                            else NotificationSeverity.WARNING
                        ),
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action_url=action_url,
                        group_key=f"approval-decision:{record.id}",
                    )
                ],
                actor_id=decided_by,
                write_audit=False,
            )
        return record


def entity_code_for_approval(record: ApprovalRequest) -> str:
    """Give decision notifications a useful subject when a document has no code."""
    return record.entity_code or record.entity_type.replace("_", " ")


class ApprovalService:
    """The approval queue and the decisions taken on it.

    Takes a :class:`Principal` as well as a :class:`TenantContext` because, alone
    among the services in this codebase, its rules depend on the caller's
    *individual* permissions and identity rather than only on their tenant: which
    rows appear in the queue, and who may decide each of them. ``company_id``
    still comes from the tenant context, never from the request (section 40).
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext, principal: Principal) -> None:
        self.session = session
        self.tenant = tenant
        self.principal = principal
        self.approvals = ApprovalRequestRepository(session, tenant)

    def _present(self, record: ApprovalRequest) -> ApprovalRequestRead:
        authority = evaluate_decision_authority(
            self.principal,
            requested_by_id=record.created_by_id,
            requested_action=record.requested_action,
        )
        # A decided request is decidable by nobody, however much authority the
        # caller holds - both decisions are terminal. Checked here as well as in
        # the state machine so the response the decider just received already
        # reports the row as closed, rather than offering a button that would
        # come back 422.
        settled = record.status != ApprovalStatus.PENDING.value
        return ApprovalRequestRead.from_model(
            record,
            required_permission=str(authority.permission) if authority.permission else None,
            can_decide=authority.allowed and not settled,
            blocked_reason=ALREADY_DECIDED if settled else authority.reason,
        )

    async def list_pending(
        self, filters: ApprovalFilters, params: PageParams
    ) -> tuple[list[ApprovalRequestRead], OffsetPagination]:
        """What is waiting for *this* approver.

        Filtered by the caller's permissions in SQL rather than in Python: a
        request they cannot decide is not merely un-clickable, it must not reach
        them at all - its ``payload`` and ``reason`` can name amounts and vendors
        they have no business seeing.
        """
        rows, pagination = await self.approvals.list_pending(
            params,
            decidable_actions=decidable_actions_for(self.principal),
            filters=filters,
        )
        return [self._present(row) for row in rows], pagination

    async def approve(
        self, approval_id: uuid.UUID, payload: ApprovalDecisionRequest
    ) -> ApprovalRequestRead:
        return await self._decide(approval_id, ApprovalStatus.APPROVED, payload)

    async def reject(
        self, approval_id: uuid.UUID, payload: ApprovalDecisionRequest
    ) -> ApprovalRequestRead:
        return await self._decide(approval_id, ApprovalStatus.REJECTED, payload)

    async def _decide(
        self,
        approval_id: uuid.UUID,
        target: ApprovalStatus,
        payload: ApprovalDecisionRequest,
    ) -> ApprovalRequestRead:
        """Refuse detached decisions that would leave the owning document pending."""
        note = payload.note
        if target is ApprovalStatus.REJECTED and (
            note is None or len(note) < MIN_REJECTION_NOTE_LENGTH
        ):
            raise ValidationError(
                "A rejection must state a reason.",
                details={"field": "note", "min_length": MIN_REJECTION_NOTE_LENGTH},
            )

        async with transaction(self.session):
            record = await self.approvals.get_for_decision(approval_id)
            if record is None:
                raise NotFoundError("Approval request", approval_id)

            APPROVAL_DECISION_MACHINE.assert_transition(current_status(record), target)
            authority = assert_may_decide(
                self.principal,
                requested_by_id=record.created_by_id,
                requested_action=record.requested_action,
            )
            raise BusinessRuleError(
                "Approval decisions must be made from the owning document workflow so the "
                "approval request and document status change together.",
                details={
                    "approval_request_id": str(record.id),
                    "requested_action": record.requested_action,
                    "required_permission": (
                        str(authority.permission) if authority.permission else None
                    ),
                    "decision": target.value,
                },
            )


__all__ = [
    "ALREADY_DECIDED",
    "APPROVAL_DECISION_MACHINE",
    "DECIDABLE_PERMISSIONS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "MIN_REJECTION_NOTE_LENGTH",
    "SELF_APPROVAL_BLOCKED",
    "ApprovalService",
    "AuditQueryService",
    "DecisionAuthority",
    "assert_may_decide",
    "close_approval",
    "current_status",
    "decidable_actions_for",
    "evaluate_decision_authority",
    "normalise_entity_type",
    "open_approval",
    "permission_for_action",
    "resolve_window",
]
