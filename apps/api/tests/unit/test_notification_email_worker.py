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


# -----------------------------------------------------------------------------
# A permanent refusal closes the attempt; anything else is retried
# -----------------------------------------------------------------------------


def _refused(code: int) -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({"person@example.com": (code, b"5.1.1 User unknown")})


@pytest.mark.parametrize(
    ("failure", "permanent"),
    [
        (_refused(550), True),
        (_refused(553), True),
        (_refused(450), False),  # greylisting, a busy mailbox: try again later
        (_refused(421), False),
        (
            smtplib.SMTPRecipientsRefused(
                {"a@example.com": (550, b"no"), "b@example.com": (451, b"later")}
            ),
            False,
        ),
        (smtplib.SMTPRecipientsRefused({}), False),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), False),
        (smtplib.SMTPDataError(554, b"rejected"), False),
        (OSError("connection refused"), False),
    ],
)
def test_only_a_5xx_refusal_of_every_recipient_is_permanent(
    failure: BaseException, permanent: bool
) -> None:
    assert worker.permanently_refused(failure) is permanent


def _email_notification(company_id: uuid.UUID, user_id: uuid.UUID) -> Notification:
    return Notification(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        severity=NotificationSeverity.INFO,
        title="Planned maintenance",
        body="The service will restart tonight.",
        channels=[NotificationChannel.EMAIL.value],
    )


