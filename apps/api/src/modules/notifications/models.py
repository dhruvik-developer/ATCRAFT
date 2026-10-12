"""In-app notifications and the per-user preferences that route them.

Master rules sections 14 ("notify the frontend"), 23 (delay alerts) and 35
(background job completion). Two tables, and every non-obvious column on them
exists to answer one of three questions.

**"Has the recipient seen it?"** ``is_read``/``read_at`` are a pair, not two
independent columns, and a check constraint keeps them consistent: a read
notification without a timestamp makes "when did the approver see this?"
unanswerable, and an unread one carrying a timestamp is simply a lie. Read state
is a two-state lifecycle enforced by
:data:`~src.modules.notifications.service.NOTIFICATION_READ_MACHINE`.

**"Where was it delivered?"** ``channels`` records the channels actually chosen
for this recipient *after* their preferences were applied - not what the caller
asked for. The in-app feed therefore selects on ``IN_APP = ANY(channels)``: a row
that exists only to drive an e-mail must never appear in the bell.
``delivered_at`` is stamped when the notification became visible in-app
(immediately) and is left NULL for an e-mail-only row until the worker that sends
it fills it in.

**"Is this the fortieth copy of the same alert?"** ``group_key`` is what stops a
delay sweep over 40 late purchase orders from producing 40 bell rows. The partial
unique index ``uq_notifications_open_group`` makes "at most one *unread* row per
(recipient, type, group key)" a database invariant rather than a convention, so
the fan-out can be written as a single ``INSERT ... ON CONFLICT DO UPDATE`` that
bumps ``occurrence_count`` instead of inserting - atomic, and immune to two
concurrent sweeps racing. The predicate includes ``is_read = false`` on purpose:
once the user has acknowledged the alert, the next occurrence deserves a new row
rather than silently re-opening an old one.

``entity_type``/``entity_id`` are a plain type-plus-uuid reference with no
foreign key, deliberately, for the same reason as ``documents.entity_id``: an
alert about a cancelled or archived document must survive that document, and a
notification that vanishes when its subject does destroys the record that anyone
was ever told.

There is no soft delete and no version counter. Notifications are not edited by
people - they are read, and they age out through a retention sweep - so a
``deleted_at`` would only hide rows from the feed while leaving them countable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity
from src.core.types import GUID


class NotificationSeverity(StrEnum):
    """How loudly the UI should present the notification.

    A presentation and escalation classification, never an authorisation one.
    ``CRITICAL`` additionally overrides an in-app opt-out - see
    ``SEVERITIES_IGNORING_PREFERENCES`` in the service.
    """

    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class NotificationChannel(StrEnum):
    """Where one notification is delivered.

    ``IN_APP`` is the bell; ``EMAIL`` is handed to Celery and sent out of band.
    Nothing is ever sent inline from a request - see the service module.
    """

    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    PUSH = "PUSH"


class NotificationType(StrEnum):
    PURCHASE_ORDER_SENT = "PURCHASE_ORDER_SENT"
    PURCHASE_ORDER_ACKNOWLEDGED = "PURCHASE_ORDER_ACKNOWLEDGED"
    PURCHASE_ORDER_DECLINED = "PURCHASE_ORDER_DECLINED"
    PURCHASE_ORDER_DATE_CHANGED = "PURCHASE_ORDER_DATE_CHANGED"
    PURCHASE_ORDER_READY = "PURCHASE_ORDER_READY"
    PURCHASE_ORDER_SHIPPED = "PURCHASE_ORDER_SHIPPED"
    VENDOR_INVOICE_STATUS = "VENDOR_INVOICE_STATUS"
    VENDOR_PAYMENT_RECORDED = "VENDOR_PAYMENT_RECORDED"
    """The closed vocabulary of things the system notifies about.

    Closed rather than free text because it is the key of
    ``notification_preferences``: a user mutes a *type*, so a caller inventing an
    ad-hoc string would produce alerts nobody can ever turn off, and a
    preferences screen that cannot enumerate its own rows. Adding a member is a
    normal code change - it appears in the preferences endpoint automatically and
    inherits the defaults in :mod:`src.modules.notifications.service`.
    """

    # --- approvals (section 14) --------------------------------------------
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"

    # --- delay alerts (section 23) -----------------------------------------
    PURCHASE_ORDER_DELAYED = "PURCHASE_ORDER_DELAYED"
    PRODUCTION_ORDER_DELAYED = "PRODUCTION_ORDER_DELAYED"
    JOB_WORK_OVERDUE = "JOB_WORK_OVERDUE"
    DISPATCH_DELAYED = "DISPATCH_DELAYED"
    SALES_ORDER_AT_RISK = "SALES_ORDER_AT_RISK"

    # --- inventory / quality ------------------------------------------------
    STOCK_BELOW_REORDER = "STOCK_BELOW_REORDER"
    STOCK_SHORTAGE = "STOCK_SHORTAGE"
    ALLOCATION_CHANGED = "ALLOCATION_CHANGED"
    QC_INSPECTION_PENDING = "QC_INSPECTION_PENDING"
    QC_FAILED = "QC_FAILED"

    # --- documents and background work (section 35) -------------------------
    GOODS_RECEIPT_POSTED = "GOODS_RECEIPT_POSTED"
    PAYMENT_DUE = "PAYMENT_DUE"
    MRP_RUN_COMPLETED = "MRP_RUN_COMPLETED"
    REPORT_READY = "REPORT_READY"
    BACKGROUND_JOB_COMPLETED = "BACKGROUND_JOB_COMPLETED"
    BACKGROUND_JOB_FAILED = "BACKGROUND_JOB_FAILED"

    # --- platform -----------------------------------------------------------
    SYSTEM_ANNOUNCEMENT = "SYSTEM_ANNOUNCEMENT"


class Notification(TenantEntity):
    """One notification addressed to one user."""

    __tablename__ = "notifications"

    #: The recipient. CASCADE because a notification addressed to a user who no
    #: longer exists is unreadable by anyone - unlike a business document, it
    #: carries no history worth orphaning. Users are deactivated rather than
    #: deleted in normal operation, so this fires only on a genuine purge.
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    notification_type: Mapped[NotificationType] = mapped_column(String(48), nullable=False)
    severity: Mapped[NotificationSeverity] = mapped_column(String(16), nullable=False)

    #: Rendered at write time, in the recipient's company context. Storing the
    #: text rather than a template id plus parameters means an alert still reads
    #: correctly after the underlying document has moved on - "PO-1042 is 6 days
    #: late" must not silently become "0 days late" when someone opens the bell.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    #: What the alert is about. No foreign key - see the module docstring.
    entity_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Where clicking the notification takes the user. Constrained by the service
    #: to a site-relative path: a notification is a link the application asks the
    #: user to trust, so an absolute URL here would be an open-redirect primitive
    #: handed to whatever code raised the alert.
    action_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Channels actually selected for this recipient after their preferences were
    #: applied. ARRAY rather than a child table: it is at most two elements, is
    #: always read whole, and is never joined to.
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=text("'{IN_APP}'::varchar[]")
    )

    #: When the recipient could first see it. Stamped at insert for anything
    #: delivered in-app; an e-mail-only row stays NULL until the sending worker
    #: fills it in, which is what makes "queued but never sent" visible.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: SMTP acceptance timestamp. Kept separate from ``delivered_at`` because a
    #: notification may be visible in-app before its e-mail is sent. Celery uses
    #: this as the idempotency guard when a late-acknowledged task is delivered
    #: more than once.
    email_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    push_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Collapse key. Alerts sharing (recipient, type, group_key) fold into one
    #: unread row - see the module docstring and the partial unique index below.
    group_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: How many times this alert has fired since it was last read. 1 for an
    #: ordinary notification; the count the bell renders as "x40".
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __table_args__ = (
        Index(
            "ix_notifications_pending_push",
            "created_at",
            postgresql_where=text("push_delivered_at IS NULL AND 'PUSH' = ANY(channels)"),
        ),
        Index(
            "ix_notifications_pending_email",
            "created_at",
            postgresql_where=text("email_delivered_at IS NULL AND 'EMAIL' = ANY(channels)"),
        ),
        # The feed index. ``is_read`` leads the sort keys because the list is
        # ordered unread-first, and the DESC pair matches the keyset predicate
        # exactly so paging deep into an old backlog stays constant cost.
        Index(
            "ix_notifications_feed",
            "company_id",
            "user_id",
            "is_read",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # The unread badge is polled by every open browser tab, so it gets its
        # own partial index: read notifications - the overwhelming majority of
        # the table over time - are not in it at all.
        Index(
            "ix_notifications_unread",
            "company_id",
            "user_id",
            postgresql_where=text("is_read = false"),
        ),
        # At most one OPEN row per (recipient, type, group). This is the arbiter
        # index for the ON CONFLICT upsert in the repository, so the predicate
        # here and the one passed as ``index_where`` must stay identical.
        Index(
            "uq_notifications_open_group",
            "company_id",
            "user_id",
            "notification_type",
            "group_key",
            unique=True,
            postgresql_where=text("group_key IS NOT NULL AND is_read = false"),
        ),
        # "Everything raised about this purchase order", for the entity timeline.
        Index(
            "ix_notifications_entity",
            "company_id",
            "entity_type",
            "entity_id",
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        # Half a reference points at something no query can express.
        CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)", name="entity_reference_complete"
        ),
        # The pair is the record of *when* it was seen; either both or neither.
        CheckConstraint("(read_at IS NOT NULL) = is_read", name="read_state_consistent"),
        # A row delivered nowhere is invisible everywhere: it would sit in the
        # table forever, uncounted and unreadable. The service refuses to create
        # one; the constraint makes that true for any writer.
        CheckConstraint("cardinality(channels) > 0", name="channels_not_empty"),
        CheckConstraint("occurrence_count >= 1", name="occurrence_count_positive"),
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        # Relative, single-slash paths only - the service enforces the same rule
        # on the way in, and this is what holds if a future caller bypasses it.
        CheckConstraint(
            "action_url IS NULL OR (action_url LIKE '/%' AND action_url NOT LIKE '//%')",
            name="action_url_is_relative",
        ),
    )


class NotificationPreference(TenantEntity):
    """One user's delivery choice for one notification type.

    Rows are sparse on purpose: a user who has never opened the preferences
    screen has none, and the service falls back to the module defaults. Writing a
    full matrix per user at signup would mean a data migration every time a
    :class:`NotificationType` member is added, and would freeze new types at
    whatever default existed when that user was created.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    notification_type: Mapped[NotificationType] = mapped_column(String(48), nullable=False)

    in_app_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    push_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        # Also the arbiter index for the preferences upsert.
        UniqueConstraint(
            "company_id", "user_id", "notification_type", name="uq_notification_preferences_key"
        ),
    )


class PushSubscription(TenantEntity):
    """One browser opt-in. Endpoints are credentials and are encrypted at rest."""

    __tablename__ = "push_subscriptions"
    user_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("users.id", ondelete="CASCADE"))
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True)
    endpoint_ciphertext: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(128))
    auth: Mapped[str] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    device_label: Mapped[str | None] = mapped_column(String(120))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    __table_args__ = (
        Index("ix_push_subscriptions_owner", "company_id", "user_id"),
        CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
    )


__all__ = [
    "Notification",
    "NotificationChannel",
    "NotificationPreference",
    "NotificationSeverity",
    "NotificationType",
    "PushSubscription",
]
