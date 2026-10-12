"""Vendor business actions, composed from the owning modules' service APIs.

The portal owns no tables. Every action delegates to the module that owns the
row - procurement's state machine and consignment rules, finance's bill and
payment rules, the documents store - with the vendor forced from the verified
token, never from a payload. What this module adds is the vendor's view: which
of those rows a vendor may see (always its own, through the procurement scope)
and which of their fields (the allow-list read models in ``schemas``).
"""

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
from src.core.permissions import Permission
from src.core.responses import OffsetPagination, api_path
from src.core.types import to_money
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.documents.service import DocumentDownload, DocumentService
from src.modules.finance import service as finance
from src.modules.finance.schemas import VendorInvoiceFilters
from src.modules.notifications.models import NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.procurement import inbound_service
from src.modules.procurement import service as procurement
from src.modules.procurement.inbound_repository import InboundDocumentRepository
from src.modules.procurement.inbound_schemas import DocumentKind, InboundDocumentRead
from src.modules.procurement.inbound_service import SHIPPING_PO_STATUSES, notify_buyer
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.reports import service as reports
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.reports.schemas import ReportJobRead
from src.modules.vendor_portal.repository import VENDOR_QC_LOOKBACK_DAYS, VendorPortalRepository
from src.modules.vendor_portal.schemas import (
    VendorPortalAccept,
    VendorPortalCategory,
    VendorPortalDashboard,
    VendorPortalDateChange,
    VendorPortalInspection,
    VendorPortalInvoiceCreate,
    VendorPortalInvoiceDetail,
    VendorPortalInvoiceSummary,
    VendorPortalInvoiceTotals,
    VendorPortalMe,
    VendorPortalOrderActionRead,
    VendorPortalOrderDetail,
    VendorPortalOrderLine,
    VendorPortalPayment,
    VendorPortalPaymentAllocation,
    VendorPortalPaymentSummary,
    VendorPortalReady,
    VendorPortalReceiptLine,
    VendorPortalReceiptRead,
    VendorPortalReceiptTotals,
    VendorPortalTimelineEvent,
)
from src.modules.vendors.schemas import TransporterOption, VendorRatingRead

#: Each list embedded in the order detail shows its first page; the dedicated
#: list routes page the rest (docs/assumptions.md §C1-8).
DETAIL_EMBED_LIMIT = 50

#: The warehouse fields that make up a delivery address, in reading order.
ADDRESS_FIELDS: tuple[str, ...] = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
)


