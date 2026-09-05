"""Notification dispatch, the bell, and delivery preferences.

This module is imported by nearly every other one, so its public surface is
deliberately tiny and deliberately stable::

    await notify(session, company_id=..., user_ids=[...], notification_type=...,
                 title=..., body=..., severity=...)
    await notify_role(session, company_id=..., role_codes=["PURCHASE_MANAGER"], ...)

Four decisions are load-bearing.

**Notifications are written in the caller's transaction.** ``notify`` opens a
nested transaction on the session it is given and never commits it, exactly like
:func:`~src.modules.audit.service.record_audit`. A purchase order that rolls back
must not leave "your approval is required" in someone's bell, and an approval
request that commits must never be silently un-notified.

**E-mail is never sent inline.** A request handler must not wait on an SMTP
conversation, and a broker that is down must not fail a goods receipt. Rows
destined for e-mail are queued to Celery *after the transaction commits*, by
task name only - :data:`NOTIFICATION_EMAIL_TASK` - so this module never imports a
task implementation, and the worker re-reads the notification from the database.
That last part matters: it means a rolled-back notification produces no e-mail
even though the enqueue already happened, because the row the task looks for is
not there.

**Preferences decide the channels, callers do not.** ``notify`` takes no channel
argument. It reads each recipient's preference for the type and stores the
channels actually selected for them, which is what the feed and the e-mail worker
then read. A recipient who has muted a type entirely gets no row at all - with
one exception, named in :data:`SEVERITIES_IGNORING_PREFERENCES`.

**Read state is a state machine, not a boolean flip.** There is exactly one legal
edge, ``UNREAD -> READ``. Re-reading an already-read notification is idempotent
rather than an error, because a double-click on the bell is not a business rule
violation; but nothing anywhere can move a notification back to unread, which is
what keeps ``read_at`` a truthful record of when the recipient first saw it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.core.ids import new_id
from src.core.logging import get_logger, get_request_id
from src.core.pagination import CursorParams
from src.core.responses import CursorPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationPreference,
    NotificationSeverity,
    NotificationType,
)
from src.modules.notifications.repository import (
    MARK_ALL_BATCH,
    UNREAD_COUNT_CEILING,
    NotificationRepository,
)
from src.modules.notifications.schemas import (
    MarkReadResult,
    NotificationFilters,
    NotificationPreferenceRead,
    NotificationRead,
    PreferencesUpdateRequest,
    UnreadCountRead,
)

log = get_logger(__name__)

AUDIT_ENTITY = "notification"

#: Celery task that sends one notification by e-mail. Referenced by name and
#: dispatched with ``send_task`` so this module never imports the worker package:
#: the API process must be able to queue work for a task it does not host, and
#: ``src.workers.tasks.notifications`` is written independently of this module.
#: The routing rule ``notifications.*`` in ``celery_app`` puts it on the
#: notifications queue, behind nothing slow.
NOTIFICATION_EMAIL_TASK = "notifications.send_email"

# --- Defaults for a user who has never opened the preferences screen ----------
#
# ASSUMPTION: an unconfigured notification type is delivered in-app and NOT by
# e-mail. In-app costs the recipient one glance at a bell they chose to open;
# unsolicited e-mail is intrusive, is not free to send, and is the setting people
# complain about. Making e-mail opt-in is therefore the conservative reading of
# "notify the user" (section 14). Change it here and nowhere else.
DEFAULT_IN_APP_ENABLED = True
DEFAULT_EMAIL_ENABLED = False

# ASSUMPTION: a CRITICAL alert is always delivered in-app, even to a user who has
# muted that type. A stock-out or a failed background job that nobody is told
# about is a silent failure, and section 53 forbids those. Muting still applies
# to e-mail, and to every other severity. Change it here and nowhere else.
SEVERITIES_IGNORING_PREFERENCES = frozenset({NotificationSeverity.CRITICAL})

#: ``notifications.title`` is String(200). A caller that overruns it has produced
#: a long sentence, not a business error, and rolling back a posted receipt over
#: a headline would be absurd - so the title is trimmed and the detail stays in
#: the body, which is unbounded TEXT.
MAX_TITLE_LENGTH = 200
MAX_ACTION_URL_LENGTH = 512
MAX_GROUP_KEY_LENGTH = 120
MAX_ENTITY_TYPE_LENGTH = 48


class NotificationReadState(StrEnum):
    """Where one notification sits in its (very short) life."""

    UNREAD = "UNREAD"
    READ = "READ"


#: The only legal edge. Declared as a machine rather than as an ``if`` so the
#: "a notification can never become unread again" rule is stated once, testable,
#: and enforced through the same ``assert_transition`` every other document uses
#: (sections 18/73).
NOTIFICATION_READ_MACHINE: StateMachine[NotificationReadState] = StateMachine(
    entity="Notification",
    initial=NotificationReadState.UNREAD,
    transitions=(
        Transition(
            source=NotificationReadState.UNREAD,
            target=NotificationReadState.READ,
            label="Mark as read",
        ),
    ),
    terminal=frozenset({NotificationReadState.READ}),
)


def read_state(notification: Notification) -> NotificationReadState:
    return NotificationReadState.READ if notification.is_read else NotificationReadState.UNREAD


# -----------------------------------------------------------------------------
# Pure helpers - no database, no session
# -----------------------------------------------------------------------------


def resolve_channels(
    severity: NotificationSeverity,
    *,
    in_app_enabled: bool,
    email_enabled: bool,
) -> tuple[str, ...]:
    """Channels one recipient actually gets, after their preferences.

    Returns an empty tuple when the recipient has muted the type entirely, which
    the caller reads as "write no row for this person": a notification delivered
    nowhere is not a record, it is a row nobody can ever see or count.
    """
    forced = severity in SEVERITIES_IGNORING_PREFERENCES
    channels: list[str] = []
    if in_app_enabled or forced:
        channels.append(NotificationChannel.IN_APP.value)
    if email_enabled:
        channels.append(NotificationChannel.EMAIL.value)
    return tuple(channels)


def normalise_title(raw: str) -> str:
    """Trim a headline to what the column holds, keeping it readable.

    The ellipsis is added inside the limit rather than after it, so the result
    always fits.
    """
    title = " ".join(raw.split())
    if not title:
        raise ValidationError("A notification needs a title.")
    if len(title) > MAX_TITLE_LENGTH:
        return title[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return title


def normalise_action_url(raw: str | None) -> str | None:
    """Accept only a site-relative path.

    A notification is a link the application asks the user to trust, and it is
    written by whichever module raised the alert. Allowing an absolute URL would
    turn any such caller - or any data it interpolates - into an open redirect
    aimed at an already-authenticated user. ``//evil.example`` is rejected too:
    browsers read a protocol-relative URL as off-site.
    """
    if raw is None:
        return None
    url = raw.strip()
    if not url:
        return None
    if not url.startswith("/") or url.startswith("//"):
        raise ValidationError(
            "action_url must be a site-relative path such as '/purchase-orders/<id>'.",
            details={"action_url": raw},
        )
    if len(url) > MAX_ACTION_URL_LENGTH:
        # Truncating produces a link to the wrong place, which is worse than no
        # link at all - and it is always a caller bug, never user input.
        raise ValidationError(
            f"action_url exceeds {MAX_ACTION_URL_LENGTH} characters.",
            details={"length": len(url)},
        )
    return url


def normalise_group_key(raw: str | None) -> str | None:
    """Validate the collapse key.

    Never truncated: two keys that differ only past the limit would silently
    collapse into one alert, and a suppressed notification is a lost one.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > MAX_GROUP_KEY_LENGTH:
        raise ValidationError(
            f"group_key exceeds {MAX_GROUP_KEY_LENGTH} characters.",
            details={"length": len(key)},
        )
    return key


