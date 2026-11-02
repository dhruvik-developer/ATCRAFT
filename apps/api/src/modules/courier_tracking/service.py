"""Courier account lifecycle, event ingestion, matching and Shiprocket polling."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.cache import cache_get, cache_set, make_key
from src.core.clock import utcnow
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext, system_tenant
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.ids import new_id
from src.core.pagination import CursorParams, PageParams
from src.core.responses import OffsetPagination
from src.core.security import (
    decrypt_courier_credential,
    encrypt_courier_credential,
    encrypt_courier_webhook_secret,
    generate_secret_token,
)
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.courier_tracking.models import (
    MAX_POLL_PER_ACCOUNT,
    CourierAccount,
    CourierAccountStatus,
    CourierProvider,
    CourierStatus,
    CourierTrackingEvent,
)
from src.modules.courier_tracking.provider import (
    CourierProviderError,
    normalize_status,
    parse_shiprocket_tracking,
    shiprocket_login,
    shiprocket_track,
)
from src.modules.courier_tracking.repository import CourierAccountRepository
from src.modules.courier_tracking.schemas import (
    CourierAccountCreate,
    CourierAccountRead,
    CourierAccountUpdate,
    CourierEventFilters,
    CourierEventRead,
    GenericWebhookEvent,
    ShiprocketCredentials,
)
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.models import Dispatch, DispatchStatus, TrackingEventSource
from src.modules.dispatch.schemas import DeliveryConfirmation, TrackingEventIn
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify_role
from src.modules.procurement import inbound_service
from src.modules.procurement.inbound_models import PurchaseOrderShipment
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.vendors.models import Transporter, TransporterType

COURIER_ACCOUNT_LIFECYCLE: StateMachine[CourierAccountStatus] = StateMachine(
    entity="Courier account",
    initial=CourierAccountStatus.ACTIVE,
    transitions=(
        Transition(CourierAccountStatus.ACTIVE, CourierAccountStatus.INACTIVE, label="Deactivate"),
        Transition(CourierAccountStatus.INACTIVE, CourierAccountStatus.ACTIVE, label="Activate"),
    ),
)
COURIER_ANOMALY_ROLES = ("DISPATCH_MANAGER", "PLANT_MANAGER", "ADMIN")
COURIER_POLL_CACHE_SECONDS = 20 * 60 * 60
COURIER_STATUS_TO_DISPATCH: dict[CourierStatus, DispatchStatus] = {
    CourierStatus.PICKED_UP: DispatchStatus.IN_TRANSIT,
    CourierStatus.IN_TRANSIT: DispatchStatus.IN_TRANSIT,
}


def account_read(account: CourierAccount) -> CourierAccountRead:
    return CourierAccountRead(
        id=account.id,
        code=account.code,
        provider=account.provider,
        display_name=account.display_name,
        is_active=account.is_active,
        auto_confirm_delivery=account.auto_confirm_delivery,
        has_credentials=account.credentials_ciphertext is not None,
        has_webhook=account.webhook_token_hash is not None,
        last_poll_at=account.last_poll_at,
        last_error=account.last_error,
    )


def _credentials_json(credentials: ShiprocketCredentials) -> str:
    return json.dumps(
        {
            "email": credentials.email,
            "password": credentials.password.get_secret_value(),
        },
        separators=(",", ":"),
    )


def _read_credentials(account: CourierAccount) -> tuple[str, str]:
    if not account.credentials_ciphertext:
        raise BusinessRuleError("Shiprocket credentials are not configured.")
    try:
        value = json.loads(decrypt_courier_credential(account.credentials_ciphertext))
        email, password = value["email"], value["password"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CourierProviderError("Stored courier credentials are invalid.") from exc
    if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
        raise CourierProviderError("Stored courier credentials are invalid.")
    return email, password


async def list_accounts(
    session: AsyncSession, tenant: TenantContext, params: PageParams
) -> tuple[list[CourierAccountRead], OffsetPagination]:
    accounts, pagination = await CourierAccountRepository(session, tenant).list_accounts(params)
    return [account_read(account) for account in accounts], pagination


async def create_account(
    session: AsyncSession, tenant: TenantContext, payload: CourierAccountCreate
) -> CourierAccountRead:
    repo = CourierAccountRepository(session, tenant)
    async with transaction(session):
        account = CourierAccount(
            id=new_id(),
            company_id=tenant.company_id,
            code=payload.code.strip(),
            provider=payload.provider,
            display_name=payload.display_name.strip(),
            credentials_ciphertext=(
                encrypt_courier_credential(_credentials_json(payload.credentials))
                if payload.credentials is not None
                else None
            ),
            auto_confirm_delivery=payload.auto_confirm_delivery,
            is_active=payload.is_active,
        )
        repo.add(account)
        if payload.transporter_ids:
            await _replace_transporter_links(session, tenant, account, payload.transporter_ids)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type="courier_account",
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Created courier account {account.display_name}",
            user_id=tenant.user_id,
            new_values={
                "provider": account.provider,
                "display_name": account.display_name,
                "is_active": account.is_active,
                "transporter_ids": [str(value) for value in payload.transporter_ids],
            },
        )
    return account_read(account)


async def update_account(
    session: AsyncSession,
    tenant: TenantContext,
    account_id: uuid.UUID,
    payload: CourierAccountUpdate,
) -> CourierAccountRead:
    repo = CourierAccountRepository(session, tenant)
    async with transaction(session):
        account = await repo.get_account(account_id, lock=True)
        if account is None:
            raise NotFoundError("Courier account not found.")
        old_values = {
            "display_name": account.display_name,
            "is_active": account.is_active,
            "auto_confirm_delivery": account.auto_confirm_delivery,
        }
        updates = payload.model_dump(exclude_unset=True)
        if "display_name" in updates and payload.display_name is not None:
            account.display_name = payload.display_name.strip()
        if payload.credentials is not None:
            if account.provider is not CourierProvider.SHIPROCKET:
                raise ValidationError("API credentials are only supported for Shiprocket.")
            account.credentials_ciphertext = encrypt_courier_credential(
                _credentials_json(payload.credentials)
            )
        if payload.auto_confirm_delivery is not None:
            account.auto_confirm_delivery = payload.auto_confirm_delivery
        if payload.is_active is not None and payload.is_active != account.is_active:
            current = (
                CourierAccountStatus.ACTIVE if account.is_active else CourierAccountStatus.INACTIVE
            )
            target = (
                CourierAccountStatus.ACTIVE if payload.is_active else CourierAccountStatus.INACTIVE
            )
            COURIER_ACCOUNT_LIFECYCLE.assert_transition(current, target)
            account.is_active = payload.is_active
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type="courier_account",
                entity_id=account.id,
                entity_code=account.code,
                from_status=current.value,
                to_status=target.value,
                user_id=tenant.user_id,
                reason="Courier account activation changed",
            )
        if payload.transporter_ids is not None:
            await _replace_transporter_links(session, tenant, account, payload.transporter_ids)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="courier_account",
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Updated courier account {account.display_name}",
            user_id=tenant.user_id,
            old_values=old_values,
            new_values={
                "display_name": account.display_name,
                "is_active": account.is_active,
                "auto_confirm_delivery": account.auto_confirm_delivery,
                "credentials_replaced": payload.credentials is not None,
                "transporter_ids": (
                    [str(value) for value in payload.transporter_ids]
                    if payload.transporter_ids is not None
                    else None
                ),
            },
        )
    return account_read(account)


async def _replace_transporter_links(
    session: AsyncSession,
    tenant: TenantContext,
    account: CourierAccount,
    transporter_ids: Sequence[uuid.UUID],
) -> None:
    ids = list(dict.fromkeys(transporter_ids))
    if len(ids) != len(transporter_ids):
        raise ValidationError("A transporter may appear only once.")
    rows = (
        list(
            (
                await session.execute(
                    select(Transporter).where(
                        Transporter.company_id == tenant.company_id,
                        Transporter.id.in_(ids),
                        Transporter.deleted_at.is_(None),
                        Transporter.is_active.is_(True),
                        Transporter.transporter_type == TransporterType.COURIER,
                    )
                )
            )
            .scalars()
            .all()
        )
        if ids
        else []
    )
    if len(rows) != len(ids):
        raise ValidationError("Every linked transporter must be an active courier in this company.")
    current = list(
        (
            await session.execute(
                select(Transporter).where(
                    Transporter.company_id == tenant.company_id,
                    Transporter.courier_account_id == account.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in current:
        row.courier_account_id = None
    for row in rows:
        row.courier_account_id = account.id


async def rotate_webhook(
    session: AsyncSession,
    tenant: TenantContext,
    account_id: uuid.UUID,
) -> tuple[CourierAccountRead, str, str]:
    repo = CourierAccountRepository(session, tenant)
    token, token_hash = generate_secret_token()
    secret = secrets.token_urlsafe(32)
    async with transaction(session):
        account = await repo.get_account(account_id, lock=True)
        if account is None:
            raise NotFoundError("Courier account not found.")
        account.webhook_token_hash = token_hash
        account.webhook_secret_ciphertext = encrypt_courier_webhook_secret(secret)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type="courier_account",
            entity_id=account.id,
            entity_code=account.code,
            summary=f"Rotated webhook credentials for {account.display_name}",
            user_id=tenant.user_id,
            context={"webhook_secret_rotated": True},
        )
    root = str(settings.public_web_url).rstrip("/")
    url = f"{root}{settings.api_v1_prefix.rstrip('/')}/integrations/courier/webhook/{token}"
    return account_read(account), url, secret


async def test_connection(
    session: AsyncSession, tenant: TenantContext, account_id: uuid.UUID
) -> CourierProvider:
    account = await CourierAccountRepository(session, tenant).get_account(account_id)
    if account is None:
        raise NotFoundError("Courier account not found.")
    if account.provider is CourierProvider.GENERIC_WEBHOOK:
        if account.webhook_token_hash is None or account.webhook_secret_ciphertext is None:
            raise BusinessRuleError("Rotate webhook credentials before testing this account.")
        return account.provider
    email, password = _read_credentials(account)
    await shiprocket_login(email, password)
    return account.provider


async def account_for_webhook(session: AsyncSession, token_hash: str) -> CourierAccount | None:
    stmt = select(CourierAccount).where(
        CourierAccount.webhook_token_hash == token_hash,
        CourierAccount.is_active.is_(True),
        CourierAccount.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_account(
    session: AsyncSession, tenant: TenantContext, account_id: uuid.UUID
) -> CourierAccount:
    account = await CourierAccountRepository(session, tenant).get_account(account_id)
    if account is None:
        raise NotFoundError("Courier account not found.")
    return account


async def ingest_generic_webhook(
    session: AsyncSession,
    account: CourierAccount,
    payload: Sequence[GenericWebhookEvent],
) -> int:
    tenant = system_tenant(account.company_id)
    events = [
        {
            "tracking_number": event.tracking_number.strip(),
            "raw_status": event.status,
            "event_time": event.event_time.astimezone(UTC),
            "location": event.location,
            "description": event.description,
            "provider_event_id": event.event_id,
            "raw_payload": event.model_dump(mode="json"),
        }
        for event in payload
    ]
    return await ingest_events(session, tenant, account, events)


async def ingest_shiprocket_payload(
    session: AsyncSession,
    tenant: TenantContext,
    account: CourierAccount,
    payload: dict[str, Any],
    *,
    tracking_number: str | None = None,
) -> int:
    return await ingest_events(
        session,
        tenant,
        account,
        parse_shiprocket_tracking(payload, fallback_tracking_number=tracking_number),
    )


async def ingest_shiprocket_webhook(
    session: AsyncSession, account: CourierAccount, payload: dict[str, Any]
) -> int:
    tracking_number = (
        payload.get("awb") or payload.get("awb_code") or payload.get("tracking_number")
    )
    raw_status = (
        payload.get("current_status") or payload.get("shipment_status") or payload.get("status")
    )
    if not isinstance(tracking_number, str) or not tracking_number.strip():
        raise ValidationError("Shiprocket webhook has no tracking number.")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise ValidationError("Shiprocket webhook has no status.")
    event_time = payload.get("event_time") or payload.get("current_timestamp")
    if isinstance(event_time, str):
        try:
            parsed_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("Shiprocket webhook event time is invalid.") from exc
    elif isinstance(event_time, datetime):
        parsed_time = event_time
    else:
        parsed_time = utcnow()
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=UTC)
    event = {
        "tracking_number": tracking_number.strip(),
        "raw_status": raw_status,
        "event_time": parsed_time.astimezone(UTC),
        "location": payload.get("location") or payload.get("current_location"),
        "description": payload.get("description") or payload.get("current_status"),
        "provider_event_id": payload.get("event_id") or payload.get("id"),
        "raw_payload": payload,
    }
    return await ingest_events(session, system_tenant(account.company_id), account, [event])


async def ingest_events(
    session: AsyncSession,
    tenant: TenantContext,
    account: CourierAccount,
    provider_events: Sequence[dict[str, Any]],
) -> int:
    if not provider_events:
        return 0
    repo = CourierAccountRepository(session, tenant)
    async with transaction(session):
        locked = await repo.get_account(account.id, lock=True)
        if locked is None or not locked.is_active:
            raise NotFoundError("Courier account is not active.")
        tracking_numbers = tuple(
            dict.fromkeys(str(event["tracking_number"]).strip() for event in provider_events)
        )
        if any(not value or len(value) > 120 for value in tracking_numbers):
            raise ValidationError("Courier tracking number is missing or too long.")

        normalized: list[dict[str, Any]] = []
        for event in provider_events:
            moment = event.get("event_time")
            if not isinstance(moment, datetime) or moment.tzinfo is None:
                raise ValidationError("Courier event time must include a timezone.")
            raw_status = " ".join(str(event.get("raw_status", "")).split())[:120]
            if not raw_status:
                raise ValidationError("Courier status is required.")
            normalized.append(
                {
                    **event,
                    "tracking_number": str(event["tracking_number"]).strip(),
                    "raw_status": raw_status,
                    "normalized_status": normalize_status(raw_status),
                    "event_time": moment.astimezone(UTC),
                    "location": _bounded_text(event.get("location"), 255),
                    "description": _bounded_text(event.get("description"), 500),
                }
            )

        dispatches = await repo.matching_dispatches(tracking_numbers)
        shipments = await repo.matching_shipments(tracking_numbers)
        dispatch_by_number: dict[str, list[Dispatch]] = {}
        for dispatch in dispatches:
            if dispatch.tracking_id:
                dispatch_by_number.setdefault(dispatch.tracking_id, []).append(dispatch)
            if dispatch.lr_number:
                dispatch_by_number.setdefault(dispatch.lr_number, []).append(dispatch)
        shipment_by_number: dict[str, list[tuple[PurchaseOrderShipment, uuid.UUID]]] = {}
        for shipment, order_id in shipments:
            if shipment.tracking_reference:
                shipment_by_number.setdefault(shipment.tracking_reference, []).append(
                    (shipment, order_id)
                )
            if shipment.lr_number:
                shipment_by_number.setdefault(shipment.lr_number, []).append((shipment, order_id))

        identities = [
            (
                event["tracking_number"],
                event["normalized_status"].value,
                event["event_time"],
                event.get("provider_event_id"),
            )
            for event in normalized
        ]
        existing = await repo.existing_events(locked.id, tenant.company_id, identities)
        existing_natural = {
            (
                row.tracking_number,
                str(row.normalized_status),
                row.event_time,
            )
            for row in existing
        }
        existing_provider_ids = {
            row.provider_event_id for row in existing if row.provider_event_id is not None
        }
        pending_effects: list[
            tuple[
                dict[str, Any],
                CourierTrackingEvent,
                Dispatch | None,
                PurchaseOrderShipment | None,
                uuid.UUID | None,
            ]
        ] = []
        for event in normalized:
            provider_id = event.get("provider_event_id")
            natural_key = (
                event["tracking_number"],
                event["normalized_status"].value,
                event["event_time"],
            )
            if natural_key in existing_natural or (
                provider_id is not None and provider_id in existing_provider_ids
            ):
                continue
            dispatch_matches = {
                row.id: row for row in dispatch_by_number.get(event["tracking_number"], [])
            }
            shipment_matches = {
                row.id: (row, order_id)
                for row, order_id in shipment_by_number.get(event["tracking_number"], [])
            }
            if len(dispatch_matches) > 1 or len(shipment_matches) > 1:
                raise BusinessRuleError(
                    "Courier tracking reference matches more than one consignment.",
                    details={"tracking_number": event["tracking_number"]},
                )
            matched_dispatch = next(iter(dispatch_matches.values()), None)
            shipment_pair = next(iter(shipment_matches.values()), None)
            matched_shipment = shipment_pair[0] if shipment_pair is not None else None
            shipment_order_id = shipment_pair[1] if shipment_pair is not None else None
            row = CourierTrackingEvent(
                id=new_id(),
                company_id=tenant.company_id,
                courier_account_id=locked.id,
                tracking_number=event["tracking_number"],
                normalized_status=event["normalized_status"],
                raw_status=event["raw_status"],
                event_time=event["event_time"],
                location=event["location"],
                description=event["description"],
                provider_event_id=provider_id,
                raw_payload=event.get("raw_payload", {}),
                dispatch_id=matched_dispatch.id if matched_dispatch is not None else None,
                shipment_id=matched_shipment.id if matched_shipment is not None else None,
            )
            session.add(row)
            pending_effects.append(
                (event, row, matched_dispatch, matched_shipment, shipment_order_id)
            )

            existing_natural.add(natural_key)
            if provider_id is not None:
                existing_provider_ids.add(provider_id)

        if not pending_effects:
            return 0

        await session.flush()
        for event, _, matched_dispatch, matched_shipment, shipment_order_id in pending_effects:
            if matched_dispatch is not None:
                await _apply_dispatch_event(session, tenant, locked, matched_dispatch, event)
            if (
                matched_shipment is not None
                and shipment_order_id is not None
                and event["normalized_status"] is CourierStatus.DELIVERED
                and matched_shipment.delivered_at is None
            ):
                await _apply_inbound_delivery(
                    session, tenant, matched_shipment, shipment_order_id, event["event_time"]
                )
            if event["normalized_status"] in {CourierStatus.RETURNED, CourierStatus.EXCEPTION}:
                await _notify_anomaly(session, tenant, locked, event, matched_dispatch)
    return len(pending_effects)


async def _apply_dispatch_event(
    session: AsyncSession,
    tenant: TenantContext,
    account: CourierAccount,
    dispatch: Dispatch,
    event: dict[str, Any],
) -> None:
    current = DispatchStatus(dispatch.status)
    status = event["normalized_status"]
    event_time = event["event_time"]
    location = event["location"]
    description = event["description"] or f"Courier status: {event['raw_status']}"

    if (
        status is CourierStatus.DELIVERED
        and account.auto_confirm_delivery
        and current in {DispatchStatus.DISPATCHED, DispatchStatus.IN_TRANSIT}
    ):
        # ASSUMPTION (§D3-n): an authenticated courier delivery scan is
        # sufficient evidence to confirm arrival when the account switch is on.
        await dispatch_service.confirm_delivery(
            session,
            tenant,
            dispatch.id,
            DeliveryConfirmation(
                actual_delivery_date=max(event_time.date(), dispatch.dispatch_date),
                delay_reason=f"Delivered per {account.display_name} API",
                delivery_note=description,
                location=location,
                event_time=event_time,
                source=TrackingEventSource.COURIER_API,
            ),
        )
        return
    target = COURIER_STATUS_TO_DISPATCH.get(status, current)
    if target is DispatchStatus.IN_TRANSIT and current is not DispatchStatus.DISPATCHED:
        target = current
    if current in {
        DispatchStatus.DELIVERED,
        DispatchStatus.QC_PENDING,
        DispatchStatus.RECEIVED,
        DispatchStatus.QC_COMPLETED,
        DispatchStatus.GRN_COMPLETED,
        DispatchStatus.CANCELLED,
    }:
        target = current
    await dispatch_service.add_tracking_event(
        session,
        tenant,
        dispatch.id,
        TrackingEventIn(
            event_status=target,
            event_time=event_time,
            location=location,
            remark=f"{status.value}: {description}"[:500],
            source=TrackingEventSource.COURIER_API,
        ),
    )


async def _apply_inbound_delivery(
    session: AsyncSession,
    tenant: TenantContext,
    shipment: PurchaseOrderShipment,
    order_id: uuid.UUID,
    delivered_at: datetime,
) -> None:
    order = (
        await session.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.company_id == tenant.company_id,
                PurchaseOrder.id == order_id,
            )
        )
    ).scalar_one_or_none()
    if order is None or order.status in {
        PurchaseOrderStatus.CLOSED,
        PurchaseOrderStatus.CANCELLED,
    }:
        return
    await inbound_service.mark_delivered(
        session, tenant, order_id, shipment.id, delivered_at=delivered_at
    )


async def _notify_anomaly(
    session: AsyncSession,
    tenant: TenantContext,
    account: CourierAccount,
    event: dict[str, Any],
    dispatch: Dispatch | None,
) -> None:
    await notify_role(
        session,
        company_id=tenant.company_id,
        role_codes=COURIER_ANOMALY_ROLES,
        notification_type=NotificationType.COURIER_TRACKING_UPDATED,
        title="Courier tracking needs attention",
        body=(
            f"{account.display_name} reported {event['normalized_status'].value} for "
            f"{event['tracking_number']}."
        ),
        severity=NotificationSeverity.WARNING,
        entity_type="dispatch" if dispatch is not None else "courier_tracking_event",
        entity_id=dispatch.id if dispatch is not None else None,
        action_url=f"/dispatch/{dispatch.id}" if dispatch is not None else "/dispatch/tracking",
        group_key=f"courier-anomaly:{account.id}:{event['tracking_number']}",
    )


async def refresh_tracking(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    dispatch_id: uuid.UUID | None = None,
    shipment_id: uuid.UUID | None = None,
) -> int:
    if (dispatch_id is None) == (shipment_id is None):
        raise ValidationError("Provide exactly one consignment to refresh.")
    if not settings.courier_tracking_enabled:
        raise BusinessRuleError("Courier tracking is disabled for this deployment.")
    repo = CourierAccountRepository(session, tenant)
    if dispatch_id is not None:
        dispatch = (
            await session.execute(
                select(Dispatch).where(
                    Dispatch.company_id == tenant.company_id, Dispatch.id == dispatch_id
                )
            )
        ).scalar_one_or_none()
        if dispatch is None or not dispatch.tracking_id:
            raise NotFoundError("Tracked dispatch not found.")
        tracking_number = dispatch.tracking_id
        transporter_id = dispatch.transporter_id
    else:
        shipment = (
            await session.execute(
                select(PurchaseOrderShipment).where(
                    PurchaseOrderShipment.company_id == tenant.company_id,
                    PurchaseOrderShipment.id == shipment_id,
                )
            )
        ).scalar_one_or_none()
        if shipment is None or not shipment.tracking_reference:
            raise NotFoundError("Tracked inbound shipment not found.")
        tracking_number = shipment.tracking_reference
        transporter_id = shipment.transporter_id

    account = await _account_for_transporter_or_first(repo, tenant, transporter_id)
    if account.provider is not CourierProvider.SHIPROCKET:
        raise BusinessRuleError("Manual refresh is supported for Shiprocket accounts only.")
    credentials = _read_credentials(account)
    token = await _shiprocket_token(account, tenant.company_id, credentials)
    payload = await shiprocket_track(tracking_number, token)
    return await ingest_shiprocket_payload(
        session, tenant, account, payload, tracking_number=tracking_number
    )


async def _account_for_transporter_or_first(
    repo: CourierAccountRepository, tenant: TenantContext, transporter_id: uuid.UUID | None
) -> CourierAccount:
    stmt = select(CourierAccount).where(
        CourierAccount.company_id == tenant.company_id,
        CourierAccount.is_active.is_(True),
        CourierAccount.deleted_at.is_(None),
        CourierAccount.provider == CourierProvider.SHIPROCKET,
    )
    if transporter_id is not None:
        mapped = stmt.join(
            Transporter,
            and_(
                Transporter.courier_account_id == CourierAccount.id,
                Transporter.id == transporter_id,
                Transporter.company_id == tenant.company_id,
            ),
        )
        account = (await repo.session.execute(mapped.limit(1))).scalar_one_or_none()
        if account is not None:
            return account
    account = (
        await repo.session.execute(
            stmt.order_by(CourierAccount.created_at, CourierAccount.id).limit(1)
        )
    ).scalar_one_or_none()
    if account is None:
        raise BusinessRuleError("No active Shiprocket account is configured for this company.")
    return account


async def _shiprocket_token(
    account: CourierAccount, company_id: uuid.UUID, credentials: tuple[str, str]
) -> str:
    cache_key = make_key("courier_shiprocket_token", company_id, account.id)
    cached = await cache_get(cache_key)
    if isinstance(cached, str) and cached:
        return cached
    token = await shiprocket_login(*credentials)
    await cache_set(cache_key, token, ttl=COURIER_POLL_CACHE_SECONDS)
    return token


async def poll_account(
    session: AsyncSession, tenant: TenantContext, account: CourierAccount
) -> tuple[int, str | None]:
    if account.provider is not CourierProvider.SHIPROCKET or not account.is_active:
        return 0, None
    if not settings.courier_tracking_enabled:
        return 0, None
    try:
        credentials = _read_credentials(account)
        token = await _shiprocket_token(account, tenant.company_id, credentials)
        repo = CourierAccountRepository(session, tenant)
        linked_ids = await repo.linked_transporter_ids(account.id)
        dispatches = await repo.in_flight_dispatches(
            limit=MAX_POLL_PER_ACCOUNT, linked_transporter_ids=linked_ids
        )
        shipments = await repo.in_flight_shipments(
            limit=MAX_POLL_PER_ACCOUNT, linked_transporter_ids=linked_ids
        )
        targets = [
            (dispatch.tracking_id, dispatch.created_at, dispatch.id)
            for dispatch in dispatches
            if dispatch.tracking_id
        ] + [
            (shipment.tracking_reference, shipment.created_at, shipment.id)
            for shipment in shipments
            if shipment.tracking_reference
        ]
        targets.sort(key=lambda row: (row[1], row[2]))
        count = 0
        for tracking_number, _, _ in targets[:MAX_POLL_PER_ACCOUNT]:
            response = await shiprocket_track(tracking_number, token)
            count += await ingest_shiprocket_payload(
                session, tenant, account, response, tracking_number=tracking_number
            )
        async with transaction(session):
            locked = await repo.get_account(account.id, lock=True)
            if locked is not None:
                locked.last_poll_at = utcnow()
                locked.last_error = None
        return count, None
    except CourierProviderError:
        message = "Courier provider request failed."
        async with transaction(session):
            locked = await CourierAccountRepository(session, tenant).get_account(
                account.id, lock=True
            )
            if locked is not None:
                locked.last_poll_at = utcnow()
                locked.last_error = message
        return 0, message


async def record_poll_failure(
    session: AsyncSession, tenant: TenantContext, account_id: uuid.UUID
) -> None:
    async with transaction(session):
        account = await CourierAccountRepository(session, tenant).get_account(account_id, lock=True)
        if account is not None:
            account.last_poll_at = utcnow()
            account.last_error = "Courier tracking poll failed."


async def list_events(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    params: CursorParams,
    filters: CourierEventFilters,
) -> tuple[list[CourierEventRead], Any]:
    rows, page = await CourierAccountRepository(session, tenant).events_page(
        params, filters=filters
    )
    return [CourierEventRead.model_validate(row) for row in rows], page


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized[:limit] or None


__all__ = [
    "COURIER_ACCOUNT_LIFECYCLE",
    "account_for_webhook",
    "account_read",
    "create_account",
    "get_account",
    "ingest_events",
    "ingest_generic_webhook",
    "ingest_shiprocket_payload",
    "ingest_shiprocket_webhook",
    "list_accounts",
    "list_events",
    "poll_account",
    "record_poll_failure",
    "refresh_tracking",
    "rotate_webhook",
    "test_connection",
    "update_account",
]
