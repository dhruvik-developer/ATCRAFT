"""Plan a confirmed sales order's shortfall into draft supply (WP E1b).

When a sales order is short after reservation - and after E2's reallocation
proposals have had their say - the sales module asks this module to raise the
documents that will fill the gap. The rules are deliberately narrow:

* **MAKE**, or **MAKE_OR_BUY with a default approved BOM**: one DRAFT production
  order per sales line, pegged through ``source_type=SALES_ORDER``,
  ``source_id`` (the order) and ``source_line_id`` (the line).
* **BUY**: one DRAFT purchase requisition per sales order, with each line
  carrying ``source_demand_refs`` back to the sales line (WP D1's shape).
* **JOB_WORK**: nothing. The line is returned as "needs job work"; obtaining it
  through the job-work flow is a human decision, and raising a production order
  would contradict the item master.
* **Everything else**: nothing, with a warning naming the line. That includes a
  production type the item master does not back with a capability flag - a part
  marked MAKE but not manufacturable, say. Confirmation is the customer-facing
  transaction and is never refused for a master-data gap; the planner's own
  stale draft is withdrawn and the warning tells the planner what to fix.

Three properties matter more than the documents themselves:

**Idempotent per line.** A re-run adjusts the drafts it already raised instead of
raising more. The planner recognises its own work by the pegging columns and by
``source_demand_refs``, never by a note or a code prefix.

**Draft only.** A production order or requisition raised here is never released,
approved or converted. ``§E1b-1`` records why: material and money commitments
are an operator's decision, and planning's job is to leave the decision ready.

**Reconciliation.** A quantity decrease, a cancelled line or a cancelled order
leaves drafts that are now too big or pointless. The planner cancels the DRAFT
documents it owns and leaves every document past DRAFT alone - a submitted
requisition is a human's commitment, and the response says so as a warning
rather than silently shrinking it.

The stock netting itself (reservations versus ordered quantity) lives in the
sales module, which owns the reservation; this module receives the per-line
shortfall and nets only the supply *it* can see: committed production output and
open requisition quantities pegged to the same line.
"""

from __future__ import annotations

import importlib
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.core.clock import today
from src.core.deps import TenantContext
from src.core.types import ZERO, to_quantity
from src.modules.mrp import engine
from src.modules.mrp.models import MrpSuggestionType
from src.modules.mrp.repository import DatabaseMrpSource
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderLineStatus

#: Production statuses whose remaining output is already committed supply. A
#: DRAFT is the planner's own document (handled separately); CANCELLED,
#: COMPLETED and CLOSED are history.
COMMITTED_PRODUCTION_STATUSES = frozenset({"APPROVED", "RELEASED", "MATERIAL_ISSUED", "IN_PROCESS"})

#: Requisition statuses that are dead for planning purposes. DRAFT is neither
#: dead nor committed: it is the document the planner may still rewrite.
DEAD_REQUISITION_STATUSES = frozenset({"REJECTED", "CANCELLED", "CLOSED"})

#: Requisition statuses in which the submitted document already commits supply.
COMMITTED_REQUISITION_STATUSES = frozenset({"PENDING_APPROVAL", "APPROVED", "CONVERTED"})

#: Requisition line statuses that still have quantity the buyer has not placed.
OPEN_REQUISITION_LINE_STATUSES = frozenset({"PENDING", "PARTIALLY_ORDERED"})

