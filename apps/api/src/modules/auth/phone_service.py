"""Self-service phone verification and auditable messaging consent."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import Principal
from src.core.errors import (
    AppError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    RateLimitedError,
)
from src.core.messaging import MessagingOutcome, assert_allowed_url, sms_provider
from src.core.ratelimit import RateLimitPolicy, enforce_policy
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.auth.repository import PhoneVerificationRepository
from src.modules.auth.schemas import (
    MessagingConsentRead,
    MessagingConsentRequest,
    PhoneUpdateRequest,
    PhoneVerificationStarted,
)
from src.modules.notifications.models import NotificationChannel
from src.modules.users.models import UserPhoneVerificationChallenge

PHONE_CODE_DIGITS = 6
# ASSUMPTION: a code lives for ten minutes, can be sent three times per hour
# with a sixty-second resend delay, and allows five guesses. These bounds are
# conservative defaults for SMS cost and account protection.
PHONE_CODE_LIFETIME = timedelta(minutes=10)
PHONE_CODE_RESEND_DELAY = timedelta(seconds=60)
PHONE_CODE_SEND_WINDOW = timedelta(hours=1)
PHONE_CODE_SEND_LIMIT = 3
PHONE_CODE_ATTEMPT_LIMIT = 5


def _code_digest(code: str, *, company_id: uuid.UUID, user_id: uuid.UUID, phone_e164: str) -> str:
    key = settings.credential_encryption_key.get_secret_value().encode("utf-8")
    material = f"{company_id}:{user_id}:{phone_e164}:{code}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _phone_suffix(phone_e164: str | None) -> str | None:
    return phone_e164[-4:] if phone_e164 else None


class PhoneProfileService:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self.session = session
        self.principal = principal
        self.repo = PhoneVerificationRepository(
            session, company_id=principal.company_id, user_id=principal.user_id
        )

    async def update_phone(self, payload: PhoneUpdateRequest) -> None:
        moment = utcnow()
        async with transaction(self.session):
            account = await self.repo.account_for_update()
            if account is None:
                raise NotFoundError("Account", self.principal.user_id)
            old_phone = account.phone_e164
            if old_phone == payload.phone:
                return

            challenge = await self.repo.challenge_for_update()
            if challenge is not None:
                challenge.consumed_at = moment
            account.phone_e164 = payload.phone
            account.phone_verified_at = None
            account.whatsapp_opt_in_at = None
            account.sms_opt_in_at = None
            await record_audit(
                self.session,
                company_id=self.principal.company_id,
                action=AuditAction.CONFIG_CHANGE,
                entity_type="user",
                entity_id=account.id,
                entity_code=account.email[:64],
                summary="Updated messaging phone number",
                user_id=account.id,
                actor_label=account.full_name,
                context={
                    "old_phone_suffix": _phone_suffix(old_phone),
                    "new_phone_suffix": _phone_suffix(payload.phone),
                    "verification_cleared": True,
                    "consent_cleared": True,
                },
            )

    async def request_verification(self, *, ip_address: str | None) -> PhoneVerificationStarted:
        user_key = str(self.principal.user_id)
        await enforce_policy(RateLimitPolicy.PHONE_OTP_SEND, user_key)
        if ip_address is not None:
            await enforce_policy(RateLimitPolicy.PHONE_OTP_SEND_PER_IP, ip_address)
        if not (
            settings.sms_auth_key.get_secret_value()
            and settings.sms_sender_id
            and settings.sms_dlt_entity_id
            and settings.sms_verification_template_id
            and settings.credential_encryption_key.get_secret_value()
        ):
            raise BusinessRuleError("SMS phone verification is not configured.")
        try:
            assert_allowed_url(settings.sms_api_url)
        except ValueError as exc:
            raise BusinessRuleError("SMS phone verification provider is not configured.") from exc

        moment = utcnow()
        code = f"{secrets.randbelow(10**PHONE_CODE_DIGITS):0{PHONE_CODE_DIGITS}d}"
        challenge_id: uuid.UUID | None = None
        phone_e164: str | None = None
        async with transaction(self.session):
            account = await self.repo.account_for_update()
            if account is None:
                raise NotFoundError("Account", self.principal.user_id)
            phone_e164 = account.phone_e164
            if phone_e164 is None:
                raise BusinessRuleError("Add a mobile number before requesting verification.")
            if account.phone_verified_at is not None:
                raise BusinessRuleError("This mobile number is already verified.")

            challenge = await self.repo.challenge_for_update()
            if challenge is not None and challenge.last_sent_at + PHONE_CODE_RESEND_DELAY > moment:
                raise RateLimitedError(
                    retry_after_seconds=max(
                        1,
                        int(
                            (
                                challenge.last_sent_at + PHONE_CODE_RESEND_DELAY - moment
                            ).total_seconds()
                        ),
                    )
                )
            if (
                challenge is not None
                and challenge.send_window_started_at + PHONE_CODE_SEND_WINDOW <= moment
            ):
                challenge.send_window_started_at = moment
                challenge.send_count = 0
            if challenge is not None and challenge.send_count >= PHONE_CODE_SEND_LIMIT:
                raise RateLimitedError(
                    retry_after_seconds=max(
                        1,
                        int(
                            (
                                challenge.send_window_started_at + PHONE_CODE_SEND_WINDOW - moment
                            ).total_seconds()
                        ),
                    )
                )

            digest = _code_digest(
                code,
                company_id=self.principal.company_id,
                user_id=self.principal.user_id,
                phone_e164=phone_e164,
            )
            if challenge is None:
                challenge = self.repo.add_challenge(
                    UserPhoneVerificationChallenge(
                        company_id=self.principal.company_id,
                        user_id=self.principal.user_id,
                        phone_e164=phone_e164,
                        code_digest=digest,
                        expires_at=moment + PHONE_CODE_LIFETIME,
                        last_sent_at=moment,
                        attempt_count=0,
                        send_count=1,
                        send_window_started_at=moment,
                        consumed_at=None,
                        created_by_id=self.principal.user_id,
                        updated_by_id=self.principal.user_id,
                    )
                )
            else:
                challenge.phone_e164 = phone_e164
                challenge.code_digest = digest
                challenge.expires_at = moment + PHONE_CODE_LIFETIME
                challenge.last_sent_at = moment
                challenge.attempt_count = 0
                challenge.send_count += 1
                challenge.consumed_at = None
                challenge.updated_by_id = self.principal.user_id
            await self.session.flush()
            challenge_id = challenge.id
            await record_audit(
                self.session,
                company_id=self.principal.company_id,
                action=AuditAction.CONFIG_CHANGE,
                entity_type="user",
                entity_id=account.id,
                entity_code=account.email[:64],
                summary="Requested messaging phone verification",
                user_id=account.id,
                actor_label=account.full_name,
                context={"phone_suffix": _phone_suffix(phone_e164)},
            )

        if phone_e164 is None or challenge_id is None:
            raise AppError("Phone verification challenge was not created.")
        delivery = await sms_provider().send_template(
            phone_e164,
            settings.sms_verification_template_id,
            [code],
        )
        if delivery.outcome is not MessagingOutcome.DELIVERED:
            async with transaction(self.session):
                current = await self.repo.challenge_for_update()
                if current is not None and current.id == challenge_id:
                    current.consumed_at = utcnow()
            raise AppError(
                "The verification code could not be sent. Please try again later.",
                code=ErrorCode.SERVICE_UNAVAILABLE,
                status_code=503,
            )
        return PhoneVerificationStarted(expires_in_seconds=int(PHONE_CODE_LIFETIME.total_seconds()))

    async def verify_phone(self, code: str) -> None:
        await enforce_policy(RateLimitPolicy.PHONE_OTP_VERIFY, str(self.principal.user_id))
        moment = utcnow()
        failure: str | None = None
        async with transaction(self.session):
            account = await self.repo.account_for_update()
            if account is None:
                raise NotFoundError("Account", self.principal.user_id)
            challenge = await self.repo.challenge_for_update()
            if (
                challenge is None
                or challenge.consumed_at is not None
                or challenge.expires_at <= moment
                or challenge.phone_e164 != account.phone_e164
                or account.phone_e164 is None
            ):
                failure = "Request a new verification code before continuing."
            elif challenge.attempt_count >= PHONE_CODE_ATTEMPT_LIMIT:
                challenge.consumed_at = moment
                failure = "The verification code has expired. Request a new one."
            else:
                candidate = _code_digest(
                    code,
                    company_id=self.principal.company_id,
                    user_id=self.principal.user_id,
                    phone_e164=challenge.phone_e164,
                )
                if not hmac.compare_digest(candidate, challenge.code_digest):
                    challenge.attempt_count += 1
                    if challenge.attempt_count >= PHONE_CODE_ATTEMPT_LIMIT:
                        challenge.consumed_at = moment
                    failure = "The verification code is incorrect or expired."
                else:
                    challenge.consumed_at = moment
                    account.phone_verified_at = moment
                    await record_audit(
                        self.session,
                        company_id=self.principal.company_id,
                        action=AuditAction.CONFIG_CHANGE,
                        entity_type="user",
                        entity_id=account.id,
                        entity_code=account.email[:64],
                        summary="Verified messaging phone number",
                        user_id=account.id,
                        actor_label=account.full_name,
                        context={"phone_suffix": _phone_suffix(account.phone_e164)},
                    )
        if failure is not None:
            raise BusinessRuleError(failure)

    async def set_consent(self, payload: MessagingConsentRequest) -> MessagingConsentRead:
        moment = utcnow()
        async with transaction(self.session):
            account = await self.repo.account_for_update()
            if account is None:
                raise NotFoundError("Account", self.principal.user_id)
            field_name = (
                "whatsapp_opt_in_at"
                if payload.channel == NotificationChannel.WHATSAPP.value
                else "sms_opt_in_at"
            )
            old_value = getattr(account, field_name) is not None
            enabled = (
                settings.whatsapp_enabled
                if payload.channel == NotificationChannel.WHATSAPP.value
                else settings.sms_enabled
            )
            if payload.opt_in and not enabled:
                raise BusinessRuleError(f"{payload.channel} notifications are not enabled.")
            if payload.opt_in and (account.phone_e164 is None or account.phone_verified_at is None):
                raise BusinessRuleError("Verify your mobile number before opting in.")
            setattr(account, field_name, moment if payload.opt_in else None)
            await record_audit(
                self.session,
                company_id=self.principal.company_id,
                action=AuditAction.CONFIG_CHANGE,
                entity_type="user",
                entity_id=account.id,
                entity_code=account.email[:64],
                summary=f"{payload.channel} messaging consent updated",
                user_id=account.id,
                actor_label=account.full_name,
                old_values={f"{payload.channel.lower()}_opt_in": old_value},
                new_values={f"{payload.channel.lower()}_opt_in": payload.opt_in},
            )
        return MessagingConsentRead(channel=payload.channel, opted_in=payload.opt_in)


__all__ = [
    "PHONE_CODE_ATTEMPT_LIMIT",
    "PHONE_CODE_LIFETIME",
    "PHONE_CODE_RESEND_DELAY",
    "PHONE_CODE_SEND_LIMIT",
    "PHONE_CODE_SEND_WINDOW",
    "PhoneProfileService",
]
