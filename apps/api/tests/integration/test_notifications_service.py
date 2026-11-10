"""Service-level tests for notifications, against a real PostgreSQL.

These cover what only a database can prove: the partial unique index behind alert
grouping, the ``ON CONFLICT`` fan-out that collapses onto it, the unread-first
keyset feed (which cannot be checked without real ordering and real paging), the
audit rows written in the same transaction as the notification, and the tenant
and recipient isolation that keeps one employee's approval queue out of another's
bell.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``) come
from ``tests/conftest.py``; the outer transaction is rolled back after each test.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.actors import SYSTEM_ACTOR_ID
from src.core.clock import utcnow
from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.core.pagination import CursorParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.notifications.alert_service import NotificationAlertService, reconcile_alert
from src.modules.notifications.alerts import ALERT_RULES, AlertCandidate, AlertRuleKey
from src.modules.notifications.models import (
    Notification,
    NotificationAlertState,
    NotificationChannel,
    NotificationPreference,
    NotificationSeverity,
    NotificationType,
)
from src.modules.notifications.repository import NotificationRepository
from src.modules.notifications.schemas import (
    NotificationAlertRulesUpdate,
    NotificationFilters,
    NotificationPreferenceUpdate,
    PreferencesUpdateRequest,
)
from src.modules.notifications.service import (
    NotificationDraft,
    NotificationService,
    notify,
    notify_many,
    notify_role,
)
from tests.notification_support import _make_user

pytestmark = pytest.mark.integration


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _make_customer(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    from src.modules.customers.models import Customer, CustomerType
    from src.modules.products.models import PriceType

    record = Customer(
        company_id=company_id,
        code=f"DL-{uuid.uuid4().hex[:8].upper()}",
        name="Dealer",
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.SALE,
    )
    session.add(record)
    await session.flush()
    return record.id


async def _make_role(session: AsyncSession, company_id: uuid.UUID, code: str, *, active=True):
    from src.modules.users.models import Role

    role = Role(
        company_id=company_id,
        code=code,
        name=f"Role {code}",
        permissions=[],
        is_active=active,
    )
    session.add(role)
    await session.flush()
    return role


async def _raise(session: AsyncSession, company_id: uuid.UUID, recipients, **overrides):
    payload: dict[str, object] = {
        "notification_type": NotificationType.APPROVAL_PENDING,
        "title": "Purchase order PO-1042 needs approval",
        "body": "Vendor Acme, 120,000.00 INR",
        "severity": NotificationSeverity.WARNING,
    }
    payload.update(overrides)
    return await notify(session, company_id=company_id, user_ids=recipients, **payload)  # type: ignore[arg-type]


async def _count_notifications(session: AsyncSession, company_id: uuid.UUID) -> int:
    stmt = (
        select(func.count()).select_from(Notification).where(Notification.company_id == company_id)
    )
    return int((await session.execute(stmt)).scalar_one())


def _service(
    session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID
) -> NotificationService:
    return NotificationService(session, TenantContext(company_id=company_id, user_id=user_id))


# -----------------------------------------------------------------------------
# Fan-out
# -----------------------------------------------------------------------------


class TestNotify:
    async def test_writes_one_row_per_recipient_with_default_channels(
        self, session: AsyncSession, company, user
    ) -> None:
        second = await _make_user(session, company.id)

        written = await _raise(session, company.id, [user.id, second.id])

        assert {n.user_id for n in written} == {user.id, second.id}
        for item in written:
            # Defaults: in-app on, e-mail opt-in only.
            assert item.channels == [NotificationChannel.IN_APP.value]
            assert item.is_read is False
            assert item.read_at is None
            assert item.occurrence_count == 1
            # In-app delivery is immediate, so the row is delivered when written.
            assert item.delivered_at is not None

    async def test_repeated_recipients_produce_one_notification(
        self, session: AsyncSession, company, user
    ) -> None:
        # A duplicated id in one ON CONFLICT statement is a PostgreSQL error, so
        # this is protection against an outage, not tidiness.
        written = await _raise(
            session, company.id, [user.id, user.id, user.id], group_key="po-delay:1"
        )
        assert len(written) == 1
        assert await _count_notifications(session, company.id) == 1

    async def test_audit_entry_is_written_in_the_same_transaction(
        self, session: AsyncSession, company, user
    ) -> None:
        await _raise(session, company.id, [user.id], actor_id=user.id)

        stmt = select(AuditLog).where(
            AuditLog.company_id == company.id, AuditLog.entity_type == "notification"
        )
        entries = list((await session.execute(stmt)).scalars().all())
        assert len(entries) == 1
        assert entries[0].action == AuditAction.CREATE
        assert entries[0].new_values["recipient_count"] == 1

    async def test_system_actor_is_not_written_to_notification_actor_columns(
        self, session: AsyncSession, company, user
    ) -> None:
        await _raise(session, company.id, [user.id], actor_id=SYSTEM_ACTOR_ID)
        await notify_many(
            session,
            company_id=company.id,
            notification_type=NotificationType.APPROVAL_GRANTED,
            drafts=[
                NotificationDraft(
                    user_ids=[user.id],
                    title="Approval granted",
                    body="The system closed the approval.",
                )
            ],
            actor_id=SYSTEM_ACTOR_ID,
        )

        rows = list(
            (
                await session.execute(
                    select(Notification).where(Notification.company_id == company.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.created_by_id is None and row.updated_by_id is None for row in rows)

    async def test_empty_recipient_list_is_a_no_op(self, session: AsyncSession, company) -> None:
        assert await _raise(session, company.id, []) == []
        assert await _count_notifications(session, company.id) == 0

    async def test_rejects_an_absolute_action_url(
        self, session: AsyncSession, company, user
    ) -> None:
        with pytest.raises(ValidationError):
            await _raise(session, company.id, [user.id], action_url="https://evil.example/steal")
        assert await _count_notifications(session, company.id) == 0


class TestGrouping:
    async def test_second_alert_collapses_onto_the_open_row(
        self, session: AsyncSession, company, user
    ) -> None:
        first = await _raise(
            session, company.id, [user.id], group_key="po-delay:42", title="PO-42 is 3 days late"
        )
        second = await _raise(
            session, company.id, [user.id], group_key="po-delay:42", title="PO-42 is 4 days late"
        )

        assert second[0].id == first[0].id
        assert second[0].occurrence_count == 2
        # The newest wording wins - a stale "3 days late" would misinform.
        assert second[0].title == "PO-42 is 4 days late"
        assert await _count_notifications(session, company.id) == 1

    async def test_grouping_does_not_move_the_feed_position(
        self, session: AsyncSession, company, user
    ) -> None:
        first = await _raise(session, company.id, [user.id], group_key="po-delay:7")
        created_at = first[0].created_at

        again = await _raise(session, company.id, [user.id], group_key="po-delay:7")

        # created_at is the keyset sort key: moving it under an open cursor would
        # make rows repeat or vanish mid-scroll.
        assert again[0].created_at == created_at

    async def test_a_new_row_is_raised_once_the_alert_has_been_read(
        self, session: AsyncSession, company, user
    ) -> None:
        first = await _raise(session, company.id, [user.id], group_key="po-delay:9")
        await _service(session, company.id, user.id).mark_read(first[0].id)

        second = await _raise(session, company.id, [user.id], group_key="po-delay:9")

        assert second[0].id != first[0].id
        assert second[0].occurrence_count == 1
        assert await _count_notifications(session, company.id) == 2

    async def test_ungrouped_alerts_never_collapse(
        self, session: AsyncSession, company, user
    ) -> None:
        await _raise(session, company.id, [user.id])
        await _raise(session, company.id, [user.id])
        assert await _count_notifications(session, company.id) == 2

    async def test_different_recipients_keep_their_own_group_rows(
        self, session: AsyncSession, company, user
    ) -> None:
        second = await _make_user(session, company.id)
        written = await _raise(session, company.id, [user.id, second.id], group_key="po-delay:11")
        assert len({n.id for n in written}) == 2


class TestSmartAlertReconciliation:
    async def test_persists_reminders_resolution_reopen_and_quiet_ticks(
        self, session: AsyncSession, company
    ) -> None:
        from src.core.permissions import Permission

        role = await _make_role(session, company.id, "INVENTORY_ALERT_READER")
        role.permissions = [str(Permission.INVENTORY_VIEW)]
        recipient = await _make_user(session, company.id, roles=(role,))
        tenant = TenantContext(company_id=company.id, user_id=recipient.id)
        rule = ALERT_RULES[AlertRuleKey.STOCK_BELOW_REORDER]
        configured = await NotificationAlertService(session, tenant).update_rules(
            NotificationAlertRulesUpdate(
                rules=[
                    {
                        "key": AlertRuleKey.STOCK_BELOW_REORDER,
                        "is_enabled": True,
                        "threshold_minutes": 0,
                        "remind_every_minutes": 60,
                    }
                ]
            )
        )
        effective = next(
            item for item in configured if item.key is AlertRuleKey.STOCK_BELOW_REORDER
        )
        assert effective.is_customized
        assert effective.remind_every_minutes == 60

        entity_id = uuid.uuid4()
        candidate = AlertCandidate(
            entity_type="inventory_balance",
            entity_id=entity_id,
            title="Stock below reorder level",
            body="Available stock is at its reorder level.",
            action_url="/inventory/balances",
        )
        moment = utcnow()

        first = await reconcile_alert(session, tenant, rule, [candidate], as_of=moment)
        quiet = await reconcile_alert(
            session, tenant, rule, [candidate], as_of=moment + timedelta(minutes=30)
        )
        reminder = await reconcile_alert(
            session, tenant, rule, [candidate], as_of=moment + timedelta(minutes=60)
        )
        resolved = await reconcile_alert(
            session, tenant, rule, [], as_of=moment + timedelta(minutes=61)
        )
        reopened = await reconcile_alert(
            session, tenant, rule, [candidate], as_of=moment + timedelta(minutes=62)
        )

        assert first["notified"] == 1
        assert quiet["notified"] == 0
        assert quiet["resolved"] == 0
        assert reminder["notified"] == 1
        assert resolved["notified"] == 0
        assert resolved["resolved"] == 1
        assert reopened["notified"] == 1

        states = list(
            (
                await session.execute(
                    select(NotificationAlertState).where(
                        NotificationAlertState.company_id == company.id,
                        NotificationAlertState.rule_key == rule.key.value,
                        NotificationAlertState.entity_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(states) == 2
        assert sum(state.resolved_at is None for state in states) == 1
        assert sum(state.notified_count for state in states) == 3

        notifications = list(
            (
                await session.execute(
                    select(Notification).where(
                        Notification.company_id == company.id,
                        Notification.user_id == recipient.id,
                        Notification.notification_type
                        == NotificationType.STOCK_BELOW_REORDER.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 1
        assert notifications[0].occurrence_count == 3
        assert notifications[0].group_key == f"alert:{rule.key.value}"


# -----------------------------------------------------------------------------
# Preferences
# -----------------------------------------------------------------------------


class TestPreferences:
    async def test_a_muted_type_produces_no_row(self, session: AsyncSession, company, user) -> None:
        service = _service(session, company.id, user.id)
        await service.update_preferences(
            PreferencesUpdateRequest(
                preferences=[
                    NotificationPreferenceUpdate(
                        notification_type=NotificationType.APPROVAL_PENDING,
                        in_app_enabled=False,
                        email_enabled=False,
                    )
                ]
            )
        )

        written = await _raise(session, company.id, [user.id])

        assert written == []
        assert await _count_notifications(session, company.id) == 0

    async def test_critical_alerts_ignore_an_in_app_opt_out(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        await service.update_preferences(
            PreferencesUpdateRequest(
                preferences=[
                    NotificationPreferenceUpdate(
                        notification_type=NotificationType.STOCK_SHORTAGE,
                        in_app_enabled=False,
                        email_enabled=False,
                    )
                ]
            )
        )

        written = await _raise(
            session,
            company.id,
            [user.id],
            notification_type=NotificationType.STOCK_SHORTAGE,
            severity=NotificationSeverity.CRITICAL,
        )

        assert len(written) == 1
        assert written[0].channels == [NotificationChannel.IN_APP.value]

    async def test_email_opt_in_records_the_channel_and_leaves_delivery_open(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        await service.update_preferences(
            PreferencesUpdateRequest(
                preferences=[
                    NotificationPreferenceUpdate(
                        notification_type=NotificationType.APPROVAL_PENDING,
                        in_app_enabled=False,
                        email_enabled=True,
                    )
                ]
            )
        )

        written = await _raise(session, company.id, [user.id])

        assert written[0].channels == [NotificationChannel.EMAIL.value]
        # Not delivered until the worker says so - that gap is what makes a
        # queued-but-never-sent message visible.
        assert written[0].delivered_at is None

    async def test_an_email_only_notification_stays_out_of_the_bell(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        await service.update_preferences(
            PreferencesUpdateRequest(
                preferences=[
                    NotificationPreferenceUpdate(
                        notification_type=NotificationType.APPROVAL_PENDING,
                        in_app_enabled=False,
                        email_enabled=True,
                    )
                ]
            )
        )
        await _raise(session, company.id, [user.id])

        items, _ = await service.list_feed(NotificationFilters(), CursorParams(limit=50))
        count = await service.unread_count()

        assert items == []
        assert count.unread_count == 0

    async def test_update_is_an_upsert_not_a_duplicate(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        request = PreferencesUpdateRequest(
            preferences=[
                NotificationPreferenceUpdate(
                    notification_type=NotificationType.REPORT_READY,
                    in_app_enabled=True,
                    email_enabled=True,
                )
            ]
        )
        await service.update_preferences(request)
        request.preferences[0].email_enabled = False
        result = await service.update_preferences(request)

        stmt = (
            select(func.count())
            .select_from(NotificationPreference)
            .where(
                NotificationPreference.company_id == company.id,
                NotificationPreference.user_id == user.id,
            )
        )
        assert int((await session.execute(stmt)).scalar_one()) == 1

        saved = next(p for p in result if p.notification_type is NotificationType.REPORT_READY)
        assert saved.email_enabled is False
        assert saved.is_customised is True

    async def test_unconfigured_types_report_the_module_defaults(
        self, session: AsyncSession, company, user
    ) -> None:
        preferences = await _service(session, company.id, user.id).get_preferences()

        assert len(preferences) == len(NotificationType)
        assert all(p.in_app_enabled for p in preferences)
        assert not any(p.email_enabled for p in preferences)
        assert not any(p.is_customised for p in preferences)


# -----------------------------------------------------------------------------
# Role fan-out
# -----------------------------------------------------------------------------


class TestNotifyRole:
    async def test_resolves_active_holders_of_the_role(
        self, session: AsyncSession, company, user
    ) -> None:
        role = await _make_role(session, company.id, "PURCHASE_APPROVER")
        member = await _make_user(session, company.id, roles=(role,))

        written = await notify_role(
            session,
            company_id=company.id,
            role_codes=["purchase_approver"],  # matched case-insensitively
            notification_type=NotificationType.APPROVAL_PENDING,
            title="PO-1042 awaits approval",
            body="Raised by Test User",
        )

        assert [n.user_id for n in written] == [member.id]

    async def test_skips_deactivated_users_and_inactive_roles(
        self, session: AsyncSession, company
    ) -> None:
        live_role = await _make_role(session, company.id, "QC_LEAD")
        dead_role = await _make_role(session, company.id, "OLD_ROLE", active=False)

        await _make_user(session, company.id, is_active=False, roles=(live_role,))
        await _make_user(session, company.id, roles=(dead_role,))

        written = await notify_role(
            session,
            company_id=company.id,
            role_codes=["QC_LEAD", "OLD_ROLE"],
            notification_type=NotificationType.QC_INSPECTION_PENDING,
            title="Inspection pending",
            body="Lot 12",
        )
        assert written == []

    async def test_does_not_reach_another_tenant(
        self, session: AsyncSession, company, other_company
    ) -> None:
        code = "SHARED_ROLE"
        await _make_role(session, company.id, code)
        foreign_role = await _make_role(session, other_company.id, code)
        await _make_user(session, other_company.id, roles=(foreign_role,))

        written = await notify_role(
            session,
            company_id=company.id,
            role_codes=[code],
            notification_type=NotificationType.APPROVAL_PENDING,
            title="Internal only",
            body="Must not cross tenants",
        )
        assert written == []


# -----------------------------------------------------------------------------
# The feed
# -----------------------------------------------------------------------------


class TestFeed:
    async def test_unread_sorts_ahead_of_read_regardless_of_age(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        older = (await _raise(session, company.id, [user.id], title="Older"))[0]
        newer = (await _raise(session, company.id, [user.id], title="Newer"))[0]
        await service.mark_read(newer.id)

        items, _ = await service.list_feed(NotificationFilters(), CursorParams(limit=50))

        assert [i.id for i in items] == [older.id, newer.id]

    async def test_paging_visits_every_row_exactly_once_across_the_boundary(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        created = [
            (await _raise(session, company.id, [user.id], title=f"N{i}"))[0] for i in range(6)
        ]
        # Read the two newest, so the read half contains rows *newer* than the
        # last unread one - the case a naive keyset predicate silently drops.
        for item in created[-2:]:
            await service.mark_read(item.id)

        seen: list[uuid.UUID] = []
        cursor: str | None = None
        for _ in range(10):
            items, pagination = await service.list_feed(
                NotificationFilters(), CursorParams(cursor=cursor, limit=2)
            )
            seen.extend(i.id for i in items)
            if not pagination.has_next:
                break
            cursor = pagination.next_cursor

        assert len(seen) == len(set(seen)) == 6
        assert set(seen) == {c.id for c in created}
        # Unread first, then read; newest first inside each half.
        assert seen[:4] == [created[3].id, created[2].id, created[1].id, created[0].id]
        assert seen[4:] == [created[5].id, created[4].id]

    async def test_unread_only_filter_pages_within_the_unread_half(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        rows = [(await _raise(session, company.id, [user.id], title=f"U{i}"))[0] for i in range(4)]
        await service.mark_read(rows[0].id)

        seen: list[uuid.UUID] = []
        cursor: str | None = None
        while True:
            items, pagination = await service.list_feed(
                NotificationFilters(unread_only=True), CursorParams(cursor=cursor, limit=2)
            )
            seen.extend(i.id for i in items)
            if not pagination.has_next:
                break
            cursor = pagination.next_cursor

        assert seen == [rows[3].id, rows[2].id, rows[1].id]

    async def test_filters_by_type_and_severity(self, session: AsyncSession, company, user) -> None:
        service = _service(session, company.id, user.id)
        await _raise(session, company.id, [user.id])
        critical = (
            await _raise(
                session,
                company.id,
                [user.id],
                notification_type=NotificationType.STOCK_SHORTAGE,
                severity=NotificationSeverity.CRITICAL,
            )
        )[0]

        items, _ = await service.list_feed(
            NotificationFilters(notification_type=NotificationType.STOCK_SHORTAGE),
            CursorParams(limit=50),
        )
        assert [i.id for i in items] == [critical.id]

        items, _ = await service.list_feed(
            NotificationFilters(severity=NotificationSeverity.CRITICAL), CursorParams(limit=50)
        )
        assert [i.id for i in items] == [critical.id]

    async def test_one_users_feed_excludes_another_users_notifications(
        self, session: AsyncSession, company, user
    ) -> None:
        colleague = await _make_user(session, company.id)
        await _raise(session, company.id, [colleague.id])

        items, _ = await _service(session, company.id, user.id).list_feed(
            NotificationFilters(), CursorParams(limit=50)
        )
        assert items == []

    async def test_another_tenant_cannot_see_the_notification(
        self, session: AsyncSession, company, other_company, user
    ) -> None:
        written = await _raise(session, company.id, [user.id])

        # Same user id, different tenant scope: the row must be unreachable.
        foreign = _service(session, other_company.id, user.id)
        items, _ = await foreign.list_feed(NotificationFilters(), CursorParams(limit=50))
        assert items == []
        with pytest.raises(NotFoundError):
            await foreign.mark_read(written[0].id)


# -----------------------------------------------------------------------------
# Acknowledgement
# -----------------------------------------------------------------------------


class TestMarkRead:
    async def test_marks_one_and_records_the_status_change(
        self, session: AsyncSession, company, user
    ) -> None:
        written = await _raise(session, company.id, [user.id])
        service = _service(session, company.id, user.id)

        result = await service.mark_read(written[0].id)

        assert result.is_read is True
        assert result.read_at is not None
        stmt = select(AuditLog).where(
            AuditLog.company_id == company.id,
            AuditLog.entity_type == "notification",
            AuditLog.action == AuditAction.STATUS_CHANGE,
        )
        entries = list((await session.execute(stmt)).scalars().all())
        assert len(entries) == 1
        assert entries[0].new_values["status"] == "READ"

    async def test_marking_read_twice_is_idempotent(
        self, session: AsyncSession, company, user
    ) -> None:
        written = await _raise(session, company.id, [user.id])
        service = _service(session, company.id, user.id)

        first = await service.mark_read(written[0].id)
        second = await service.mark_read(written[0].id)

        # The timestamp records when it was FIRST seen, so it must not move.
        assert second.read_at == first.read_at

    async def test_unknown_id_is_not_found(self, session: AsyncSession, company, user) -> None:
        with pytest.raises(NotFoundError):
            await _service(session, company.id, user.id).mark_read(uuid.uuid4())

    async def test_a_colleagues_notification_is_not_found_rather_than_forbidden(
        self, session: AsyncSession, company, user
    ) -> None:
        colleague = await _make_user(session, company.id)
        written = await _raise(session, company.id, [colleague.id])

        with pytest.raises(NotFoundError):
            await _service(session, company.id, user.id).mark_read(written[0].id)

    async def test_read_all_clears_the_badge(self, session: AsyncSession, company, user) -> None:
        service = _service(session, company.id, user.id)
        for _ in range(3):
            await _raise(session, company.id, [user.id])

        before = await service.unread_count()
        result = await service.mark_all_read()
        after = await service.unread_count()

        assert before.unread_count == 3
        assert result.marked == 3
        assert result.remaining_unread == 0
        assert after.unread_count == 0

    async def test_read_all_leaves_other_users_alone(
        self, session: AsyncSession, company, user
    ) -> None:
        colleague = await _make_user(session, company.id)
        await _raise(session, company.id, [user.id, colleague.id])

        await _service(session, company.id, user.id).mark_all_read()

        assert (await _service(session, company.id, colleague.id).unread_count()).unread_count == 1


class TestUnreadCount:
    async def test_reports_the_ceiling_as_capped(
        self, session: AsyncSession, company, user
    ) -> None:
        repo = NotificationRepository(
            session, TenantContext(company_id=company.id, user_id=user.id)
        )
        for _ in range(4):
            await _raise(session, company.id, [user.id])

        total, capped = await repo.unread_count(user.id, ceiling=3)

        assert (total, capped) == (3, True)

    async def test_read_notifications_are_not_counted(
        self, session: AsyncSession, company, user
    ) -> None:
        service = _service(session, company.id, user.id)
        written = await _raise(session, company.id, [user.id])
        await service.mark_read(written[0].id)

        assert (await service.unread_count()).unread_count == 0


class TestRetentionOfHistory:
    async def test_read_notifications_stay_readable_in_the_feed(
        self, session: AsyncSession, company, user
    ) -> None:
        # A read notification is history, not rubbish: it must remain listable so
        # "was I told, and when?" is answerable.
        service = _service(session, company.id, user.id)
        written = await _raise(session, company.id, [user.id])
        await service.mark_read(written[0].id)

        items, _ = await service.list_feed(NotificationFilters(), CursorParams(limit=50))
        assert [i.id for i in items] == [written[0].id]
        assert items[0].read_at is not None
        assert items[0].read_at <= utcnow() + timedelta(seconds=1)


class TestDealerRecipientResolution:
    async def test_resolves_only_active_customer_users_with_the_requested_permission(
        self, session: AsyncSession, company, other_company
    ) -> None:
        customer_id = await _make_customer(session, company.id)
        other_customer_id = await _make_customer(session, other_company.id)
        permission = "dealer_portal.account"
        allowed = await _make_user(
            session,
            company.id,
            customer_id=customer_id,
            extra_permissions=[permission],
        )
        await _make_user(
            session,
            company.id,
            customer_id=customer_id,
            extra_permissions=[permission],
            denied_permissions=[permission],
        )
        await _make_user(
            session,
            company.id,
            is_active=False,
            customer_id=customer_id,
            extra_permissions=[permission],
        )
        await _make_user(
            session,
            company.id,
            customer_id=other_customer_id,
            extra_permissions=[permission],
        )
        await _make_user(
            session,
            other_company.id,
            customer_id=other_customer_id,
            extra_permissions=[permission],
        )
        repo = NotificationRepository(
            session, TenantContext(company_id=company.id, user_id=allowed.id)
        )

        found = await repo.user_ids_for_customer(customer_id, permission=permission)

        assert found == [allowed.id]