REQUISITION_STATUSES = frozenset(
    {"DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "CONVERTED", "CLOSED", "CANCELLED"}
)

DOCUMENT_KIND_PRODUCTION = "PRODUCTION_ORDER"
DOCUMENT_KIND_REQUISITION = "PURCHASE_REQUISITION"


@dataclass(frozen=True, slots=True)
class PlannedDocument:
    """One draft document that now covers part of one sales line.

    A requisition carries several sales lines, so it produces one row per line
    it covers; the ``id``/``code`` pair is the same on each. The read schema
    says the same thing and the screen groups by document.
    """

    kind: str
    id: uuid.UUID
    code: str
    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class JobWorkNeed:
    """A short line deliberately left to the job-work flow."""

    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ShortfallPlan:
    """The outcome of planning one order's shortfall."""

    documents: tuple[PlannedDocument, ...] = ()
    needs_job_work: tuple[JobWorkNeed, ...] = ()
    warnings: tuple[str, ...] = ()


def _models(module: str) -> Any:
    """One module's models, imported at call time.

    Production and procurement own their tables, and MRP is deliberately built
    without a compile-time dependency on either (the same rule that keeps the
    module deployable on its own). The import happens inside the planning call,
    which only exists when both modules are present.
    """
    return importlib.import_module(module)


def _remaining(planned: Any, produced: Any, rejected: Any = ZERO) -> Decimal:
    return max(Decimal(planned) - Decimal(produced) - Decimal(rejected), ZERO)


def _ref_line_id(refs: Sequence[Mapping[str, Any]] | None, order_id: uuid.UUID) -> uuid.UUID | None:
    """The sales line a requisition line is pegged to, if it is pegged to this order."""
    for ref in refs or ():
        if str(ref.get("demand_type", "")).upper() != "SALES_ORDER":
            continue
        if str(ref.get("demand_id", "")).lower() != str(order_id).lower():
            continue
        raw = ref.get("demand_line_id")
        if raw:
            try:
                return uuid.UUID(str(raw))
            except ValueError:  # pragma: no cover - refs are written by this module
                return None
    return None


async def _production_documents(
    session: Any, tenant: TenantContext, order_id: uuid.UUID
) -> list[Any]:
    """Every production order pegged to this sales order, one statement."""
    models = _models("src.modules.production.models")

    stmt = select(models.ProductionOrder).where(
        models.ProductionOrder.company_id == tenant.company_id,
        models.ProductionOrder.source_type == models.ProductionSourceType.SALES_ORDER,
        models.ProductionOrder.source_id == order_id,
    )
    return list((await session.execute(stmt)).scalars())


async def _requisition_documents(
    session: Any, tenant: TenantContext, order_id: uuid.UUID
) -> tuple[Any | None, list[Any]]:
    """The order's requisition (if any) and its lines, two statements.

    The planner raises one requisition per order, and the header's source pair
    is the link. A hand-raised requisition with the same source pair is treated
    the same way: DRAFT lines are adjusted through the procurement service,
    anything further along blocks additions.
    """
    models = _models("src.modules.procurement.models")

    header = (
        await session.execute(
            select(models.PurchaseRequisition).where(
                models.PurchaseRequisition.company_id == tenant.company_id,
                models.PurchaseRequisition.source_type == models.PurchaseSourceType.SALES_ORDER,
                models.PurchaseRequisition.source_id == order_id,
            )
        )
    ).scalar_one_or_none()
    if header is None:
        return None, []
    lines = list(
        (
            await session.execute(
                select(models.PurchaseRequisitionLine).where(
                    models.PurchaseRequisitionLine.company_id == tenant.company_id,
                    models.PurchaseRequisitionLine.requisition_id == header.id,
                )
            )
        ).scalars()
    )
    return header, lines


def _production_dates(item: Any, line: SalesOrderLine, order: SalesOrder) -> tuple[date, date]:
    """The draft's start and finish: finish by the promise, start a lead earlier.

    Neither date is allowed into the past: a draft that says "start last month"
    is noise on the planner's board, and the shortfall is real *now*. The finish
    is the line's own required date when it has one, else the order's.
    """
    as_of = today()
    required = max(line.required_date or order.required_date, as_of)
    lead = max(int(getattr(item, "lead_time_days", 0) or 0), 0)
    start = max(required - timedelta(days=lead), as_of)
    return start, required


async def _plan_production(
    session: Any,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    line: SalesOrderLine,
    item: Any,
    target: Decimal,
    drafts: list[Any],
    reason: str,
) -> list[PlannedDocument]:
    """Create, resize or cancel the DRAFT production orders for one line."""
    schemas = _models("src.modules.production.schemas")
    service = _models("src.modules.production.service")
    models = _models("src.modules.production.models")

    ordered = sorted(drafts, key=lambda doc: (str(doc.code), str(doc.id)))
    start, end = _production_dates(item, line, order)

    if target <= ZERO:
        for draft in ordered:
            await service.cancel_order(
                session,
                tenant,
                draft.id,
                schemas.CancelRequest(reason=reason),
            )
        return []

    keep = ordered[0] if ordered else None
    extra = ordered[1:]
    for draft in extra:
        await service.cancel_order(session, tenant, draft.id, schemas.CancelRequest(reason=reason))

    if keep is None:
        created = await service.create_order(
            session,
            tenant,
            schemas.ProductionOrderCreate(
                item_id=line.item_id,
                planned_quantity=target,
                warehouse_id=order.warehouse_id,
                planned_start_date=start,
                planned_end_date=end,
                priority=order.priority,
                source_type=models.ProductionSourceType.SALES_ORDER,
                source_id=order.id,
                source_line_id=line.id,
                notes=f"Shortfall on {order.code} line {line.line_number} (WP E1b)",
            ),
        )
        return [
            PlannedDocument(
                kind=DOCUMENT_KIND_PRODUCTION,
                id=created.id,
                code=created.code,
                line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                quantity=to_quantity(target),
                status=str(created.status),
            )
        ]

    changes: dict[str, Any] = {}
    if Decimal(keep.planned_quantity) != target:
        changes["planned_quantity"] = target
    if keep.planned_start_date != start:
        changes["planned_start_date"] = start
    if keep.planned_end_date != end:
        changes["planned_end_date"] = end
    if changes:
        keep = await service.update_order(
            session,
            tenant,
            keep.id,
            schemas.ProductionOrderUpdate(**changes),
        )
    return [
        PlannedDocument(
            kind=DOCUMENT_KIND_PRODUCTION,
            id=keep.id,
            code=keep.code,
            line_id=line.id,
            line_number=line.line_number,
            item_id=line.item_id,
            quantity=to_quantity(target),
            status=str(keep.status),
        )
    ]


def _requisition_line_payload(
    source: Any,
    *,
    line_number: int,
    item_id: uuid.UUID,
    quantity: Decimal,
    required_date: date,
    uom_id: uuid.UUID,
    refs: Sequence[Any] | None,
    description: str | None,
    estimated_unit_price: Any = None,
    suggested_vendor_id: uuid.UUID | None = None,
    notes: str | None = None,
) -> Any:
    """Build the wire shape the procurement service validates."""
    return source(
        line_number=line_number,
        item_id=item_id,
        description=description,
        quantity=quantity,
        uom_id=uom_id,
        required_date=required_date,
        estimated_unit_price=estimated_unit_price,
        suggested_vendor_id=suggested_vendor_id,
        source_demand_refs=list(refs or ()),
        notes=notes,
    )


async def _plan_requisition(
    session: Any,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    purchase: Sequence[tuple[SalesOrderLine, Any, Decimal]],
    header: Any | None,
    existing_lines: Sequence[Any],
    reason: str,
    warnings: list[str],
) -> list[PlannedDocument]:
    """Create or resize the order's one DRAFT requisition.

    ``purchase`` is the lines that should be bought, already net of committed
    supply. A submitted requisition is never rewritten: the response carries a
    warning naming each line it could not cover, because cancelling or amending
    it is a buyer's decision.
    """
    models = _models("src.modules.procurement.models")
    schemas = _models("src.modules.procurement.schemas")
    service = _models("src.modules.procurement.service")

    ref_type = schemas.SourceDemandRef
    line_schema = schemas.RequisitionLineIn

    def refs_for(line: SalesOrderLine) -> list[Any]:
        return [
            ref_type(
                demand_type="SALES_ORDER",
                demand_id=order.id,
                demand_line_id=line.id,
            )
        ]

    required = max(order.required_date, today())

    if header is not None and str(header.status) != str(models.RequisitionStatus.DRAFT):
        for line, item, _target in purchase:
            warnings.append(
                f"Line {line.line_number} ({getattr(item, 'code', line.item_id)}): "
                f"requisition {header.code} is {header.status} and cannot be amended, "
                "so no new line was added."
            )
        return []

    if header is None:
        if not purchase:
            return []
        payload = schemas.RequisitionCreate(
            required_date=required,
            warehouse_id=order.warehouse_id,
            priority=order.priority,
            purpose=f"Shortfall on {order.code}",
            notes=reason,
            source_type=models.PurchaseSourceType.SALES_ORDER,
            source_id=order.id,
            lines=[
                _requisition_line_payload(
                    line_schema,
                    line_number=index,
                    item_id=line.item_id,
                    quantity=target,
                    required_date=max(line.required_date or order.required_date, today()),
                    uom_id=item.base_uom_id,
                    refs=refs_for(line),
                    description=None,
                )
                for index, (line, item, target) in enumerate(purchase, start=1)
            ],
        )
        created, _lines = await service.create_requisition_for_planning(session, tenant, payload)
        return [
            PlannedDocument(
                kind=DOCUMENT_KIND_REQUISITION,
                id=created.id,
                code=created.code,
                line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                quantity=to_quantity(target),
                status=str(created.status),
            )
            for line, _item, target in purchase
        ]

    # The existing document stays the one document for this order. Typed lines -
    # those without demand refs - are preserved exactly; planner lines are
    # recomputed from the current shortfall, and a line whose item is bought no
    # longer is dropped.
    wanted_by_line = {line.id: (line, item, target) for line, item, target in purchase}
    payload_lines: list[Any] = []
    covered: list[tuple[SalesOrderLine, Any, Decimal]] = []
    next_number = 1
    seen_line_ids: set[uuid.UUID] = set()

    for existing in sorted(existing_lines, key=lambda row: row.line_number):
        ref_line = _ref_line_id(existing.source_demand_refs, order.id)
        if ref_line is None:
            # A human's typed line. Item, quantity and price are untouched.
            existing_refs = [ref_type(**ref) for ref in (existing.source_demand_refs or [])]
            payload_lines.append(
                _requisition_line_payload(
                    line_schema,
                    line_number=next_number,
                    item_id=existing.item_id,
                    quantity=Decimal(existing.quantity),
                    required_date=existing.required_date or required,
                    uom_id=existing.uom_id,
                    refs=existing_refs,
                    description=existing.description,
                    estimated_unit_price=existing.estimated_unit_price,
                    suggested_vendor_id=existing.suggested_vendor_id,
                    notes=existing.notes,
                )
            )
            next_number += 1
            continue
        wanted = wanted_by_line.get(ref_line)
        if wanted is None:
            # No longer short, or reclassified away from purchase: the planner's
            # line goes; the document itself stays if anything else remains.
            continue
        line, item, target = wanted
        seen_line_ids.add(line.id)
        payload_lines.append(
            _requisition_line_payload(
                line_schema,
                line_number=next_number,
                item_id=line.item_id,
                quantity=target,
                required_date=max(line.required_date or order.required_date, today()),
                uom_id=existing.uom_id,
                refs=refs_for(line),
                description=existing.description,
                estimated_unit_price=existing.estimated_unit_price,
                suggested_vendor_id=existing.suggested_vendor_id,
                notes=existing.notes,
            )
        )
        covered.append((line, item, target))
        next_number += 1

    for line, item, target in purchase:
        if line.id in seen_line_ids:
            continue
        payload_lines.append(
            _requisition_line_payload(
                line_schema,
                line_number=next_number,
                item_id=line.item_id,
                quantity=target,
                required_date=max(line.required_date or order.required_date, today()),
                uom_id=item.base_uom_id,
                refs=refs_for(line),
                description=None,
            )
        )
        covered.append((line, item, target))
        next_number += 1

    if not payload_lines:
        await service.delete_draft_requisition(session, tenant, header.id, reason=reason)
        return []

    updated, _lines = await service.update_requisition(
        session,
        tenant,
        header.id,
        schemas.RequisitionUpdate(
            required_date=required,
            priority=order.priority,
            purpose=f"Shortfall on {order.code}",
            lines=payload_lines,
        ),
    )
    return [
        PlannedDocument(
            kind=DOCUMENT_KIND_REQUISITION,
            id=updated.id,
            code=updated.code,
            line_id=line.id,
            line_number=line.line_number,
            item_id=line.item_id,
            quantity=to_quantity(target),
            status=str(updated.status),
        )
        for line, _item, target in covered
    ]


async def _withdraw_production_drafts(
    session: Any,
    tenant: TenantContext,
    drafts: Sequence[Any],
    *,
    reason: str,
) -> None:
    """Cancel draft jobs the item master no longer justifies.

    A draft is the planner's own document, so withdrawing it is not a business
    decision; a submitted or released job is history and is left alone.
    """
    if not drafts:
        return
    schemas = _models("src.modules.production.schemas")
    service = _models("src.modules.production.service")
    for draft in drafts:
        await service.cancel_order(session, tenant, draft.id, schemas.CancelRequest(reason=reason))


async def plan_sales_order_shortfall(
    session: Any,
    tenant: TenantContext,
    *,
    order: SalesOrder,
    lines: Sequence[SalesOrderLine],
    shortfall: Mapping[uuid.UUID, Decimal],
    reason: str,
) -> ShortfallPlan:
    """Plan one order's shortfall into DRAFT supply.

    Runs in the caller's transaction: the drafts commit with the confirmation or
    the edit that caused them, and roll back with it. Every write goes through
    the owning module's public service, so its validation, numbering and audit
    rules apply exactly as they do to a hand-raised document.
    """
    producing = [
        line
        for line in lines
        if SalesOrderLineStatus(line.status) is not SalesOrderLineStatus.CANCELLED
    ]
    line_by_id = {line.id: line for line in lines}
    demanded = {line.id for line in producing if Decimal(shortfall.get(line.id, ZERO)) > ZERO}

    production_drafts = await _production_documents(session, tenant, order.id)
    header, existing_lines = await _requisition_documents(session, tenant, order.id)

    committed: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    draft_by_line: dict[uuid.UUID, list[Any]] = defaultdict(list)

    for document in production_drafts:
        line_id = document.source_line_id
        if line_id is None or line_id not in line_by_id:
            continue
        status = str(document.status)
        if status == "DRAFT":
            draft_by_line[line_id].append(document)
        elif status in COMMITTED_PRODUCTION_STATUSES:
            committed[line_id] += _remaining(
                document.planned_quantity,
                document.produced_quantity,
                document.rejected_quantity,
            )

    requisition_status = str(header.status) if header is not None else None
    requisition_is_draft = requisition_status == "DRAFT"
    for existing in existing_lines:
        line_id = _ref_line_id(existing.source_demand_refs, order.id)
        if line_id is None or line_id not in line_by_id:
            continue
        if requisition_status in DEAD_REQUISITION_STATUSES:
            continue
        if requisition_is_draft:
            # Owned by the planner: rewritten below, not counted as supply.
            continue
        if (
            requisition_status in COMMITTED_REQUISITION_STATUSES
            and str(existing.status) in OPEN_REQUISITION_LINE_STATUSES
        ):
            committed[line_id] += max(
                Decimal(existing.quantity) - Decimal(existing.ordered_quantity), ZERO
            )

    item_ids = {
        line_by_id[line_id].item_id
        for line_id in set(demanded) | set(draft_by_line)
        if line_id in line_by_id
    }
    source = DatabaseMrpSource(session, tenant)
    items = await source.planning_items(sorted(item_ids))
    structures = await source.bom_structures(sorted(item_ids), today())

    documents: list[PlannedDocument] = []
    job_work: list[JobWorkNeed] = []
    warnings: list[str] = []
    purchase: list[tuple[SalesOrderLine, Any, Decimal]] = []

    for line in lines:
        target = to_quantity(
            max(Decimal(shortfall.get(line.id, ZERO)) - committed.get(line.id, ZERO), ZERO)
        )
        drafts = draft_by_line.get(line.id, [])
        item = items.get(line.item_id)
        if item is None:
            if drafts:
                warnings.append(
                    f"Line {line.line_number}: the item no longer exists in the item "
                    "master, so its draft supply could not be replanned."
                )
            continue

        has_default_bom = bool(structures.get(line.item_id) and structures[line.item_id].is_default)
        choice = engine.choose_suggestion_type(item, has_default_bom=has_default_bom)

        # A suggestion the master data does not permit is reported, never
        # forced. Confirmation is the customer-facing transaction; a missing
        # capability flag is the planner's problem to fix, not a reason to
        # refuse the order. Any draft raised before the flag changed is
        # withdrawn because it is the planner's own document.
        if choice is MrpSuggestionType.MANUFACTURE and not item.can_manufacture:
            await _withdraw_production_drafts(session, tenant, drafts, reason=reason)
            warnings.append(
                f"Line {line.line_number} ({item.code}): the item master says this part "
                "is made, but it is not flagged as manufacturable, so no production "
                "order was raised."
            )
            continue
        if choice is MrpSuggestionType.JOB_WORK and not item.can_job_work:
            await _withdraw_production_drafts(session, tenant, drafts, reason=reason)
            warnings.append(
                f"Line {line.line_number} ({item.code}): the item master says this part "
                "is job-worked, but it is not flagged for job work, so no supply was "
                "planned."
            )
            continue
        if choice is MrpSuggestionType.PURCHASE and not item.can_purchase:
            await _withdraw_production_drafts(session, tenant, drafts, reason=reason)
            warnings.append(
                f"Line {line.line_number} ({item.code}): the item master says this part "
                "is bought, but it is not flagged as purchasable, so no requisition "
                "was raised."
            )
            continue

        if choice is MrpSuggestionType.MANUFACTURE:
            documents.extend(
                await _plan_production(
                    session,
                    tenant,
                    order=order,
                    line=line,
                    item=item,
                    target=target,
                    drafts=drafts,
                    reason=reason,
                )
            )
        elif choice is MrpSuggestionType.JOB_WORK:
            # Nothing is created; a previously raised production draft no longer
            # matches the item master and is withdrawn.
            await _withdraw_production_drafts(session, tenant, drafts, reason=reason)
            if target > ZERO:
                job_work.append(
                    JobWorkNeed(
                        line_id=line.id,
                        line_number=line.line_number,
                        item_id=line.item_id,
                        item_code=getattr(item, "code", None),
                        quantity=target,
                    )
                )
        else:
            await _withdraw_production_drafts(session, tenant, drafts, reason=reason)
            if target > ZERO:
                purchase.append((line, item, target))

    documents.extend(
        await _plan_requisition(
            session,
            tenant,
            order=order,
            purchase=purchase,
            header=header,
            existing_lines=existing_lines,
            reason=reason,
            warnings=warnings,
        )
    )

    return ShortfallPlan(
        documents=tuple(documents),
        needs_job_work=tuple(job_work),
        warnings=tuple(warnings),
    )


__all__ = [
    "DOCUMENT_KIND_PRODUCTION",
    "DOCUMENT_KIND_REQUISITION",
    "JobWorkNeed",
    "PlannedDocument",
    "ShortfallPlan",
    "plan_sales_order_shortfall",
]