class VendorPortalService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        if tenant.vendor_id is None:
            # The router's ``require_vendor_user`` refuses first; this is the
            # second line, for any caller that reaches the service another way.
            raise AuthorizationError("A vendor account is required.", code=ErrorCode.SCOPE_DENIED)
        self.session, self.tenant = session, tenant
        self.vendor_id: uuid.UUID = tenant.vendor_id
        self.repo = VendorPortalRepository(session, tenant)
        self.orders = PurchaseOrderRepository(session, tenant)

    # --- profile and dashboard ------------------------------------------------

    async def me(self, principal: Principal) -> VendorPortalMe:
        vendor = await self.repo.vendor()
        if vendor is None:
            raise NotFoundError("Vendor", self.vendor_id)
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
        return VendorPortalDashboard(**row._mapping, qc_lookback_days=VENDOR_QC_LOOKBACK_DAYS)

    # --- one order ------------------------------------------------------------

    async def order(self, order_id: uuid.UUID) -> VendorPortalOrderDetail:
        order = await self.orders.get_or_404(order_id)
        tracking = await inbound_service.tracking_row(self.session, self.tenant, order_id)
        detail = await procurement.get_purchase_order_detail(self.session, self.tenant, order_id)
        delivery_address = detail.delivery_address
        if not delivery_address:
            warehouse = await self.repo.warehouse(detail.warehouse_id)
            if warehouse is not None:
                delivery_address = ", ".join(
                    str(getattr(warehouse, key))
                    for key in ADDRESS_FIELDS
                    if getattr(warehouse, key, None)
                )
        labels = await self.repo.line_labels(order_id)
        lines = [
            VendorPortalOrderLine.model_validate(
                {
                    **line.model_dump(),
                    "item_code": labels.get(line.id, {}).get("code"),
                    "item_name": labels.get(line.id, {}).get("name"),
                    "item_category_id": labels.get(line.id, {}).get("category_id"),
                    "uom_code": labels.get(line.id, {}).get("uom_code"),
                }
            )
            for line in detail.lines
        ]
        embed = PageParams(limit=DETAIL_EMBED_LIMIT)
        shipments, shipments_page = await inbound_service.list_shipments(
            self.session, self.tenant, order_id, embed
        )
        documents, documents_page = await inbound_service.list_documents(
            self.session, self.tenant, order_id, embed
        )
        receipts, receipts_page = await self.receipts(embed, order_id)
        invoices = await self.repo.invoice_totals(order_id)
        return VendorPortalOrderDetail(
            id=detail.id,
            code=detail.code,
            po_date=detail.po_date,
            status=detail.status,
            tracking=tracking,
            vendor_id=detail.vendor_id,
            warehouse_id=detail.warehouse_id,
            item_category_id=detail.item_category_id,
            item_category_code=detail.item_category_code,
            item_category_name=detail.item_category_name,
            currency=detail.currency,
            subtotal=detail.subtotal,
            discount=detail.discount,
            tax_amount=detail.tax_amount,
            freight_amount=detail.freight_amount,
            total_amount=detail.total_amount,
            payment_terms_days=detail.payment_terms_days,
            expected_delivery_date=detail.expected_delivery_date,
            vendor_committed_date=detail.vendor_committed_date,
            delivery_address=delivery_address,
            terms=detail.terms,
            vendor_reference=detail.vendor_reference,
            sent_at=detail.sent_at,
            acknowledged_at=detail.acknowledged_at,
            ready_for_dispatch_at=detail.ready_for_dispatch_at,
            ready_packages=detail.ready_packages,
            ready_note=order.ready_note,
            vendor_decline_reason=detail.vendor_decline_reason,
            assigned_vendor_user_id=detail.assigned_vendor_user_id,
            assigned_vendor_user_name=detail.assigned_vendor_user_name,
            version=detail.version,
            lines=lines,
            timeline=[
                VendorPortalTimelineEvent(
                    id=row.id, at=row.at, from_status=row.from_status, to_status=row.to_status
                )
                for row in await self.repo.timeline(order_id)
            ],
            invoice_summary=VendorPortalInvoiceTotals(**invoices._mapping),
            receipt_summary=VendorPortalReceiptTotals(
                count=receipts_page.total if receipts_page.total is not None else len(receipts),
                qc_pending=tracking.qc_pending,
            ),
            shipment_count=tracking.shipment_count,
            documents_count=tracking.documents_count,
            shipments=shipments,
            shipments_pagination=shipments_page,
            documents=documents,
            documents_pagination=documents_page,
            receipts=receipts,
            receipts_pagination=receipts_page,
        )

    # --- order actions ----------------------------------------------------------

    async def accept(
        self, principal: Principal, order_id: uuid.UUID, payload: VendorPortalAccept
    ) -> VendorPortalOrderActionRead:
        """SENT -> ACKNOWLEDGED, with the vendor's own committed date and reference."""
        async with transaction(self.session):
            order = await self._lock(order_id)
            if (
                payload.committed_delivery_date is not None
                and payload.committed_delivery_date < order.po_date
            ):
                raise ValidationError("The committed delivery date cannot precede the order date.")
            await procurement.transition_purchase_order(
                self.session,
                self.tenant,
                principal,
                order_id,
                PurchaseOrderStatus.ACKNOWLEDGED,
                reason=payload.note or "Accepted by the vendor",
                vendor_reference=payload.vendor_reference,
            )
            if payload.committed_delivery_date is not None:
                # ASSUMPTION: the vendor's promise never moves the buyer's
                # planning date, which MRP nets on; a buyer adopts it explicitly.
                # docs/assumptions.md §C1-1.
                before = order.vendor_committed_date
                order.vendor_committed_date = payload.committed_delivery_date
                await self.session.flush()
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.UPDATE,
                    entity_type="purchase_order",
                    entity_id=order.id,
                    entity_code=order.code,
                    summary=f"Vendor committed delivery of {order.code} by "
                    f"{payload.committed_delivery_date.isoformat()}",
                    user_id=self.tenant.user_id,
                    old_values={"vendor_committed_date": before},
                    new_values={"vendor_committed_date": order.vendor_committed_date},
                )
            committed = (
                f" for delivery by {payload.committed_delivery_date.isoformat()}"
                if payload.committed_delivery_date
                else ""
            )
            await notify_buyer(
                self.session,
                self.tenant,
                order,
                NotificationType.PURCHASE_ORDER_ACKNOWLEDGED,
                f"The vendor accepted {order.code}{committed}."
                + (f" Note: {payload.note}" if payload.note else ""),
            )
            return VendorPortalOrderActionRead.model_validate(order)

    async def decline(
        self, principal: Principal, order_id: uuid.UUID, reason: str
    ) -> VendorPortalOrderActionRead:
        """SENT -> VENDOR_DECLINED. The buyer then cancels the order or sends it again."""
        async with transaction(self.session):
            order = await self._lock(order_id)
            await procurement.transition_purchase_order(
                self.session,
                self.tenant,
                principal,
                order_id,
                PurchaseOrderStatus.VENDOR_DECLINED,
                reason=reason,
            )
            await notify_buyer(
                self.session,
                self.tenant,
                order,
                NotificationType.PURCHASE_ORDER_DECLINED,
                f"The vendor declined {order.code}. Reason: {reason}",
            )
            return VendorPortalOrderActionRead.model_validate(order)

    async def change_date(
        self, order_id: uuid.UUID, payload: VendorPortalDateChange
    ) -> VendorPortalOrderActionRead:
        """Move the vendor's own committed date; the planning date stays the buyer's."""
        return await self._update(
            order_id,
            {"vendor_committed_date": payload.date},
            summary=f"Vendor moved its committed delivery date to {payload.date.isoformat()}",
            notice=NotificationType.PURCHASE_ORDER_DATE_CHANGED,
            body=f"New committed delivery date {payload.date.isoformat()}. "
            f"Reason: {payload.reason}",
        )

    async def ready(
        self, order_id: uuid.UUID, payload: VendorPortalReady
    ) -> VendorPortalOrderActionRead:
        """Flag the goods as packed and waiting for collection (a flag, not a status)."""
        if payload.ready_on is not None and payload.ready_on.tzinfo is None:
            raise ValidationError("ready_on must include a timezone offset.")
        packages = f" - {payload.packages} package(s)" if payload.packages else ""
        return await self._update(
            order_id,
            {
                "ready_for_dispatch_at": payload.ready_on or utcnow(),
                "ready_packages": payload.packages,
                "ready_note": payload.note,
            },
            summary="Vendor marked the order ready for dispatch",
            notice=NotificationType.PURCHASE_ORDER_READY,
            body="Ready for dispatch"
            + packages
            + (f". Note: {payload.note}" if payload.note else "."),
        )

    async def _update(
        self,
        order_id: uuid.UUID,
        values: dict[str, Any],
        *,
        summary: str,
        notice: NotificationType,
        body: str,
    ) -> VendorPortalOrderActionRead:
        async with transaction(self.session):
            order = await self._lock(order_id)
            if order.status not in SHIPPING_PO_STATUSES:
                raise BusinessRuleError(
                    f"Purchase order {order.code} is {order.status}; this needs an accepted "
                    "order with goods still due.",
                    details={"status": str(order.status)},
                )
            committed = values.get("vendor_committed_date")
            if committed is not None and committed < order.po_date:
                raise ValidationError("The committed delivery date cannot precede the order date.")
            before = {key: getattr(order, key) for key in values}
            for key, value in values.items():
                setattr(order, key, value)
            self.orders.touch(order)
            await self.session.flush()
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type="purchase_order",
                entity_id=order.id,
                entity_code=order.code,
                summary=f"{summary} ({order.code})",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=values,
            )
            await notify_buyer(self.session, self.tenant, order, notice, body)
            return VendorPortalOrderActionRead.model_validate(order)

    async def _lock(self, order_id: uuid.UUID) -> PurchaseOrder:
        order = await self.orders.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        return order

    # --- receipts ------------------------------------------------------------------

    async def receipts(
        self, params: PageParams, order_id: uuid.UUID | None = None
    ) -> tuple[list[VendorPortalReceiptRead], OffsetPagination]:
        """Own posted receipts with their lines and quality's verdicts - four reads a page."""
        if order_id is not None:
            await self.orders.get_or_404(order_id)
        rows, page = await self.repo.receipts(params, order_id)
        ids = [row.id for row in rows]
        lines = await self.repo.receipt_lines(ids)
        inspections = await self.repo.receipt_inspections(ids)
        codes = await self.repo.order_codes([row.purchase_order_id for row in rows])
        return [
            VendorPortalReceiptRead(
                id=row.id,
                code=row.code,
                purchase_order_id=row.purchase_order_id,
                purchase_order_code=codes.get(row.purchase_order_id, ""),
                receipt_date=row.receipt_date,
                status=str(row.status),
                lines=[
                    VendorPortalReceiptLine.model_validate(line) for line in lines.get(row.id, [])
                ],
                inspections=[
                    VendorPortalInspection.model_validate(inspection)
                    for inspection in inspections.get(row.id, [])
                ],
            )
            for row in rows
        ], page

    # --- bills and payments ------------------------------------------------------

    async def create_invoice(self, payload: VendorPortalInvoiceCreate) -> VendorPortalInvoiceDetail:
        """Raise a bill against one of the vendor's own orders and submit it for review.

        Every reference is checked as the vendor's before finance sees it, and a
        foreign one is "not found": finance's own messages name the document
        they refused, which would tell one vendor another's receipt code.
        """
        async with transaction(self.session):
            await self.orders.get_or_404(payload.purchase_order_id)
            await self._assert_bill_references(payload)
            attachment = None
            if payload.invoice_document_id is not None:
                attachment = await InboundDocumentRepository(
                    self.session, self.tenant
                ).invoice_document(payload.invoice_document_id, payload.purchase_order_id)
                if attachment is None:
                    raise NotFoundError("Invoice document", payload.invoice_document_id)
                if attachment.vendor_invoice_id is not None:
                    raise BusinessRuleError("This invoice file is already attached to a bill.")
            created = await finance.create_invoice(
                self.session, self.tenant, payload.staff_payload(self.vendor_id)
            )
            if attachment is not None:
                attachment.vendor_invoice_id = created.id
                await self.session.flush()
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    user_id=self.tenant.user_id,
                    action=AuditAction.UPDATE,
                    entity_type="purchase_order_document",
                    entity_id=attachment.id,
                    summary=f"Attached the uploaded invoice file to {created.code}",
                    old_values={"vendor_invoice_id": None},
                    new_values={"vendor_invoice_id": created.id},
                )
            # ASSUMPTION: a vendor's bill goes straight to review, with no cost
            # centre - staff tag centres while reviewing. docs/assumptions.md §C1-3.
            submitted = await finance.submit_invoice(
                self.session, self.tenant, created.id, reason="Submitted by the vendor"
            )
            return VendorPortalInvoiceDetail.model_validate(submitted.model_dump())

    async def _assert_bill_references(self, payload: VendorPortalInvoiceCreate) -> None:
        order_id = payload.purchase_order_id
        if payload.goods_receipt_id is not None and (
            await self.repo.own_receipt(payload.goods_receipt_id, order_id) is None
        ):
            raise NotFoundError("Goods receipt", payload.goods_receipt_id)
        order_lines = await self.repo.order_lines(order_id)
        named_receipt_lines = [
            line.goods_receipt_line_id for line in payload.lines if line.goods_receipt_line_id
        ]
        receipt_lines = (
            await self.repo.order_receipt_lines(order_id) if named_receipt_lines else set()
        )
        order_items = set(order_lines.values())
        for line in payload.lines:
            if line.purchase_order_line_id is not None and line.purchase_order_line_id not in (
                order_lines
            ):
                raise NotFoundError("Purchase order line", line.purchase_order_line_id)
            if line.goods_receipt_line_id is not None and line.goods_receipt_line_id not in (
                receipt_lines
            ):
                raise NotFoundError("Goods receipt line", line.goods_receipt_line_id)
            # ASSUMPTION: a vendor bills only what it was ordered; a freight or
            # service line names no item at all. docs/assumptions.md §C1-3.
            if line.item_id is not None and line.item_id not in order_items:
                raise NotFoundError("Item", line.item_id)

    async def invoices(
        self, params: PageParams
    ) -> tuple[list[VendorPortalInvoiceSummary], OffsetPagination]:
        rows, page = await finance.list_invoices(
            self.session,
            self.tenant,
            filters=VendorInvoiceFilters(vendor_id=self.vendor_id),
            params=params,
            sort=SortParams(),
        )
        return [VendorPortalInvoiceSummary.model_validate(row.model_dump()) for row in rows], page

    async def invoice(self, invoice_id: uuid.UUID) -> VendorPortalInvoiceDetail:
        detail = await finance.get_invoice_detail(self.session, self.tenant, invoice_id)
        if detail.vendor_id != self.vendor_id:  # finance's own scope already ensures this
            raise NotFoundError("Vendor invoice", invoice_id)
        return VendorPortalInvoiceDetail.model_validate(detail.model_dump())

    async def payments(
        self, params: PageParams
    ) -> tuple[list[VendorPortalPayment], OffsetPagination]:
        rows, allocations, codes, page = await self.repo.payment_page(params)
        by_payment: dict[uuid.UUID, list[VendorPortalPaymentAllocation]] = {}
        for allocation in allocations:
            code, number = codes.get(allocation.vendor_invoice_id, (None, None))
            by_payment.setdefault(allocation.payment_id, []).append(
                VendorPortalPaymentAllocation(
                    vendor_invoice_id=allocation.vendor_invoice_id,
                    invoice_code=code,
                    vendor_invoice_number=number,
                    amount=allocation.amount,
                )
            )
        return [
            VendorPortalPayment(
                id=row.id,
                code=row.code,
                payment_date=row.payment_date,
                amount=row.amount,
                allocated_amount=row.allocated_amount,
                unallocated_amount=to_money(row.amount - row.allocated_amount),
                currency=row.currency,
                payment_method=row.payment_method,
                reference_number=row.reference_number,
                bank_name=row.bank_name,
                instrument_date=row.instrument_date,
                status=row.status,
                allocations=by_payment.get(row.id, []),
            )
            for row in rows
        ], page

    async def payment_summary(self) -> list[VendorPortalPaymentSummary]:
        return [
            VendorPortalPaymentSummary(**row._mapping)
            for row in await self.repo.payment_summary(today())
        ]

    # --- pickers, files and jobs ---------------------------------------------------

    async def transporters(
        self, params: PageParams
    ) -> tuple[list[TransporterOption], OffsetPagination]:
        rows, pagination = await self.repo.transporters(params)
        return [TransporterOption.model_validate(row) for row in rows], pagination

    async def download(self, document_id: uuid.UUID) -> DocumentDownload:
        """A file shared with the vendor on one of its own orders - else "not found".

        Shared means filed through the order's documents route (it carries a
        typed link) or the generated PO PDF; anything a buyer filed against the
        order from the generic Documents screen stays internal.
        """
        document = await DocumentService(self.session, self.tenant).get_document(document_id)
        if document.entity_type != "purchase_order" or document.entity_id is None:
            raise NotFoundError("Document", document_id)
        order = await self.orders.get(document.entity_id)
        if order is None:
            raise NotFoundError("Document", document_id)
        shared = await InboundDocumentRepository(self.session, self.tenant).visible_document(
            document_id, order.id
        )
        if shared is None:
            raise NotFoundError("Document", document_id)
        return await DocumentService(self.session, self.tenant).open_download(document_id)

    async def job(self, job_id: uuid.UUID) -> ReportJobRead:
        """The vendor's own PO-PDF job, with a download link the portal can follow."""
        snapshot = await ReportSnapshotRepository(self.session, self.tenant).get(job_id)
        parameters = (snapshot.parameters or {}) if snapshot is not None else {}
        order_ref = parameters.get("purchase_order_id")
        if (
            snapshot is None
            or snapshot.created_by_id != self.tenant.user_id
            or snapshot.report_key != ReportKey.PURCHASE_ORDER
            or not order_ref
        ):
            raise NotFoundError("Report job", job_id)
        if await self.orders.get(uuid.UUID(str(order_ref))) is None:
            raise NotFoundError("Report job", job_id)
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
    kind: DocumentKind,
    shipment_id: uuid.UUID | None,
) -> InboundDocumentRead:
    """A typed upload from the vendor: an invoice needs the invoice right, the rest ship.

    The route admits either right; which one this upload needs depends on the
    form's ``kind``, so the narrower check happens here, before any byte is stored.
    """
    principal.require(
        Permission.VENDOR_PORTAL_INVOICE if kind == "INVOICE" else Permission.VENDOR_PORTAL_SHIP
    )
    return await inbound_service.upload_document(
        session, tenant, order_id, upload, kind, shipment_id
    )


async def vendor_rating(session: AsyncSession, tenant: TenantContext) -> VendorRatingRead:
    """The vendor's own rating, computed by the vendors module (never stored here)."""
    from src.modules.vendors.service import VendorService

    if tenant.vendor_id is None:
        raise AuthorizationError("A vendor account is required.", code=ErrorCode.SCOPE_DENIED)
    return await VendorService(session, tenant).preview_rating(tenant.vendor_id)


__all__ = [
    "DETAIL_EMBED_LIMIT",
    "VendorPortalService",
    "upload_vendor_document",
    "vendor_rating",
]
