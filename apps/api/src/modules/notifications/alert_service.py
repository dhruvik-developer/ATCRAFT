"""Company-scoped smart-alert configuration."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.ids import new_id
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.notifications.alerts import (
    ALERT_RULES,
    AlertCandidate,
    AlertRule,
    AlertRuleKey,
    AlertStateSnapshot,
    plan_reconciliation,
)
from src.modules.notifications.models import (
    NotificationAlertRule,
    NotificationSeverity,
    NotificationType,
)
from src.modules.notifications.repository import (
    NotificationAlertRepository,
    NotificationRepository,
)
from src.modules.notifications.schemas import (
    NotificationAlertRuleRead,
    NotificationAlertRulesUpdate,
)
from src.modules.notifications.service import NotificationDraft, notify_many

_SESSION_CACHE_KEY = "notifications.alert_rule_settings"


class NotificationAlertService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = NotificationAlertRepository(session, tenant)

    @staticmethod
    def _effective(
        stored: list[NotificationAlertRule],
    ) -> list[NotificationAlertRuleRead]:
        by_key = {AlertRuleKey(row.rule_key): row for row in stored}
        effective = []
        for key, rule in ALERT_RULES.items():
            override = by_key.get(key)
            effective.append(
                NotificationAlertRuleRead(
                    key=key,
                    notification_type=rule.notification_type,
                    name=rule.name,
                    description=rule.description,
                    severity=rule.severity,
                    granularity=rule.granularity,
                    is_enabled=override.is_enabled if override else True,
                    threshold_minutes=(
                        override.threshold_minutes if override else rule.default_threshold_minutes
                    ),
                    remind_every_minutes=(
                        override.remind_every_minutes
                        if override
                        else rule.default_remind_every_minutes
                    ),
                    is_customized=override is not None,
                )
            )
        return effective

    async def list_rules(self) -> list[NotificationAlertRuleRead]:
        return self._effective(await self.repo.list_rules())

    async def update_rules(
        self, payload: NotificationAlertRulesUpdate
    ) -> list[NotificationAlertRuleRead]:
        current = await self.repo.list_rules()
        effective_before = {item.key: item for item in self._effective(current)}
        moment = utcnow()
        changed: list[dict[str, Any]] = []
        reset: list[str] = []
        before_values: dict[str, dict[str, Any]] = {}
        after_values: dict[str, dict[str, Any]] = {}
        for item in payload.rules:
            old = effective_before[item.key]
            rule = ALERT_RULES[item.key]
            new_values = {
                "is_enabled": item.is_enabled,
                "threshold_minutes": item.threshold_minutes,
                "remind_every_minutes": item.remind_every_minutes,
            }
            old_values = {
                "is_enabled": old.is_enabled,
                "threshold_minutes": old.threshold_minutes,
                "remind_every_minutes": old.remind_every_minutes,
            }
            if old_values == new_values:
                continue
            before_values[item.key.value] = old_values
            after_values[item.key.value] = new_values
            defaults = {
                "is_enabled": True,
                "threshold_minutes": rule.default_threshold_minutes,
                "remind_every_minutes": rule.default_remind_every_minutes,
            }
            if new_values == defaults:
                if old.is_customized:
                    reset.append(item.key.value)
                continue
            changed.append(
                {
                    "id": new_id(),
                    "company_id": self.tenant.company_id,
                    "rule_key": item.key.value,
                    "is_enabled": item.is_enabled,
                    "threshold_minutes": item.threshold_minutes,
                    "remind_every_minutes": item.remind_every_minutes,
                    "created_at": moment,
                    "updated_at": moment,
                    "created_by_id": self.tenant.user_id,
                    "updated_by_id": self.tenant.user_id,
                }
            )

        if changed or reset:
            async with transaction(self.session):
                await self.repo.upsert_rules(changed)
                await self.repo.delete_rules(reset)
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CONFIG_CHANGE,
                    entity_type="notification_alert_rule",
                    summary=f"Updated {len(changed) + len(reset)} smart alert rule(s)",
                    user_id=self.tenant.user_id,
                    old_values=before_values,
                    new_values=after_values,
                )
            self.session.info.pop(_SESSION_CACHE_KEY, None)

        return await self.list_rules()


async def effective_alert_settings(
    session: AsyncSession, tenant: TenantContext
) -> dict[AlertRuleKey, tuple[bool, int, int]]:
    """Load effective values once per session, never across requests or jobs."""
    cache = session.info.setdefault(_SESSION_CACHE_KEY, {})
    company_id = tenant.company_id
    cached = cache.get(company_id)
    if cached is not None:
        return cached

    stored = await NotificationAlertRepository(session, tenant).list_rules()
    reads = NotificationAlertService._effective(stored)
    result = {
        row.key: (row.is_enabled, row.threshold_minutes, row.remind_every_minutes) for row in reads
    }
    cache[company_id] = result
    return result


async def reconcile_alert(
    session: AsyncSession,
    tenant: TenantContext,
    rule: AlertRule,
    candidates: list[AlertCandidate],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    """Persist one rule's current episode set and notify only when due."""
    settings_by_key = await effective_alert_settings(session, tenant)
    enabled, _, remind_every_minutes = settings_by_key[rule.key]
    if not enabled:
        return {"detected": len(candidates), "notified": 0, "resolved": 0, "notified_ids": []}
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware.")

    by_id = {candidate.entity_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError(f"{rule.key} scanner returned duplicate entity ids.")

    repo = NotificationAlertRepository(session, tenant)
    moment = as_of
    async with transaction(session):
        stored = await repo.list_open_states(rule.key.value)
        open_states = {
            row.entity_id: AlertStateSnapshot(
                entity_id=row.entity_id,
                last_notified_at=row.last_notified_at,
                notified_count=row.notified_count,
            )
            for row in stored
        }
        plan = plan_reconciliation(
            open_states=open_states,
            current_entity_ids=set(by_id),
            as_of=moment,
            remind_every=timedelta(minutes=remind_every_minutes),
        )

        await repo.resolve_states(rule.key.value, tuple(plan.gone_ids), at=moment)
        now_rows = [
            {
                "id": new_id(),
                "company_id": tenant.company_id,
                "rule_key": rule.key.value,
                "entity_type": by_id[entity_id].entity_type,
                "entity_id": entity_id,
                "first_detected_at": moment,
                "last_detected_at": moment,
                "last_notified_at": None,
                "notified_count": 0,
                "resolved_at": None,
                "created_at": moment,
                "updated_at": moment,
                "created_by_id": None,
                "updated_by_id": None,
            }
            for entity_id in plan.new_ids
        ]
        await repo.insert_open_states(now_rows)
        await repo.update_detected(rule.key.value, tuple(by_id), at=moment)
        due_candidates = [by_id[entity_id] for entity_id in plan.notify_ids]
        if not due_candidates:
            return {
                "detected": len(by_id),
                "notified": 0,
                "resolved": len(plan.gone_ids),
                "notified_ids": [],
            }

        notification_repo = NotificationRepository(session, tenant)
        internal_ids: set[uuid.UUID] = set()
        if rule.internal_permission is not None:
            internal_ids.update(
                await notification_repo.user_ids_with_permission(rule.internal_permission)
            )

        creators = (
            {
                candidate.creator_id
                for candidate in due_candidates
                if candidate.creator_id is not None
            }
            if rule.notify_creator
            else set()
        )
        creator_ids = set(await notification_repo.internal_user_ids(tuple(creators)))
        vendor_ids = {
            candidate.vendor_id
            for candidate in due_candidates
            if candidate.vendor_id is not None and rule.vendor_permission is not None
        }
        vendor_users = (
            await notification_repo.user_ids_by_vendor(
                tuple(vendor_ids), permission=rule.vendor_permission
            )
            if rule.vendor_permission is not None
            else {}
        )
        assigned_pairs = [
            (candidate.assigned_user_id, candidate.vendor_id)
            for candidate in due_candidates
            if candidate.assigned_user_id is not None and candidate.vendor_id is not None
        ]
        assigned_users = await notification_repo.active_assigned_vendor_users(assigned_pairs)

        drafts: list[NotificationDraft] = []
        notified_entity_ids: set[uuid.UUID] = set()
        if rule.granularity.value == "DIGEST":
            if internal_ids:
                notified_entity_ids.update(plan.notify_ids)
                drafts.append(
                    NotificationDraft(
                        user_ids=tuple(internal_ids),
                        title=f"{len(due_candidates)} {rule.name.lower()} alert(s)",
                        body=f"{len(due_candidates)} records currently meet the alert condition.",
                        severity=NotificationSeverity(rule.severity),
                        action_url=due_candidates[0].action_url,
                        group_key=f"alert:{rule.key.value}",
                    )
                )
        else:
            for candidate in due_candidates:
                recipients = set(internal_ids)
                if candidate.creator_id is not None and candidate.creator_id in creator_ids:
                    recipients.add(candidate.creator_id)
                if candidate.vendor_id is not None:
                    recipients.update(vendor_users.get(candidate.vendor_id, ()))
                if (
                    candidate.assigned_user_id is not None
                    and candidate.assigned_user_id in assigned_users
                ):
                    recipients.add(candidate.assigned_user_id)
                if not recipients:
                    continue
                notified_entity_ids.add(candidate.entity_id)
                drafts.append(
                    NotificationDraft(
                        user_ids=tuple(recipients),
                        title=candidate.title,
                        body=candidate.body,
                        severity=NotificationSeverity(rule.severity),
                        entity_type=candidate.entity_type,
                        entity_id=candidate.entity_id,
                        action_url=candidate.action_url,
                        group_key=f"alert:{rule.key.value}:{candidate.entity_id}",
                    )
                )

        written = await notify_many(
            session,
            company_id=tenant.company_id,
            notification_type=NotificationType(rule.notification_type),
            drafts=drafts,
            write_audit=False,
        )
        await repo.mark_notified(rule.key.value, tuple(plan.notify_ids), at=moment)
        if written:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.STATUS_CHANGE,
                entity_type="notification_alert_rule",
                summary=f"Smart alert {rule.key.value} notified {len(due_candidates)} episode(s)",
                user_id=None,
                actor_label="notifications.scan_smart_alerts",
                context={
                    "rule": rule.key.value,
                    "entity_ids": [str(entity_id) for entity_id in sorted(notified_entity_ids)],
                    "notification_count": len(written),
                },
            )

        return {
            "detected": len(by_id),
            "notified": len(written),
            "resolved": len(plan.gone_ids),
            "notified_ids": sorted(notified_entity_ids) if written else [],
        }


__all__ = [
    "NotificationAlertService",
    "effective_alert_settings",
    "reconcile_alert",
]
