"""Browser registration is explicit opt-in; notification preferences remain sparse."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import NotFoundError, ValidationError
from src.core.ids import new_id
from src.core.pagination import CursorParams
from src.core.responses import CursorPagination
from src.core.security import encrypt_push_subscription
from src.core.webpush import vapid_public_key_b64url
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.notifications.models import NotificationType
from src.modules.notifications.push_repository import PushSubscriptionRepository
from src.modules.notifications.push_schemas import (
    PushConfigRead,
    PushSubscribeRequest,
    PushSubscriptionRead,
)
from src.modules.notifications.service import notify


def endpoint_digest(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


class PushService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant
        self.repo = PushSubscriptionRepository(session, tenant)

    @staticmethod
    def config() -> PushConfigRead:
        return PushConfigRead(
            enabled=settings.web_push_enabled,
            vapid_public_key=vapid_public_key_b64url() if settings.web_push_enabled else None,
        )

    async def subscribe(
        self, payload: PushSubscribeRequest, user_agent: str | None
    ) -> PushSubscriptionRead:
        if not settings.web_push_enabled:
            raise ValidationError("Web Push is disabled.")
        moment = utcnow()
        try:
            expires = (
                datetime.fromtimestamp(payload.expiration_time / 1000, UTC)
                if payload.expiration_time is not None
                else None
            )
        except (ValueError, OverflowError, OSError) as exc:
            raise ValidationError("Device expiration is outside the supported range.") from exc
        if expires is not None and expires <= moment:
            raise ValidationError("Device subscription has expired.")
        async with transaction(self.session):
            row = await self.repo.upsert(
                {
                    "id": new_id(),
                    "company_id": self.tenant.company_id,
                    "user_id": self.tenant.user_id,
                    "endpoint_hash": endpoint_digest(payload.endpoint),
                    "endpoint_ciphertext": encrypt_push_subscription(payload.endpoint),
                    "p256dh": payload.keys.p256dh,
                    "auth": payload.keys.auth,
                    "user_agent": user_agent[:512] if user_agent else None,
                    "device_label": payload.device_label,
                    "expires_at": expires,
                    "last_seen_at": moment,
                    "last_success_at": None,
                    "last_failure_at": None,
                    "failure_count": 0,
                    "created_at": moment,
                    "updated_at": moment,
                    "created_by_id": self.tenant.user_id,
                    "updated_by_id": self.tenant.user_id,
                }
            )
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                user_id=self.tenant.user_id,
                action=AuditAction.CREATE,
                entity_type="push_subscription",
                entity_id=row.id,
                summary="Registered browser push device",
            )
        return PushSubscriptionRead.model_validate(row)

    async def list_devices(
        self, params: CursorParams
    ) -> tuple[list[PushSubscriptionRead], CursorPagination]:
        rows, pagination = await self.repo.list_keyset(params, stmt=self.repo.own())
        return [PushSubscriptionRead.model_validate(row) for row in rows], pagination

    async def remove(self, subscription_id: uuid.UUID) -> None:
        async with transaction(self.session):
            if not await self.repo.remove(subscription_id=subscription_id):
                raise NotFoundError("Push subscription", subscription_id)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                user_id=self.tenant.user_id,
                action=AuditAction.DELETE,
                entity_type="push_subscription",
                entity_id=subscription_id,
                summary="Removed browser push device",
            )

    async def unregister(self, endpoint: str) -> bool:
        async with transaction(self.session):
            removed = await self.repo.remove(endpoint_hash=endpoint_digest(endpoint))
            if removed:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    user_id=self.tenant.user_id,
                    action=AuditAction.DELETE,
                    entity_type="push_subscription",
                    summary="Unregistered browser push device",
                )
            return removed

    async def test(self) -> int:
        rows = await notify(
            self.session,
            company_id=self.tenant.company_id,
            user_ids=[self.tenant.user_id],
            notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
            title="Test notification",
            body="Your browser is ready for ATCRAFT notifications.",
            action_url="/notifications",
            actor_id=self.tenant.user_id,
        )
        return sum("PUSH" in row.channels for row in rows)
