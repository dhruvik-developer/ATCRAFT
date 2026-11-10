"""Tenant-scoped persistence for courier accounts, events and tracking targets."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_, select

from src.common.repository import BaseRepository
from src.core.pagination import CursorParams, PageParams
from src.core.responses import CursorPagination, OffsetPagination
from src.modules.courier_tracking.models import (
    CourierAccount,
    CourierProvider,
    CourierTrackingEvent,
)
from src.modules.dispatch.models import IN_FLIGHT_STATUSES, Dispatch
from src.modules.procurement.inbound_models import PurchaseOrderShipment
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.vendors.models import Transporter


class CourierAccountRepository(BaseRepository[CourierAccount]):
    model = CourierAccount
    entity_label = "Courier account"

    async def get_account(
        self, account_id: uuid.UUID, *, lock: bool = False
    ) -> CourierAccount | None:
        stmt = self._scoped().where(CourierAccount.id == account_id)
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_accounts(
        self, params: PageParams
    ) -> tuple[list[CourierAccount], OffsetPagination]:
        stmt = self._scoped().order_by(CourierAccount.created_at.desc(), CourierAccount.id.desc())
        return await self.list_page(params, stmt=stmt)

    async def by_token_hash(self, token_hash: str) -> CourierAccount | None:
        stmt = select(CourierAccount).where(
            CourierAccount.webhook_token_hash == token_hash,
            CourierAccount.is_active.is_(True),
            CourierAccount.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_ids(self, company_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(CourierAccount.id).where(
            CourierAccount.company_id == company_id,
            CourierAccount.is_active.is_(True),
            CourierAccount.deleted_at.is_(None),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def shiprocket_account_for(
        self, transporter_id: uuid.UUID | None
    ) -> CourierAccount | None:
        """The active Shiprocket account a manual refresh polls through.

        The one linked to ``transporter_id`` when there is one, otherwise the
        company's oldest active Shiprocket account - ``(created_at, id)``, so two
        created in one transaction still answer the same way every time.
        """
        stmt = select(CourierAccount).where(
            CourierAccount.company_id == self.tenant.company_id,
            CourierAccount.is_active.is_(True),
            CourierAccount.deleted_at.is_(None),
            CourierAccount.provider == CourierProvider.SHIPROCKET,
        )
        if transporter_id is not None:
            linked = stmt.join(
                Transporter,
                and_(
                    Transporter.courier_account_id == CourierAccount.id,
                    Transporter.id == transporter_id,
                    Transporter.company_id == self.tenant.company_id,
                ),
            )
            account = (await self.session.execute(linked.limit(1))).scalar_one_or_none()
            if account is not None:
                return account
        oldest = stmt.order_by(CourierAccount.created_at, CourierAccount.id).limit(1)
        return (await self.session.execute(oldest)).scalar_one_or_none()

    async def linked_transporter_ids(self, account_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(Transporter.id).where(
            Transporter.company_id == self.tenant.company_id,
            Transporter.courier_account_id == account_id,
            Transporter.deleted_at.is_(None),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_dispatch(self, dispatch_id: uuid.UUID) -> Dispatch | None:
        """One of this company's dispatches, tracked or not - the caller decides."""
        stmt = select(Dispatch).where(
            Dispatch.company_id == self.tenant.company_id, Dispatch.id == dispatch_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_shipment(self, shipment_id: uuid.UUID) -> PurchaseOrderShipment | None:
        """One of this company's inbound shipments, tracked or not - the caller decides."""
        stmt = select(PurchaseOrderShipment).where(
            PurchaseOrderShipment.company_id == self.tenant.company_id,
            PurchaseOrderShipment.id == shipment_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def matching_dispatches(self, tracking_numbers: Sequence[str]) -> list[Dispatch]:
        if not tracking_numbers:
            return []
        stmt = select(Dispatch).where(
            Dispatch.company_id == self.tenant.company_id,
            or_(
                Dispatch.tracking_id.in_(tracking_numbers),
                Dispatch.lr_number.in_(tracking_numbers),
            ),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def matching_shipments(
        self, tracking_numbers: Sequence[str]
    ) -> list[tuple[PurchaseOrderShipment, uuid.UUID]]:
        if not tracking_numbers:
            return []
        stmt = (
            select(PurchaseOrderShipment, PurchaseOrder.id)
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderShipment.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderShipment.company_id,
                ),
            )
            .where(
                PurchaseOrderShipment.company_id == self.tenant.company_id,
                or_(
                    PurchaseOrderShipment.tracking_reference.in_(tracking_numbers),
                    PurchaseOrderShipment.lr_number.in_(tracking_numbers),
                ),
            )
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def existing_events(
        self,
        account_id: uuid.UUID,
        company_id: uuid.UUID,
        events: Sequence[tuple[str, str, datetime, str | None]],
    ) -> list[CourierTrackingEvent]:
        if not events:
            return []
        predicates = []
        for tracking, status, event_time, provider_id in events:
            natural_key = and_(
                CourierTrackingEvent.tracking_number == tracking,
                CourierTrackingEvent.normalized_status == status,
                CourierTrackingEvent.event_time == event_time,
            )
            predicates.append(
                or_(
                    natural_key,
                    CourierTrackingEvent.provider_event_id == provider_id,
                )
                if provider_id is not None
                else natural_key
            )
        stmt = select(CourierTrackingEvent).where(
            CourierTrackingEvent.company_id == company_id,
            CourierTrackingEvent.courier_account_id == account_id,
            or_(*predicates),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def events_page(
        self, params: CursorParams, *, filters: Any
    ) -> tuple[list[CourierTrackingEvent], CursorPagination]:
        stmt: Select[Any] = select(CourierTrackingEvent).where(
            CourierTrackingEvent.company_id == self.tenant.company_id
        )
        if filters.tracking_number is not None:
            stmt = stmt.where(CourierTrackingEvent.tracking_number == filters.tracking_number)
        if filters.dispatch_id is not None:
            stmt = stmt.where(CourierTrackingEvent.dispatch_id == filters.dispatch_id)
        if filters.shipment_id is not None:
            stmt = stmt.where(CourierTrackingEvent.shipment_id == filters.shipment_id)
        return await CourierTrackingEventRepository(self.session, self.tenant).list_keyset(
            params,
            stmt=stmt,
        )

    async def in_flight_dispatches(
        self, *, limit: int, linked_transporter_ids: Sequence[uuid.UUID]
    ) -> list[Dispatch]:
        carrier_filter = (
            Dispatch.transporter_id.in_(linked_transporter_ids)
            if linked_transporter_ids
            else Dispatch.id.is_not(None)
        )
        stmt = (
            select(Dispatch)
            .where(
                Dispatch.company_id == self.tenant.company_id,
                Dispatch.status.in_(IN_FLIGHT_STATUSES),
                Dispatch.tracking_id.is_not(None),
                carrier_filter,
            )
            .order_by(Dispatch.created_at, Dispatch.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def in_flight_shipments(
        self, *, limit: int, linked_transporter_ids: Sequence[uuid.UUID]
    ) -> list[PurchaseOrderShipment]:
        carrier_filter = (
            PurchaseOrderShipment.transporter_id.in_(linked_transporter_ids)
            if linked_transporter_ids
            else PurchaseOrderShipment.id.is_not(None)
        )
        stmt = (
            select(PurchaseOrderShipment)
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderShipment.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderShipment.company_id,
                ),
            )
            .where(
                PurchaseOrderShipment.company_id == self.tenant.company_id,
                PurchaseOrderShipment.delivered_at.is_(None),
                PurchaseOrderShipment.tracking_reference.is_not(None),
                carrier_filter,
                PurchaseOrder.status.in_(
                    (
                        PurchaseOrderStatus.ACKNOWLEDGED,
                        PurchaseOrderStatus.PARTIALLY_RECEIVED,
                        PurchaseOrderStatus.RECEIVED,
                        PurchaseOrderStatus.QC_PENDING,
                        PurchaseOrderStatus.QC_COMPLETED,
                    )
                ),
            )
            .order_by(PurchaseOrderShipment.created_at, PurchaseOrderShipment.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class CourierTrackingEventRepository(BaseRepository[CourierTrackingEvent]):
    model = CourierTrackingEvent
