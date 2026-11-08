"""Notification write routes over HTTP: bell, preferences, alert rules, messaging.

Channel selection, collapsing and delivery are pinned in
``tests/integration/test_notifications_service.py``, ``test_messaging_channels``
and ``tests/api/test_webpush_http.py``; the generic suites cover every route's
anonymous, unentitled and unknown-id answers. This file walks the six handlers
no test had executed through HTTP, with real rows:

* ``POST /notifications/push/test`` raises one announcement on the caller's own
  bell and reports how many devices it was queued to;
* ``POST /notifications/read-all`` acknowledges the caller's backlog - nobody
  else's - with one audit row per batch;
* ``PUT /notifications/preferences`` upserts the caller's own rows and answers
  with the whole effective matrix;
* ``PUT /notifications/alert-rules`` stores an override only where a rule
  departs from its code default, and deletes it again on a reset;
* ``PUT /notifications/channel-templates`` upserts this company's messaging
  mappings without touching another company's;
* ``POST /notifications/channel-templates/test`` sends one template to the
  caller's verified number - through a fake SMS gateway, never the network.

Celery's ``send_task`` is stubbed for every test, so no fan-out leaves the process.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.messaging import MessagingOutcome, MessagingResult
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.notifications import channel_service
from src.modules.notifications import service as notification_service
from src.modules.notifications.alerts import ALERT_RULES, AlertRuleKey
from src.modules.notifications.models import (
    Notification,
    NotificationAlertRule,
    NotificationChannelTemplate,
    NotificationPreference,
    NotificationType,
)
from src.modules.notifications.service import notify
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.test_webpush_http import _payload as push_subscription

pytestmark = [pytest.mark.api, pytest.mark.integration]

ANNOUNCEMENT = NotificationType.SYSTEM_ANNOUNCEMENT.value
PHONE = "+919876543210"


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_type: str
) -> list[AuditLog]:
    # (created_at, id): every row written in one transaction shares now().
    stmt = (
        select(AuditLog)
        .where(AuditLog.company_id == company_id, AuditLog.entity_type == entity_type)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.scalars(stmt)).all())


async def bell(
    session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID, *, read: bool = False
) -> uuid.UUID:
    [row] = await notify(
        session,
        company_id=company_id,
        user_ids=[user_id],
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        title="Plant shutdown on Sunday",
        body="Maintenance window",
    )
    if read:
        row.is_read, row.read_at = True, utcnow()
        await session.flush()
    return row.id


@dataclass
class SmsGateway:
    """Stands in for the SMS provider factory; records what it was asked to send."""

    outcome: MessagingOutcome = MessagingOutcome.DELIVERED
    sent: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def __call__(self) -> SmsGateway:
        return self

    async def send_template(self, phone: str, template_id: str, values: list[str]) -> Any:
        self.sent.append((phone, template_id, list(values)))
        return MessagingResult(self.outcome)


def sms_mapping(**overrides: Any) -> dict[str, Any]:
    return {
        "channel": "SMS",
        "notification_type": ANNOUNCEMENT,
        "template_id": "dlt-1007",
        "sms_text": "ATCRAFT: {#var#} - {#var#}",
        "parameters": ["title", "code"],
        **overrides,
    }


def whatsapp_mapping(**overrides: Any) -> dict[str, Any]:
    return {
        "channel": "WHATSAPP",
        "notification_type": ANNOUNCEMENT,
        "template_name": "plant_announcement",
        "language": "en",
        "parameters": ["title", "body"],
        **overrides,
    }


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
def push_on(monkeypatch) -> list[Any]:
    queued: list[Any] = []

    def queue(rows: Any) -> int:
        queued.extend(rows)
        return len(rows)

    monkeypatch.setattr(settings, "web_push_enabled", True)
    monkeypatch.setattr(notification_service, "_send_push_tasks", queue)
    return queued


@pytest.fixture
async def colleague(session, tenant) -> uuid.UUID:
    return (await seed_support.seed_user(session, company_id=tenant.company_id)).id


@pytest.fixture
def gateway(monkeypatch) -> SmsGateway:
    """SMS switched on and pointed at a fake: the provider factory never builds a client."""
    fake = SmsGateway()
    monkeypatch.setattr(channel_service, "sms_provider", fake)
    monkeypatch.setattr(settings, "sms_enabled", True)
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    return fake


@pytest.fixture
async def reachable(session, user) -> None:
    """The caller's own number, verified, with SMS consent on record."""
    user.phone_e164 = PHONE
    user.phone_verified_at = utcnow()
    user.sms_opt_in_at = utcnow()
    await session.flush()


