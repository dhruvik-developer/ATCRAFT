from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.core.permissions import Permission
from src.modules.notifications.alert_service import NotificationAlertService
from src.modules.notifications.alerts import (
    ALERT_RULES,
    AlertGranularity,
    AlertRuleKey,
    AlertStateSnapshot,
    plan_reconciliation,
)
from src.modules.notifications.models import NotificationAlertRule, NotificationType
from src.modules.notifications.schemas import NotificationAlertRulesUpdate
from src.workers.tasks.notifications import SMART_ALERT_SCANNERS

pytestmark = pytest.mark.unit


def test_alert_rule_registry_covers_every_rule_key() -> None:
    assert set(ALERT_RULES) == set(AlertRuleKey)
    assert all(rule.default_threshold_minutes >= 0 for rule in ALERT_RULES.values())
    assert all(rule.default_remind_every_minutes > 0 for rule in ALERT_RULES.values())
    assert all(rule.notification_type for rule in ALERT_RULES.values())
    assert set(SMART_ALERT_SCANNERS) == set(ALERT_RULES)


def test_registry_marks_reorder_alert_as_digest() -> None:
    rule = ALERT_RULES[AlertRuleKey.STOCK_BELOW_REORDER]

    assert rule.granularity is AlertGranularity.DIGEST


def test_registry_uses_declared_notification_types_and_permissions() -> None:
    permissions = {permission.value for permission in Permission}

    for rule in ALERT_RULES.values():
        assert NotificationType(rule.notification_type)
        if rule.internal_permission is not None:
            assert rule.internal_permission in permissions
        if rule.vendor_permission is not None:
            assert rule.vendor_permission in permissions


def test_reconcile_identifies_new_still_gone_and_reminder_due_episodes() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    new_id, due_id, quiet_id, gone_id = (uuid.uuid4() for _ in range(4))
    states = {
        due_id: AlertStateSnapshot(
            entity_id=due_id,
            last_notified_at=now - timedelta(hours=25),
            notified_count=1,
        ),
        quiet_id: AlertStateSnapshot(
            entity_id=quiet_id,
            last_notified_at=now - timedelta(hours=23),
            notified_count=3,
        ),
        gone_id: AlertStateSnapshot(
            entity_id=gone_id,
            last_notified_at=now - timedelta(hours=1),
            notified_count=1,
        ),
    }

    plan = plan_reconciliation(
        open_states=states,
        current_entity_ids={new_id, due_id, quiet_id},
        as_of=now,
        remind_every=timedelta(hours=24),
    )

    assert plan.new_ids == frozenset({new_id})
    assert plan.still_ids == frozenset({due_id, quiet_id})
    assert plan.gone_ids == frozenset({gone_id})
    assert plan.notify_ids == frozenset({new_id, due_id})


def test_reconcile_handles_empty_current_set_without_broadening() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    prior_id = uuid.uuid4()

    plan = plan_reconciliation(
        open_states={
            prior_id: AlertStateSnapshot(
                entity_id=prior_id,
                last_notified_at=now - timedelta(hours=1),
                notified_count=1,
            )
        },
        current_entity_ids=set(),
        as_of=now,
        remind_every=timedelta(hours=24),
    )

    assert plan.new_ids == frozenset()
    assert plan.still_ids == frozenset()
    assert plan.gone_ids == frozenset({prior_id})
    assert plan.notify_ids == frozenset()


def test_reconcile_quiet_tick_does_not_notify_or_resolve() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    entity_id = uuid.uuid4()

    plan = plan_reconciliation(
        open_states={
            entity_id: AlertStateSnapshot(
                entity_id=entity_id,
                last_notified_at=now - timedelta(hours=1),
                notified_count=2,
            )
        },
        current_entity_ids={entity_id},
        as_of=now,
        remind_every=timedelta(hours=24),
    )

    assert plan.new_ids == frozenset()
    assert plan.gone_ids == frozenset()
    assert plan.notify_ids == frozenset()


def test_effective_rules_include_code_defaults_for_sparse_settings() -> None:
    effective = NotificationAlertService._effective([])

    assert len(effective) == len(ALERT_RULES)
    assert all(not rule.is_customized for rule in effective)
    assert {
        rule.key: (rule.is_enabled, rule.threshold_minutes, rule.remind_every_minutes)
        for rule in effective
    } == {
        key: (True, rule.default_threshold_minutes, rule.default_remind_every_minutes)
        for key, rule in ALERT_RULES.items()
    }


def test_effective_rules_apply_company_overrides() -> None:
    custom = NotificationAlertRule(
        rule_key=AlertRuleKey.PO_NOT_ACKNOWLEDGED.value,
        is_enabled=False,
        threshold_minutes=60,
        remind_every_minutes=120,
    )

    effective = {rule.key: rule for rule in NotificationAlertService._effective([custom])}

    assert effective[AlertRuleKey.PO_NOT_ACKNOWLEDGED].is_customized
    assert not effective[AlertRuleKey.PO_NOT_ACKNOWLEDGED].is_enabled
    assert effective[AlertRuleKey.PO_NOT_ACKNOWLEDGED].threshold_minutes == 60
    assert effective[AlertRuleKey.PO_NOT_ACKNOWLEDGED].remind_every_minutes == 120


def test_alert_rule_update_rejects_duplicate_keys() -> None:
    with pytest.raises(ValidationError, match="only once"):
        NotificationAlertRulesUpdate(
            rules=[
                {
                    "key": AlertRuleKey.PO_NOT_ACKNOWLEDGED,
                    "is_enabled": True,
                    "threshold_minutes": 60,
                    "remind_every_minutes": 60,
                },
                {
                    "key": AlertRuleKey.PO_NOT_ACKNOWLEDGED,
                    "is_enabled": False,
                    "threshold_minutes": 90,
                    "remind_every_minutes": 120,
                },
            ]
        )


def test_alert_rule_update_requires_nonnegative_threshold_and_positive_reminder() -> None:
    with pytest.raises(ValidationError):
        NotificationAlertRulesUpdate(
            rules=[
                {
                    "key": AlertRuleKey.PO_NOT_ACKNOWLEDGED,
                    "is_enabled": True,
                    "threshold_minutes": -1,
                    "remind_every_minutes": 60,
                }
            ]
        )
