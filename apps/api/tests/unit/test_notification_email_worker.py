"""Worker delivery rules without a database or SMTP server."""

from __future__ import annotations

import smtplib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from src.core.clock import utcnow
from src.core.config import EmailProvider, settings
from src.core.mail import OutboundEmail
from src.core.security import encrypt_password_reset_delivery_token, hash_secret_token
from src.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationSeverity,
    NotificationType,
)
from src.modules.users.models import PasswordResetToken, User
from src.workers.tasks import notifications as worker

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, row: tuple[object, object] | None) -> None:
        self.row = row

    def one_or_none(self) -> tuple[object, object] | None:
        return self.row


class _Session:
    def __init__(self, row: tuple[object, object] | None) -> None:
        self.row = row

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.row)


def _patch_session(monkeypatch: pytest.MonkeyPatch, row: tuple[object, object] | None) -> None:
    @asynccontextmanager
    async def scope() -> AsyncIterator[_Session]:
        yield _Session(row)

    monkeypatch.setattr(worker, "session_scope", scope)


def _account(company_id: uuid.UUID, user_id: uuid.UUID) -> User:
    return User(
        id=user_id,
        company_id=company_id,
        email="person@example.com",
        full_name="Test Person",
        password_hash="unused-in-this-test",
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture
def enabled_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_provider", EmailProvider.smtp)
    monkeypatch.setattr(settings, "public_web_url", "https://erp.example.com")
    monkeypatch.setattr(settings, "email_from_address", "no-reply@example.com")


async def test_password_reset_worker_sends_and_stamps_without_returning_the_token(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = "one-time-reset-token"
    record = PasswordResetToken(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        token_hash=hash_secret_token(token),
        email_token_ciphertext=encrypt_password_reset_delivery_token(token),
        expires_at=utcnow() + timedelta(minutes=30),
    )
    sent: list[OutboundEmail] = []

    _patch_session(monkeypatch, (record, _account(company_id, user_id)))

    async def capture(mail: OutboundEmail) -> None:
        sent.append(mail)

    monkeypatch.setattr(worker, "deliver_email", capture)

    result = await worker._deliver_password_reset_email(str(company_id), str(record.id))

    assert result == {"reset_token_id": str(record.id), "delivered": True}
    assert token not in repr(result)
    assert record.email_sent_at is not None
    assert record.email_token_ciphertext is None
    assert len(sent) == 1
    assert f"token={token}" in sent[0].text_body


async def test_password_reset_worker_skips_a_retried_delivery(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = "already-sent-token"
    record = PasswordResetToken(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        token_hash=hash_secret_token(token),
        expires_at=utcnow() + timedelta(minutes=30),
        email_sent_at=utcnow(),
    )
    _patch_session(monkeypatch, (record, _account(company_id, user_id)))

    async def must_not_send(_mail: OutboundEmail) -> None:
        raise AssertionError("duplicate delivery attempted")

    monkeypatch.setattr(worker, "deliver_email", must_not_send)

    result = await worker._deliver_password_reset_email(str(company_id), str(record.id))

    assert result["already_delivered"] is True


async def test_notification_worker_uses_its_own_email_delivery_marker(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    notification = Notification(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        severity=NotificationSeverity.INFO,
        title="Planned maintenance",
        body="The service will restart tonight.",
        channels=[NotificationChannel.IN_APP.value, NotificationChannel.EMAIL.value],
        delivered_at=utcnow(),
    )
    original_in_app_delivery = notification.delivered_at
    sent: list[OutboundEmail] = []
    _patch_session(monkeypatch, (notification, _account(company_id, user_id)))

    async def capture(mail: OutboundEmail) -> None:
        sent.append(mail)

    monkeypatch.setattr(worker, "deliver_email", capture)

    result = await worker._deliver_notification_email(
        str(notification.id), str(company_id), str(user_id)
    )

    assert result["delivered"] is True
    assert notification.email_delivered_at is not None
    assert notification.delivered_at == original_in_app_delivery
    assert sent[0].recipient == "person@example.com"


# -----------------------------------------------------------------------------
# The SMTP conversation runs with no transaction open
# -----------------------------------------------------------------------------


class _RecordingSession(_Session):
    def __init__(self, row: tuple[object, object] | None) -> None:
        super().__init__(row)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.row)


def _patch_recording_session(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, object] | None
) -> dict[str, object]:
    """Every scope opened, and whether one is open right now."""
    state: dict[str, object] = {"open": False, "scopes": []}

    @asynccontextmanager
    async def scope() -> AsyncIterator[_RecordingSession]:
        session = _RecordingSession(row)
        state["scopes"].append(session)  # type: ignore[union-attr]
        state["open"] = True
        try:
            yield session
        finally:
            state["open"] = False

    monkeypatch.setattr(worker, "session_scope", scope)
    return state


def _reset_record(company_id: uuid.UUID, user_id: uuid.UUID, token: str) -> PasswordResetToken:
    return PasswordResetToken(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        token_hash=hash_secret_token(token),
        email_token_ciphertext=encrypt_password_reset_delivery_token(token),
        expires_at=utcnow() + timedelta(minutes=30),
    )


def _compiled(statement: object) -> tuple[str, dict[str, object]]:
    from sqlalchemy.dialects import postgresql

    compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(compiled), dict(compiled.params)


async def test_password_reset_link_carries_the_token_in_the_fragment(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query parameter lands in every access log on the path; the fragment
    never leaves the browser."""
    company_id, user_id, token = uuid.uuid4(), uuid.uuid4(), "fragment-only-token"
    record = _reset_record(company_id, user_id, token)
    sent: list[OutboundEmail] = []
    _patch_session(monkeypatch, (record, _account(company_id, user_id)))

    async def capture(mail: OutboundEmail) -> None:
        sent.append(mail)

    monkeypatch.setattr(worker, "deliver_email", capture)

    await worker._deliver_password_reset_email(str(company_id), str(record.id))

    assert f"https://erp.example.com/reset-password#token={token}" in sent[0].text_body
    assert f"?token={token}" not in sent[0].text_body
    assert f"?token={token}" not in sent[0].html_body


async def test_password_reset_is_sent_after_the_claiming_transaction_closed(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id, user_id, token = uuid.uuid4(), uuid.uuid4(), "claimed-token"
    record = _reset_record(company_id, user_id, token)
    state = _patch_recording_session(monkeypatch, (record, _account(company_id, user_id)))
    observed: dict[str, object] = {}

    async def capture(mail: OutboundEmail) -> None:
        observed["transaction_open"] = state["open"]
        observed["claimed"] = record.email_sent_at is not None
        observed["ciphertext_dropped"] = record.email_token_ciphertext is None

    monkeypatch.setattr(worker, "deliver_email", capture)

    result = await worker._deliver_password_reset_email(str(company_id), str(record.id))

    assert result["delivered"] is True
    assert observed == {"transaction_open": False, "claimed": True, "ciphertext_dropped": True}
    assert len(state["scopes"]) == 1  # type: ignore[arg-type]


async def test_password_reset_releases_its_claim_when_delivery_fails(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry and the recovery sweep both key on ``email_sent_at IS NULL``
    plus a ciphertext, so a failed send must put both back."""
    company_id, user_id, token = uuid.uuid4(), uuid.uuid4(), "released-token"
    record = _reset_record(company_id, user_id, token)
    ciphertext = record.email_token_ciphertext
    state = _patch_recording_session(monkeypatch, (record, _account(company_id, user_id)))

    async def refuse(mail: OutboundEmail) -> None:
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(worker, "deliver_email", refuse)

    with pytest.raises(smtplib.SMTPServerDisconnected):
        await worker._deliver_password_reset_email(str(company_id), str(record.id))

    scopes = state["scopes"]
    assert len(scopes) == 2, "one transaction to claim, one to release"  # type: ignore[arg-type]
    (release,) = scopes[1].statements  # type: ignore[index]
    sql, params = _compiled(release)
    assert sql.startswith("UPDATE password_reset_tokens SET")
    assert params["email_sent_at"] is None
    assert params["email_token_ciphertext"] == ciphertext
    assert record.id in params.values()
    assert company_id in params.values()
    assert record.email_sent_at in params.values(), "only our own claim is released"


async def test_notification_email_releases_its_claim_when_delivery_fails(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    notification = Notification(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        severity=NotificationSeverity.INFO,
        title="Planned maintenance",
        body="The service will restart tonight.",
        channels=[NotificationChannel.EMAIL.value],
    )
    state = _patch_recording_session(monkeypatch, (notification, _account(company_id, user_id)))

    async def refuse(mail: OutboundEmail) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(worker, "deliver_email", refuse)

    with pytest.raises(OSError):
        await worker._deliver_notification_email(
            str(notification.id), str(company_id), str(user_id)
        )

    (release,) = state["scopes"][1].statements  # type: ignore[index]
    sql, params = _compiled(release)
    assert sql.startswith("UPDATE notifications SET")
    assert params["email_delivered_at"] is None
    # The in-app stamp was ours too, so it goes back as well.
    assert params["delivered_at"] is None