# -----------------------------------------------------------------------------
# POST /notifications/push/test
# -----------------------------------------------------------------------------


async def test_a_test_push_lands_on_the_callers_own_bell(api_client, session, tenant):
    assert ok(await api_client.post("/notifications/push/test")) == {"queued": 0}

    rows = (
        await session.execute(
            select(
                Notification.user_id, Notification.notification_type, Notification.channels
            ).where(Notification.company_id == tenant.company_id)
        )
    ).all()
    assert [tuple(row) for row in rows] == [(tenant.user_id, ANNOUNCEMENT, ["IN_APP"])]


async def test_a_test_push_is_queued_to_a_registered_device(api_client, session, tenant, push_on):
    ok(await api_client.post("/notifications/push/subscriptions", json=push_subscription()))

    assert ok(await api_client.post("/notifications/push/test")) == {"queued": 1}

    channels = await session.scalar(
        select(Notification.channels).where(
            Notification.user_id == tenant.user_id,
            Notification.notification_type == ANNOUNCEMENT,
        )
    )
    assert channels == ["IN_APP", "PUSH"]


# -----------------------------------------------------------------------------
# POST /notifications/read-all
# -----------------------------------------------------------------------------


async def test_read_all_acknowledges_only_the_callers_backlog_and_audits_it(
    api_client, session, tenant, colleague
):
    company_id = tenant.company_id
    mine = [await bell(session, company_id, tenant.user_id) for _ in range(2)]
    await bell(session, company_id, tenant.user_id, read=True)
    theirs = await bell(session, company_id, colleague)

    assert ok(await api_client.post("/notifications/read-all")) == {
        "marked": 2,
        "remaining_unread": 0,
    }

    states = dict(
        (
            await session.execute(
                select(Notification.id, Notification.read_at).where(
                    Notification.id.in_([*mine, theirs])
                )
            )
        ).all()
    )
    assert all(states[row_id] is not None for row_id in mine)
    assert states[theirs] is None

    async def acknowledgements() -> list[AuditLog]:
        return [
            row
            for row in await audit_rows(session, tenant.company_id, "notification")
            if row.action == AuditAction.STATUS_CHANGE
        ]

    [entry] = await acknowledgements()
    assert entry.created_by_id == tenant.user_id
    assert entry.context["marked"] == 2
    assert (entry.old_values, entry.new_values) == ({"status": "UNREAD"}, {"status": "READ"})

    assert ok(await api_client.post("/notifications/read-all")) == {
        "marked": 0,
        "remaining_unread": 0,
    }
    assert len(await acknowledgements()) == 1


# -----------------------------------------------------------------------------
# PUT /notifications/preferences
# -----------------------------------------------------------------------------


async def test_preferences_are_upserted_for_the_caller_and_answered_in_full(
    api_client, session, tenant, colleague
):
    first = ok(
        await api_client.put(
            "/notifications/preferences",
            json={
                "preferences": [
                    {
                        "notification_type": ANNOUNCEMENT,
                        "in_app_enabled": True,
                        "email_enabled": True,
                        "push_enabled": False,
                    }
                ]
            },
        )
    )

    assert len(first) == len(NotificationType)
    by_type = {row["notification_type"]: row for row in first}
    assert by_type[ANNOUNCEMENT] == {
        "notification_type": ANNOUNCEMENT,
        "in_app_enabled": True,
        "email_enabled": True,
        "push_enabled": False,
        "whatsapp_enabled": False,
        "sms_enabled": False,
        "is_customised": True,
    }
    assert sum(row["is_customised"] for row in first) == 1

    second = ok(
        await api_client.put(
            "/notifications/preferences",
            json={
                "preferences": [
                    {
                        "notification_type": ANNOUNCEMENT,
                        "in_app_enabled": True,
                        "email_enabled": False,
                    }
                ]
            },
        )
    )
    assert {row["notification_type"]: row for row in second}[ANNOUNCEMENT]["email_enabled"] is False

    stored = (
        await session.execute(
            select(NotificationPreference.user_id, NotificationPreference.email_enabled).where(
                NotificationPreference.company_id == tenant.company_id
            )
        )
    ).all()
    assert [tuple(row) for row in stored] == [(tenant.user_id, False)]
    trail = await audit_rows(session, tenant.company_id, "notification_preference")
    assert [(row.action, row.created_by_id) for row in trail] == [
        (AuditAction.UPDATE, tenant.user_id)
    ] * 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: NotificationService.update_preferences audits in_app/email/whatsapp/sms but "
        "leaves push_enabled out of new_values, so a push opt-out leaves no trace"
    ),
)
async def test_a_push_opt_out_is_on_the_audit_record(api_client, session, tenant):
    ok(
        await api_client.put(
            "/notifications/preferences",
            json={
                "preferences": [
                    {
                        "notification_type": ANNOUNCEMENT,
                        "in_app_enabled": True,
                        "email_enabled": False,
                        "push_enabled": False,
                    }
                ]
            },
        )
    )

    [entry] = await audit_rows(session, tenant.company_id, "notification_preference")
    recorded = entry.new_values[ANNOUNCEMENT]
    assert recorded.get("push_enabled") is False, recorded


