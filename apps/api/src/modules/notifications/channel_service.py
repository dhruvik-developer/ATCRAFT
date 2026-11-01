"""Tenant-owned WhatsApp/SMS template administration and test delivery."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError, ErrorCode, NotFoundError
from src.core.ids import new_id
from src.core.messaging import (
    MessagingOutcome,
    WhatsAppCloudProvider,
    assert_allowed_url,
    sms_provider,
)
from src.core.pagination import CursorParams
from src.core.responses import CursorPagination
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.notifications.models import NotificationChannel
from src.modules.notifications.repository import NotificationMessagingRepository
from src.modules.notifications.schemas import (
    NotificationChannelsRead,
    NotificationChannelTemplateRead,
    NotificationChannelTemplatesUpdate,
    NotificationChannelTestRequest,
    NotificationDeliveryRead,
)


class NotificationChannelService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = NotificationMessagingRepository(session, tenant)

    async def channel_status(self) -> NotificationChannelsRead:
        return NotificationChannelsRead(
            whatsapp_enabled=settings.whatsapp_enabled,
            sms_enabled=settings.sms_enabled,
        )

    async def list_templates(self) -> list[NotificationChannelTemplateRead]:
        rows = await self.repo.list_templates()
        return [NotificationChannelTemplateRead.model_validate(row) for row in rows]

    async def list_deliveries(
        self, params: CursorParams
    ) -> tuple[list[NotificationDeliveryRead], CursorPagination]:
        rows, pagination = await self.repo.list_deliveries(params)
        return [NotificationDeliveryRead.model_validate(row) for row in rows], pagination

    async def update_templates(
        self, payload: NotificationChannelTemplatesUpdate
    ) -> list[NotificationChannelTemplateRead]:
        moment = utcnow()
        rows: list[dict[str, Any]] = []
        for item in payload.templates:
            rows.append(
                {
                    "id": new_id(),
                    "company_id": self.tenant.company_id,
                    "channel": item.channel,
                    "notification_type": item.notification_type.value,
                    "template_name": item.template_name,
                    "template_id": item.template_id,
                    "language": item.language,
                    "parameters": list(item.parameters),
                    "sms_text": item.sms_text,
                    "is_active": item.is_active,
                    "created_at": moment,
                    "updated_at": moment,
                    "created_by_id": self.tenant.user_id,
                    "updated_by_id": self.tenant.user_id,
                }
            )
        async with transaction(self.session):
            written = await self.repo.upsert_templates(rows)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CONFIG_CHANGE,
                entity_type="notification_channel_template",
                summary=f"Updated {len(written)} messaging template(s)",
                user_id=self.tenant.user_id,
                new_values={
                    "templates": [
                        {
                            "channel": item.channel,
                            "notification_type": item.notification_type.value,
                            "is_active": item.is_active,
                        }
                        for item in payload.templates
                    ]
                },
            )
        return [NotificationChannelTemplateRead.model_validate(row) for row in written]

    async def test_send(self, payload: NotificationChannelTestRequest) -> None:
        channel = payload.channel
        enabled = (
            settings.whatsapp_enabled
            if channel == NotificationChannel.WHATSAPP.value
            else settings.sms_enabled
        )
        if not enabled:
            raise BusinessRuleError(f"{channel} notifications are not enabled.")

        profile = await self.repo.own_messaging_profile()
        if profile is None:
            raise NotFoundError("Account", self.tenant.user_id)
        phone, verified_at, whatsapp_opt_in_at, sms_opt_in_at = profile
        consent_at = (
            whatsapp_opt_in_at if channel == NotificationChannel.WHATSAPP.value else sms_opt_in_at
        )
        if phone is None or verified_at is None or consent_at is None:
            raise BusinessRuleError(
                "A verified mobile number and current consent are required for a test message."
            )

        template = await self.repo.template(channel, payload.notification_type)
        if template is None or not template.is_active:
            raise NotFoundError("Active messaging template", payload.notification_type.value)

        sample_values = {
            "title": "ATCRAFT test",
            "body": "Messaging template test",
            "code": "123456",
            "link": str(settings.public_web_url),
            "amount": "0.00",
            "date": utcnow().date().isoformat(),
        }
        parameters = [sample_values[name] for name in template.parameters]

        async with transaction(self.session):
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CONFIG_CHANGE,
                entity_type="notification_channel_template",
                entity_id=template.id,
                summary=f"Sent {channel} messaging template test",
                user_id=self.tenant.user_id,
                new_values={
                    "test_send": True,
                    "channel": channel,
                    "notification_type": payload.notification_type.value,
                },
            )

        try:
            if channel == NotificationChannel.WHATSAPP.value:
                url = (
                    f"{settings.whatsapp_api_base.rstrip('/')}/"
                    f"{settings.whatsapp_api_version}/"
                    f"{settings.whatsapp_phone_number_id}/messages"
                )
                assert_allowed_url(url)
                result = await WhatsAppCloudProvider().send_template(
                    phone, template.template_name or "", template.language, parameters
                )
            else:
                assert_allowed_url(settings.sms_api_url)
                result = await sms_provider().send_template(
                    phone, template.template_id or "", parameters
                )
        except ValueError as exc:
            raise BusinessRuleError("Messaging provider URL is not configured.") from exc

        if result.outcome is MessagingOutcome.RETRY:
            raise AppError(
                "The test message could not be sent temporarily. Try again later.",
                code=ErrorCode.SERVICE_UNAVAILABLE,
                status_code=503,
            )
        if result.outcome is MessagingOutcome.FAILED:
            raise BusinessRuleError("The provider rejected the test message.")


__all__ = ["NotificationChannelService"]