def normalise_entity_reference(
    entity_type: str | None, entity_id: uuid.UUID | None
) -> tuple[str | None, uuid.UUID | None]:
    """Both halves of the reference, or neither."""
    normalised = entity_type.strip().lower() if entity_type else None
    if not normalised:
        normalised = None
    if (normalised is None) != (entity_id is None):
        raise ValidationError("entity_type and entity_id must be supplied together.")
    if normalised is not None and len(normalised) > MAX_ENTITY_TYPE_LENGTH:
        raise ValidationError(f"entity_type exceeds {MAX_ENTITY_TYPE_LENGTH} characters.")
    return normalised, entity_id


def dedupe_recipients(user_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Unique recipients, in the order first seen.

    Not merely tidiness: PostgreSQL refuses to let one ``ON CONFLICT DO UPDATE``
    statement touch the same row twice, so a repeated recipient in a grouped
    fan-out would abort the whole statement.
    """
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for user_id in user_ids:
        if user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)
    return ordered


# -----------------------------------------------------------------------------
# E-mail hand-off
# -----------------------------------------------------------------------------

_PENDING_EMAIL_KEY = "notifications.pending_email"
_EMAIL_HOOK_KEY = "notifications.email_hook_installed"


def _send_email_tasks(payloads: Sequence[dict[str, Any]]) -> int:
    """Publish the queued e-mail jobs. Never raises.

    The in-app notification is already committed and is the system of record; a
    broker outage must degrade delivery, not fail the business operation that has
    already happened. The failure is logged with the ids so the messages can be
    replayed.
    """
    from src.workers.celery_app import celery_app

    sent = 0
    for payload in payloads:
        try:
            celery_app.send_task(NOTIFICATION_EMAIL_TASK, kwargs=payload)
            sent += 1
        except Exception as exc:
            log.error(
                "notifications.email_enqueue_failed",
                notification_id=payload.get("notification_id"),
                error=str(exc),
            )
    return sent


def _flush_pending_emails(sync_session: SyncSession) -> None:
    """``after_commit`` hook: hand the committed rows to Celery.

    Publishing runs in a worker thread when there is a running event loop.
    ``send_task`` opens a socket to the broker, and doing that inline would block
    the loop that is serving every other request for the duration.
    """
    payloads = sync_session.info.pop(_PENDING_EMAIL_KEY, None)
    if not payloads:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # A synchronous caller (a Celery worker raising follow-up alerts, a
        # management script): nothing to protect, so publish inline.
        _send_email_tasks(payloads)
        return
    loop.run_in_executor(None, _send_email_tasks, payloads)


def _drop_pending_emails(sync_session: SyncSession) -> None:
    """``after_rollback`` hook: the notifications never happened."""
    sync_session.info.pop(_PENDING_EMAIL_KEY, None)


def queue_email_delivery(session: AsyncSession, notifications: Sequence[Notification]) -> int:
    """Stage e-mail dispatch for the rows that asked for it.

    Returns how many were staged. Nothing is published until the caller's
    transaction commits - see the hooks above - so a fan-out that rolls back
    sends nothing.
    """
    payloads = [
        {
            "notification_id": str(item.id),
            "company_id": str(item.company_id),
            "user_id": str(item.user_id),
            # Read by the worker's task_prerun signal, so one business operation
            # keeps a single correlation id across API and worker logs.
            "request_id": get_request_id(),
        }
        for item in notifications
        if NotificationChannel.EMAIL.value in (item.channels or ())
    ]
    if not payloads:
        return 0

    sync_session = session.sync_session
    pending: list[dict[str, Any]] = sync_session.info.setdefault(_PENDING_EMAIL_KEY, [])
    pending.extend(payloads)

    if not sync_session.info.get(_EMAIL_HOOK_KEY):
        sync_session.info[_EMAIL_HOOK_KEY] = True
        event.listen(sync_session, "after_commit", _flush_pending_emails)
        event.listen(sync_session, "after_rollback", _drop_pending_emails)
    return len(payloads)


# -----------------------------------------------------------------------------
# The cross-module API
# -----------------------------------------------------------------------------


async def notify(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_ids: Sequence[uuid.UUID],
    notification_type: NotificationType,
    title: str,
    body: str,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
    group_key: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Raise one notification for each recipient, honouring their preferences.

    Runs in the caller's transaction. Returns the rows that now exist - which is
    fewer than ``user_ids`` when someone has muted the type, and may be existing
    rows rather than new ones when ``group_key`` collapsed the alert onto an
    unacknowledged copy.

    ASSUMPTION: a group collapses only while the alert is still unread, and the
    newest wording replaces the old. Once the recipient has acknowledged it, the
    next occurrence is a new notification - the conservative reading, because
    re-opening an acknowledged row would let an alert change under someone who
    has already dealt with it, and could hide a genuinely new occurrence.

    ``group_key`` is the flood control: passing a key like
    ``f"po-delay:{purchase_order_id}"`` means an hourly delay sweep updates one
    bell row with the current wording and an occurrence count, instead of adding
    a row every hour until someone gives up and mutes the type.
    """
    recipients = dedupe_recipients(user_ids)
    if not recipients:
        return []

    clean_title = normalise_title(title)
    # A caller with nothing more to say than the headline is not an error, so
    # the title is reused rather than storing a blank detail pane - and rather
    # than raising, which would roll back the business change the alert is
    # about. A blank title has no such fallback and is rejected above.
    clean_body = body.strip() if body and body.strip() else clean_title
    clean_entity_type, clean_entity_id = normalise_entity_reference(entity_type, entity_id)
    clean_action_url = normalise_action_url(action_url)
    clean_group_key = normalise_group_key(group_key)

    repo = NotificationRepository.for_company(session, company_id, actor_id)
    preferences = await repo.preferences_for_type(recipients, notification_type)

    moment = utcnow()
    rows: list[dict[str, Any]] = []
    muted: list[uuid.UUID] = []

    for user_id in recipients:
        in_app_enabled, email_enabled = preferences.get(
            user_id, (DEFAULT_IN_APP_ENABLED, DEFAULT_EMAIL_ENABLED)
        )
        channels = resolve_channels(
            severity, in_app_enabled=in_app_enabled, email_enabled=email_enabled
        )
        if not channels:
            muted.append(user_id)
            continue

        rows.append(
            {
                "id": new_id(),
                "company_id": company_id,
                "user_id": user_id,
                "notification_type": notification_type.value,
                "severity": severity.value,
                "title": clean_title,
                "body": clean_body,
                "entity_type": clean_entity_type,
                "entity_id": clean_entity_id,
                "action_url": clean_action_url,
                "is_read": False,
                "read_at": None,
                "channels": list(channels),
                # ASSUMPTION: ``delivered_at`` means "reached the recipient
                # through at least one channel". In-app delivery is
                # instantaneous, so the row is delivered the moment it exists;
                # an e-mail-only row stays NULL until the sending worker fills
                # it in, which is what makes "queued but never sent" visible.
                # The alternative reading - delivered only once the e-mail is
                # accepted by the relay - would leave every in-app notification
                # permanently undelivered.
                "delivered_at": moment if NotificationChannel.IN_APP.value in channels else None,
                "group_key": clean_group_key,
                "occurrence_count": 1,
                "created_at": moment,
                "updated_at": moment,
                "created_by_id": actor_id,
                "updated_by_id": actor_id,
            }
        )

    if not rows:
        log.info(
            "notifications.all_recipients_muted",
            company_id=str(company_id),
            notification_type=str(notification_type),
            recipients=len(recipients),
        )
        return []

    async with transaction(session):
        written = await repo.upsert_many(rows)

        # One audit entry for the fan-out, not one per recipient: the business
        # event is "these people were told", and 500 near-identical rows would
        # bury the document changes the audit log exists to make findable.
        await record_audit(
            session,
            company_id=company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_ENTITY,
            entity_id=written[0].id if len(written) == 1 else None,
            entity_code=str(notification_type),
            summary=f"Notified {len(written)} recipient(s): {clean_title}",
            user_id=actor_id,
            new_values={
                "notification_type": str(notification_type),
                "severity": str(severity),
                "title": clean_title,
                "group_key": clean_group_key,
                "recipient_count": len(written),
            },
            context={
                "entity_type": clean_entity_type,
                "entity_id": clean_entity_id,
                "muted_recipients": len(muted),
            },
        )

        # Stage while the business transaction is still open. SQLAlchemy's
        # after_commit hook publishes only once the rows above are durable; a
        # rollback clears the staged jobs instead. Registering this after the
        # context exited would miss the commit that should trigger delivery.
        queued = queue_email_delivery(session, written)

    log.info(
        "notifications.raised",
        company_id=str(company_id),
        notification_type=str(notification_type),
        severity=str(severity),
        written=len(written),
        muted=len(muted),
        email_queued=queued,
        group_key=clean_group_key,
    )
    return written


async def notify_role(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    role_codes: Sequence[str],
    notification_type: NotificationType,
    title: str,
    body: str,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
    group_key: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Notify everyone holding one of ``role_codes`` in this company.

    This is how "a purchase order is waiting for approval" reaches the people who
    can actually approve it: the recipient list is derived from the role
    assignment at the moment the event happens, so it stays correct when someone
    joins, leaves or changes job without any module holding a stale list of user
    ids.

    Inactive users, retired roles and deactivated roles are excluded. An empty
    result is returned quietly - a company that has not yet staffed a role is a
    configuration state, not an error to abort the purchase order for - but it is
    logged, because "nobody was told" is worth being able to find later.
    """
    repo = NotificationRepository.for_company(session, company_id, actor_id)
    recipients = await repo.user_ids_for_roles(role_codes)

    if not recipients:
        log.warning(
            "notifications.no_recipients_for_roles",
            company_id=str(company_id),
            role_codes=list(role_codes),
            notification_type=str(notification_type),
        )
        return []

    return await notify(
        session,
        company_id=company_id,
        user_ids=recipients,
        notification_type=notification_type,
        title=title,
        body=body,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
        group_key=group_key,
        actor_id=actor_id,
    )


# -----------------------------------------------------------------------------
# The recipient's own bell
# -----------------------------------------------------------------------------


def effective_preferences(
    stored: Sequence[NotificationPreference],
) -> list[NotificationPreferenceRead]:
    """Every notification type with the setting that will actually be applied.

    Computed rather than stored: types gain members over time, and a screen that
    could only show the rows a user happens to have saved would hide every new
    alert type from the person who wanted to control it.
    """
    by_type = {preference.notification_type: preference for preference in stored}
    return [
        NotificationPreferenceRead(
            notification_type=notification_type,
            in_app_enabled=(
                by_type[notification_type].in_app_enabled
                if notification_type in by_type
                else DEFAULT_IN_APP_ENABLED
            ),
            email_enabled=(
                by_type[notification_type].email_enabled
                if notification_type in by_type
                else DEFAULT_EMAIL_ENABLED
            ),
            is_customised=notification_type in by_type,
        )
        for notification_type in NotificationType
    ]


class NotificationService:
    """Everything the signed-in user can do with their own notifications.

    Every method is scoped to ``tenant.user_id``. There is no way to address
    another person's bell through this class, and no endpoint takes a recipient:
    an employee's approval queue is not readable by their colleagues.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = NotificationRepository(session, tenant)

    async def list_feed(
        self, filters: NotificationFilters, params: CursorParams
    ) -> tuple[list[NotificationRead], CursorPagination]:
        rows, pagination = await self.repo.list_feed(self.tenant.user_id, filters, params)
        return [NotificationRead.model_validate(row) for row in rows], pagination

    async def unread_count(self) -> UnreadCountRead:
        total, is_capped = await self.repo.unread_count(self.tenant.user_id)
        return UnreadCountRead(unread_count=total, is_capped=is_capped)

    async def mark_read(self, notification_id: uuid.UUID) -> NotificationRead:
        """Acknowledge one notification.

        Idempotent: acknowledging an already-read notification returns it
        unchanged instead of raising, because a repeated click is not a business
        error - while the state machine still refuses to move it back to unread,
        which is what makes ``read_at`` trustworthy.
        """
        notification = await self.repo.get_for_user(notification_id, self.tenant.user_id)
        if notification is None:
            raise NotFoundError("Notification", notification_id)

        current = read_state(notification)
        if current is NotificationReadState.READ:
            return NotificationRead.model_validate(notification)

        NOTIFICATION_READ_MACHINE.assert_transition(current, NotificationReadState.READ)
        moment = utcnow()

        async with transaction(self.session):
            notification.is_read = True
            notification.read_at = moment
            self.repo.touch(notification)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY,
                entity_id=notification.id,
                entity_code=str(notification.notification_type),
                from_status=str(NotificationReadState.UNREAD),
                to_status=str(NotificationReadState.READ),
                user_id=self.tenant.user_id,
            )

        return NotificationRead.model_validate(notification)

    async def mark_all_read(self) -> MarkReadResult:
        """Acknowledge the recipient's unread backlog, one bounded batch.

        Bounded because a single UPDATE across an enormous backlog would hold
        that many row locks in one transaction. ``remaining_unread`` tells the
        client whether to call again, so the operation still completes - it just
        does not do it in one lock-heavy statement.
        """
        moment = utcnow()

        async with transaction(self.session):
            marked = await self.repo.mark_read_batch(
                self.tenant.user_id, moment, limit=MARK_ALL_BATCH
            )
            if marked:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.STATUS_CHANGE,
                    entity_type=AUDIT_ENTITY,
                    summary=f"Marked {marked} notification(s) read",
                    user_id=self.tenant.user_id,
                    old_values={"status": str(NotificationReadState.UNREAD)},
                    new_values={"status": str(NotificationReadState.READ)},
                    context={"marked": marked, "batch_limit": MARK_ALL_BATCH},
                )

        remaining, _ = await self.repo.unread_count(
            self.tenant.user_id, ceiling=UNREAD_COUNT_CEILING
        )
        return MarkReadResult(marked=marked, remaining_unread=remaining)

    # --- preferences --------------------------------------------------------

    async def get_preferences(self) -> list[NotificationPreferenceRead]:
        stored = await self.repo.preferences_for_user(self.tenant.user_id)
        return effective_preferences(stored)

    async def update_preferences(
        self, payload: PreferencesUpdateRequest
    ) -> list[NotificationPreferenceRead]:
        """Save the submitted rows and return the full effective matrix."""
        moment = utcnow()
        rows = [
            {
                "id": new_id(),
                "company_id": self.tenant.company_id,
                "user_id": self.tenant.user_id,
                "notification_type": preference.notification_type.value,
                "in_app_enabled": preference.in_app_enabled,
                "email_enabled": preference.email_enabled,
                "created_at": moment,
                "updated_at": moment,
                "created_by_id": self.tenant.user_id,
                "updated_by_id": self.tenant.user_id,
            }
            for preference in payload.preferences
        ]

        async with transaction(self.session):
            await self.repo.upsert_preferences(self.tenant.user_id, rows)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type="notification_preference",
                summary=f"Updated {len(rows)} notification preference(s)",
                user_id=self.tenant.user_id,
                new_values={
                    str(preference.notification_type): {
                        "in_app_enabled": preference.in_app_enabled,
                        "email_enabled": preference.email_enabled,
                    }
                    for preference in payload.preferences
                },
            )

        stored = await self.repo.preferences_for_user(self.tenant.user_id)
        return effective_preferences(stored)


__all__ = [
    "DEFAULT_EMAIL_ENABLED",
    "DEFAULT_IN_APP_ENABLED",
    "MAX_TITLE_LENGTH",
    "NOTIFICATION_EMAIL_TASK",
    "NOTIFICATION_READ_MACHINE",
    "SEVERITIES_IGNORING_PREFERENCES",
    "NotificationReadState",
    "NotificationService",
    "dedupe_recipients",
    "effective_preferences",
    "normalise_action_url",
    "normalise_entity_reference",
    "normalise_group_key",
    "normalise_title",
    "notify",
    "notify_role",
    "queue_email_delivery",
    "read_state",
    "resolve_channels",
]