@pytest.mark.parametrize(
    ("rows", "field"),
    [
        ([], "preferences"),
        (["ROW", "ROW"], "preferences"),
        (
            [{"notification_type": "NOT_A_TYPE", "in_app_enabled": True, "email_enabled": True}],
            "preferences.0.notification_type",
        ),
        (
            [{"notification_type": ANNOUNCEMENT, "email_enabled": True}],
            "preferences.0.in_app_enabled",
        ),
    ],
    ids=["empty", "repeated-type", "unknown-type", "missing-flag"],
)
async def test_malformed_preferences_are_field_errors_and_store_nothing(
    api_client, session, tenant, rows, field
):
    row = {"notification_type": ANNOUNCEMENT, "in_app_enabled": True, "email_enabled": True}
    body = {"preferences": [row if value == "ROW" else value for value in rows]}

    assert field in field_errors(await api_client.put("/notifications/preferences", json=body))
    count = await session.scalar(
        select(func.count())
        .select_from(NotificationPreference)
        .where(NotificationPreference.company_id == tenant.company_id)
    )
    assert count == 0


# -----------------------------------------------------------------------------
# PUT /notifications/alert-rules
# -----------------------------------------------------------------------------


async def test_an_alert_rule_override_is_stored_audited_and_removed_on_reset(
    api_client, session, tenant
):
    rule = ALERT_RULES[AlertRuleKey.QC_PENDING]
    custom = {
        "key": "QC_PENDING",
        "is_enabled": False,
        "threshold_minutes": 120,
        "remind_every_minutes": 240,
    }

    rules = ok(await api_client.put("/notifications/alert-rules", json={"rules": [custom]}))

    assert len(rules) == len(ALERT_RULES)
    qc = next(row for row in rules if row["key"] == "QC_PENDING")
    assert (qc["is_enabled"], qc["threshold_minutes"], qc["remind_every_minutes"]) == (
        False,
        120,
        240,
    )
    assert qc["is_customized"] is True
    assert sum(row["is_customized"] for row in rules) == 1
    stored = (
        await session.execute(
            select(NotificationAlertRule.rule_key, NotificationAlertRule.threshold_minutes).where(
                NotificationAlertRule.company_id == tenant.company_id
            )
        )
    ).all()
    assert [tuple(row) for row in stored] == [("QC_PENDING", 120)]

    # Re-sending the same values changes nothing and writes no audit row.
    ok(await api_client.put("/notifications/alert-rules", json={"rules": [custom]}))

    defaults = {
        "key": "QC_PENDING",
        "is_enabled": True,
        "threshold_minutes": rule.default_threshold_minutes,
        "remind_every_minutes": rule.default_remind_every_minutes,
    }
    reset = ok(await api_client.put("/notifications/alert-rules", json={"rules": [defaults]}))
    assert next(row for row in reset if row["key"] == "QC_PENDING")["is_customized"] is False
    remaining = await session.scalar(
        select(func.count())
        .select_from(NotificationAlertRule)
        .where(NotificationAlertRule.company_id == tenant.company_id)
    )
    assert remaining == 0

    trail = await audit_rows(session, tenant.company_id, "notification_alert_rule")
    assert [row.action for row in trail] == [AuditAction.CONFIG_CHANGE] * 2
    assert trail[0].new_values["QC_PENDING"]["threshold_minutes"] == 120
    assert trail[1].old_values["QC_PENDING"]["threshold_minutes"] == 120


