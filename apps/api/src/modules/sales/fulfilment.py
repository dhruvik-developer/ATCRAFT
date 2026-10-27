"""The sales order's fulfilment trail (WP E1b section 3).

One question - "what has happened to this order?" - answered from every module
that touches it: the reservation that promised the stock, the MRP suggestions
that saw the shortage, the production and job-work orders raised to make it, the
requisition and purchase orders raised to buy it, the inspections that judged
it, the packing and dispatch documents that moved it, the transfer that brought
it, the invoices and credit notes that billed it, and the returns that came
back.

Two structural rules shape this module:

* **One statement per family.** Every family is read as a set for the whole
  order, never per line: a ten-line order with eleven families must not become
  a hundred and ten queries (section 32). Some families need a header and a line
  read; none needs a read *per document*.
* **Owning modules' tables, read-only.** This is a reporting surface, so it
  reads the tables directly with the owning models imported at call time. It
  never writes and never re-implements a module's rules; a status it shows is
  the status that module stored. The alternative - a public read seam on every
  module - would be nine nearly identical projection methods maintained in nine
  places.

The response is deliberately one flexible document shape rather than a schema
per family. The timeline renders every family the same way, and the few fields
that differ (a tracking note, a carton count, a rejection quantity) travel in
``detail``/``secondary_quantity``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.types import ZERO
from src.modules.sales.models import SalesOrderLine
from src.modules.sales.repository import (
    ItemLookupRepository,
    SalesOrderLineRepository,
    SalesOrderRepository,
)
from src.modules.sales.schemas import (
    FulfilmentDocumentRead,
    FulfilmentLineRead,
    FulfilmentRead,
)

#: The sales order's own document kind, used for the reservation summary. Not a
#: document in the trail (the order is the trail's subject).
SALES_ORDER_ENTITY = "sales_order"


def _date(value: Any) -> Any:
    """A datetime is rendered as its date; a date is already one."""
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _document(
    *,
    kind: str,
    document_id: uuid.UUID,
    code: str,
    status: Any,
    url: str,
    item_id: uuid.UUID | None = None,
    item_code: str | None = None,
    quantity: Any = None,
    secondary_quantity: Any = None,
    document_date: Any = None,
    expected_date: Any = None,
    line_id: uuid.UUID | None = None,
    detail: str | None = None,
) -> FulfilmentDocumentRead:
    return FulfilmentDocumentRead(
        kind=kind,
        id=document_id,
        code=str(code),
        status=str(status),
        item_id=item_id,
        item_code=item_code,
        quantity=_decimal(quantity),
        secondary_quantity=_decimal(secondary_quantity),
        document_date=_date(document_date),
        expected_date=_date(expected_date),
        line_id=line_id,
        detail=detail,
        url=url,
    )


# -----------------------------------------------------------------------------
# MRP
# -----------------------------------------------------------------------------


async def _mrp_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    """Suggestions that name this order directly or through canonical BOM roots.

    Shared suggestions retain their aggregate quantity and appear at document
    level. A root identity is causal provenance, never a quantitative allocation.
    """
    from src.core.demand_provenance import (
        LEGACY_MRP_SOURCE_LIST_LIMIT,
        project_demand_provenance,
    )
    from src.modules.mrp.models import MrpRun, MrpSuggestion

    stmt = (
        select(MrpSuggestion, MrpRun.code)
        .join(
            MrpRun,
            and_(MrpRun.id == MrpSuggestion.run_id, MrpRun.company_id == MrpSuggestion.company_id),
        )
        .where(
            MrpSuggestion.company_id == tenant.company_id,
            or_(
                MrpSuggestion.source_demands.contains([{"source_id": str(order_id)}]),
                MrpSuggestion.source_demands.contains(
                    [
                        {
                            "root_demand_refs": [
                                {"demand_type": "SALES_ORDER", "demand_id": str(order_id)}
                            ]
                        }
                    ]
                ),
            ),
        )
        .order_by(MrpSuggestion.required_date, MrpSuggestion.item_code, MrpSuggestion.id)
    )
    documents: list[FulfilmentDocumentRead] = []
    for suggestion, run_code in (await session.execute(stmt)).tuples().all():
        line_id = None
        detail = str(suggestion.suggestion_type)
        raw_sources = suggestion.source_demands or []
        sources = [source for source in raw_sources if isinstance(source, dict)]
        provenance = project_demand_provenance(raw_sources)
        has_metadata = any(
            "root_demand_refs" in source or "root_demand_refs_complete" in source
            for source in sources
        )
        legacy_unprovable = not has_metadata and (
            len(sources) >= LEGACY_MRP_SOURCE_LIST_LIMIT
            or any("via_item_id" in source or source.get("source") == "BOM" for source in sources)
        )
        if len(sources) != len(raw_sources) or legacy_unprovable:
            detail += " · incomplete demand links; quantity is the aggregate plan"
        elif has_metadata and provenance is not None:
            if provenance.root_demand_refs_complete and not any(
                root.demand_type == "SALES_ORDER" and root.demand_id == order_id
                for root in provenance.root_demand_refs
            ):
                continue
            if provenance.root_demand_refs_complete and len(provenance.root_demand_refs) == 1:
                root = provenance.root_demand_refs[0]
                if root.demand_type == "SALES_ORDER" and root.demand_id == order_id:
                    line_id = root.demand_line_id
            else:
                detail += " · shared demand; quantity is the aggregate plan"
        else:
            # Older direct facts predate typed metadata. Keep their single-root
            # attribution, including rows whose old shape omitted source type.
            identities = {
                (str(source.get("source_id", "")).lower(), source.get("source_line_id"))
                for source in sources
                if source.get("source_id")
            }
            if len(identities) == 1:
                source_id, raw_line_id = next(iter(identities))
                if source_id == str(order_id).lower() and raw_line_id:
                    try:
                        line_id = uuid.UUID(str(raw_line_id))
                    except ValueError:
                        line_id = None
            elif len(identities) > 1:
                detail += " · shared demand; quantity is the aggregate plan"
        documents.append(
            _document(
                kind="MRP_SUGGESTION",
                document_id=suggestion.id,
                code=f"{run_code}/{suggestion.item_code}",
                status=suggestion.status,
                url=f"/mrp/runs/{suggestion.run_id}",
                item_id=suggestion.item_id,
                item_code=suggestion.item_code,
                quantity=suggestion.suggested_quantity,
                document_date=suggestion.required_date,
                expected_date=suggestion.expected_available_date,
                line_id=line_id,
                detail=detail,
            )
        )
    return documents


# -----------------------------------------------------------------------------
# Production, assembly and job work
# -----------------------------------------------------------------------------


async def _production_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> tuple[list[FulfilmentDocumentRead], list[uuid.UUID]]:
    """Production orders sourced from the order, plus their work-order ids."""
    from src.modules.production.models import ProductionOrder, ProductionSourceType, WorkOrder

    stmt = select(ProductionOrder).where(
        ProductionOrder.company_id == tenant.company_id,
        ProductionOrder.source_type == ProductionSourceType.SALES_ORDER,
        ProductionOrder.source_id == order_id,
    )
    orders = list((await session.execute(stmt)).scalars())
    documents = [
        _document(
            kind="PRODUCTION_ORDER",
            document_id=order.id,
            code=order.code,
            status=order.status,
            url=f"/production/{order.id}",
            item_id=order.item_id,
            quantity=order.planned_quantity,
            secondary_quantity=order.produced_quantity,
            document_date=order.planned_start_date,
            expected_date=order.planned_end_date,
            line_id=order.source_line_id,
            detail=(
                f"produced {order.produced_quantity} of {order.planned_quantity}"
                + (f", rejected {order.rejected_quantity}" if order.rejected_quantity else "")
            ),
        )
        for order in orders
    ]
    work_order_ids: list[uuid.UUID] = []
    if orders:
        work_orders = (
            await session.execute(
                select(WorkOrder.id).where(
                    WorkOrder.company_id == tenant.company_id,
                    WorkOrder.production_order_id.in_([order.id for order in orders]),
                )
            )
        ).scalars()
        work_order_ids = list(work_orders)
    return documents, work_order_ids


async def _assembly_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    from src.modules.assembly.models import AssemblyOrder, AssemblySourceType

    stmt = select(AssemblyOrder).where(
        AssemblyOrder.company_id == tenant.company_id,
        AssemblyOrder.source_type == AssemblySourceType.SALES_ORDER,
        AssemblyOrder.source_id == order_id,
    )
    return [
        _document(
            kind="ASSEMBLY_ORDER",
            document_id=order.id,
            code=order.code,
            status=order.status,
            url="/assembly",
            item_id=order.item_id,
            quantity=order.planned_quantity,
            secondary_quantity=order.assembled_quantity,
            document_date=order.planned_start,
            expected_date=order.planned_end,
            detail=f"assembled {order.assembled_quantity} of {order.planned_quantity}",
        )
        for order in (await session.execute(stmt)).scalars()
    ]


async def _job_work_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    from src.modules.job_work.models import JobWorkOrder, JobWorkSourceType

    stmt = select(JobWorkOrder).where(
        JobWorkOrder.company_id == tenant.company_id,
        JobWorkOrder.source_type == JobWorkSourceType.SALES_ORDER,
        JobWorkOrder.source_id == order_id,
    )
    return [
        _document(
            kind="JOB_WORK_ORDER",
            document_id=order.id,
            code=order.code,
            status=order.status,
            url=f"/job-work/{order.id}",
            document_date=order.issued_at,
            expected_date=order.expected_return_date,
            detail=str(order.process_type),
        )
        for order in (await session.execute(stmt)).scalars()
    ]


# -----------------------------------------------------------------------------
# Procurement
# -----------------------------------------------------------------------------


def _ref_line_id(refs: Sequence[dict[str, Any]] | None, order_id: uuid.UUID) -> uuid.UUID | None:
    for ref in refs or ():
        if str(ref.get("demand_type", "")).upper() != "SALES_ORDER":
            continue
        if str(ref.get("demand_id", "")).lower() != str(order_id).lower():
            continue
        raw = ref.get("demand_line_id")
        if raw:
            try:
                return uuid.UUID(str(raw))
            except ValueError:  # pragma: no cover - written by the planner
                return None
    return None


async def _procurement_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> tuple[list[FulfilmentDocumentRead], list[uuid.UUID]]:
    """Requisitions, their purchase orders and the goods receipts against them.

    Returns the goods-receipt ids too, because the QC step inspects those
    receipts and the caller would otherwise have to query for them twice.
    """
    from src.modules.procurement.models import (
        GoodsReceipt,
        PurchaseOrder,
        PurchaseOrderLine,
        PurchaseRequisition,
        PurchaseRequisitionLine,
        PurchaseSourceType,
    )

    header = (
        await session.execute(
            select(PurchaseRequisition).where(
                PurchaseRequisition.company_id == tenant.company_id,
                PurchaseRequisition.source_type == PurchaseSourceType.SALES_ORDER,
                PurchaseRequisition.source_id == order_id,
            )
        )
    ).scalar_one_or_none()

    documents: list[FulfilmentDocumentRead] = []
    requisition_line_ids: list[uuid.UUID] = []
    line_attribution: dict[uuid.UUID, uuid.UUID] = {}

    if header is not None:
        lines = list(
            (
                await session.execute(
                    select(PurchaseRequisitionLine).where(
                        PurchaseRequisitionLine.company_id == tenant.company_id,
                        PurchaseRequisitionLine.requisition_id == header.id,
                    )
                )
            ).scalars()
        )
        for line in lines:
            line_id = _ref_line_id(line.source_demand_refs, order_id)
            if line_id is not None:
                line_attribution[line.id] = line_id
            requisition_line_ids.append(line.id)
            documents.append(
                _document(
                    kind="PURCHASE_REQUISITION",
                    document_id=header.id,
                    code=header.code,
                    status=header.status,
                    url="/procurement/requisitions",
                    item_id=line.item_id,
                    quantity=line.quantity,
                    secondary_quantity=line.ordered_quantity,
                    document_date=header.requisition_date,
                    expected_date=line.required_date,
                    line_id=line_id,
                    detail=f"line {line.line_number}: {line.status}",
                )
            )

    po_ids: set[uuid.UUID] = set()
    if requisition_line_ids:
        po_lines = list(
            (
                await session.execute(
                    select(PurchaseOrderLine).where(
                        PurchaseOrderLine.company_id == tenant.company_id,
                        PurchaseOrderLine.requisition_line_id.in_(requisition_line_ids),
                    )
                )
            ).scalars()
        )
        po_ids = {line.purchase_order_id for line in po_lines}
        if po_ids:
            headers = {
                po.id: po
                for po in (
                    await session.execute(
                        select(PurchaseOrder).where(
                            PurchaseOrder.company_id == tenant.company_id,
                            PurchaseOrder.id.in_(po_ids),
                        )
                    )
                ).scalars()
            }
            for po_line in po_lines:
                po = headers.get(po_line.purchase_order_id)
                if po is None:
                    continue
                documents.append(
                    _document(
                        kind="PURCHASE_ORDER",
                        document_id=po.id,
                        code=po.code,
                        status=po.status,
                        url=f"/procurement/purchase-orders/{po.id}",
                        item_id=po_line.item_id,
                        quantity=po_line.quantity,
                        secondary_quantity=po_line.received_quantity,
                        document_date=po.po_date,
                        expected_date=po_line.required_date or po.expected_delivery_date,
                        line_id=(
                            line_attribution.get(po_line.requisition_line_id)
                            if po_line.requisition_line_id is not None
                            else None
                        ),
                        detail=f"line {po_line.line_number}: {po_line.status}",
                    )
                )

    receipts = []
    if po_ids:
        receipts = list(
            (
                await session.execute(
                    select(GoodsReceipt).where(
                        GoodsReceipt.company_id == tenant.company_id,
                        GoodsReceipt.purchase_order_id.in_(po_ids),
                    )
                )
            ).scalars()
        )
    receipt_ids = [receipt.id for receipt in receipts]
    documents.extend(
        _document(
            kind="GOODS_RECEIPT",
            document_id=receipt.id,
            code=receipt.code,
            status=receipt.status,
            url="/procurement/goods-receipts",
            quantity=receipt.total_received_quantity,
            secondary_quantity=receipt.total_accepted_quantity,
            document_date=receipt.receipt_date,
            detail=(
                f"accepted {receipt.total_accepted_quantity}, "
                f"rejected {receipt.total_rejected_quantity}"
            ),
        )
        for receipt in receipts
    )
    return documents, receipt_ids


# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------


async def _quality_documents(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    work_order_ids: Iterable[uuid.UUID],
    assembly_ids: Iterable[uuid.UUID],
    job_work_ids: Iterable[uuid.UUID],
    receipt_ids: Iterable[uuid.UUID],
) -> list[FulfilmentDocumentRead]:
    """Inspections raised against the order's documents, one statement.

    The reference pairs are grouped by type so an inspection can never be
    attributed across families: a work order and an assembly order are both
    named by a bare UUID, and the type is half the reference.
    """
    from src.modules.quality.models import QcInspection

    pairs = (
        ("work_order", list(work_order_ids)),
        ("assembly_order", list(assembly_ids)),
        ("job_work_order", list(job_work_ids)),
        ("goods_receipt", list(receipt_ids)),
    )
    clauses = [
        and_(
            QcInspection.reference_type == reference_type,
            QcInspection.reference_id.in_(identifiers),
        )
        for reference_type, identifiers in pairs
        if identifiers
    ]
    if not clauses:
        return []
    stmt = (
        select(QcInspection)
        .where(QcInspection.company_id == tenant.company_id, or_(*clauses))
        .order_by(QcInspection.inspected_at)
    )
    return [
        _document(
            kind="QC_INSPECTION",
            document_id=inspection.id,
            code=inspection.code,
            status=inspection.status,
            url="/quality",
            item_id=inspection.item_id,
            quantity=inspection.inspected_quantity,
            secondary_quantity=inspection.accepted_quantity,
            document_date=inspection.inspected_at,
            detail=(
                f"{inspection.result}; accepted {inspection.accepted_quantity}, "
                f"rejected {inspection.rejected_quantity}"
            ),
        )
        for inspection in (await session.execute(stmt)).scalars()
    ]


# -----------------------------------------------------------------------------
# Packing, dispatch and transfers
# -----------------------------------------------------------------------------


async def _packing_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    from src.modules.packing.models import PackingOrder

    stmt = select(PackingOrder).where(
        PackingOrder.company_id == tenant.company_id,
        PackingOrder.sales_order_id == order_id,
    )
    return [
        _document(
            kind="PACKING_ORDER",
            document_id=order.id,
            code=order.code,
            status=order.status,
            url="/packing",
            document_date=order.planned_date,
            expected_date=order.packed_date,
            secondary_quantity=order.total_cartons,
            detail=f"{order.total_cartons} carton(s)",
        )
        for order in (await session.execute(stmt)).scalars()
    ]


async def _dispatch_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> tuple[list[FulfilmentDocumentRead], list[uuid.UUID]]:
    """Dispatches and their latest tracking event, two statements."""
    from src.modules.dispatch.models import Dispatch, DispatchTrackingEvent

    dispatches = list(
        (
            await session.execute(
                select(Dispatch).where(
                    Dispatch.company_id == tenant.company_id,
                    Dispatch.sales_order_id == order_id,
                )
            )
        ).scalars()
    )
    dispatch_ids = [dispatch.id for dispatch in dispatches]
    latest: dict[uuid.UUID, DispatchTrackingEvent] = {}
    if dispatch_ids:
        events = (
            await session.execute(
                select(DispatchTrackingEvent)
                .where(
                    DispatchTrackingEvent.company_id == tenant.company_id,
                    DispatchTrackingEvent.dispatch_id.in_(dispatch_ids),
                )
                .order_by(
                    DispatchTrackingEvent.dispatch_id, DispatchTrackingEvent.event_time.desc()
                )
            )
        ).scalars()
        for event in events:
            latest.setdefault(event.dispatch_id, event)

    documents = []
    for dispatch in dispatches:
        latest_event = latest.get(dispatch.id)
        detail_parts = [f"{dispatch.total_boxes} box(es)"]
        if dispatch.tracking_id:
            detail_parts.append(f"tracking {dispatch.tracking_id}")
        if latest_event is not None:
            detail_parts.append(
                f"latest: {latest_event.event_status}"
                + (f" at {latest_event.location}" if latest_event.location else "")
            )
        documents.append(
            _document(
                kind="DISPATCH",
                document_id=dispatch.id,
                code=dispatch.code,
                status=dispatch.status,
                url="/dispatch",
                document_date=dispatch.dispatch_date,
                expected_date=dispatch.expected_delivery_date,
                secondary_quantity=dispatch.total_boxes,
                detail="; ".join(detail_parts),
            )
        )
    return documents, dispatch_ids


async def _transfer_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    from src.modules.inventory.models import StockTransfer, TransferSourceType

    stmt = select(StockTransfer).where(
        StockTransfer.company_id == tenant.company_id,
        StockTransfer.source_type == TransferSourceType.SALES_ORDER,
        StockTransfer.source_id == order_id,
    )
    return [
        _document(
            kind="STOCK_TRANSFER",
            document_id=transfer.id,
            code=transfer.code,
            status=transfer.status,
            url="/inventory/movements",
            quantity=transfer.total_quantity,
            document_date=transfer.transfer_date,
            expected_date=transfer.expected_date,
            detail=transfer.reason,
        )
        for transfer in (await session.execute(stmt)).scalars()
    ]


# -----------------------------------------------------------------------------
# Finance and returns
# -----------------------------------------------------------------------------


async def _finance_documents(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[FulfilmentDocumentRead]:
    """Invoices and the credit notes raised against them, two statements."""
    from src.modules.finance.models import CreditNote, SalesInvoice

    invoices = list(
        (
            await session.execute(
                select(SalesInvoice).where(
                    SalesInvoice.company_id == tenant.company_id,
                    SalesInvoice.sales_order_id == order_id,
                )
            )
        ).scalars()
    )
    documents = [
        _document(
            kind="SALES_INVOICE",
            document_id=invoice.id,
            code=invoice.code,
            status=invoice.status,
            url="/finance/sales-invoices",
            document_date=invoice.invoice_date,
            expected_date=invoice.due_date,
            detail=f"{invoice.currency} {invoice.total_amount} (paid {invoice.paid_amount})",
        )
        for invoice in invoices
    ]
    invoice_ids = [invoice.id for invoice in invoices]
    if invoice_ids:
        notes = list(
            (
                await session.execute(
                    select(CreditNote).where(
                        CreditNote.company_id == tenant.company_id,
                        CreditNote.sales_invoice_id.in_(invoice_ids),
                    )
                )
            ).scalars()
        )
        documents.extend(
            _document(
                kind="CREDIT_NOTE",
                document_id=note.id,
                code=note.code,
                status=note.status,
                url="/finance/credit-notes",
                document_date=note.note_date,
                detail=f"{note.currency} {note.total_amount} ({note.reason_type})",
            )
            for note in notes
        )
    return documents


async def _return_documents(
    session: AsyncSession, tenant: TenantContext, dispatch_ids: Sequence[uuid.UUID]
) -> list[FulfilmentDocumentRead]:
    if not dispatch_ids:
        return []
    from src.modules.sales_returns.models import SalesReturn

    stmt = select(SalesReturn).where(
        SalesReturn.company_id == tenant.company_id,
        SalesReturn.dispatch_id.in_(list(dispatch_ids)),
    )
    return [
        _document(
            kind="SALES_RETURN",
            document_id=returned.id,
            code=returned.code,
            status=returned.status,
            url="/sales-returns",
            document_date=returned.received_at or returned.created_at,
            detail=str(returned.source),
        )
        for returned in (await session.execute(stmt)).scalars()
    ]


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


async def order_fulfilment(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> FulfilmentRead:
    """Everything that has happened, or is planned, for one sales order."""
    order = await SalesOrderRepository(session, tenant).get_or_404(order_id)
    lines = await SalesOrderLineRepository(session, tenant).list_for_order(order.id)
    line_repo = SalesOrderLineRepository(session, tenant)
    live = await line_repo.live_reservation_quantities(
        [line.reservation_id for line in lines if line.reservation_id is not None]
    )
    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])

    mrp_documents = await _mrp_documents(session, tenant, order.id)
    production_documents, work_order_ids = await _production_documents(session, tenant, order.id)
    assembly_documents = await _assembly_documents(session, tenant, order.id)
    job_work_documents = await _job_work_documents(session, tenant, order.id)
    procurement_documents, receipt_ids = await _procurement_documents(session, tenant, order.id)
    assembly_ids = [document.id for document in assembly_documents]
    job_work_ids = [document.id for document in job_work_documents]
    quality_documents = await _quality_documents(
        session,
        tenant,
        work_order_ids=work_order_ids,
        assembly_ids=assembly_ids,
        job_work_ids=job_work_ids,
        receipt_ids=receipt_ids,
    )
    packing_documents = await _packing_documents(session, tenant, order.id)
    dispatch_documents, dispatch_ids = await _dispatch_documents(session, tenant, order.id)
    transfer_documents = await _transfer_documents(session, tenant, order.id)
    finance_documents = await _finance_documents(session, tenant, order.id)
    return_documents = await _return_documents(session, tenant, dispatch_ids)

    def code_of(line: SalesOrderLine) -> str | None:
        info = items.get(line.item_id)
        return getattr(info, "code", None) if info is not None else None

    return FulfilmentRead(
        order_id=order.id,
        code=order.code,
        status=order.status,
        lines=[
            FulfilmentLineRead(
                line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                item_code=code_of(line),
                ordered_quantity=Decimal(line.quantity),
                allocated_quantity=Decimal(line.allocated_quantity),
                reserved_quantity=(
                    Decimal(live.get(line.reservation_id, ZERO))
                    if line.reservation_id is not None
                    else ZERO
                ),
                dispatched_quantity=Decimal(line.dispatched_quantity),
                shortfall_quantity=max(
                    Decimal(line.quantity)
                    - Decimal(line.dispatched_quantity)
                    - (
                        Decimal(live.get(line.reservation_id, ZERO))
                        if line.reservation_id is not None
                        else ZERO
                    ),
                    ZERO,
                ),
                status=line.status,
                reservation_id=line.reservation_id,
            )
            for line in lines
        ],
        mrp_suggestions=mrp_documents,
        production_orders=production_documents,
        assembly_orders=assembly_documents,
        job_work_orders=job_work_documents,
        requisitions=[
            document
            for document in procurement_documents
            if document.kind == "PURCHASE_REQUISITION"
        ],
        purchase_orders=[
            document for document in procurement_documents if document.kind == "PURCHASE_ORDER"
        ],
        goods_receipts=[
            document for document in procurement_documents if document.kind == "GOODS_RECEIPT"
        ],
        inspections=quality_documents,
        packing_orders=packing_documents,
        dispatches=dispatch_documents,
        transfers=transfer_documents,
        invoices=[document for document in finance_documents if document.kind == "SALES_INVOICE"],
        credit_notes=[document for document in finance_documents if document.kind == "CREDIT_NOTE"],
        returns=return_documents,
    )


__all__ = ["order_fulfilment"]