async def test_a_refused_notification_email_keeps_its_marker_and_is_not_retried(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The retry and the recovery sweep both look for a NULL marker: keeping it
    is what stops an undeliverable address being re-sent every two minutes."""
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    notification = _email_notification(company_id, user_id)
    state = _patch_recording_session(monkeypatch, (notification, _account(company_id, user_id)))

    async def refuse(mail: OutboundEmail) -> None:
        raise _refused(550)

    monkeypatch.setattr(worker, "deliver_email", refuse)

    result = await worker._deliver_notification_email(
        str(notification.id), str(company_id), str(user_id)
    )

    assert result == {
        "notification_id": str(notification.id),
        "delivered": False,
        "detail": "refused",
    }
    assert notification.email_delivered_at is not None
    (release,) = state["scopes"][1].statements  # type: ignore[index]
    sql, params = _compiled(release)
    assert sql.startswith("UPDATE notifications SET delivered_at=")
    assert "email_delivered_at=" not in sql.split(" WHERE ")[0], "the marker stays"
    assert params["delivered_at"] is None, "it never reached the recipient"
    out = capsys.readouterr().out
    assert "notifications.email_refused" in out
    assert "550" in out
    assert "person@example.com" not in out


async def test_a_refusal_with_no_stamp_to_give_back_writes_nothing(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    notification = _email_notification(company_id, user_id)
    notification.channels = [NotificationChannel.IN_APP.value, NotificationChannel.EMAIL.value]
    notification.delivered_at = utcnow()
    state = _patch_recording_session(monkeypatch, (notification, _account(company_id, user_id)))

    async def refuse(mail: OutboundEmail) -> None:
        raise _refused(550)

    monkeypatch.setattr(worker, "deliver_email", refuse)

    result = await worker._deliver_notification_email(
        str(notification.id), str(company_id), str(user_id)
    )

    assert result["detail"] == "refused"
    assert len(state["scopes"]) == 1  # type: ignore[arg-type]


async def test_a_temporary_refusal_releases_the_claim_for_the_retry(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    notification = _email_notification(company_id, user_id)
    state = _patch_recording_session(monkeypatch, (notification, _account(company_id, user_id)))

    async def greylist(mail: OutboundEmail) -> None:
        raise _refused(450)

    monkeypatch.setattr(worker, "deliver_email", greylist)

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        await worker._deliver_notification_email(
            str(notification.id), str(company_id), str(user_id)
        )

    (release,) = state["scopes"][1].statements  # type: ignore[index]
    _, params = _compiled(release)
    assert params["email_delivered_at"] is None


async def test_a_refused_reset_is_not_mailed_again_by_the_retry_or_the_sweep(
    enabled_mail: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sweep selects an unsent row *with* a delivery copy; a refused one keeps
    the copy purged, and the person asks for a new link."""
    company_id, user_id, token = uuid.uuid4(), uuid.uuid4(), "refused-token"
    record = _reset_record(company_id, user_id, token)
    state = _patch_recording_session(monkeypatch, (record, _account(company_id, user_id)))

    async def refuse(mail: OutboundEmail) -> None:
        raise _refused(550)

    monkeypatch.setattr(worker, "deliver_email", refuse)

    result = await worker._deliver_password_reset_email(str(company_id), str(record.id))

    assert result == {"reset_token_id": str(record.id), "delivered": False, "detail": "refused"}
    (release,) = state["scopes"][1].statements  # type: ignore[index]
    sql, params = _compiled(release)
    assert sql.startswith("UPDATE password_reset_tokens SET")
    assert params["email_sent_at"] is None
    assert params["email_token_ciphertext"] is None
    out = capsys.readouterr().out
    assert "auth.password_reset.email_refused" in out
    assert "person@example.com" not in out
    assert token not in out


# -----------------------------------------------------------------------------
# The recovery sweep: one address the relay will not take stops nobody else
# -----------------------------------------------------------------------------


class _SweepResult:
    def __init__(self, rows: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
        self.rows = rows
        self.rowcount = 0

    def all(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return self.rows


def _patch_sweep(
    monkeypatch: pytest.MonkeyPatch, pending: list[tuple[uuid.UUID, uuid.UUID]]
) -> None:
    """The sweep's own read: an expiry purge, then the rows it will deliver."""

    class _SweepSession:
        async def execute(self, _statement: object) -> _SweepResult:
            return _SweepResult(pending)

    @asynccontextmanager
    async def scope() -> AsyncIterator[_SweepSession]:
        yield _SweepSession()

    monkeypatch.setattr(worker, "session_scope", scope)


async def test_the_reset_sweep_delivers_the_rest_when_one_address_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oldest first, so the failing row used to be first on every tick until its
    link expired - and no later reset was recovered."""
    company_id = uuid.uuid4()
    failing, queued_behind = uuid.uuid4(), uuid.uuid4()
    _patch_sweep(monkeypatch, [(failing, company_id), (queued_behind, company_id)])
    attempted: list[str] = []

    async def deliver(company: str, reset_token_id: str) -> dict[str, object]:
        attempted.append(reset_token_id)
        if reset_token_id == str(failing):
            raise smtplib.SMTPRecipientsRefused({"person@example.com": (452, b"mailbox full")})
        return {"reset_token_id": reset_token_id, "delivered": True}

    monkeypatch.setattr(worker, "_deliver_password_reset_email", deliver)

    result = await worker._dispatch_pending_password_resets()

    assert attempted == [str(failing), str(queued_behind)]
    assert result == {
        "scanned": 2,
        "delivered": 1,
        "failed": 1,
        "expired_credentials_purged": 0,
    }
    out = capsys.readouterr().out
    assert "auth.password_reset.recovery_failed" in out
    assert str(failing) in out
    assert "SMTPRecipientsRefused" in out
    assert "person@example.com" not in out


async def test_the_reset_sweep_still_fails_on_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a transport failure is one row's problem; anything else is the code's."""
    company_id = uuid.uuid4()
    _patch_sweep(monkeypatch, [(uuid.uuid4(), company_id), (uuid.uuid4(), company_id)])

    async def broken(company: str, reset_token_id: str) -> dict[str, object]:
        raise KeyError("token")

    monkeypatch.setattr(worker, "_deliver_password_reset_email", broken)

    with pytest.raises(KeyError):
        await worker._dispatch_pending_password_resets()