@pytest.mark.parametrize(
    ("rules", "field"),
    [
        ([], "rules"),
        (["RULE", "RULE"], "rules"),
        (
            [
                {
                    "key": "NOT_A_RULE",
                    "is_enabled": True,
                    "threshold_minutes": 0,
                    "remind_every_minutes": 60,
                }
            ],
            "rules.0.key",
        ),
        (
            [
                {
                    "key": "QC_PENDING",
                    "is_enabled": True,
                    "threshold_minutes": -1,
                    "remind_every_minutes": 60,
                }
            ],
            "rules.0.threshold_minutes",
        ),
        (
            [
                {
                    "key": "QC_PENDING",
                    "is_enabled": True,
                    "threshold_minutes": 0,
                    "remind_every_minutes": 0,
                }
            ],
            "rules.0.remind_every_minutes",
        ),
    ],
    ids=["empty", "repeated-key", "unknown-key", "negative-threshold", "zero-reminder"],
)
async def test_malformed_alert_rules_are_field_errors_and_store_nothing(
    api_client, session, tenant, rules, field
):
    rule = {
        "key": "QC_PENDING",
        "is_enabled": False,
        "threshold_minutes": 30,
        "remind_every_minutes": 60,
    }
    body = {"rules": [rule if value == "RULE" else value for value in rules]}

    assert field in field_errors(await api_client.put("/notifications/alert-rules", json=body))
    count = await session.scalar(
        select(func.count())
        .select_from(NotificationAlertRule)
        .where(NotificationAlertRule.company_id == tenant.company_id)
    )
    assert count == 0


# -----------------------------------------------------------------------------
# PUT /notifications/channel-templates
# -----------------------------------------------------------------------------


async def test_messaging_templates_are_upserted_for_this_company_only(
    api_client, session, tenant, other_company
):
    theirs = NotificationChannelTemplate(
        company_id=other_company.id,
        channel="SMS",
        notification_type=ANNOUNCEMENT,
        template_id="their-dlt",
        sms_text="{#var#}",
        parameters=["title"],
    )
    session.add(theirs)
    await session.flush()
    theirs_id = theirs.id

    written = ok(
        await api_client.put(
            "/notifications/channel-templates",
            json={"templates": [sms_mapping(), whatsapp_mapping()]},
        )
    )
    assert {(row["channel"], row["template_id"], row["template_name"]) for row in written} == {
        ("SMS", "dlt-1007", None),
        ("WHATSAPP", None, "plant_announcement"),
    }

    updated = ok(
        await api_client.put(
            "/notifications/channel-templates",
            json={"templates": [sms_mapping(template_id="dlt-2008", is_active=False)]},
        )
    )
    assert [(row["template_id"], row["is_active"]) for row in updated] == [("dlt-2008", False)]
    listed = ok(await api_client.get("/notifications/channel-templates"))
    assert sorted(row["channel"] for row in listed) == ["SMS", "WHATSAPP"]

    ours = (
        await session.execute(
            select(
                NotificationChannelTemplate.channel, NotificationChannelTemplate.template_id
            ).where(NotificationChannelTemplate.company_id == tenant.company_id)
        )
    ).all()
    assert sorted(tuple(row) for row in ours) == [("SMS", "dlt-2008"), ("WHATSAPP", None)]
    untouched = await session.scalar(
        select(NotificationChannelTemplate.template_id).where(
            NotificationChannelTemplate.id == theirs_id
        )
    )
    assert untouched == "their-dlt"
    trail = await audit_rows(session, tenant.company_id, "notification_channel_template")
    assert [row.action for row in trail] == [AuditAction.CONFIG_CHANGE] * 2


