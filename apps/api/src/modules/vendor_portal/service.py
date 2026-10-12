"""Vendor business actions compose the owning modules' service APIs."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.pagination import PageParams, SortParams
from src.core.responses import OffsetPagination, api_path
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.documents.service import DocumentService
from src.modules.finance import service as finance
from src.modules.finance.schemas import (
    PaymentDetail,
    VendorInvoiceDetail,
    VendorInvoiceFilters,
    VendorInvoiceSummary,
)
from src.modules.notifications.models import NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.procurement import service as procurement
from src.modules.procurement.inbound_service import notify_buyer, tracking_read
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.procurement.schemas import PurchaseOrderSummary
from src.modules.reports import service as reports
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.reports.schemas import ReportJobRead
from src.modules.vendor_portal.repository import VendorPortalRepository
from src.modules.vendor_portal.schemas import (
    VendorPortalAccept,
    VendorPortalCategory,
    VendorPortalDashboard,
    VendorPortalDateChange,
    VendorPortalInvoiceCreate,
    VendorPortalMe,
    VendorPortalOrderDetail,
    VendorPortalPaymentSummary,
    VendorPortalReady,
    VendorPortalReceiptRead,
)


class VendorPortalService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        if tenant.vendor_id is None:
            raise AuthorizationError("A vendor account is required.", code=ErrorCode.SCOPE_DENIED)
        self.session, self.tenant = session, tenant
        self.repo = VendorPortalRepository(session, tenant)

    async def me(self, principal: Principal) -> VendorPortalMe:
        vendor = await self.repo.vendor()
        if vendor is None:
            raise NotFoundError("Vendor", self.tenant.vendor_id)
        categories = await self.repo.categories()
        unread, _ = await NotificationRepository(self.session, self.tenant).unread_count(
            self.tenant.user_id
        )
        return VendorPortalMe(
            id=vendor.id,
            code=vendor.code,
            name=vendor.name,
            roles=list(principal.role_codes),
            categories=[
                VendorPortalCategory(id=c.id, code=c.code, name=c.name) for c in categories
            ],
            contact_name=principal.full_name,
            email=principal.email,
            phone=vendor.phone,
            unread_count=unread,
        )

    async def dashboard(self) -> VendorPortalDashboard:
        row = await self.repo.dashboard(today())
        return VendorPortalDashboard(**dict(row._mapping))

    async def order(self, order_id: uuid.UUID) -> VendorPortalOrderDetail:
        order = await PurchaseOrderRepository(self.session, self.tenant).get_or_404(order_id)
        detail = await procurement.get_purchase_order_detail(self.session, self.tenant, order_id)
        row = await self.repo.order_projection(order_id)
        if not detail.delivery_address:
            warehouse = await self.repo.warehouse(order.warehouse_id)
            if warehouse:
                detail.delivery_address = ", ".join(
                    str(getattr(warehouse, key))
                    for key in (
                        "address_line1",
                        "address_line2",
                        "city",
                        "state",
                        "postal_code",
                        "country",
                    )
                    if getattr(warehouse, key)
                )
        labels = await self.repo.line_labels(order_id)
        # A field extension remains compatible with staff line contracts.
        for line in detail.lines:
            label = labels.get(line.id, {})
            line.item_code = label.get("code")
            line.item_name = label.get("name")
            line.item_category_id = label.get("category_id")
            line.uom_code = label.get("uom_code")
        from src.modules.procurement import inbound_service

        shipments, shipments_page = await inbound_service.list_shipments(
            self.session, self.tenant, order_id, PageParams(limit=50)
        )
        documents, documents_page = await inbound_service.list_documents(
            self.session, self.tenant, order_id, PageParams(limit=50)
        )
        receipts, receipts_page = await self.receipts(PageParams(limit=50), order_id)
        return VendorPortalOrderDetail(
            **detail.model_dump(),
            tracking=tracking_read(row, today()),
            timeline=await self.repo.timeline(order_id),
            documents_count=row.documents_count,
            invoice_summary=await self.repo.invoice_summary(order_id),
            shipments=shipments,
            shipments_pagination=shipments_page,
            documents=documents,
            documents_pagination=documents_page,
            receipts=receipts,
            receipts_pagination=receipts_page,
            shipment_count=await self.repo.shipment_count(order_id),
            receipt_summary={"count": row.receipt_count, "qc_pending": row.qc_count > 0},
        )

    async def accept(
        self, principal: Principal, order_id: uuid.UUID, payload: VendorPortalAccept
    ) -> PurchaseOrderSummary:
        async with transaction(self.session):
            order = await PurchaseOrderRepository(self.session, self.tenant).lock_for_update(
                order_id
            )
            if order is None:
                raise NotFoundError("Purchase order", order_id)
            if payload.committed_delivery_date and payload.committed_delivery_date < order.po_date:
                raise ValidationError("The committed delivery date cannot precede the order date.")
            # ASSUMPTION: vendor commitment never silently changes the buyer planning date.
            # docs/assumptions.md §C1-1.
            before = order.vendor_committed_date
            order.vendor_committed_date = payload.committed_delivery_date
            await procurement.transition_purchase_order(
                self.session,
                self.tenant,
                principal,
                order_id,
                PurchaseOrderStatus.ACKNOWLEDGED,
                reason=payload.note or "Vendor accepted",
                vendor_reference=payload.vendor_reference,
            )
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type="purchase_order",
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Vendor committed delivery for {order.code}",
                user_id=self.tenant.user_id,
                old_values={"vendor_committed_date": before},
                new_values={"vendor_committed_date": order.vendor_committed_date},
            )
            await notify_buyer(
                self.session,
                self.tenant,
                order,
                NotificationType.PURCHASE_ORDER_ACKNOWLEDGED,
                payload.note or "Purchase order accepted",
            )
            return PurchaseOrderSummary.model_validate(order)

    async def decline(
        self, principal: Principal, order_id: uuid.UUID, reason: str
    ) -> PurchaseOrderSummary:
        async with transaction(self.session):
            order = await PurchaseOrderRepository(self.session, self.tenant).lock_for_update(
                order_id
            )
            if order is None:
                raise NotFoundError("Purchase order", order_id)
            order.vendor_decline_reason = reason
            await procurement.transition_purchase_order(
                self.session,
                self.tenant,
                principal,
                order_id,
                PurchaseOrderStatus.VENDOR_DECLINED,
                reason=reason,
            )
            await notify_buyer(
                self.session, self.tenant, order, NotificationType.PURCHASE_ORDER_DECLINED, reason
            )
            return PurchaseOrderSummary.model_validate(order)

    async def change_date(
        self, order_id: uuid.UUID, payload: VendorPortalDateChange
    ) -> PurchaseOrderSummary:
        return await self._update(
            order_id,
            {"vendor_committed_date": payload.date},
            payload.reason,
            NotificationType.PURCHASE_ORDER_DATE_CHANGED,
        )

    async def ready(self, order_id: uuid.UUID, payload: VendorPortalReady) -> PurchaseOrderSummary:
        if payload.ready_on and payload.ready_on.tzinfo is None:
            raise ValidationError("ready_on must include a timezone.")
        return await self._update(
            order_id,
            {
                "ready_for_dispatch_at": payload.ready_on or utcnow(),
                "ready_packages": payload.packages,
                "ready_note": payload.note,
            },
            payload.note or "Ready for dispatch",
            NotificationType.PURCHASE_ORDER_READY,
        )

    async def _update(
        self, order_id: uuid.UUID, values: dict[str, Any], reason: str, kind: NotificationType
    ) -> PurchaseOrderSummary:
        async with transaction(self.session):
            order = await PurchaseOrderRepository(self.session, self.tenant).lock_for_update(
                order_id
            )
            if order is None:
                raise NotFoundError("Purchase order", order_id)
            if order.status not in ("ACKNOWLEDGED", "PARTIALLY_RECEIVED"):
                raise BusinessRuleError(
                    "The action requires an accepted purchase order with goods still due."
                )
            if (
                values.get("vendor_committed_date")
                and values["vendor_committed_date"] < order.po_date
            ):
                raise ValidationError("The committed delivery date cannot precede the order date.")
            before = {key: getattr(order, key) for key in values}
            for key, value in values.items():
                setattr(order, key, value)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type="purchase_order",
                entity_id=order.id,
                entity_code=order.code,
                summary=reason,
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=values,
            )
            await notify_buyer(self.session, self.tenant, order, kind, reason)
            await self.session.flush()
            return PurchaseOrderSummary.model_validate(order)

    async def receipts(
        self, params: PageParams, order_id: uuid.UUID | None = None
    ) -> tuple[list[VendorPortalReceiptRead], OffsetPagination]:
        if order_id:
            await PurchaseOrderRepository(self.session, self.tenant).get_or_404(order_id)
        rows, page = await self.repo.receipts(params, order_id)
        ids = [r.id for r in rows]
        lines = await self.repo.receipt_lines(ids)
        inspections = await self.repo.receipt_inspections(ids)
        return [
            VendorPortalReceiptRead(
                id=r.id,
                code=r.code,
                purchase_order_id=r.purchase_order_id,
                receipt_date=r.receipt_date,
                status=str(r.status),
                lines=lines.get(r.id, []),
                inspections=inspections.get(r.id, []),
            )
            for r in rows
        ], page

    async def create_invoice(self, payload: VendorPortalInvoiceCreate) -> VendorInvoiceDetail:
        async with transaction(self.session):
            await PurchaseOrderRepository(self.session, self.tenant).get_or_404(
                payload.purchase_order_id
            )
            assert self.tenant.vendor_id is not None
            attachment = None
            if payload.invoice_document_id:
                from src.modules.procurement.inbound_repository import InboundDocumentRepository

                attachment = await InboundDocumentRepository(
                    self.session, self.tenant
                ).invoice_document(payload.invoice_document_id, payload.purchase_order_id)
                if attachment is None:
                    raise NotFoundError("Invoice document", payload.invoice_document_id)
                if attachment.vendor_invoice_id is not None:
                    raise ValidationError("This invoice document is already attached to a bill.")
            result = await finance.create_invoice(
                self.session, self.tenant, payload.staff_payload(self.tenant.vendor_id)
            )
            if attachment is not None:
                attachment.vendor_invoice_id = result.id
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    user_id=self.tenant.user_id,
                    action=AuditAction.UPDATE,
                    entity_type="purchase_order_document",
                    entity_id=attachment.id,
                    summary="Attached uploaded invoice file to vendor bill",
                    new_values={"vendor_invoice_id": result.id},
                )
            # Vendors submit bills for staff review immediately; all centres remain NULL.
            return await finance.submit_invoice(
                self.session, self.tenant, result.id, reason="Submitted by vendor"
            )

    async def invoices(
        self, params: PageParams
    ) -> tuple[list[VendorInvoiceSummary], OffsetPagination]:
        return await finance.list_invoices(
            self.session,
            self.tenant,
            filters=VendorInvoiceFilters(),
            params=params,
            sort=SortParams(),
        )

    async def payments(self, params: PageParams) -> tuple[list[PaymentDetail], OffsetPagination]:
        from src.modules.finance.service import payment_detail

        rows, allocations, codes, page = await self.repo.payment_page(params)
        return [
            payment_detail(r, [a for a in allocations if a.payment_id == r.id], codes) for r in rows
        ], page

    async def transporters(self, params: PageParams) -> Any:
        from src.modules.vendors.schemas import TransporterOption

        rows, pagination = await self.repo.transporters(params)
        return [TransporterOption.model_validate(row) for row in rows], pagination

    async def payment_summary(self) -> list[VendorPortalPaymentSummary]:
        return [
            VendorPortalPaymentSummary(**dict(r._mapping))
            for r in await self.repo.payment_summary(today())
        ]

    async def download(self, document_id: uuid.UUID) -> Any:
        doc = await DocumentService(self.session, self.tenant).get_document(document_id)
        if doc.entity_type != "purchase_order" or doc.entity_id is None:
            raise NotFoundError("Document", document_id)
        await PurchaseOrderRepository(self.session, self.tenant).get_or_404(doc.entity_id)
        return await DocumentService(self.session, self.tenant).open_download(document_id)

    async def job(self, job_id: uuid.UUID) -> ReportJobRead:
        snapshot = await ReportSnapshotRepository(self.session, self.tenant).get_or_404(job_id)
        if (
            snapshot.created_by_id != self.tenant.user_id
            or snapshot.report_key != "PURCHASE_ORDER"
            or not snapshot.parameters
            or not snapshot.parameters.get("purchase_order_id")
        ):
            raise NotFoundError("Report job", job_id)
        await PurchaseOrderRepository(self.session, self.tenant).get_or_404(
            uuid.UUID(snapshot.parameters["purchase_order_id"])
        )
        read = await reports.get_job(self.session, self.tenant, job_id)
        if read.document_id:
            read.download_url = api_path(f"vendor-portal/documents/{read.document_id}/download")
        return read


async def upload_vendor_document(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    upload: Any,
    kind: Any,
    shipment_id: uuid.UUID | None,
) -> Any:
    from src.core.permissions import Permission
    from src.modules.procurement.inbound_service import upload_document

    principal.require(
        Permission.VENDOR_PORTAL_INVOICE if kind == "INVOICE" else Permission.VENDOR_PORTAL_SHIP
    )
    return await upload_document(session, tenant, order_id, upload, kind, shipment_id)


async def vendor_rating(session: AsyncSession, tenant: TenantContext) -> Any:
    from src.modules.vendors.service import VendorService

    assert tenant.vendor_id is not None
    return await VendorService(session, tenant).preview_rating(tenant.vendor_id)