@pytest.mark.parametrize(
    ("templates", "field"),
    [
        ([], "templates"),
        ([whatsapp_mapping(sms_text="Not for WhatsApp")], "templates.0"),
        ([sms_mapping(parameters=["title"])], "templates.0"),
        ([sms_mapping(channel="EMAIL")], "templates.0.channel"),
        ([sms_mapping(), sms_mapping(template_id="dlt-9")], "templates"),
    ],
    ids=["empty", "whatsapp-with-text", "placeholders-mismatch", "unknown-channel", "repeated"],
)
async def test_malformed_messaging_templates_are_field_errors_and_store_nothing(
    api_client, session, tenant, templates, field
):
    assert field in field_errors(
        await api_client.put("/notifications/channel-templates", json={"templates": templates})
    )
    count = await session.scalar(
        select(func.count())
        .select_from(NotificationChannelTemplate)
        .where(NotificationChannelTemplate.company_id == tenant.company_id)
    )
    assert count == 0


# -----------------------------------------------------------------------------
# POST /notifications/channel-templates/test
# -----------------------------------------------------------------------------


async def test_a_template_test_is_sent_to_the_callers_verified_number_and_audited(
    api_client, session, tenant, gateway, reachable
):
    [template] = ok(
        await api_client.put(
            "/notifications/channel-templates", json={"templates": [sms_mapping()]}
        )
    )

    sent = ok(
        await api_client.post(
            "/notifications/channel-templates/test",
            json={"channel": "SMS", "notification_type": ANNOUNCEMENT},
        )
    )

    assert sent == {"sent": True}
    assert gateway.sent == [(PHONE, template["template_id"], ["ATCRAFT test", "123456"])]
    tested = [
        row
        for row in await audit_rows(session, tenant.company_id, "notification_channel_template")
        if row.entity_id is not None
    ]
    assert [(row.new_values["test_send"], row.new_values["channel"]) for row in tested] == [
        (True, "SMS")
    ]


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        ("channel-off", 422, "BUSINESS_RULE_VIOLATION"),
        ("unverified-number", 422, "BUSINESS_RULE_VIOLATION"),
        ("no-template", 404, "NOT_FOUND"),
        ("inactive-template", 404, "NOT_FOUND"),
        ("only-another-companys-template", 404, "NOT_FOUND"),
    ],
)
async def test_a_template_test_is_refused_before_anything_is_sent(
    api_client, session, user, other_company, gateway, reachable, monkeypatch, setup, status, code
):
    if setup == "channel-off":
        monkeypatch.setattr(settings, "sms_enabled", False)
    if setup == "unverified-number":
        user.phone_verified_at = None
        await session.flush()
    if setup in {"channel-off", "unverified-number"}:
        ok(
            await api_client.put(
                "/notifications/channel-templates", json={"templates": [sms_mapping()]}
            )
        )
    if setup == "inactive-template":
        ok(
            await api_client.put(
                "/notifications/channel-templates",
                json={"templates": [sms_mapping(is_active=False)]},
            )
        )
    if setup == "only-another-companys-template":
        session.add(
            NotificationChannelTemplate(
                company_id=other_company.id,
                channel="SMS",
                notification_type=ANNOUNCEMENT,
                template_id="their-dlt",
                sms_text="{#var#}",
                parameters=["title"],
            )
        )
        await session.flush()

    refused(
        await api_client.post(
            "/notifications/channel-templates/test",
            json={"channel": "SMS", "notification_type": ANNOUNCEMENT},
        ),
        status,
        code,
    )
    assert gateway.sent == []


@pytest.mark.parametrize(
    ("outcome", "status", "code"),
    [
        (MessagingOutcome.FAILED, 422, "BUSINESS_RULE_VIOLATION"),
        (MessagingOutcome.RETRY, 503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_a_provider_that_refuses_the_test_is_reported(
    api_client, gateway, reachable, outcome, status, code
):
    gateway.outcome = outcome
    ok(
        await api_client.put(
            "/notifications/channel-templates", json={"templates": [sms_mapping()]}
        )
    )

    refused(
        await api_client.post(
            "/notifications/channel-templates/test",
            json={"channel": "SMS", "notification_type": ANNOUNCEMENT},
        ),
        status,
        code,
    )
    assert len(gateway.sent) == 1


async def test_a_template_test_names_a_known_channel_and_type(api_client, gateway):
    fields = field_errors(
        await api_client.post(
            "/notifications/channel-templates/test",
            json={"channel": "EMAIL", "notification_type": "NOT_A_TYPE"},
        )
    )

    assert {"channel", "notification_type"} <= set(fields)
    assert gateway.sent == []
