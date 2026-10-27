"""Procurement business rules and transaction boundaries.

The rules that matter, and why each one lives here rather than in the caller:

1. **A purchase order that duplicates an open one is refused.** Master rules
   section 16: same vendor, same item, same warehouse, overlapping delivery
   window, or - strongest of all - the same source demand. Creating it anyway
   needs an explicit acknowledgement from somebody holding
   ``purchase_order.approve``, and the reason is written onto the document. The
   matching itself is pure and lives in :mod:`service_rules`, so the rule can be
   argued with the business without a database.
2. **Posting a goods receipt is one transaction or none of it.** Tolerance
   check, stock movement, QC routing, purchase-order advancement and every audit
   row commit together. A receipt that moved stock but left the order at SENT is
   the defect this structure makes impossible.
3. **Over-receipt is bounded by a named constant, checked here and again by a
   CHECK constraint.** Application code is not trusted alone with the number a
   vendor gets to invoice for (section 17).
4. **Quantity that requires incoming inspection is received into ``QC_HOLD``,
   not ``AVAILABLE``.** It is physically present and must never be promised to a
   sales order until quality has released it (database.md section 8).
5. **The quotation recommendation is advice with its reasoning attached**, and
   the buyer's own decision is what gets recorded (section 15).
6. **An approved requisition splits into one DRAFT order per (vendor,
   procurement category, warehouse)**, each handed to the vendor manager scoped
   to its category - all in one transaction, or none of it when any line cannot
   be resolved. Every rule on the way is pure (``service_rules``); this module
   loads what they read in one statement per entity type.

The quality module is reached through a guarded import. When it is deployed,
posting raises the incoming inspection inside the same transaction; when it is
not, the receipt line is left flagged ``PENDING`` with the quantity already on
QC hold, which is exactly the work queue quality picks up when it lands.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.common.warehouse_scope import assert_warehouse_scope
from src.core.actors import actor_id
from src.core.clock import today, utcnow
from src.core.constants import DEFAULT_CURRENCY
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission, resolve_role_grants
from src.core.responses import OffsetPagination
from src.core.types import ZERO, to_money, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.schemas import StockBatchCreate
from src.modules.inventory.service import MovementRequest, TraceabilityService, post_movements
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify
from src.modules.procurement.demand_provenance import canonical_demand_refs
from src.modules.procurement.inbound_repository import po_is_late
from src.modules.procurement.models import (
    DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
    EDITABLE_PO_STATUSES,
    OPEN_REQUISITION_STATUSES,
    RECEIVABLE_PO_STATUSES,
    GoodsReceipt,
    GoodsReceiptLine,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    PurchaseReturn,
    PurchaseReturnLine,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    ReceiptLineQcStatus,
    RequisitionLineStatus,
    RequisitionStatus,
    Rfq,
    RfqLine,
    RfqStatus,
    VendorQuotation,
    VendorQuotationLine,
)
from src.modules.procurement.repository import (
    BatchLookupRepository,
    CategoryFacts,
    GoodsReceiptLineRepository,
    GoodsReceiptRepository,
    ItemCategoryLookupRepository,
    ItemVendorLinkRepository,
    OpenOrderLine,
    OrganizationLookupRepository,
    PendingApprovalRepository,
    PurchasableItem,
    PurchasableItemRepository,
    PurchaseOrderLineRepository,
    PurchaseOrderRepository,
    PurchaseReturnLineRepository,
    PurchaseReturnRepository,
    QuotationLineRepository,
    QuotationRepository,
    RequisitionLineRepository,
    RequisitionRepository,
    RfqLineRepository,
    RfqRepository,
    VendorAssigneeRepository,
    VendorCategoryLookupRepository,
    VendorFacts,
    VendorLookupRepository,
    VendorUserFacts,
    WarehouseLookupRepository,
)
from src.modules.procurement.schemas import (
    MAX_LINES_PER_DOCUMENT,
    ComparisonItemRow,
    ComparisonOffer,
    ComparisonVendorColumn,
    DuplicateCandidateRead,
    DuplicateCheckRequest,
    DuplicateCheckResult,
    GoodsReceiptCreate,
    GoodsReceiptDetail,
    GoodsReceiptFilters,
    GoodsReceiptLineRead,
    GoodsReceiptPostResult,
    GoodsReceiptSummary,
    PendingApproval,
    PendingApprovalFilters,
    PostedMovement,
    PurchaseOrderCreate,
    PurchaseOrderDetail,
    PurchaseOrderFilters,
    PurchaseOrderLineIn,
    PurchaseOrderLineRead,
    PurchaseOrderSummary,
    PurchaseOrderTracking,
    PurchaseOrderUpdate,
    PurchaseOrderVendorAssignee,
    PurchaseReturnCreate,
    PurchaseReturnDetail,
    PurchaseReturnFilters,
    PurchaseReturnLineRead,
    PurchaseReturnPostResult,
    PurchaseReturnSummary,
    QuotationComparison,
    QuotationCreate,
    QuotationDetail,
    QuotationFilters,
    QuotationLineIn,
    QuotationLineRead,
    QuotationSummary,
    QuotationUpdate,
    RequisitionConversionGroup,
    RequisitionConversionLine,
    RequisitionConversionLineOverride,
    RequisitionConversionRequest,
    RequisitionConversionResult,
    RequisitionConversionUnresolvedLine,
    RequisitionCreate,
    RequisitionDetail,
    RequisitionFilters,
    RequisitionLineIn,
    RequisitionLineRead,
    RequisitionListRow,
    RequisitionSummary,
    RequisitionUpdate,
    RfqCreate,
    RfqDetail,
    RfqFilters,
    RfqLineIn,
    RfqLineRead,
    RfqSummary,
    RfqUpdate,
    TrackedReceipt,
)
from src.modules.procurement.service_rules import (
    DEFAULT_SCORE_WEIGHTS,
    GOODS_RECEIPT_LIFECYCLE,
    MAX_ORDERS_PER_CONVERSION,
    PURCHASE_ORDER_LIFECYCLE,
    PURCHASE_RETURN_LIFECYCLE,
    QUOTATION_LIFECYCLE,
    REQUISITION_LIFECYCLE,
    REQUOTABLE_QUOTATION_STATUSES,
    RETURN_POOL_AVAILABLE,
    RETURN_POOL_DAMAGED,
    RFQ_LIFECYCLE,
    VENDOR_PO_RESPOND_PERMISSION,
    AssigneeCandidate,
    CandidateInput,
    ConversionGroup,
    DuplicateCandidate,
    InspectionOutcome,
    LineAmounts,
    OfferInput,
    PlannedConversionLine,
    ProposedLine,
    ReturnPoolFacts,
    ScoreWeights,
    VendorAssigneeReason,
    assert_within_order_limit,
    assert_within_over_receipt_tolerance,
    category_lineage,
    choose_vendor_assignee,
    classify_candidates,
    compute_line_amounts,
    conversion_delivery_date,
    duplicate_refusal,
    effective_unit_price,
    group_conversion_lines,
    lineage_from,
    may_take_order,
    order_is_fully_received,
    po_line_status_for,
    preferred_category_vendor,
    rank_offers,
    receipt_line_qc_status,
    receipt_unit_cost,
    recommendation_reason,
    requisition_line_status_for,
    resolve_line_price,
    resolve_line_vendor,
    resolve_procurement_category,
    resolve_return_pool,
    scope_covers_category,
    single_order_category,
    sum_document_totals,
)
from src.modules.vendors.models import Vendor
from src.modules.vendors.service import can_receive_purchase_orders

log = get_logger(__name__)

# --- audit entity names -------------------------------------------------------

AUDIT_REQUISITION = "purchase_requisition"
AUDIT_RFQ = "rfq"
AUDIT_QUOTATION = "vendor_quotation"
AUDIT_PURCHASE_ORDER = "purchase_order"
AUDIT_GOODS_RECEIPT = "goods_receipt"
AUDIT_PURCHASE_RETURN = "purchase_return"

#: Reference type written onto every inventory ledger row this module posts, so
#: "what did this GRN move?" is one indexed lookup (database.md section 4.2).
MOVEMENT_REFERENCE_GRN = "goods_receipt"
MOVEMENT_REFERENCE_RETURN = "purchase_return"

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
ORDER_AUDITED_FIELDS = [
    "code",
    "vendor_id",
    "po_date",
    "expected_delivery_date",
    "warehouse_id",
    "branch_id",
    "status",
    "priority",
    "currency",
    "subtotal",
    "discount",
    "tax_amount",
    "freight_amount",
    "total_amount",
    "payment_terms_days",
    "delivery_address",
    "terms",
    "source_type",
    "source_id",
    "assigned_vendor_user_id",
    "item_category_id",
    "duplicate_acknowledged",
    "notes",
]

REQUISITION_AUDITED_FIELDS = [
    "code",
    "requisition_date",
    "required_date",
    "warehouse_id",
    "department_id",
    "branch_id",
    "status",
    "priority",
    "requested_by_id",
    "source_type",
    "source_id",
    "purpose",
    "notes",
]

# ASSUMPTION: the approver of a procurement document may not be the person who
# raised it, unless they are a platform superuser or hold SUPER_ADMIN. Approval
# is evidence of a second person's judgement (master rules section 42), and a
# purchase order signed off by the buyer who raised it is not evidence of
# anything. Recorded in docs/assumptions.md section 2; one predicate, in one
# place, if a small team wants it relaxed.
SUPER_ADMIN_ROLE_CODE = "SUPER_ADMIN"


def can_approve(principal: Principal, created_by_id: uuid.UUID | None) -> bool:
    """Whether this principal may approve a document raised by ``created_by_id``."""
    if principal.is_superuser or SUPER_ADMIN_ROLE_CODE in principal.role_codes:
        return True
    return created_by_id is None or created_by_id != principal.user_id


def _assert_separate_approver(principal: Principal, created_by_id: uuid.UUID | None) -> None:
    if not can_approve(principal, created_by_id):
        raise AuthorizationError(
            "A document cannot be approved by the person who raised it.",
            code=ErrorCode.APPROVAL_REQUIRED,
        )


# =============================================================================
# Quality integration - guarded import (database.md section 8)
# =============================================================================


class QualityPort(Protocol):
    """What procurement asks of the quality module when it is deployed.

    Deliberately narrow: one call, raising one incoming inspection against one
    received line. Anything wider would couple the receipt path to a module
    that is built independently of it.
    """

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        quantity: Decimal,
        warehouse_id: uuid.UUID,
        reference_type: str,
        reference_id: uuid.UUID,
        reference_code: str,
        vendor_id: uuid.UUID | None = None,
        batch_id: uuid.UUID | None = None,
    ) -> Any: ...


def resolve_quality_port() -> QualityPort | None:
    """The quality module's inspection factory, if it is deployed.

    Only the *absence* of the module is tolerated. A failure inside it is not
    swallowed: an inspection that could not be raised must roll the receipt back
    with everything else, or the stock sits on QC hold with nobody told to look
    at it.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    factory = getattr(quality_service, "create_incoming_inspection", None)
    return factory if callable(factory) else None


class InspectionReaderPort(Protocol):
    """The quality module's read of every inspection one document raised."""

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        reference_type: str,
        reference_id: uuid.UUID,
    ) -> Sequence[Any]: ...


def resolve_inspection_reader_port() -> InspectionReaderPort | None:
    """The quality module's ``inspections_for_reference``, if it is deployed.

    Read when a purchase return is drawn against a line that was received into
    QC hold: only an inspection can have moved that stock on, and the pool it
    moved it to is what the return must leave.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    reader = getattr(quality_service, "inspections_for_reference", None)
    return reader if callable(reader) else None


def _inspection_outcome(inspection: Any) -> InspectionOutcome | None:
    """Read an inspection row's disposition without importing quality's types.

    ``rework`` and ``hold`` are both quantity that never left QC hold. A row
    with no disposition at all is one that has not been decided.
    """
    if inspection is None:
        return None

    def quantity(name: str) -> Decimal:
        return Decimal(getattr(inspection, name, 0) or 0)

    return InspectionOutcome(
        accepted=quantity("accepted_quantity"),
        rejected=quantity("rejected_quantity"),
        still_held=quantity("rework_quantity") + quantity("hold_quantity"),
        scrap=quantity("scrap_quantity"),
    )


async def _return_pool_facts(
    session: AsyncSession,
    tenant: TenantContext,
    receipt: GoodsReceipt,
    receipt_lines: Sequence[GoodsReceiptLine],
) -> dict[uuid.UUID, ReturnPoolFacts]:
    """What each receipt line still holds, per pool, for the return rules.

    Two reads for the whole document: the quantity earlier returns already took
    from each pool, and - only when a line was held for inspection - the
    inspections the receipt raised. A held line whose inspection cannot be
    found (none raised, quality absent, or undecided) is reported with no
    outcome, which the rule refuses as "still on QC hold".
    """
    returned = await PurchaseReturnLineRepository(session, tenant).posted_quantities_by_pool(
        [line.id for line in receipt_lines]
    )
    inspections: dict[uuid.UUID, Any] = {}
    if any(line.requires_qc for line in receipt_lines):
        reader = resolve_inspection_reader_port()
        if reader is not None:
            for inspection in await reader(session, tenant, MOVEMENT_REFERENCE_GRN, receipt.id):
                inspections[inspection.id] = inspection

    return {
        line.id: ReturnPoolFacts(
            accepted_quantity=Decimal(line.accepted_quantity),
            held_for_qc=bool(line.requires_qc),
            inspection=_inspection_outcome(
                inspections.get(line.qc_inspection_id) if line.qc_inspection_id else None
            ),
            returned_available=returned.get((line.id, RETURN_POOL_AVAILABLE), ZERO),
            returned_damaged=returned.get((line.id, RETURN_POOL_DAMAGED), ZERO),
        )
        for line in receipt_lines
    }


# =============================================================================
# Shared helpers
# =============================================================================


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    machine: Any,
    entity: Any,
    entity_type: str,
    entity_code: str | None,
    target: Any,
    reason: str,
    status_enum: Any,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> None:
    """Move a document's status and record it.

    Every status change in this module funnels through here, so there is exactly
    one place that writes a procurement status column and exactly one place that
    writes the matching audit row (master rules section 73).
    """
    current = status_enum(entity.status)
    machine.assert_transition(current, target)
    entity.status = target
    if hasattr(entity, "updated_by_id"):
        # NULL for the system actor a reconciliation sweep carries - the nil
        # placeholder is not a user (src.core.actors).
        entity.updated_by_id = actor_id(tenant.user_id)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=entity_type,
        entity_id=entity.id,
        entity_code=entity_code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )


async def _load_items(
    session: AsyncSession, tenant: TenantContext, item_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, PurchasableItem]:
    """Batched item facts, with every unknown id reported at once."""
    items = await PurchasableItemRepository(session, tenant).load(item_ids)
    missing = [i for i in dict.fromkeys(item_ids) if i not in items]
    if missing:
        raise NotFoundError("Item", ", ".join(str(i) for i in missing))
    return items


def _assert_purchasable(items: dict[uuid.UUID, PurchasableItem]) -> None:
    """Master rules section 1: the capability flag decides, never the item group."""
    blocked = [i.code for i in items.values() if not i.can_purchase]
    if blocked:
        raise BusinessRuleError(
            f"These items are not flagged as purchasable: {', '.join(sorted(blocked))}.",
            details={"item_codes": sorted(blocked)},
        )
    inactive = [i.code for i in items.values() if not i.is_active]
    if inactive:
        raise BusinessRuleError(
            f"These items are inactive and cannot be ordered: {', '.join(sorted(inactive))}.",
            details={"item_codes": sorted(inactive)},
        )


async def _assert_warehouse(
    session: AsyncSession, tenant: TenantContext, warehouse_id: uuid.UUID
) -> None:
    if not await WarehouseLookupRepository(session, tenant).exists_in_tenant(warehouse_id):
        raise NotFoundError("Warehouse", warehouse_id)


async def _load_vendor(
    session: AsyncSession, tenant: TenantContext, vendor_id: uuid.UUID
) -> Vendor:
    vendor = await VendorLookupRepository(session, tenant).get(vendor_id)
    if vendor is None:
        raise NotFoundError("Vendor", vendor_id)
    return vendor


def _assert_orderable(vendor: Vendor) -> None:
    # The predicate lives in the vendors module so no caller re-derives
    # "approved and not switched off" and gets one of the three flags wrong.
    if not can_receive_purchase_orders(vendor):
        raise BusinessRuleError(
            f"Vendor {vendor.code} is not approved for purchase orders.",
            details={"vendor_id": str(vendor.id), "vendor_code": vendor.code},
        )


async def _assert_organisation(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> None:
    """A branch or department a client names must be a live row of this tenant.

    The foreign key alone accepts another company's branch - ``branches.id`` is
    globally unique - so the tenant check is made here, through the
    organization module's tables, which sit below procurement.
    """
    probes = OrganizationLookupRepository(session, tenant)
    if branch_id is not None and not await probes.branch_exists(branch_id):
        raise NotFoundError("Branch", branch_id)
    if department_id is not None and not await probes.department_exists(department_id):
        raise NotFoundError("Department", department_id)


async def _assert_suggested_vendors(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[RequisitionLineIn]
) -> None:
    """A vendor a requisition line suggests must be a live vendor of this tenant.

    The requisition split reads the suggestion as its second vendor source
    (docs/assumptions.md §51), and a foreign key to ``vendors.id`` is
    satisfied by another company's vendor - so the tenant check is made here,
    once, in one statement. Approval is not required: a suggestion is the
    requester's hint, and the conversion checks the vendor can take orders when
    it acts on it.
    """
    named = sorted(
        {line.suggested_vendor_id for line in lines if line.suggested_vendor_id is not None},
        key=str,
    )
    if not named:
        return
    known = await VendorLookupRepository(session, tenant).load(named)
    missing = [
        str(vendor_id)
        for vendor_id in named
        if vendor_id not in known or known[vendor_id].is_archived
    ]
    if missing:
        raise NotFoundError("Vendor", ", ".join(missing))


async def _lock_requisition_sources(
    session: AsyncSession, tenant: TenantContext, requisition_line_ids: Iterable[uuid.UUID]
) -> tuple[dict[uuid.UUID, PurchaseRequisition], dict[uuid.UUID, PurchaseRequisitionLine]]:
    """Lock the requisitions and lines order lines draw on: headers first, both by id.

    Every path that moves a requisition's ordered quantity takes these locks in
    this order - the conversion, a buyer's own purchase order, and an edit that
    rewrites an order's lines - so two of them never hold one half each. The
    headers are found with a column-only read, so the lines are first loaded by
    the locking statement itself and the values read are the locked ones.
    Unknown and foreign ids are simply absent from the result.
    """
    line_ids = list(dict.fromkeys(requisition_line_ids))
    if not line_ids:
        return {}, {}
    line_repo = RequisitionLineRepository(session, tenant)
    header_ids = await line_repo.requisition_ids_of(line_ids)
    requisitions = await RequisitionRepository(session, tenant).lock_many(header_ids)
    lines = await line_repo.lock_many(line_ids)
    return {r.id: r for r in requisitions}, {line.id: line for line in lines}


def _assert_requisition_links(
    lines: Sequence[PurchaseOrderLineIn],
    requisitions: dict[uuid.UUID, PurchaseRequisition],
    source_lines: dict[uuid.UUID, PurchaseRequisitionLine],
    *,
    already_linked: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Every requisition line an order line claims: this tenant's, approved, same item.

    A foreign key to ``purchase_requisition_lines.id`` proves the row exists
    somewhere, not that it is this company's, that its requisition was ever
    approved, or that it asked for the item being ordered. Any of the three
    would push ordered quantity onto demand this order does not satisfy.

    ``already_linked`` are the requisition lines the order claimed before an
    edit: keeping such a link is not ordering against the requisition anew, so
    it survives the requisition being closed in the meantime.
    """
    claimed = [line for line in lines if line.requisition_line_id is not None]
    missing = sorted(
        {str(line.requisition_line_id) for line in claimed}
        - {str(line_id) for line_id in source_lines}
    )
    if missing:
        raise NotFoundError("Purchase requisition line", ", ".join(missing))

    newly_claimed = {
        source_lines[line.requisition_line_id].requisition_id
        for line in claimed
        if line.requisition_line_id is not None and line.requisition_line_id not in already_linked
    }
    unapproved = sorted(
        {
            f"{requisition.code} is {requisition.status}"
            for requisition_id, requisition in requisitions.items()
            if requisition_id in newly_claimed
            and RequisitionStatus(requisition.status) not in OPEN_REQUISITION_STATUSES
        }
    )
    if unapproved:
        raise BusinessRuleError(
            "Purchase orders can only be raised against approved requisitions: "
            + "; ".join(unapproved)
            + ".",
            details={"requisitions": unapproved},
        )

    mismatched = [
        line.line_number
        for line in claimed
        if source_lines[line.requisition_line_id or uuid.UUID(int=0)].item_id != line.item_id
    ]
    if mismatched:
        raise ValidationError(
            "An order line must order the item its requisition line asked for; lines "
            f"{', '.join(str(n) for n in mismatched)} do not.",
            details={"line_numbers": mismatched},
        )


async def _assert_quotation_links(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    lines: Sequence[PurchaseOrderLineIn],
) -> None:
    """Every quotation line an order line claims: this tenant's, selected, same vendor and item."""
    claimed = [line for line in lines if line.quotation_line_id is not None]
    if not claimed:
        return
    quotation_lines = {
        line.id: line
        for line in await QuotationLineRepository(session, tenant).load_many(
            [line.quotation_line_id for line in claimed if line.quotation_line_id is not None]
        )
    }
    missing = sorted(
        {str(line.quotation_line_id) for line in claimed} - {str(i) for i in quotation_lines}
    )
    if missing:
        raise NotFoundError("Vendor quotation line", ", ".join(missing))

    quotations = {
        quotation.id: quotation
        for quotation in await QuotationRepository(session, tenant).get_many(
            sorted({line.quotation_id for line in quotation_lines.values()})
        )
    }
    unselected = sorted(
        quotation.vendor_reference
        for quotation in quotations.values()
        if QuotationStatus(quotation.status) is not QuotationStatus.SELECTED
    )
    if unselected:
        raise BusinessRuleError(
            "Purchase orders can only be priced from a selected quotation: "
            f"{', '.join(unselected)} has not been selected.",
            details={"quotations": unselected},
        )
    if any(quotation.vendor_id != vendor_id for quotation in quotations.values()):
        raise ValidationError(
            "A quotation line can only price an order on the vendor that quoted it.",
            details={"vendor_id": str(vendor_id)},
        )
    mismatched = [
        line.line_number
        for line in claimed
        if quotation_lines[line.quotation_line_id or uuid.UUID(int=0)].item_id != line.item_id
    ]
    if mismatched:
        raise ValidationError(
            "An order line must order the item its quotation line priced; lines "
            f"{', '.join(str(n) for n in mismatched)} do not.",
            details={"line_numbers": mismatched},
        )


async def _assert_order_source(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    source_type: PurchaseSourceType,
    source_id: uuid.UUID | None,
) -> None:
    """The header source, where it names a document this module owns.

    ``source_type``/``source_id`` is a bare pair on purpose (models docstring):
    MRP suggestions and sales orders live in modules procurement does not read.
    A requisition or a quotation is this module's own, and a quotation named
    here is the one ``_link_source_documents`` marks as converted - so both are
    checked like the line links are.
    """
    if source_id is None:
        return
    if source_type is PurchaseSourceType.PURCHASE_REQUISITION:
        requisition = await RequisitionRepository(session, tenant).get(source_id)
        if requisition is None:
            raise NotFoundError("Purchase requisition", source_id)
        if RequisitionStatus(requisition.status) not in OPEN_REQUISITION_STATUSES:
            raise BusinessRuleError(
                f"Requisition {requisition.code} is {requisition.status}; only an approved "
                "requisition can be ordered against.",
                details={"status": str(requisition.status)},
            )
    elif source_type is PurchaseSourceType.VENDOR_QUOTATION:
        quotation = await QuotationRepository(session, tenant).get(source_id)
        if quotation is None:
            raise NotFoundError("Vendor quotation", source_id)
        if QuotationStatus(quotation.status) is not QuotationStatus.SELECTED:
            raise BusinessRuleError(
                f"Quotation {quotation.vendor_reference} is {quotation.status}; an order can "
                "only be raised from a selected quotation.",
                details={"status": str(quotation.status)},
            )
        if quotation.vendor_id != vendor_id:
            raise ValidationError(
                "A quotation can only source an order on the vendor that quoted it.",
                details={"source_id": str(source_id)},
            )


# --- the order's procurement category and its vendor-side assignee ------------


async def _order_category(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    items: dict[uuid.UUID, PurchasableItem],
    item_ids: Sequence[uuid.UUID],
) -> tuple[uuid.UUID | None, tuple[uuid.UUID, ...]]:
    """The procurement category an order's lines share, and that category's lineage.

    Each line resolves against the order's vendor exactly as a converted line
    does (:func:`resolve_procurement_category`); the order carries the result
    when every line agrees and NULL when they do not (docs/assumptions.md
    §50). Two statements whatever the line count.
    """
    item_categories = [items[item_id].category_id for item_id in item_ids]
    known = sorted({c for c in item_categories if c is not None}, key=str)
    facts = await ItemCategoryLookupRepository(session, tenant).load(known)
    qualified = (
        await VendorCategoryLookupRepository(session, tenant).categories_of([vendor_id])
    ).get(vendor_id, frozenset())

    resolved: dict[uuid.UUID | None, tuple[uuid.UUID, ...]] = {}
    for category_id in item_categories:
        lineage = category_lineage(facts[category_id].path) if category_id in facts else ()
        procurement = resolve_procurement_category(lineage, qualified)
        resolved.setdefault(procurement, lineage_from(lineage, procurement))
    chosen = single_order_category(resolved)
    return chosen, resolved.get(chosen, ()) if chosen is not None else ()


async def _category_lineage_of(
    session: AsyncSession, tenant: TenantContext, category_id: uuid.UUID | None
) -> tuple[uuid.UUID, ...]:
    if category_id is None:
        return ()
    facts = await ItemCategoryLookupRepository(session, tenant).load([category_id])
    return category_lineage(facts[category_id].path) if category_id in facts else ()


def _as_assignee_candidate(user: VendorUserFacts) -> AssigneeCandidate:
    """Fold a vendor user's roles, grants and denials into the assignee rule's view.

    The same fold the request path applies (``core.permissions``), so "may act
    on a purchase order" here is exactly what that user's next request will be
    allowed to do.
    """
    granted, _ = resolve_role_grants(user.roles, user.extra_permissions, user.denied_permissions)
    return AssigneeCandidate(
        user_id=user.id,
        vendor_id=user.vendor_id,
        is_active=user.is_active,
        is_deleted=False,
        can_respond=user.is_superuser or str(VENDOR_PO_RESPOND_PERMISSION) in granted,
        allowed_category_ids=user.allowed_category_ids,
    )


def _assignee_refusal(user_id: uuid.UUID, message: str) -> ValidationError:
    return ValidationError(
        message,
        details={
            "assigned_vendor_user_id": str(user_id),
            "fields": {"assigned_vendor_user_id": [message]},
        },
    )


async def _assert_vendor_assignee(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    user_id: uuid.UUID,
    vendor_id: uuid.UUID,
    category_lineage_: Sequence[uuid.UUID],
) -> None:
    """Refuse an assignee who cannot take this order.

    The user must exist in this tenant (else "not found", the same answer as a
    foreign id), be active, belong to the order's vendor, hold the vendor-side
    order right, and either be unrestricted or be scoped to the order's
    category or an ancestor of it. A foreign key to ``users.id`` checks none of
    that, and an order assigned to the wrong person is an order nobody at the
    vendor will see.
    """
    user = await VendorAssigneeRepository(session, tenant).one(user_id)
    if user is None:
        raise NotFoundError("User", user_id)
    candidate = _as_assignee_candidate(user)
    if candidate.vendor_id != vendor_id:
        raise _assignee_refusal(user_id, f"{user.full_name} is not a user of this order's vendor.")
    if not candidate.is_active:
        raise _assignee_refusal(user_id, f"{user.full_name}'s account is deactivated.")
    if not candidate.can_respond:
        raise _assignee_refusal(
            user_id, f"{user.full_name} has no right to act on purchase orders in the portal."
        )
    if candidate.allowed_category_ids and not scope_covers_category(
        candidate.allowed_category_ids, category_lineage_
    ):
        raise _assignee_refusal(
            user_id, f"{user.full_name} is not responsible for this order's item category."
        )


async def _assignee_still_fits(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    user_id: uuid.UUID,
    vendor_id: uuid.UUID,
    category_lineage_: Sequence[uuid.UUID],
) -> bool:
    user = await VendorAssigneeRepository(session, tenant).one(user_id)
    return user is not None and may_take_order(
        _as_assignee_candidate(user), vendor_id=vendor_id, category_lineage=category_lineage_
    )


# =============================================================================
# Purchase requisition
# =============================================================================


def requisition_detail(
    requisition: PurchaseRequisition, lines: Sequence[PurchaseRequisitionLine]
) -> RequisitionDetail:
    detail = RequisitionDetail.model_validate(requisition)
    detail.lines = [RequisitionLineRead.model_validate(line) for line in lines]
    return detail


def _apply_requisition_line(
    line: PurchaseRequisitionLine, payload: RequisitionLineIn, item: PurchasableItem
) -> PurchaseRequisitionLine:
    line.line_number = payload.line_number
    line.item_id = payload.item_id
    line.description = payload.description or item.name
    line.quantity = to_quantity(payload.quantity)
    line.uom_id = payload.uom_id or item.order_uom_id
    line.required_date = payload.required_date
    line.estimated_unit_price = payload.estimated_unit_price
    line.suggested_vendor_id = payload.suggested_vendor_id
    line.source_demand_refs = (
        [ref.model_dump(mode="json") for ref in payload.source_demand_refs]
        if payload.source_demand_refs
        else None
    )
    line.notes = payload.notes
    line.status = requisition_line_status_for(
        quantity=line.quantity, ordered_quantity=line.ordered_quantity or ZERO
    )
    return line


async def create_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payload: RequisitionCreate,
) -> tuple[PurchaseRequisition, list[PurchaseRequisitionLine]]:
    """Raise a DRAFT requisition. Nothing is committed to a vendor yet."""
    return await _create_requisition(
        session,
        tenant,
        payload,
        department_id=principal.department_id,
        branch_id=principal.branch_id,
    )


async def create_requisition_for_planning(
    session: AsyncSession,
    tenant: TenantContext,
    payload: RequisitionCreate,
) -> tuple[PurchaseRequisition, list[PurchaseRequisitionLine]]:
    """Raise a DRAFT requisition on the planner's behalf (WP E1b).

    The sales-order planner has a tenant but no requester: it is automation
    running inside the confirmation transaction, not a person. So the document
    carries no department or branch fallback - the caller names what it knows -
    and a buyer still reviews and submits it, exactly like a hand-raised one.

    Deliberately separate from :func:`create_requisition`, which derives the
    fallbacks from a verified principal: passing a synthetic principal here
    would put a person's name on a document they never saw.
    """
    return await _create_requisition(session, tenant, payload, department_id=None, branch_id=None)


async def create_requisition_from_mrp(
    session: AsyncSession, tenant: TenantContext, request: Any
) -> tuple[uuid.UUID, str]:
    """Planning's conversion hook (``mrp.service.CONVERSION_HOOKS``).

    Resolved by name at call time by the MRP module, which is why the request is
    typed loosely here: importing ``mrp.schemas`` would make procurement depend
    on a module that is built independently of it and may not be deployed.

    Runs inside the caller's transaction, so the suggestion and the requisition
    it became commit together or not at all. The requisition is raised as a
    DRAFT under the planner's own name - the buyer still reviews and submits it,
    exactly as a hand-raised one.
    """
    demand_refs = canonical_demand_refs(getattr(request, "source_demands", None))
    if not demand_refs:
        raise BusinessRuleError(
            "Planning demand links are missing. Rerun the plan before creating a "
            "purchase requisition.",
            details={"field": "source_demands"},
        )
    items = await _load_items(session, tenant, [request.item_id])
    item = items[request.item_id]

    warehouse_id = (
        request.warehouse_ids[0] if request.warehouse_ids else None
    ) or item.default_warehouse_id
    if warehouse_id is None:
        raise BusinessRuleError(
            f"Item {item.code} has no default warehouse and the planning run named none, "
            "so there is nowhere to receive the purchase.",
            details={"item_code": item.code, "suggestion_id": str(request.suggestion_id)},
        )

    requisition, _ = await _create_requisition(
        session,
        tenant,
        RequisitionCreate(
            required_date=request.required_date,
            warehouse_id=warehouse_id,
            source_type=PurchaseSourceType.MRP_SUGGESTION,
            source_id=request.suggestion_id,
            notes=request.notes,
            lines=[
                RequisitionLineIn(
                    line_number=1,
                    item_id=request.item_id,
                    quantity=to_quantity(request.quantity),
                    required_date=request.required_date,
                    source_demand_refs=demand_refs,
                )
            ],
        ),
        department_id=None,
        branch_id=None,
    )
    return requisition.id, requisition.code


async def _create_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    payload: RequisitionCreate,
    *,
    department_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
) -> tuple[PurchaseRequisition, list[PurchaseRequisitionLine]]:
    """The shared body of :func:`create_requisition` and the MRP hook.

    ``department_id`` and ``branch_id`` are the fallbacks for a payload that
    names neither - the requester's own for a hand-raised document, nothing for
    one raised by planning, which carries no principal.
    """
    repo = RequisitionRepository(session, tenant)
    line_repo = RequisitionLineRepository(session, tenant)

    async with transaction(session):
        await _assert_warehouse(session, tenant, payload.warehouse_id)
        # Only what the client named is checked: the fallbacks are the caller's
        # own placement, read from the verified principal.
        await _assert_organisation(
            session, tenant, branch_id=payload.branch_id, department_id=payload.department_id
        )
        items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
        _assert_purchasable(items)
        await _assert_suggested_vendors(session, tenant, payload.lines)

        requisition_date = payload.requisition_date or today()
        if payload.required_date < requisition_date:
            raise ValidationError("The required date cannot be before the requisition date.")

        code = await next_document_number(
            session, tenant.company_id, DocTypes.PURCHASE_REQUISITION, on=requisition_date
        )
        requisition = PurchaseRequisition(
            code=code,
            requisition_date=requisition_date,
            required_date=payload.required_date,
            warehouse_id=payload.warehouse_id,
            department_id=payload.department_id or department_id,
            branch_id=payload.branch_id or branch_id,
            requested_by_id=payload.requested_by_id or tenant.user_id,
            status=RequisitionStatus.DRAFT,
            priority=payload.priority,
            source_type=payload.source_type,
            source_id=payload.source_id,
            purpose=payload.purpose,
            notes=payload.notes,
        )
        repo.add(requisition)
        await session.flush()

        lines = [
            _apply_requisition_line(
                line_repo.add(PurchaseRequisitionLine(requisition_id=requisition.id)),
                payload_line,
                items[payload_line.item_id],
            )
            for payload_line in payload.lines
        ]
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_REQUISITION,
            entity_id=requisition.id,
            entity_code=requisition.code,
            summary=f"Raised {requisition.code} with {len(lines)} line(s)",
            user_id=tenant.user_id,
            new_values={
                **snapshot(requisition, REQUISITION_AUDITED_FIELDS),
                "line_count": len(lines),
            },
        )

    return requisition, lines


async def update_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    requisition_id: uuid.UUID,
    payload: RequisitionUpdate,
) -> tuple[PurchaseRequisition, list[PurchaseRequisitionLine]]:
    """Edit a draft. Anything past DRAFT is re-raised rather than rewritten."""
    repo = RequisitionRepository(session, tenant)
    line_repo = RequisitionLineRepository(session, tenant)

    async with transaction(session):
        requisition = await repo.lock_for_update(requisition_id)
        if requisition is None:
            raise NotFoundError("Purchase requisition", requisition_id)
        if RequisitionStatus(requisition.status) is not RequisitionStatus.DRAFT:
            raise BusinessRuleError(
                f"Requisition {requisition.code} is {requisition.status} and can no longer "
                "be edited.",
                details={"status": str(requisition.status)},
            )

        before = snapshot(requisition, REQUISITION_AUDITED_FIELDS)

        if payload.warehouse_id is not None:
            await _assert_warehouse(session, tenant, payload.warehouse_id)
            requisition.warehouse_id = payload.warehouse_id
        await _assert_organisation(
            session, tenant, branch_id=payload.branch_id, department_id=payload.department_id
        )
        for field in (
            "required_date",
            "department_id",
            "branch_id",
            "requested_by_id",
            "priority",
            "purpose",
            "notes",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(requisition, field, value)
        if requisition.required_date < requisition.requisition_date:
            raise ValidationError("The required date cannot be before the requisition date.")

        lines: list[PurchaseRequisitionLine]
        if payload.lines is not None:
            items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
            _assert_purchasable(items)
            await _assert_suggested_vendors(session, tenant, payload.lines)
            await line_repo.delete_for_requisition(requisition.id)
            lines = [
                _apply_requisition_line(
                    line_repo.add(PurchaseRequisitionLine(requisition_id=requisition.id)),
                    payload_line,
                    items[payload_line.item_id],
                )
                for payload_line in payload.lines
            ]
        else:
            lines = await line_repo.list_for_requisition(requisition.id)

        repo.touch(requisition)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_REQUISITION,
            entity_id=requisition.id,
            entity_code=requisition.code,
            summary=f"Updated {requisition.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(requisition, REQUISITION_AUDITED_FIELDS),
        )

    return requisition, lines


async def transition_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    requisition_id: uuid.UUID,
    target: RequisitionStatus,
    *,
    reason: str,
) -> PurchaseRequisition:
    """Submit, approve, reject, cancel or close a requisition."""
    repo = RequisitionRepository(session, tenant)

    async with transaction(session):
        requisition = await repo.lock_for_update(requisition_id)
        if requisition is None:
            raise NotFoundError("Purchase requisition", requisition_id)

        if target is RequisitionStatus.APPROVED:
            _assert_separate_approver(principal, requisition.created_by_id)
            requisition.approved_by_id = principal.user_id
            requisition.approved_at = utcnow()
        elif target is RequisitionStatus.REJECTED:
            _assert_separate_approver(principal, requisition.created_by_id)
            requisition.rejection_reason = reason
        elif target is RequisitionStatus.CANCELLED:
            requisition.cancellation_reason = reason

        action = {
            RequisitionStatus.APPROVED: AuditAction.APPROVE,
            RequisitionStatus.REJECTED: AuditAction.REJECT,
            RequisitionStatus.CANCELLED: AuditAction.CANCEL,
        }.get(target, AuditAction.STATUS_CHANGE)

        await _transition(
            session,
            tenant,
            machine=REQUISITION_LIFECYCLE,
            entity=requisition,
            entity_type=AUDIT_REQUISITION,
            entity_code=requisition.code,
            target=target,
            reason=reason,
            status_enum=RequisitionStatus,
            action=action,
        )

    return requisition


async def delete_draft_requisition(
    session: AsyncSession, tenant: TenantContext, requisition_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a draft outright. Anything further along is cancelled instead."""
    repo = RequisitionRepository(session, tenant)
    line_repo = RequisitionLineRepository(session, tenant)

    async with transaction(session):
        requisition = await repo.lock_for_update(requisition_id)
        if requisition is None:
            raise NotFoundError("Purchase requisition", requisition_id)
        if RequisitionStatus(requisition.status) is not RequisitionStatus.DRAFT:
            raise BusinessRuleError(
                f"Requisition {requisition.code} is {requisition.status}; cancel it instead "
                "of deleting it.",
                details={"status": str(requisition.status)},
            )

        code, requisition_uuid = requisition.code, requisition.id
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_REQUISITION,
            entity_id=requisition_uuid,
            entity_code=code,
            summary=f"Deleted draft requisition {code}",
            user_id=tenant.user_id,
            context={"reason": reason},
        )
        await line_repo.delete_for_requisition(requisition_uuid)
        await repo.hard_delete(requisition)


async def list_requisitions(
    session: AsyncSession,
    tenant: TenantContext,
    filters: RequisitionFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[RequisitionListRow], OffsetPagination]:
    """A page of requisitions, each with how many of its lines are still to order.

    Two statements for the page: the requisitions, then one grouped count of
    their open lines.
    """
    rows, pagination = await RequisitionRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    open_lines = await RequisitionLineRepository(session, tenant).open_line_counts(
        [row.id for row in rows]
    )
    return [
        RequisitionListRow(
            **RequisitionSummary.model_validate(row).model_dump(),
            open_line_count=open_lines.get(row.id, 0),
        )
        for row in rows
    ], pagination


async def get_requisition_detail(
    session: AsyncSession, tenant: TenantContext, requisition_id: uuid.UUID
) -> RequisitionDetail:
    repo = RequisitionRepository(session, tenant)
    requisition = await repo.get(requisition_id)
    if requisition is None:
        raise NotFoundError("Purchase requisition", requisition_id)
    lines = await RequisitionLineRepository(session, tenant).list_for_requisition(requisition_id)
    return requisition_detail(requisition, lines)


# =============================================================================
# Request for quotation
# =============================================================================


def rfq_detail(rfq: Rfq, lines: Sequence[RfqLine]) -> RfqDetail:
    detail = RfqDetail.model_validate(rfq)
    detail.lines = [RfqLineRead.model_validate(line) for line in lines]
    return detail


def _apply_rfq_line(line: RfqLine, payload: RfqLineIn, item: PurchasableItem) -> RfqLine:
    line.line_number = payload.line_number
    line.item_id = payload.item_id
    line.description = payload.description or item.name
    line.quantity = to_quantity(payload.quantity)
    line.uom_id = payload.uom_id or item.order_uom_id
    line.required_date = payload.required_date
    line.requisition_line_id = payload.requisition_line_id
    line.specification = payload.specification
    line.notes = payload.notes
    return line


def _rfq_lines_from_requisition(
    requisition_lines: Sequence[PurchaseRequisitionLine],
) -> list[RfqLineIn]:
    """Copy an approved requisition's open lines onto an RFQ.

    Lines already fully ordered are skipped: re-quoting something that has been
    bought is how a second, duplicate order gets raised.
    """
    payloads: list[RfqLineIn] = []
    for line in requisition_lines:
        outstanding = Decimal(line.quantity) - Decimal(line.ordered_quantity)
        if outstanding <= 0:
            continue
        payloads.append(
            RfqLineIn(
                line_number=line.line_number,
                item_id=line.item_id,
                description=line.description,
                quantity=to_quantity(outstanding),
                uom_id=line.uom_id,
                required_date=line.required_date,
                requisition_line_id=line.id,
            )
        )
    if not payloads:
        raise BusinessRuleError(
            "Every line on this requisition has already been ordered; there is nothing to quote."
        )
    return payloads


async def create_rfq(
    session: AsyncSession, tenant: TenantContext, payload: RfqCreate
) -> tuple[Rfq, list[RfqLine]]:
    repo = RfqRepository(session, tenant)
    line_repo = RfqLineRepository(session, tenant)
    requisition_repo = RequisitionRepository(session, tenant)
    requisition_line_repo = RequisitionLineRepository(session, tenant)

    async with transaction(session):
        await _assert_warehouse(session, tenant, payload.warehouse_id)

        line_payloads = payload.lines
        requisition: PurchaseRequisition | None = None
        if payload.requisition_id is not None:
            requisition = await requisition_repo.get(payload.requisition_id)
            if requisition is None:
                raise NotFoundError("Purchase requisition", payload.requisition_id)
            if RequisitionStatus(requisition.status) not in (
                RequisitionStatus.APPROVED,
                RequisitionStatus.CONVERTED,
            ):
                raise BusinessRuleError(
                    f"Requisition {requisition.code} is {requisition.status}; only an approved "
                    "requisition can be quoted.",
                    details={"status": str(requisition.status)},
                )
            if line_payloads is None:
                line_payloads = _rfq_lines_from_requisition(
                    await requisition_line_repo.list_for_requisition(requisition.id)
                )

        if not line_payloads:
            raise ValidationError("An RFQ needs at least one line.")

        vendors = await VendorLookupRepository(session, tenant).load(payload.vendor_ids)
        unknown = [v for v in payload.vendor_ids if v not in vendors]
        if unknown:
            raise NotFoundError("Vendor", ", ".join(str(v) for v in unknown))

        items = await _load_items(session, tenant, [line.item_id for line in line_payloads])
        _assert_purchasable(items)

        rfq_date = payload.rfq_date or today()
        code = await next_document_number(session, tenant.company_id, DocTypes.RFQ, on=rfq_date)
        rfq = Rfq(
            code=code,
            rfq_date=rfq_date,
            response_due_date=payload.response_due_date,
            required_date=payload.required_date,
            requisition_id=payload.requisition_id,
            warehouse_id=payload.warehouse_id,
            vendor_ids=list(dict.fromkeys(payload.vendor_ids)),
            status=RfqStatus.DRAFT,
            currency=payload.currency,
            terms=payload.terms,
            notes=payload.notes,
        )
        repo.add(rfq)
        await session.flush()

        lines = [
            _apply_rfq_line(
                line_repo.add(RfqLine(rfq_id=rfq.id)), payload_line, items[payload_line.item_id]
            )
            for payload_line in line_payloads
        ]
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_RFQ,
            entity_id=rfq.id,
            entity_code=rfq.code,
            summary=(
                f"Raised {rfq.code} for {len(lines)} line(s) to {len(rfq.vendor_ids)} vendor(s)"
            ),
            user_id=tenant.user_id,
            new_values={
                "code": rfq.code,
                "warehouse_id": rfq.warehouse_id,
                "requisition_id": rfq.requisition_id,
                "vendor_ids": rfq.vendor_ids,
                "line_count": len(lines),
            },
        )

    return rfq, lines


async def update_rfq(
    session: AsyncSession, tenant: TenantContext, rfq_id: uuid.UUID, payload: RfqUpdate
) -> tuple[Rfq, list[RfqLine]]:
    repo = RfqRepository(session, tenant)
    line_repo = RfqLineRepository(session, tenant)

    async with transaction(session):
        rfq = await repo.lock_for_update(rfq_id)
        if rfq is None:
            raise NotFoundError("RFQ", rfq_id)
        if RfqStatus(rfq.status) is not RfqStatus.DRAFT:
            raise BusinessRuleError(
                f"RFQ {rfq.code} has already been sent and can no longer be edited.",
                details={"status": str(rfq.status)},
            )

        if payload.warehouse_id is not None:
            await _assert_warehouse(session, tenant, payload.warehouse_id)
            rfq.warehouse_id = payload.warehouse_id
        if payload.vendor_ids is not None:
            vendors = await VendorLookupRepository(session, tenant).load(payload.vendor_ids)
            unknown = [v for v in payload.vendor_ids if v not in vendors]
            if unknown:
                raise NotFoundError("Vendor", ", ".join(str(v) for v in unknown))
            rfq.vendor_ids = list(dict.fromkeys(payload.vendor_ids))
        for field in ("response_due_date", "required_date", "currency", "terms", "notes"):
            value = getattr(payload, field)
            if value is not None:
                setattr(rfq, field, value)

        lines: list[RfqLine]
        if payload.lines is not None:
            items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
            _assert_purchasable(items)
            await line_repo.delete_for_rfq(rfq.id)
            lines = [
                _apply_rfq_line(
                    line_repo.add(RfqLine(rfq_id=rfq.id)),
                    payload_line,
                    items[payload_line.item_id],
                )
                for payload_line in payload.lines
            ]
        else:
            lines = await line_repo.list_for_rfq(rfq.id)

        repo.touch(rfq)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_RFQ,
            entity_id=rfq.id,
            entity_code=rfq.code,
            summary=f"Updated {rfq.code}",
            user_id=tenant.user_id,
        )

    return rfq, lines


async def transition_rfq(
    session: AsyncSession,
    tenant: TenantContext,
    rfq_id: uuid.UUID,
    target: RfqStatus,
    *,
    reason: str,
) -> Rfq:
    repo = RfqRepository(session, tenant)

    async with transaction(session):
        rfq = await repo.lock_for_update(rfq_id)
        if rfq is None:
            raise NotFoundError("RFQ", rfq_id)

        if target is RfqStatus.SENT:
            if not rfq.vendor_ids:
                raise BusinessRuleError(
                    f"RFQ {rfq.code} names no vendors; add at least one before sending it."
                )
            rfq.sent_at = utcnow()
        elif target is RfqStatus.CLOSED:
            rfq.closed_at = utcnow()
        elif target is RfqStatus.CANCELLED:
            rfq.cancellation_reason = reason

        await _transition(
            session,
            tenant,
            machine=RFQ_LIFECYCLE,
            entity=rfq,
            entity_type=AUDIT_RFQ,
            entity_code=rfq.code,
            target=target,
            reason=reason,
            status_enum=RfqStatus,
            action=(
                AuditAction.CANCEL if target is RfqStatus.CANCELLED else AuditAction.STATUS_CHANGE
            ),
        )

    return rfq


async def list_rfqs(
    session: AsyncSession,
    tenant: TenantContext,
    filters: RfqFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[RfqSummary], OffsetPagination]:
    rows, pagination = await RfqRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [RfqSummary.model_validate(row) for row in rows], pagination


async def get_rfq_detail(
    session: AsyncSession, tenant: TenantContext, rfq_id: uuid.UUID
) -> RfqDetail:
    rfq = await RfqRepository(session, tenant).get(rfq_id)
    if rfq is None:
        raise NotFoundError("RFQ", rfq_id)
    lines = await RfqLineRepository(session, tenant).list_for_rfq(rfq_id)
    return rfq_detail(rfq, lines)


# =============================================================================
# Vendor quotation
# =============================================================================


def quotation_detail(
    quotation: VendorQuotation, lines: Sequence[VendorQuotationLine]
) -> QuotationDetail:
    detail = QuotationDetail.model_validate(quotation)
    detail.lines = [QuotationLineRead.model_validate(line) for line in lines]
    return detail


def _apply_quotation_line(
    line: VendorQuotationLine,
    payload: QuotationLineIn,
    item: PurchasableItem,
    amounts: LineAmounts,
) -> VendorQuotationLine:
    line.line_number = payload.line_number
    line.rfq_line_id = payload.rfq_line_id
    line.item_id = payload.item_id
    line.description = payload.description or item.name
    line.quantity = to_quantity(payload.quantity)
    line.uom_id = payload.uom_id or item.order_uom_id
    line.unit_price = payload.unit_price
    line.discount_percentage = payload.discount_percentage or ZERO
    line.discount = amounts.discount_amount
    # Tax comes from the item master, never from the payload: a quotation that
    # understated GST would flow straight onto a purchase order and then onto a
    # statutory document (master rules section 38).
    line.tax_rate = item.gst_rate
    line.tax_amount = amounts.tax_amount
    line.line_total = amounts.line_total
    line.lead_time_days = payload.lead_time_days
    line.min_order_quantity = payload.min_order_quantity or ZERO
    line.notes = payload.notes
    return line


def _price_quotation_lines(
    payloads: Sequence[QuotationLineIn], items: dict[uuid.UUID, PurchasableItem]
) -> list[LineAmounts]:
    return [
        compute_line_amounts(
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            tax_rate=items[payload.item_id].gst_rate,
            discount_percentage=payload.discount_percentage,
            discount_amount=payload.discount,
        )
        for payload in payloads
    ]


async def _load_rfq_for_quoting(
    session: AsyncSession, tenant: TenantContext, rfq_id: uuid.UUID
) -> Rfq:
    rfq = await RfqRepository(session, tenant).get(rfq_id)
    if rfq is None:
        raise NotFoundError("RFQ", rfq_id)
    if RfqStatus(rfq.status) in (RfqStatus.CANCELLED, RfqStatus.CLOSED):
        raise BusinessRuleError(
            f"RFQ {rfq.code} is {rfq.status}; it can no longer take quotations.",
            details={"status": str(rfq.status)},
        )
    return rfq


def quotation_terms(
    *,
    payment_terms_days: int | None,
    lead_time_days: int | None,
    vendor: Vendor,
) -> tuple[int, int]:
    """The commercial terms a quotation is stored with.

    An *omitted* value falls back to the vendor master's agreed terms. A value
    of ``0`` is kept: an ex-stock quote has a zero-day lead time and a cash
    quote has zero-day terms, and both change the comparison score. ``or`` would
    silently swap either for the vendor's default, which is why this is written
    with ``is None`` - the same rule ``create_purchase_order`` applies.
    """
    return (
        payment_terms_days if payment_terms_days is not None else int(vendor.payment_terms_days),
        lead_time_days if lead_time_days is not None else int(vendor.lead_time_days),
    )


async def create_quotation(
    session: AsyncSession, tenant: TenantContext, payload: QuotationCreate
) -> tuple[VendorQuotation, list[VendorQuotationLine]]:
    """Record what one vendor quoted against an RFQ."""
    repo = QuotationRepository(session, tenant)
    line_repo = QuotationLineRepository(session, tenant)

    async with transaction(session):
        rfq = await _load_rfq_for_quoting(session, tenant, payload.rfq_id)
        vendor = await _load_vendor(session, tenant, payload.vendor_id)

        if await repo.by_rfq_vendor(rfq.id, vendor.id) is not None:
            raise BusinessRuleError(
                f"Vendor {vendor.code} has already quoted on {rfq.code}; record the revised "
                "offer on that quotation instead of a second one. A rejected or lapsed "
                "quotation is reopened by the revision.",
                code=ErrorCode.DUPLICATE_DOCUMENT,
                status_code=409,
                details={"rfq_code": rfq.code, "vendor_code": vendor.code},
            )

        items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
        amounts = _price_quotation_lines(payload.lines, items)
        totals = sum_document_totals(amounts, freight_amount=payload.freight_amount)
        payment_terms_days, lead_time_days = quotation_terms(
            payment_terms_days=payload.payment_terms_days,
            lead_time_days=payload.lead_time_days,
            vendor=vendor,
        )

        quotation = VendorQuotation(
            rfq_id=rfq.id,
            vendor_id=vendor.id,
            vendor_reference=payload.vendor_reference.strip(),
            quotation_date=payload.quotation_date or today(),
            valid_until=payload.valid_until,
            currency=payload.currency,
            payment_terms_days=payment_terms_days,
            lead_time_days=lead_time_days,
            subtotal=totals.subtotal,
            discount=totals.discount,
            tax_amount=totals.tax_amount,
            freight_amount=totals.freight_amount,
            total_amount=totals.total_amount,
            status=QuotationStatus.RECEIVED,
            terms=payload.terms,
            notes=payload.notes,
        )
        repo.add(quotation)
        await session.flush()

        lines = [
            _apply_quotation_line(
                line_repo.add(VendorQuotationLine(quotation_id=quotation.id)),
                payload_line,
                items[payload_line.item_id],
                line_amounts,
            )
            for payload_line, line_amounts in zip(payload.lines, amounts, strict=True)
        ]
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_QUOTATION,
            entity_id=quotation.id,
            entity_code=quotation.vendor_reference,
            summary=(
                f"Recorded quotation {quotation.vendor_reference} from {vendor.code} "
                f"on {rfq.code}: {quotation.currency} {quotation.total_amount}"
            ),
            user_id=tenant.user_id,
            new_values={
                "rfq_id": rfq.id,
                "vendor_id": vendor.id,
                "total_amount": quotation.total_amount,
                "lead_time_days": quotation.lead_time_days,
                "line_count": len(lines),
            },
        )

    return quotation, lines


async def update_quotation(
    session: AsyncSession, tenant: TenantContext, quotation_id: uuid.UUID, payload: QuotationUpdate
) -> tuple[VendorQuotation, list[VendorQuotationLine]]:
    """Replace a quotation with the vendor's revised offer."""
    repo = QuotationRepository(session, tenant)
    line_repo = QuotationLineRepository(session, tenant)

    async with transaction(session):
        quotation = await repo.lock_for_update(quotation_id)
        if quotation is None:
            raise NotFoundError("Vendor quotation", quotation_id)
        current = QuotationStatus(quotation.status)
        if QUOTATION_LIFECYCLE.is_terminal(current):
            raise BusinessRuleError(
                f"Quotation {quotation.vendor_reference} is {quotation.status} and can no "
                "longer be revised; a purchase order is raised against it.",
                details={"status": str(quotation.status)},
            )
        if current in REQUOTABLE_QUOTATION_STATUSES:
            # ASSUMPTION: a rejected or lapsed offer is reopened by the revised
            # one rather than refused - the buyer asked the vendor to requote,
            # and the RFQ still holds one row per vendor. The reopening is a
            # status change on the state machine with its own audit row, in the
            # same transaction as the revision (docs/assumptions.md).
            rfq = await _load_rfq_for_quoting(session, tenant, quotation.rfq_id)
            await _transition(
                session,
                tenant,
                machine=QUOTATION_LIFECYCLE,
                entity=quotation,
                entity_type=AUDIT_QUOTATION,
                entity_code=quotation.vendor_reference,
                target=QuotationStatus.RECEIVED,
                reason=payload.reason or f"Vendor requoted on {rfq.code}",
                status_enum=QuotationStatus,
                action=AuditAction.STATUS_CHANGE,
            )
            quotation.decision_reason = None
            quotation.selected_by_id = None
            quotation.selected_at = None

        before = snapshot(quotation, ["total_amount", "lead_time_days", "valid_until"])

        for field in (
            "quotation_date",
            "valid_until",
            "currency",
            "payment_terms_days",
            "lead_time_days",
            "terms",
            "notes",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(quotation, field, value)
        if payload.vendor_reference is not None:
            quotation.vendor_reference = payload.vendor_reference.strip()

        lines: list[VendorQuotationLine]
        if payload.lines is not None:
            items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
            amounts = _price_quotation_lines(payload.lines, items)
            await line_repo.delete_for_quotation(quotation.id)
            lines = [
                _apply_quotation_line(
                    line_repo.add(VendorQuotationLine(quotation_id=quotation.id)),
                    payload_line,
                    items[payload_line.item_id],
                    line_amounts,
                )
                for payload_line, line_amounts in zip(payload.lines, amounts, strict=True)
            ]
        else:
            lines = await line_repo.list_for_quotation(quotation.id)
            amounts = [
                LineAmounts(
                    gross_amount=to_money(Decimal(line.quantity) * Decimal(line.unit_price)),
                    discount_amount=Decimal(line.discount),
                    taxable_amount=to_money(
                        Decimal(line.quantity) * Decimal(line.unit_price) - Decimal(line.discount)
                    ),
                    tax_amount=Decimal(line.tax_amount),
                    line_total=Decimal(line.line_total),
                )
                for line in lines
            ]

        freight = (
            payload.freight_amount
            if payload.freight_amount is not None
            else Decimal(quotation.freight_amount)
        )
        totals = sum_document_totals(amounts, freight_amount=freight)
        quotation.subtotal = totals.subtotal
        quotation.discount = totals.discount
        quotation.tax_amount = totals.tax_amount
        quotation.freight_amount = totals.freight_amount
        quotation.total_amount = totals.total_amount

        repo.touch(quotation)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_QUOTATION,
            entity_id=quotation.id,
            entity_code=quotation.vendor_reference,
            summary=f"Revised quotation {quotation.vendor_reference}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(quotation, ["total_amount", "lead_time_days", "valid_until"]),
        )

    return quotation, lines


async def decide_quotation(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    quotation_id: uuid.UUID,
    target: QuotationStatus,
    *,
    reason: str,
) -> VendorQuotation:
    """Select or reject one vendor's offer, with the buyer's reason recorded."""
    repo = QuotationRepository(session, tenant)

    async with transaction(session):
        quotation = await repo.lock_for_update(quotation_id)
        if quotation is None:
            raise NotFoundError("Vendor quotation", quotation_id)

        if target is QuotationStatus.SELECTED:
            if quotation.valid_until is not None and quotation.valid_until < today():
                raise BusinessRuleError(
                    f"Quotation {quotation.vendor_reference} lapsed on "
                    f"{quotation.valid_until.isoformat()}; ask the vendor to requote.",
                    details={"valid_until": quotation.valid_until.isoformat()},
                )
            quotation.selected_by_id = principal.user_id
            quotation.selected_at = utcnow()
        quotation.decision_reason = reason

        await _transition(
            session,
            tenant,
            machine=QUOTATION_LIFECYCLE,
            entity=quotation,
            entity_type=AUDIT_QUOTATION,
            entity_code=quotation.vendor_reference,
            target=target,
            reason=reason,
            status_enum=QuotationStatus,
            action=(
                AuditAction.APPROVE
                if target is QuotationStatus.SELECTED
                else AuditAction.REJECT
                if target is QuotationStatus.REJECTED
                else AuditAction.STATUS_CHANGE
            ),
        )

    return quotation


async def list_quotations(
    session: AsyncSession,
    tenant: TenantContext,
    filters: QuotationFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[QuotationSummary], OffsetPagination]:
    rows, pagination = await QuotationRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [QuotationSummary.model_validate(row) for row in rows], pagination


async def get_quotation_detail(
    session: AsyncSession, tenant: TenantContext, quotation_id: uuid.UUID
) -> QuotationDetail:
    quotation = await QuotationRepository(session, tenant).get(quotation_id)
    # A vendor principal is told "not found" rather than "forbidden" for another
    # vendor's quotation: confirming a competitor quoted, and on what, is itself
    # the disclosure. Same treatment as another vendor's purchase order.
    if quotation is None or (
        tenant.vendor_id is not None and quotation.vendor_id != tenant.vendor_id
    ):
        raise NotFoundError("Vendor quotation", quotation_id)
    lines = await QuotationLineRepository(session, tenant).list_for_quotation(quotation_id)
    return quotation_detail(quotation, lines)


# --- comparison ---------------------------------------------------------------


def _offer_lead_time(line: VendorQuotationLine, quotation: VendorQuotation) -> int:
    """The line's own lead time when it has one, otherwise the header's.

    A vendor quotes one lead time for a stocked part and another for a machined
    one on the same sheet of paper; the line is the more specific promise.
    """
    return int(line.lead_time_days if line.lead_time_days is not None else quotation.lead_time_days)


async def compare_quotations(
    session: AsyncSession,
    tenant: TenantContext,
    rfq_id: uuid.UUID,
    *,
    weights: ScoreWeights = DEFAULT_SCORE_WEIGHTS,
) -> QuotationComparison:
    """Side-by-side comparison of every quotation received against one RFQ.

    Four batched reads for the whole board - RFQ, its lines, every quotation,
    every quotation line - plus one vendor lookup and one item lookup. Building
    it per vendor would be the N+1 the rules forbid, on a screen a buyer opens
    for every purchase they make.
    """
    # The board is every vendor's price for the same basket, side by side. There
    # is no reading of it a vendor principal is entitled to, whatever permission
    # a misconfigured role hands them (master rules section 16).
    if tenant.vendor_id is not None:
        raise AuthorizationError(
            "The quotation comparison is not available to vendor accounts.",
            code=ErrorCode.SCOPE_DENIED,
        )

    rfq_repo = RfqRepository(session, tenant)
    rfq = await rfq_repo.get(rfq_id)
    if rfq is None:
        raise NotFoundError("RFQ", rfq_id)

    rfq_lines = await RfqLineRepository(session, tenant).list_for_rfq(rfq.id)
    quotations = await QuotationRepository(session, tenant).for_rfq(rfq.id)
    lines_by_quotation = await QuotationLineRepository(session, tenant).list_for_quotations(
        [q.id for q in quotations]
    )
    vendors = await VendorLookupRepository(session, tenant).load([q.vendor_id for q in quotations])
    items = await PurchasableItemRepository(session, tenant).load(
        [line.item_id for line in rfq_lines]
        + [line.item_id for lines in lines_by_quotation.values() for line in lines]
    )

    as_of = today()
    vendor_names = {vendor_id: facts.name for vendor_id, facts in vendors.items()}

    columns = [
        _comparison_column(
            quotation,
            vendors.get(quotation.vendor_id),
            lines_by_quotation.get(quotation.id, []),
            as_of=as_of,
        )
        for quotation in quotations
    ]

    currencies = {q.currency for q in quotations}
    mixed_currency = len(currencies) > 1

    rows, best_vendor_counts = _comparison_rows(
        rfq_lines=rfq_lines,
        quotations=quotations,
        lines_by_quotation=lines_by_quotation,
        vendors=vendors,
        items=items,
        vendor_names=vendor_names,
        as_of=as_of,
        weights=weights,
        mixed_currency=mixed_currency,
    )

    overall_vendor_id, overall_reason = _overall_recommendation(
        columns=columns,
        best_vendor_counts=best_vendor_counts,
        vendor_names=vendor_names,
        mixed_currency=mixed_currency,
    )

    return QuotationComparison(
        rfq_id=rfq.id,
        rfq_code=rfq.code,
        rfq_status=RfqStatus(rfq.status),
        currency=rfq.currency,
        vendors=columns,
        items=rows,
        recommended_vendor_id=overall_vendor_id,
        recommendation_reason=overall_reason,
        mixed_currency=mixed_currency,
    )


def _comparison_column(
    quotation: VendorQuotation,
    vendor: VendorFacts | None,
    lines: Sequence[VendorQuotationLine],
    *,
    as_of: date,
) -> ComparisonVendorColumn:
    expired = quotation.valid_until is not None and quotation.valid_until < as_of
    return ComparisonVendorColumn(
        vendor_id=quotation.vendor_id,
        vendor_code=vendor.code if vendor else "",
        vendor_name=vendor.name if vendor else "",
        quotation_id=quotation.id,
        quotation_status=QuotationStatus(quotation.status),
        vendor_rating=vendor.rating if vendor else None,
        rating_sample_size=vendor.rating_sample_size if vendor else 0,
        lead_time_days=int(quotation.lead_time_days),
        payment_terms_days=int(quotation.payment_terms_days),
        currency=quotation.currency,
        subtotal=Decimal(quotation.subtotal),
        freight_amount=Decimal(quotation.freight_amount),
        tax_amount=Decimal(quotation.tax_amount),
        total_amount=Decimal(quotation.total_amount),
        valid_until=quotation.valid_until,
        is_expired=expired,
        quoted_item_count=len(lines),
    )


def _comparison_rows(
    *,
    rfq_lines: Sequence[RfqLine],
    quotations: Sequence[VendorQuotation],
    lines_by_quotation: dict[uuid.UUID, list[VendorQuotationLine]],
    vendors: dict[uuid.UUID, VendorFacts],
    items: dict[uuid.UUID, PurchasableItem],
    vendor_names: dict[uuid.UUID, str],
    as_of: date,
    weights: ScoreWeights,
    mixed_currency: bool,
) -> tuple[list[ComparisonItemRow], dict[uuid.UUID, int]]:
    """One row per item, every vendor's offer scored and ranked against it."""
    quotation_by_id = {q.id: q for q in quotations}

    # Offers are grouped by item rather than by RFQ line, so a vendor who quoted
    # an item the RFQ did not ask for still appears on the board.
    offers_by_item: dict[uuid.UUID, list[tuple[VendorQuotation, VendorQuotationLine]]] = {}
    for quotation_id, lines in lines_by_quotation.items():
        quotation = quotation_by_id[quotation_id]
        for line in lines:
            offers_by_item.setdefault(line.item_id, []).append((quotation, line))

    rfq_line_by_item = {line.item_id: line for line in rfq_lines}
    ordered_items = list(rfq_line_by_item) + [
        item_id for item_id in offers_by_item if item_id not in rfq_line_by_item
    ]

    rows: list[ComparisonItemRow] = []
    best_vendor_counts: dict[uuid.UUID, int] = {}

    for item_id in ordered_items:
        offers = offers_by_item.get(item_id, [])
        item = items.get(item_id)
        rfq_line = rfq_line_by_item.get(item_id)

        inputs = [
            OfferInput(
                vendor_id=quotation.vendor_id,
                quotation_id=quotation.id,
                quotation_line_id=line.id,
                effective_unit_price=effective_unit_price(
                    unit_price=Decimal(line.unit_price),
                    discount_percentage=Decimal(line.discount_percentage),
                ),
                lead_time_days=_offer_lead_time(line, quotation),
                vendor_rating=(
                    vendors[quotation.vendor_id].rating if quotation.vendor_id in vendors else None
                ),
                is_expired=quotation.valid_until is not None and quotation.valid_until < as_of,
            )
            for quotation, line in offers
        ]
        ranked = rank_offers(inputs, weights=weights)
        line_by_id = {line.id: (quotation, line) for quotation, line in offers}

        row_offers: list[ComparisonOffer] = []
        for entry in ranked:
            quotation, line = line_by_id[entry.offer.quotation_line_id]
            vendor = vendors.get(quotation.vendor_id)
            row_offers.append(
                ComparisonOffer(
                    vendor_id=quotation.vendor_id,
                    vendor_code=vendor.code if vendor else "",
                    vendor_name=vendor.name if vendor else "",
                    quotation_id=quotation.id,
                    quotation_line_id=line.id,
                    unit_price=Decimal(line.unit_price),
                    discount_percentage=Decimal(line.discount_percentage),
                    effective_unit_price=entry.offer.effective_unit_price,
                    quantity=Decimal(line.quantity),
                    tax_rate=Decimal(line.tax_rate),
                    line_total=Decimal(line.line_total),
                    lead_time_days=entry.offer.lead_time_days,
                    vendor_rating=entry.offer.vendor_rating,
                    payment_terms_days=int(quotation.payment_terms_days),
                    is_expired=entry.offer.is_expired,
                    is_lowest_price=entry.is_lowest_price,
                    is_fastest=entry.is_fastest,
                    score=entry.score,
                    rank=entry.rank,
                )
            )

        live = [entry for entry in ranked if not entry.offer.is_expired]
        if mixed_currency:
            # Comparing 100 EUR against 9,000 INR by magnitude would recommend
            # the wrong vendor with total confidence. Better to show the board
            # and say why no winner is named.
            recommended_vendor_id = None
            reason = (
                "Vendors quoted in different currencies; convert to one currency before "
                "comparing these prices."
            )
        elif live:
            recommended_vendor_id = live[0].offer.vendor_id
            reason = recommendation_reason(ranked, vendor_names=vendor_names)
            best_vendor_counts[recommended_vendor_id] = (
                best_vendor_counts.get(recommended_vendor_id, 0) + 1
            )
        else:
            recommended_vendor_id = None
            reason = recommendation_reason(ranked, vendor_names=vendor_names)

        rows.append(
            ComparisonItemRow(
                item_id=item_id,
                item_code=item.code if item else "",
                item_name=item.name if item else "",
                quantity=Decimal(rfq_line.quantity) if rfq_line else ZERO,
                uom_id=rfq_line.uom_id if rfq_line else None,
                rfq_line_id=rfq_line.id if rfq_line else None,
                offers=row_offers,
                recommended_vendor_id=recommended_vendor_id,
                recommendation_reason=reason,
            )
        )

    return rows, best_vendor_counts


def _overall_recommendation(
    *,
    columns: Sequence[ComparisonVendorColumn],
    best_vendor_counts: dict[uuid.UUID, int],
    vendor_names: dict[uuid.UUID, str],
    mixed_currency: bool,
) -> tuple[uuid.UUID | None, str]:
    """Name one vendor for the whole basket, or say why none is named.

    The vendor that wins the most lines, not the lowest total: splitting an
    order across three vendors to save on two lines usually costs more in
    freight and follow-up than it saves, and the per-item rows are there for a
    buyer who wants to split anyway.
    """
    if not columns:
        return None, "No quotations have been received against this RFQ."
    if mixed_currency:
        return None, (
            "Quotations were received in more than one currency; no overall recommendation is "
            "made until they are converted to a common currency."
        )
    if not best_vendor_counts:
        return None, "Every quotation received has lapsed; ask the vendors to requote."

    totals_by_vendor = {c.vendor_id: Decimal(c.total_amount) for c in columns}
    winner_id = max(
        best_vendor_counts,
        # Most lines won; ties broken by the lower total so the result is stable.
        # The total stays a Decimal: rounding a quotation through binary float to
        # break a tie can pick the dearer vendor, and money never touches float
        # anywhere in this codebase (master rules section 84).
        key=lambda vendor_id: (
            best_vendor_counts[vendor_id],
            -totals_by_vendor.get(vendor_id, ZERO),
        ),
    )
    won = best_vendor_counts[winner_id]
    total_lines = sum(best_vendor_counts.values())
    column = next((c for c in columns if c.vendor_id == winner_id), None)
    name = vendor_names.get(winner_id, "This vendor")

    detail = (
        f" at {column.currency} {column.total_amount} with a {column.lead_time_days}-day lead time"
        if column
        else ""
    )
    return winner_id, (
        f"{name} is recommended: best value on {won} of {total_lines} quoted line(s){detail}."
    )


# =============================================================================
# Duplicate purchase order prevention (master rules section 16)
# =============================================================================


def _as_candidate_input(row: OpenOrderLine) -> CandidateInput:
    return CandidateInput(
        purchase_order_id=row.purchase_order_id,
        purchase_order_code=row.purchase_order_code,
        purchase_order_line_id=row.line_id,
        status=row.status,
        vendor_id=row.vendor_id,
        item_id=row.item_id,
        warehouse_id=row.warehouse_id,
        ordered_quantity=row.quantity,
        received_quantity=row.received_quantity,
        required_date=row.required_date,
        source_type=row.source_type,
        source_id=row.source_id,
        requisition_line_id=row.requisition_line_id,
    )


def _date_envelope(dates: Sequence[date | None]) -> tuple[date | None, date | None]:
    """The coarse window the SQL narrows on, before per-item matching.

    Widened by the duplicate window on both sides so a candidate that is only
    just inside one line's window is still fetched.
    """
    known = [d for d in dates if d is not None]
    if not known:
        return None, None
    from datetime import timedelta

    span = timedelta(days=DUPLICATE_REQUIRED_DATE_WINDOW_DAYS)
    return min(known) - span, max(known) + span


async def find_duplicate_candidates(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    item_id: uuid.UUID,
    required_date: date | None,
    warehouse_id: uuid.UUID,
    source_type: PurchaseSourceType | None = None,
    source_id: uuid.UUID | None = None,
    exclude_order_id: uuid.UUID | None = None,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
    requisition_line_id: uuid.UUID | None = None,
) -> list[DuplicateCandidate]:
    """Open purchase orders that may already cover this demand.

    The public single-item contract from master rules section 16. Matches on
    vendor, item, warehouse, an overlapping required-date window, the source
    demand reference and the pending requisition line, over orders that are
    still open. Multi-line callers use :func:`find_duplicates_for_lines`, which
    asks the same question for a whole document in one statement.
    """
    date_from, date_to = _date_envelope([required_date])
    rows = await PurchaseOrderRepository(session, tenant).open_lines_for_duplicate_check(
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        item_ids=[item_id],
        date_from=date_from,
        date_to=date_to,
        source_type=source_type,
        source_id=source_id,
        exclude_order_id=exclude_order_id,
        requisition_line_ids=[requisition_line_id] if requisition_line_id else (),
    )
    return classify_candidates(
        [_as_candidate_input(row) for row in rows],
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        required_date=required_date,
        source_type=source_type,
        source_id=source_id,
        window_days=window_days,
        requisition_line_id=requisition_line_id,
    )


def _classify_for_lines(
    candidates: list[CandidateInput],
    *,
    vendor_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lines: Sequence[ProposedLine],
    source_type: PurchaseSourceType | None,
    source_id: uuid.UUID | None,
) -> list[DuplicateCandidate]:
    """Apply the per-line rules over an already-fetched candidate set."""
    found: dict[uuid.UUID, DuplicateCandidate] = {}
    for line in lines:
        for candidate in classify_candidates(
            candidates,
            vendor_id=vendor_id,
            item_id=line.item_id,
            warehouse_id=warehouse_id,
            required_date=line.required_date,
            source_type=source_type,
            source_id=source_id,
            requisition_line_id=line.requisition_line_id,
        ):
            # Keyed by order line so two proposed lines for the same item do not
            # report the same existing line twice.
            found.setdefault(candidate.purchase_order_line_id, candidate)
    return list(found.values())


async def find_duplicates_for_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    vendor_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lines: Sequence[ProposedLine],
    source_type: PurchaseSourceType | None,
    source_id: uuid.UUID | None,
    exclude_order_id: uuid.UUID | None = None,
) -> list[DuplicateCandidate]:
    """The whole-document form: one query for every line's items.

    The per-line rules are then applied in memory over the fetched set, so a
    twenty-line order costs one round trip rather than twenty.
    """
    if not lines:
        return []

    date_from, date_to = _date_envelope([line.required_date for line in lines])
    rows = await PurchaseOrderRepository(session, tenant).open_lines_for_duplicate_check(
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        item_ids=[line.item_id for line in lines],
        date_from=date_from,
        date_to=date_to,
        source_type=source_type,
        source_id=source_id,
        exclude_order_id=exclude_order_id,
        requisition_line_ids=[
            line.requisition_line_id for line in lines if line.requisition_line_id is not None
        ],
    )
    return _classify_for_lines(
        [_as_candidate_input(row) for row in rows],
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        lines=lines,
        source_type=source_type,
        source_id=source_id,
    )


def _duplicate_read(candidate: DuplicateCandidate) -> DuplicateCandidateRead:
    return DuplicateCandidateRead(
        purchase_order_id=candidate.purchase_order_id,
        purchase_order_code=candidate.purchase_order_code,
        purchase_order_line_id=candidate.purchase_order_line_id,
        status=candidate.status,
        vendor_id=candidate.vendor_id,
        item_id=candidate.item_id,
        warehouse_id=candidate.warehouse_id,
        ordered_quantity=candidate.ordered_quantity,
        received_quantity=candidate.received_quantity,
        open_quantity=candidate.open_quantity,
        required_date=candidate.required_date,
        source_type=candidate.source_type,
        source_id=candidate.source_id,
        confidence=str(candidate.confidence),
        match_reasons=list(candidate.match_reasons),
        recommended_action=str(candidate.recommended_action),
        available_actions=[str(a) for a in candidate.available_actions],
    )


async def check_duplicates(
    session: AsyncSession, tenant: TenantContext, payload: DuplicateCheckRequest
) -> DuplicateCheckResult:
    """What an order would collide with, asked before it is raised.

    Read-only, so the buyer sees the warning while they can still act on it
    rather than after a refused submission.
    """
    candidates = await find_duplicates_for_lines(
        session,
        tenant,
        vendor_id=payload.vendor_id,
        warehouse_id=payload.warehouse_id,
        lines=[
            ProposedLine(
                item_id=line.item_id,
                required_date=line.required_date or payload.expected_delivery_date,
                requisition_line_id=line.requisition_line_id,
            )
            for line in payload.lines
        ],
        source_type=payload.source_type,
        source_id=payload.source_id,
    )
    return DuplicateCheckResult(
        candidates=[_duplicate_read(c) for c in candidates],
        requires_acknowledgement=bool(candidates),
    )


# =============================================================================
# Purchase order
# =============================================================================


def order_detail(order: PurchaseOrder, lines: Sequence[PurchaseOrderLine]) -> PurchaseOrderDetail:
    detail = PurchaseOrderDetail.model_validate(order)
    detail.lines = [PurchaseOrderLineRead.model_validate(line) for line in lines]
    return detail


def _apply_order_line(
    line: PurchaseOrderLine,
    payload: PurchaseOrderLineIn,
    item: PurchasableItem,
    amounts: LineAmounts,
) -> PurchaseOrderLine:
    line.line_number = payload.line_number
    line.item_id = payload.item_id
    line.description = payload.description or item.name
    line.quantity = to_quantity(payload.quantity)
    line.uom_id = payload.uom_id or item.order_uom_id
    line.unit_price = payload.unit_price
    line.discount_percentage = payload.discount_percentage or ZERO
    line.discount = amounts.discount_amount
    # Tax rate comes from the item master, never the payload (section 38).
    line.tax_rate = item.gst_rate
    line.tax_amount = amounts.tax_amount
    line.line_total = amounts.line_total
    line.required_date = payload.required_date
    line.requires_qc = payload.requires_qc
    line.requisition_line_id = payload.requisition_line_id
    line.quotation_line_id = payload.quotation_line_id
    line.notes = payload.notes
    line.status = po_line_status_for(
        quantity=line.quantity, received_quantity=line.received_quantity or ZERO
    )
    return line


def _price_order_lines(
    payloads: Sequence[PurchaseOrderLineIn], items: dict[uuid.UUID, PurchasableItem]
) -> list[LineAmounts]:
    return [
        compute_line_amounts(
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            tax_rate=items[payload.item_id].gst_rate,
            discount_percentage=payload.discount_percentage,
            discount_amount=payload.discount,
        )
        for payload in payloads
    ]


def _apply_order_totals(order: PurchaseOrder, amounts: list[LineAmounts], freight: Decimal) -> None:
    totals = sum_document_totals(amounts, freight_amount=freight)
    order.subtotal = totals.subtotal
    order.discount = totals.discount
    order.tax_amount = totals.tax_amount
    order.freight_amount = totals.freight_amount
    order.total_amount = totals.total_amount


async def create_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payload: PurchaseOrderCreate,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine], list[DuplicateCandidate]]:
    """Raise a DRAFT purchase order, refusing an undeclared duplicate.

    The duplicate check (master rules section 16) runs inside the creating
    transaction, so an order raised concurrently for the same demand is seen.
    Proceeding anyway needs ``acknowledge_duplicates`` *and*
    ``purchase_order.approve``: overriding a duplicate warning is an approval
    decision, not a data-entry one, and the reason is written onto the document.

    Every reference the client names is checked first - branch, the requisition
    and quotation lines and header source, and the vendor-side assignee - and
    the order's procurement category is derived from its lines, never taken from
    the payload. The rest is :func:`_create_purchase_order`, the body a
    requisition conversion shares.
    """
    async with transaction(session):
        vendor = await _load_vendor(session, tenant, payload.vendor_id)
        _assert_orderable(vendor)
        await _assert_warehouse(session, tenant, payload.warehouse_id)
        await _assert_organisation(session, tenant, branch_id=payload.branch_id)

        items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
        _assert_purchasable(items)

        # The links a client names are checked before anything is written (T3),
        # and the requisition rows they touch are locked here - headers, then
        # lines - before the duplicate lock below, the order every path that
        # moves ordered quantity uses.
        await _assert_order_source(
            session,
            tenant,
            vendor_id=vendor.id,
            source_type=payload.source_type,
            source_id=payload.source_id,
        )
        requisitions, source_lines = await _lock_requisition_sources(
            session,
            tenant,
            [line.requisition_line_id for line in payload.lines if line.requisition_line_id],
        )
        _assert_requisition_links(payload.lines, requisitions, source_lines)
        await _assert_quotation_links(session, tenant, vendor_id=vendor.id, lines=payload.lines)

        item_category_id, lineage = await _order_category(
            session,
            tenant,
            vendor_id=vendor.id,
            items=items,
            item_ids=[line.item_id for line in payload.lines],
        )
        if payload.assigned_vendor_user_id is not None:
            await _assert_vendor_assignee(
                session,
                tenant,
                user_id=payload.assigned_vendor_user_id,
                vendor_id=vendor.id,
                category_lineage_=lineage,
            )

        return await _create_purchase_order(
            session,
            tenant,
            principal,
            payload,
            vendor=vendor,
            items=items,
            item_category_id=item_category_id,
        )


async def _create_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payload: PurchaseOrderCreate,
    *,
    vendor: Vendor,
    items: dict[uuid.UUID, PurchasableItem],
    item_category_id: uuid.UUID | None,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine], list[DuplicateCandidate]]:
    """The shared body of a buyer's own order and of a requisition conversion.

    Runs inside the caller's transaction, after the caller has checked the
    vendor, the warehouse, the items and every link the payload names: takes
    the duplicate lock, refuses an undeclared duplicate, numbers and inserts the
    DRAFT order with its derived category, pushes the ordered quantity back to
    the requisition, and writes the CREATE audit row.
    """
    repo = PurchaseOrderRepository(session, tenant)
    line_repo = PurchaseOrderLineRepository(session, tenant)

    po_date = payload.po_date or today()
    if payload.expected_delivery_date and payload.expected_delivery_date < po_date:
        raise ValidationError("The expected delivery date cannot be before the order date.")

    # The duplicate check below is a check-then-act: it reads open orders and
    # then inserts one. Two buyers raising the same order at the same moment
    # cannot see each other's uncommitted row, so both would find nothing and
    # both would be created without the acknowledgement section 16 requires.
    # Serialising on the vendor and warehouse - the pair the search itself
    # narrows on - makes the second caller read the first one's committed
    # order. A caller raising several orders takes all of its keys first, in
    # sorted order, so re-taking one here is a no-op rather than a new wait.
    await advisory_xact_lock(
        session, tenant.company_id, "po-duplicate", payload.vendor_id, payload.warehouse_id
    )

    candidates = await find_duplicates_for_lines(
        session,
        tenant,
        vendor_id=vendor.id,
        warehouse_id=payload.warehouse_id,
        lines=[
            ProposedLine(
                item_id=line.item_id,
                required_date=line.required_date or payload.expected_delivery_date,
                requisition_line_id=line.requisition_line_id,
            )
            for line in payload.lines
        ],
        source_type=payload.source_type,
        source_id=payload.source_id,
    )
    if candidates:
        if not payload.acknowledge_duplicates:
            raise duplicate_refusal(candidates)
        if not principal.has(Permission.PURCHASE_ORDER_APPROVE):
            raise PermissionDeniedError(str(Permission.PURCHASE_ORDER_APPROVE))
        log.info(
            "procurement.duplicate_acknowledged",
            vendor_id=str(vendor.id),
            candidate_count=len(candidates),
            user_id=str(principal.user_id),
        )

    amounts = _price_order_lines(payload.lines, items)
    code = await next_document_number(
        session, tenant.company_id, DocTypes.PURCHASE_ORDER, on=po_date
    )

    order = PurchaseOrder(
        code=code,
        vendor_id=vendor.id,
        po_date=po_date,
        expected_delivery_date=payload.expected_delivery_date,
        warehouse_id=payload.warehouse_id,
        branch_id=payload.branch_id or principal.branch_id,
        status=PurchaseOrderStatus.DRAFT,
        priority=payload.priority,
        currency=payload.currency,
        payment_terms_days=(
            payload.payment_terms_days
            if payload.payment_terms_days is not None
            else vendor.payment_terms_days
        ),
        delivery_address=payload.delivery_address,
        terms=payload.terms,
        source_type=payload.source_type,
        source_id=payload.source_id,
        assigned_vendor_user_id=payload.assigned_vendor_user_id,
        item_category_id=item_category_id,
        vendor_reference=payload.vendor_reference,
        duplicate_acknowledged=bool(candidates) and payload.acknowledge_duplicates,
        duplicate_acknowledgement_reason=(
            payload.duplicate_acknowledgement_reason if candidates else None
        ),
        notes=payload.notes,
    )
    _apply_order_totals(order, amounts, payload.freight_amount)
    repo.add(order)
    await session.flush()

    lines = [
        _apply_order_line(
            line_repo.add(PurchaseOrderLine(purchase_order_id=order.id)),
            payload_line,
            items[payload_line.item_id],
            line_amounts,
        )
        for payload_line, line_amounts in zip(payload.lines, amounts, strict=True)
    ]
    await session.flush()

    await _link_source_documents(session, tenant, order, lines)

    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.CREATE,
        entity_type=AUDIT_PURCHASE_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        summary=(
            f"Raised {order.code} on {vendor.code}: {len(lines)} line(s), "
            f"{order.currency} {order.total_amount}"
        ),
        user_id=tenant.user_id,
        new_values={
            **snapshot(order, ORDER_AUDITED_FIELDS),
            "line_count": len(lines),
            "duplicate_candidates": [c.purchase_order_code for c in candidates],
        },
    )

    return order, lines, candidates


async def _link_source_documents(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    lines: Sequence[PurchaseOrderLine],
) -> None:
    """Push the ordered quantity back onto the requisition and quotation it came from.

    Batched: the requisition lines are loaded in one statement and updated in
    memory. Without this the requisition stays PENDING forever and the next
    buyer re-orders it - the duplicate the previous section exists to prevent.
    """
    requisition_line_ids = [
        line.requisition_line_id for line in lines if line.requisition_line_id is not None
    ]
    if requisition_line_ids:
        # Locked, not merely read: ``ordered_quantity`` below is a
        # read-modify-write on a row two orders raised from the same requisition
        # line would otherwise both take a stale copy of.
        requisition_lines = await RequisitionLineRepository(session, tenant).lock_many(
            requisition_line_ids
        )
        by_id = {line.id: line for line in requisition_lines}
        touched_requisitions: set[uuid.UUID] = set()
        for order_line in lines:
            source_line = by_id.get(order_line.requisition_line_id or uuid.UUID(int=0))
            if source_line is None:
                continue
            source_line.ordered_quantity = to_quantity(
                Decimal(source_line.ordered_quantity) + Decimal(order_line.quantity)
            )
            source_line.status = requisition_line_status_for(
                quantity=source_line.quantity, ordered_quantity=source_line.ordered_quantity
            )
            touched_requisitions.add(source_line.requisition_id)
        # Written now rather than left to a later flush: the session does not
        # autoflush, and a conversion raising several orders from one
        # requisition must not let the next order's reads miss this one's.
        await session.flush()

        # One statement for every requisition the order draws on, not one SELECT
        # per requisition inside the loop: a twenty-line order sourced from
        # twenty requisitions is twenty round trips on the write path, which is
        # the N+1 this module treats as a defect (master rules section 32).
        requisitions = await RequisitionRepository(session, tenant).get_many(
            sorted(touched_requisitions)
        )
        for requisition in sorted(requisitions, key=lambda r: r.id):
            if RequisitionStatus(requisition.status) is RequisitionStatus.APPROVED:
                await _transition(
                    session,
                    tenant,
                    machine=REQUISITION_LIFECYCLE,
                    entity=requisition,
                    entity_type=AUDIT_REQUISITION,
                    entity_code=requisition.code,
                    target=RequisitionStatus.CONVERTED,
                    reason=f"Purchase order {order.code} raised",
                    status_enum=RequisitionStatus,
                )

    if order.source_type is PurchaseSourceType.VENDOR_QUOTATION and order.source_id:
        quotation = await QuotationRepository(session, tenant).get(order.source_id)
        if quotation is not None:
            quotation.converted_purchase_order_id = order.id
            await session.flush()


async def _release_source_lines(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[PurchaseOrderLine]
) -> None:
    """Give the requisition its demand back when order lines stop existing.

    ``ordered_quantity`` is what tells the next buyer a requisition line has
    already been bought. Only ever adding to it means a line whose purchase
    order was cancelled, rejected or edited away stays marked ORDERED for good:
    the demand is never re-raised and the shortage is silently dropped - the
    mirror image of the duplicate :func:`_link_source_documents` exists to
    prevent. Clamped at zero so a partially released line cannot go negative.
    """
    requisition_line_ids = [
        line.requisition_line_id for line in lines if line.requisition_line_id is not None
    ]
    if not requisition_line_ids:
        return

    source_lines = await RequisitionLineRepository(session, tenant).lock_many(requisition_line_ids)
    by_id = {line.id: line for line in source_lines}
    for order_line in lines:
        source_line = by_id.get(order_line.requisition_line_id or uuid.UUID(int=0))
        if source_line is None:
            continue
        released = Decimal(source_line.ordered_quantity) - Decimal(order_line.quantity)
        source_line.ordered_quantity = to_quantity(released if released > 0 else ZERO)
        source_line.status = requisition_line_status_for(
            quantity=source_line.quantity, ordered_quantity=source_line.ordered_quantity
        )
    await session.flush()


async def update_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    tenant_principal: Principal,
    order_id: uuid.UUID,
    payload: PurchaseOrderUpdate,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    """Edit an order that has not yet been approved.

    Past approval the vendor has, or is about to have, a copy of the document
    and a quantity change is an amendment that must go back through approval -
    so the order is rejected here rather than silently rewritten under the
    approver's signature.
    """
    repo = PurchaseOrderRepository(session, tenant)
    line_repo = PurchaseOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        if PurchaseOrderStatus(order.status) not in EDITABLE_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status} and can no longer be edited; "
                "cancel it and raise an amended order.",
                details={"status": str(order.status)},
            )

        before = snapshot(order, ORDER_AUDITED_FIELDS)

        if payload.warehouse_id is not None:
            await _assert_warehouse(session, tenant, payload.warehouse_id)
            order.warehouse_id = payload.warehouse_id
        await _assert_organisation(session, tenant, branch_id=payload.branch_id)
        for field in (
            "expected_delivery_date",
            "branch_id",
            "priority",
            "currency",
            "payment_terms_days",
            "delivery_address",
            "terms",
            "notes",
            "vendor_reference",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(order, field, value)
        if order.expected_delivery_date and order.expected_delivery_date < order.po_date:
            raise ValidationError("The expected delivery date cannot be before the order date.")

        lines: list[PurchaseOrderLine]
        lineage: tuple[uuid.UUID, ...] | None = None
        if payload.lines is not None:
            items = await _load_items(session, tenant, [line.item_id for line in payload.lines])
            _assert_purchasable(items)
            amounts = _price_order_lines(payload.lines, items)

            outgoing = await line_repo.lock_for_order(order.id)
            already_linked = frozenset(
                line.requisition_line_id for line in outgoing if line.requisition_line_id
            )
            # Every requisition the edit touches - the lines going and the lines
            # coming - locked headers first, as every other path does.
            requisitions, source_lines = await _lock_requisition_sources(
                session,
                tenant,
                [
                    *already_linked,
                    *(p.requisition_line_id for p in payload.lines if p.requisition_line_id),
                ],
            )
            _assert_requisition_links(
                payload.lines, requisitions, source_lines, already_linked=already_linked
            )
            await _assert_quotation_links(
                session, tenant, vendor_id=order.vendor_id, lines=payload.lines
            )

            # The outgoing lines gave their quantity to a requisition line when
            # the order was raised. Deleting them without handing it back leaves
            # that demand marked as bought by an order line that no longer
            # exists, so it is never re-raised.
            await _release_source_lines(session, tenant, outgoing)
            await line_repo.delete_for_order(order.id)

            # The category follows the lines - it is derived, never typed.
            order.item_category_id, lineage = await _order_category(
                session,
                tenant,
                vendor_id=order.vendor_id,
                items=items,
                item_ids=[line.item_id for line in payload.lines],
            )
            lines = [
                _apply_order_line(
                    line_repo.add(PurchaseOrderLine(purchase_order_id=order.id)),
                    payload_line,
                    items[payload_line.item_id],
                    line_amounts,
                )
                for payload_line, line_amounts in zip(payload.lines, amounts, strict=True)
            ]
        else:
            lines = await line_repo.list_for_order(order.id)
            amounts = [
                LineAmounts(
                    gross_amount=to_money(Decimal(line.quantity) * Decimal(line.unit_price)),
                    discount_amount=Decimal(line.discount),
                    taxable_amount=to_money(
                        Decimal(line.quantity) * Decimal(line.unit_price) - Decimal(line.discount)
                    ),
                    tax_amount=Decimal(line.tax_amount),
                    line_total=Decimal(line.line_total),
                )
                for line in lines
            ]

        if payload.assigned_vendor_user_id is not None:
            if lineage is None:
                lineage = await _category_lineage_of(session, tenant, order.item_category_id)
            await _assert_vendor_assignee(
                session,
                tenant,
                user_id=payload.assigned_vendor_user_id,
                vendor_id=order.vendor_id,
                category_lineage_=lineage,
            )
            order.assigned_vendor_user_id = payload.assigned_vendor_user_id
        elif (
            lineage is not None
            and order.assigned_vendor_user_id is not None
            and not await _assignee_still_fits(
                session,
                tenant,
                user_id=order.assigned_vendor_user_id,
                vendor_id=order.vendor_id,
                category_lineage_=lineage,
            )
        ):
            # The new lines moved the order out of its assignee's categories.
            # Cleared rather than refused: this payload has no way to clear an
            # assignee, so refusing would leave the edit impossible. Send picks
            # or defaults a new one, and the audit diff records the change.
            order.assigned_vendor_user_id = None

        freight = (
            payload.freight_amount
            if payload.freight_amount is not None
            else Decimal(order.freight_amount)
        )
        _apply_order_totals(order, amounts, freight)
        repo.touch(order)
        await session.flush()

        if payload.lines is not None:
            # Re-attach the replacement lines to whatever they now claim to
            # satisfy, so the requisition's ordered quantity matches the lines
            # that actually exist.
            await _link_source_documents(session, tenant, order, lines)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PURCHASE_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(order, ORDER_AUDITED_FIELDS),
        )

    return order, lines


async def transition_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    order_id: uuid.UUID,
    target: PurchaseOrderStatus,
    *,
    reason: str,
    assigned_vendor_user_id: uuid.UUID | None = None,
    expected_delivery_date: date | None = None,
    vendor_reference: str | None = None,
) -> PurchaseOrder:
    """Submit, approve, reject, send, acknowledge, cancel or close an order."""
    repo = PurchaseOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        assignee_before = order.assigned_vendor_user_id

        if target is PurchaseOrderStatus.PENDING_APPROVAL:
            lines = await PurchaseOrderLineRepository(session, tenant).list_for_order(order.id)
            if not lines:
                raise BusinessRuleError(
                    f"Purchase order {order.code} has no lines and cannot be submitted."
                )
            vendor = await _load_vendor(session, tenant, order.vendor_id)
            _assert_orderable(vendor)
        elif target is PurchaseOrderStatus.APPROVED:
            _assert_separate_approver(principal, order.created_by_id)
            order.approved_by_id = principal.user_id
            order.approved_at = utcnow()
        elif target is PurchaseOrderStatus.REJECTED:
            _assert_separate_approver(principal, order.created_by_id)
            order.rejection_reason = reason
        elif target is PurchaseOrderStatus.SENT:
            # The edge first, so an order that cannot be sent says so rather
            # than complaining about who it would have been sent to.
            PURCHASE_ORDER_LIFECYCLE.assert_transition(PurchaseOrderStatus(order.status), target)
            if order.status == PurchaseOrderStatus.VENDOR_DECLINED:
                # ASSUMPTION: a declined order waits for the buyer, who re-sends
                # or cancels it (docs/assumptions.md §C1-10). A re-send goes back
                # to a vendor that may have been suspended since approval.
                _assert_orderable(await _load_vendor(session, tenant, order.vendor_id))
                # The refusal is history now (its audit row keeps the reason);
                # left on the row it would read as the answer to this send.
                order.vendor_decline_reason = None
            order.assigned_vendor_user_id = await _send_assignee(
                session, tenant, order, requested=assigned_vendor_user_id
            )
            order.sent_at = utcnow()
        elif target is PurchaseOrderStatus.VENDOR_DECLINED:
            # Recorded here rather than by the caller, so no path can decline an
            # order without the reason the buyer needs to decide what to do.
            order.vendor_decline_reason = reason
        elif target is PurchaseOrderStatus.ACKNOWLEDGED:
            order.acknowledged_at = utcnow()
            if expected_delivery_date is not None:
                if expected_delivery_date < order.po_date:
                    raise ValidationError(
                        "The acknowledged delivery date cannot be before the order date."
                    )
                order.expected_delivery_date = expected_delivery_date
            if vendor_reference is not None:
                order.vendor_reference = vendor_reference
        elif target is PurchaseOrderStatus.CANCELLED:
            order.cancellation_reason = reason

        action = {
            PurchaseOrderStatus.APPROVED: AuditAction.PURCHASE_APPROVAL,
            PurchaseOrderStatus.REJECTED: AuditAction.REJECT,
            PurchaseOrderStatus.CANCELLED: AuditAction.CANCEL,
        }.get(target, AuditAction.STATUS_CHANGE)

        await _transition(
            session,
            tenant,
            machine=PURCHASE_ORDER_LIFECYCLE,
            entity=order,
            entity_type=AUDIT_PURCHASE_ORDER,
            entity_code=order.code,
            target=target,
            reason=reason,
            status_enum=PurchaseOrderStatus,
            action=action,
        )

        if target is PurchaseOrderStatus.SENT and order.assigned_vendor_user_id != assignee_before:
            # The status-change row says the order was sent; this one says to
            # whom, which is the half the vendor portal's visibility turns on.
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_PURCHASE_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=f"Assigned {order.code} to a vendor user on sending",
                user_id=tenant.user_id,
                old_values={"assigned_vendor_user_id": assignee_before},
                new_values={"assigned_vendor_user_id": order.assigned_vendor_user_id},
            )

        if target is PurchaseOrderStatus.SENT:
            # The assignee when there is one; otherwise every vendor user who may
            # respond and can see the order (a user scoped to other categories
            # would be told about an order their portal answers 404 for).
            eligible = await vendor_recipients_for_order(
                session, tenant, order, VENDOR_PO_RESPOND_PERMISSION
            )
            recipients = (
                [order.assigned_vendor_user_id]
                if order.assigned_vendor_user_id in eligible
                else eligible
            )
            await notify(
                session,
                company_id=tenant.company_id,
                user_ids=recipients,
                notification_type=NotificationType.PURCHASE_ORDER_SENT,
                title=f"New purchase order {order.code}",
                body=f"Please review and accept {order.code}.",
                entity_type=AUDIT_PURCHASE_ORDER,
                entity_id=order.id,
                action_url=vendor_order_url(order.id),
                actor_id=tenant.user_id,
            )

        if target is PurchaseOrderStatus.CLOSED:
            await _short_close_lines(session, tenant, order)
        elif target in (PurchaseOrderStatus.CANCELLED, PurchaseOrderStatus.REJECTED):
            # Neither state is reachable once anything has been received, so the
            # whole ordered quantity goes back to the requisition that asked for
            # it. Leaving it counted would mark the demand as bought by an order
            # nobody will ever deliver.
            await _release_source_lines(
                session,
                tenant,
                await PurchaseOrderLineRepository(session, tenant).lock_for_order(order.id),
            )

    return order


async def _short_close_lines(
    session: AsyncSession, tenant: TenantContext, order: PurchaseOrder
) -> None:
    """Stop expecting the undelivered balance of a short-closed order."""
    lines = await PurchaseOrderLineRepository(session, tenant).lock_for_order(order.id)
    for line in lines:
        if PurchaseOrderLineStatus(line.status) in (
            PurchaseOrderLineStatus.PENDING,
            PurchaseOrderLineStatus.PARTIALLY_RECEIVED,
        ):
            line.status = po_line_status_for(
                quantity=line.quantity,
                received_quantity=line.received_quantity,
                short_closed=True,
            )
    await session.flush()


async def record_shipment(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    courier_name: str | None,
    tracking_reference: str | None,
    shipped_at: Any = None,
    expected_delivery_date: date | None = None,
) -> PurchaseOrder:
    """Record the inbound courier details (master rules section 23).

    Typically written by the vendor's dispatch manager through the portal, which
    is why it is a separate call from the buyer-side edit: it is allowed on a
    sent order, where the rest of the document is frozen.
    """
    repo = PurchaseOrderRepository(session, tenant)

    async with transaction(session):
        order = await repo.lock_for_update(order_id)
        if order is None:
            raise NotFoundError("Purchase order", order_id)
        # The repository's ``_scoped`` already confines a vendor principal to
        # its own orders, so this cannot be another vendor's row. Kept as a
        # second line of defence, and placed *before* any status or field is
        # inspected: a message built from a foreign row - its code, its status
        # - would itself be the disclosure the "not found" answer exists to
        # prevent (master rules section 16).
        if tenant.vendor_id is not None and order.vendor_id != tenant.vendor_id:
            raise NotFoundError("Purchase order", order_id)
        if PurchaseOrderStatus(order.status) not in (
            *RECEIVABLE_PO_STATUSES,
            PurchaseOrderStatus.APPROVED,
        ):
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; shipment details can only be "
                "recorded on an order that is with the vendor.",
                details={"status": str(order.status)},
            )

        before = snapshot(
            order, ["courier_name", "tracking_reference", "shipped_at", "expected_delivery_date"]
        )
        if courier_name is not None:
            order.courier_name = courier_name
        if tracking_reference is not None:
            order.tracking_reference = tracking_reference
        if shipped_at is not None:
            order.shipped_at = shipped_at
        if expected_delivery_date is not None:
            if expected_delivery_date < order.po_date:
                raise ValidationError("The expected delivery date cannot be before the order date.")
            order.expected_delivery_date = expected_delivery_date

        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PURCHASE_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Recorded inbound shipment details on {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(
                order,
                ["courier_name", "tracking_reference", "shipped_at", "expected_delivery_date"],
            ),
        )

    return order


async def list_purchase_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: PurchaseOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[PurchaseOrderSummary], OffsetPagination]:
    """One page of the order book, each row's category named.

    The labels come from one batched read over the page's distinct categories,
    whatever the page size - the buyers reading this list hold no
    ``category.view`` to name them themselves.
    """
    rows, pagination = await PurchaseOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort, today=today()
    )
    summaries = [PurchaseOrderSummary.model_validate(row) for row in rows]
    categories = await ItemCategoryLookupRepository(session, tenant).load(
        sorted({s.item_category_id for s in summaries if s.item_category_id is not None}, key=str)
    )
    for summary in summaries:
        category = categories.get(summary.item_category_id) if summary.item_category_id else None
        if category is not None:
            summary.item_category_code = category.code
            summary.item_category_name = category.name
    return summaries, pagination


async def get_purchase_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> PurchaseOrderDetail:
    order = await _get_order_for_tenant(session, tenant, order_id)
    lines = await PurchaseOrderLineRepository(session, tenant).list_for_order(order_id)
    return await describe_purchase_order(session, tenant, order, lines)


async def describe_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    lines: Sequence[PurchaseOrderLine],
) -> PurchaseOrderDetail:
    """The detail view, with the category and the assignee named.

    A buyer holds ``purchase_order.view`` but usually neither ``category.view``
    nor ``user.view``, so the two ids alone would read as two UUIDs. At most two
    small reads, and none when the order has neither.
    """
    detail = order_detail(order, lines)
    if order.item_category_id is not None:
        categories = await ItemCategoryLookupRepository(session, tenant).load(
            [order.item_category_id]
        )
        category = categories.get(order.item_category_id)
        if category is not None:
            detail.item_category_code = category.code
            detail.item_category_name = category.name
    if order.assigned_vendor_user_id is not None:
        names = await VendorAssigneeRepository(session, tenant).names(
            [order.assigned_vendor_user_id]
        )
        detail.assigned_vendor_user_name = names.get(order.assigned_vendor_user_id)
    return detail


async def _get_order_for_tenant(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> PurchaseOrder:
    order = await PurchaseOrderRepository(session, tenant).get(order_id)
    # A vendor principal is told "not found" rather than "forbidden": confirming
    # that another vendor's order exists is itself a disclosure. The repository's
    # ``_scoped`` already filters the read; the second clause is belt and braces.
    if order is None or (tenant.vendor_id is not None and order.vendor_id != tenant.vendor_id):
        raise NotFoundError("Purchase order", order_id)
    return order


async def _send_assignee(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    *,
    requested: uuid.UUID | None,
) -> uuid.UUID | None:
    """Who an order goes to when it is sent.

    The user the buyer names, validated - an unfit name is refused. Otherwise
    the one already on the order, if they can still take it; otherwise the
    assignee rule's choice, which may be nobody (``choose_vendor_assignee``).

    An account can be deactivated or re-scoped between conversion and Send. The
    stale assignee is replaced rather than refused, for the reason an edit that
    leaves the assignee's categories clears it: an APPROVED order cannot be
    edited, so refusing here would leave it with no way to the vendor at all
    unless somebody else happened to be eligible to name.
    """
    lineage = await _category_lineage_of(session, tenant, order.item_category_id)
    if requested is not None:
        await _assert_vendor_assignee(
            session, tenant, user_id=requested, vendor_id=order.vendor_id, category_lineage_=lineage
        )
        return requested
    stored = order.assigned_vendor_user_id
    if stored is not None and await _assignee_still_fits(
        session, tenant, user_id=stored, vendor_id=order.vendor_id, category_lineage_=lineage
    ):
        return stored
    users = await VendorAssigneeRepository(session, tenant).candidates([order.vendor_id])
    chosen, _ = choose_vendor_assignee(
        [_as_assignee_candidate(user) for user in users],
        vendor_id=order.vendor_id,
        category_lineage=lineage,
    )
    return chosen


async def list_vendor_assignees(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> list[PurchaseOrderVendorAssignee]:
    """The vendor users this order may be sent to, the rule's choice marked.

    Exactly the users Send would accept (:func:`_assert_vendor_assignee`), so
    the picker cannot offer someone the endpoint then refuses. Three reads: the
    order's category, the vendor's users with their roles, and the labels of
    their category scopes.
    """
    order = await _get_order_for_tenant(session, tenant, order_id)
    lineage = await _category_lineage_of(session, tenant, order.item_category_id)
    users = await VendorAssigneeRepository(session, tenant).candidates([order.vendor_id])
    candidates = {user.id: _as_assignee_candidate(user) for user in users}
    suggested, _ = choose_vendor_assignee(
        candidates.values(), vendor_id=order.vendor_id, category_lineage=lineage
    )
    eligible = [
        user
        for user in users
        if may_take_order(candidates[user.id], vendor_id=order.vendor_id, category_lineage=lineage)
    ]
    labels = await ItemCategoryLookupRepository(session, tenant).load(
        sorted({c for user in eligible for c in user.allowed_category_ids}, key=str)
    )
    return [
        PurchaseOrderVendorAssignee(
            id=user.id,
            full_name=user.full_name,
            email=user.email,
            allowed_category_ids=list(user.allowed_category_ids),
            allowed_category_codes=[
                labels[c].code for c in user.allowed_category_ids if c in labels
            ],
            is_suggested=user.id == suggested,
        )
        for user in eligible
    ]


# --- telling the vendor (WP C1a) ------------------------------------------------


def vendor_order_url(order_id: uuid.UUID) -> str:
    """The vendor app's order page - the deep link of every vendor-facing order alert."""
    return f"/vendor/orders/{order_id}"


def _quantity_text(value: Decimal | None) -> str:
    """A quantity as a sentence states it: ``20``, not ``20.0000``."""
    text = format(Decimal(value or 0).normalize(), "f")
    return "0" if text == "-0" else text


async def vendor_recipients_for_order(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    permission: Permission,
) -> list[uuid.UUID]:
    """The order's vendor users who hold ``permission`` and can see the order.

    ASSUMPTION: a vendor-facing notification reaches only users who could act on
    it (docs/assumptions.md §C1-11).

    "Can see" is ``PurchaseOrderRepository._scoped``'s rule, restated over the
    same facts: an unrestricted user, a user whose category scope covers the
    order's category (or an ancestor of it), or the user the order is assigned
    to. Telling anyone else would hand them a link their portal answers "not
    found" for. Two reads: the order's category lineage and the vendor's users
    with their roles, folded by the request path's own rule.
    """
    lineage = await _category_lineage_of(session, tenant, order.item_category_id)
    users = await VendorAssigneeRepository(session, tenant).candidates([order.vendor_id])
    recipients: list[uuid.UUID] = []
    for user in users:
        if not user.is_active or user.vendor_id != order.vendor_id:
            continue
        granted, _ = resolve_role_grants(
            user.roles, user.extra_permissions, user.denied_permissions
        )
        if not (user.is_superuser or str(permission) in granted):
            continue
        sees_order = (
            not user.allowed_category_ids
            or user.id == order.assigned_vendor_user_id
            or scope_covers_category(user.allowed_category_ids, lineage)
        )
        if sees_order:
            recipients.append(user.id)
    return recipients


async def notify_vendor_about_order(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    *,
    permission: Permission,
    notification_type: NotificationType,
    title: str,
    body: str,
    entity_type: str,
    entity_id: uuid.UUID,
    action_url: str,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    group_key: str | None = None,
) -> None:
    """Tell the order's vendor users who may act on the event, in the caller's transaction."""
    await notify(
        session,
        company_id=tenant.company_id,
        user_ids=await vendor_recipients_for_order(session, tenant, order, permission),
        notification_type=notification_type,
        title=title,
        body=body,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
        group_key=group_key,
        actor_id=tenant.user_id,
    )


async def notify_vendor_about_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    receipt_id: uuid.UUID,
    *,
    permission: Permission,
    notification_type: NotificationType,
    title: str,
    body: str,
    entity_type: str,
    entity_id: uuid.UUID,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    group_key: str | None = None,
) -> None:
    """The same, for an event on one goods receipt (quality's verdict on it).

    The deep link is the receipt's order in the vendor app (``/vendor/orders/:id``,
    WP C2a), whose detail shows the receipts and their QC results. A receipt this
    tenant cannot read tells nobody.
    """
    receipt = await GoodsReceiptRepository(session, tenant).get(receipt_id)
    if receipt is None:
        return
    order = await PurchaseOrderRepository(session, tenant).get(receipt.purchase_order_id)
    if order is None:
        return
    await notify_vendor_about_order(
        session,
        tenant,
        order,
        permission=permission,
        notification_type=notification_type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=vendor_order_url(order.id),
        severity=severity,
        group_key=group_key,
    )


# --- dispatch tracking (master rules section 23) ------------------------------


async def order_tracking(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> PurchaseOrderTracking:
    """The inbound tracking view of one order.

    Four reads: the order, its lines, its receipts and the vendor. Every number
    on the board is derived from the order lines rather than re-aggregated from
    the receipt lines, because those totals are exactly what posting maintains.
    """
    order = await _get_order_for_tenant(session, tenant, order_id)
    lines = await PurchaseOrderLineRepository(session, tenant).list_for_order(order.id)
    receipts = await GoodsReceiptRepository(session, tenant).for_order(order.id)
    vendors = await VendorLookupRepository(session, tenant).load([order.vendor_id])
    qc_pending_lines = await GoodsReceiptLineRepository(session, tenant).qc_pending_count(order.id)

    ordered = to_quantity(sum((Decimal(line.quantity) for line in lines), ZERO))
    received = to_quantity(sum((Decimal(line.received_quantity) for line in lines), ZERO))
    accepted = to_quantity(sum((Decimal(line.accepted_quantity) for line in lines), ZERO))
    rejected = to_quantity(sum((Decimal(line.rejected_quantity) for line in lines), ZERO))

    open_lines = [
        line
        for line in lines
        if PurchaseOrderLineStatus(line.status)
        in (PurchaseOrderLineStatus.PENDING, PurchaseOrderLineStatus.PARTIALLY_RECEIVED)
    ]
    pending = to_quantity(
        sum(
            (
                Decimal(line.quantity) - Decimal(line.received_quantity)
                for line in open_lines
                if Decimal(line.quantity) > Decimal(line.received_quantity)
            ),
            ZERO,
        )
    )

    status = PurchaseOrderStatus(order.status)
    grn_pending = bool(open_lines) and status in RECEIVABLE_PO_STATUSES
    as_of = today()
    days_late = (
        (as_of - order.expected_delivery_date).days
        if order.expected_delivery_date is not None and order.expected_delivery_date < as_of
        else 0
    )

    return PurchaseOrderTracking(
        purchase_order_id=order.id,
        code=order.code,
        status=status,
        vendor_id=order.vendor_id,
        vendor_name=vendors[order.vendor_id].name if order.vendor_id in vendors else "",
        warehouse_id=order.warehouse_id,
        po_date=order.po_date,
        expected_delivery_date=order.expected_delivery_date,
        sent_at=order.sent_at,
        acknowledged_at=order.acknowledged_at,
        shipped_at=order.shipped_at,
        courier_name=order.courier_name,
        tracking_reference=order.tracking_reference,
        ordered_quantity=ordered,
        received_quantity=received,
        accepted_quantity=accepted,
        rejected_quantity=rejected,
        pending_quantity=pending,
        # Late only while something is still outstanding: an order delivered a
        # week after the promised date is a supplier performance fact, not a
        # live problem for the tracking board.
        is_delayed=po_is_late(
            status=str(status),
            open_quantity=pending,
            expected_date=order.expected_delivery_date,
            as_of=as_of,
        ),
        days_late=days_late,
        grn_pending=grn_pending,
        qc_pending=qc_pending_lines > 0 or status is PurchaseOrderStatus.QC_PENDING,
        qc_pending_line_count=qc_pending_lines,
        receipts=[
            TrackedReceipt(
                id=receipt.id,
                code=receipt.code,
                receipt_date=receipt.receipt_date,
                status=GoodsReceiptStatus(receipt.status),
                total_received_quantity=Decimal(receipt.total_received_quantity),
                total_accepted_quantity=Decimal(receipt.total_accepted_quantity),
                total_rejected_quantity=Decimal(receipt.total_rejected_quantity),
            )
            for receipt in receipts
        ],
    )


# =============================================================================
# Requisition to purchase orders: the category-wise split
# =============================================================================
#
# Approved requisitions become one DRAFT purchase order per (vendor, procurement
# category, warehouse). The rules that decide each step are pure functions in
# ``service_rules``; this section loads what they read - one statement per
# entity type, whatever the line count - and writes what they decide, inside
# one transaction.


@dataclass(slots=True)
class _ConversionPlan:
    """Everything a conversion resolved, before (preview) or for (convert) writing."""

    requisitions: list[PurchaseRequisition]
    groups: list[ConversionGroup]
    unresolved: list[RequisitionConversionUnresolvedLine]
    skipped_line_count: int
    po_date: date
    items: dict[uuid.UUID, PurchasableItem]
    vendors: dict[uuid.UUID, Vendor]
    categories: dict[uuid.UUID, CategoryFacts]
    #: Per group, in group order: the chosen vendor user and why.
    assignees: list[tuple[uuid.UUID | None, VendorAssigneeReason]]
    user_names: dict[uuid.UUID, str]


_UNRESOLVED_NO_VENDOR = (
    "No vendor: none was named, the requisition line suggests none, the item has no "
    "preferred vendor and its category has no preferred vendor."
)


def _unorderable_item_reasons(item: PurchasableItem | None, item_code: str) -> list[str]:
    """Why the item on an approved requisition line can no longer be ordered, if so.

    The rules a buyer's own order applies (:func:`_assert_purchasable`), stated
    per line: a requisition approved last month can name an item retired,
    deactivated or unflagged for purchase since. Refusing the whole proposal for
    it would leave the buyer nothing to act on; an unresolved line is shown,
    with this reason, and can be left out (docs/assumptions.md §51).
    """
    if item is None:
        return [f"Item {item_code} has been retired and can no longer be ordered."]
    reasons: list[str] = []
    if not item.can_purchase:
        reasons.append(f"Item {item_code} is not flagged as purchasable.")
    if not item.is_active:
        reasons.append(f"Item {item_code} is inactive and cannot be ordered.")
    return reasons


async def _plan_conversion(
    session: AsyncSession,
    tenant: TenantContext,
    requisition_ids: Sequence[uuid.UUID],
    payload: RequisitionConversionRequest,
    *,
    lock: bool,
) -> _ConversionPlan:
    """Resolve every open line of the requisitions into proposed orders.

    With ``lock`` the requisition headers and then their lines are locked, each
    in primary-key order, before anything is read from them - the conversion's
    half of the lock order every ordered-quantity writer shares. Without it
    (the preview) nothing is locked and nothing is written.
    """
    ids = list(dict.fromkeys(requisition_ids))
    requisition_repo = RequisitionRepository(session, tenant)
    line_repo = RequisitionLineRepository(session, tenant)

    loaded = await (requisition_repo.lock_many(ids) if lock else requisition_repo.get_many(ids))
    by_id = {requisition.id: requisition for requisition in loaded}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        raise NotFoundError("Purchase requisition", ", ".join(missing))
    requisitions = sorted(by_id.values(), key=lambda requisition: requisition.code)
    closed = [
        f"{requisition.code} is {requisition.status}"
        for requisition in requisitions
        if RequisitionStatus(requisition.status) not in OPEN_REQUISITION_STATUSES
    ]
    if closed:
        raise BusinessRuleError(
            "Only an approved requisition can be converted into purchase orders: "
            + "; ".join(closed)
            + ".",
            details={"requisitions": closed},
        )

    if lock:
        lines = await line_repo.lock_for_requisitions(ids)
    else:
        grouped = await line_repo.list_for_requisitions(ids)
        lines = [line for requisition_id in ids for line in grouped.get(requisition_id, [])]

    overrides = {override.requisition_line_id: override for override in payload.lines}
    foreign = sorted({str(i) for i in overrides} - {str(line.id) for line in lines})
    if foreign:
        raise ValidationError(
            "These lines are not on the requisitions being converted: " + ", ".join(foreign) + ".",
            details={"requisition_line_ids": foreign},
        )

    # What is still to order: live lines with ordered quantity short of the
    # request, less anything the buyer excluded.
    po_date = payload.po_date or today()
    open_lines: list[
        tuple[PurchaseRequisitionLine, RequisitionConversionLineOverride | None, Decimal]
    ] = []
    skipped = 0
    for line in sorted(lines, key=lambda row: (by_id[row.requisition_id].code, row.line_number)):
        override = overrides.get(line.id)
        outstanding = to_quantity(Decimal(line.quantity) - Decimal(line.ordered_quantity))
        if (
            RequisitionLineStatus(line.status) is RequisitionLineStatus.CANCELLED
            or outstanding <= 0
            or (override is not None and override.exclude)
        ):
            skipped += 1
            continue
        open_lines.append((line, override, outstanding))
    if not open_lines:
        return _ConversionPlan(
            requisitions=requisitions,
            groups=[],
            unresolved=[],
            skipped_line_count=skipped,
            po_date=po_date,
            items={},
            vendors={},
            categories={},
            assignees=[],
            user_names={},
        )

    # Everything the rules read, one statement per entity type. The item master
    # is read as it stands now, not as it stood at approval: an item retired,
    # deactivated or unflagged for purchase since then makes its own line
    # unresolved - listed with the reason, so the buyer can leave it out -
    # instead of failing the whole proposal with no line on screen to act on.
    item_ids = [line.item_id for line, _, _ in open_lines]
    item_repo = PurchasableItemRepository(session, tenant)
    items = await item_repo.load(item_ids)
    retired_codes = await item_repo.codes(
        [item_id for item_id in dict.fromkeys(item_ids) if item_id not in items]
    )
    item_category_ids = {item.category_id for item in items.values()}
    categories = await ItemCategoryLookupRepository(session, tenant).load(
        sorted((c for c in item_category_ids if c is not None), key=str)
    )
    lineages = {
        category_id: category_lineage(facts.path) for category_id, facts in categories.items()
    }
    terms = await ItemVendorLinkRepository(session, tenant).purchasing_terms(item_ids)
    preferred_by_category = await VendorCategoryLookupRepository(session, tenant).preferred_for(
        sorted({c for lineage in lineages.values() for c in lineage}, key=str)
    )

    def item_lineage(item_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        item = items.get(item_id)
        if item is None or item.category_id is None:
            return ()
        return lineages.get(item.category_id, ())

    choices = [
        resolve_line_vendor(
            override=override.vendor_id if override else None,
            suggested=line.suggested_vendor_id,
            item_preferred=terms.preferred_vendor.get(line.item_id),
            category_preferred=preferred_category_vendor(
                item_lineage(line.item_id), preferred_by_category
            ),
        )
        for line, override, _ in open_lines
    ]
    named_vendors = {o.vendor_id for o in overrides.values() if o.vendor_id is not None}
    vendors = {
        vendor.id: vendor
        for vendor in await VendorLookupRepository(session, tenant).get_many(
            sorted({v for v, _ in choices if v is not None} | named_vendors, key=str)
        )
    }
    # A vendor the buyer named and this tenant does not have is a bad request,
    # answered the way a foreign id always is - not an unresolved line.
    unknown = sorted(str(v) for v in named_vendors if v not in vendors)
    if unknown:
        raise NotFoundError("Vendor", ", ".join(unknown))
    qualifications = await VendorCategoryLookupRepository(session, tenant).categories_of(
        sorted(vendors, key=str)
    )

    planned: list[PlannedConversionLine] = []
    unresolved: list[RequisitionConversionUnresolvedLine] = []
    for (line, override, outstanding), (vendor_id, vendor_source) in zip(
        open_lines, choices, strict=True
    ):
        requisition = by_id[line.requisition_id]
        item = items.get(line.item_id)
        item_code = (
            item.code if item is not None else retired_codes.get(line.item_id, str(line.item_id))
        )
        vendor = vendors.get(vendor_id) if vendor_id is not None else None
        reasons = _unorderable_item_reasons(item, item_code)
        if vendor_id is None:
            reasons.append(_UNRESOLVED_NO_VENDOR)
        elif vendor is None:
            reasons.append("The vendor this line resolves to has been archived.")
        elif not can_receive_purchase_orders(vendor):
            reasons.append(f"Vendor {vendor.code} is not approved for purchase orders.")
        price, price_source = resolve_line_price(
            override=override.unit_price if override else None,
            estimated=line.estimated_unit_price,
            last_purchase=terms.last_price.get((line.item_id, vendor_id)) if vendor_id else None,
        )
        if price is None:
            reasons.append(
                "No price: none was named, the requisition line has no estimate, and "
                + (
                    f"vendor {vendor.code} has no last purchase price for {item_code}."
                    if vendor is not None
                    else "there is no vendor to take a last purchase price from."
                )
            )
        if (
            reasons
            or vendor_id is None
            or vendor_source is None
            or price is None
            or price_source is None
        ):
            unresolved.append(
                RequisitionConversionUnresolvedLine(
                    requisition_id=requisition.id,
                    requisition_code=requisition.code,
                    requisition_line_id=line.id,
                    line_number=line.line_number,
                    item_id=line.item_id,
                    item_code=item_code,
                    vendor_id=vendor_id,
                    reasons=reasons,
                )
            )
            continue

        lineage = item_lineage(line.item_id)
        procurement_category = resolve_procurement_category(
            lineage, qualifications.get(vendor_id, frozenset())
        )
        planned.append(
            PlannedConversionLine(
                requisition_id=requisition.id,
                requisition_code=requisition.code,
                requisition_line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                description=line.description,
                quantity=to_quantity(
                    override.quantity if override and override.quantity else outstanding
                ),
                uom_id=line.uom_id,
                required_date=conversion_delivery_date(
                    line.required_date or requisition.required_date, po_date
                ),
                unit_price=price,
                price_source=price_source,
                vendor_id=vendor_id,
                vendor_source=vendor_source,
                item_category_id=procurement_category,
                category_lineage=lineage_from(lineage, procurement_category),
                warehouse_id=requisition.warehouse_id,
                priority=int(requisition.priority),
            )
        )

    groups = group_conversion_lines(planned)

    # A procurement category can be an ancestor the item lookup did not read.
    wanted = sorted(
        {
            g.item_category_id
            for g in groups
            if g.item_category_id is not None and g.item_category_id not in categories
        },
        key=str,
    )
    if wanted:
        categories = {
            **categories,
            **await ItemCategoryLookupRepository(session, tenant).load(wanted),
        }

    users = await VendorAssigneeRepository(session, tenant).candidates(
        sorted({group.vendor_id for group in groups}, key=str)
    )
    candidates = [_as_assignee_candidate(user) for user in users]
    return _ConversionPlan(
        requisitions=requisitions,
        groups=groups,
        unresolved=unresolved,
        skipped_line_count=skipped,
        po_date=po_date,
        items=items,
        vendors=vendors,
        categories=categories,
        assignees=[
            choose_vendor_assignee(
                candidates, vendor_id=group.vendor_id, category_lineage=group.category_lineage
            )
            for group in groups
        ],
        user_names={user.id: user.full_name for user in users},
    )


def _group_expected_date(
    group: ConversionGroup, payload: RequisitionConversionRequest
) -> date | None:
    """The header promise: the buyer's date, else the earliest date a line asks for."""
    if payload.expected_delivery_date is not None:
        return payload.expected_delivery_date
    dates = [line.required_date for line in group.lines if line.required_date is not None]
    return min(dates) if dates else None


def _group_priority(group: ConversionGroup, payload: RequisitionConversionRequest) -> int:
    """The buyer's priority, else the most urgent of the requisitions in the order."""
    if payload.priority is not None:
        return payload.priority
    return min(line.priority for line in group.lines)


def _conversion_order_payload(
    group: ConversionGroup,
    payload: RequisitionConversionRequest,
    *,
    po_date: date,
    assignee_id: uuid.UUID | None,
) -> PurchaseOrderCreate:
    """The order one group becomes, in the same shape a buyer's own order takes."""
    return PurchaseOrderCreate(
        vendor_id=group.vendor_id,
        warehouse_id=group.warehouse_id,
        po_date=po_date,
        expected_delivery_date=_group_expected_date(group, payload),
        priority=_group_priority(group, payload),
        # ASSUMPTION: a converted order is in the company's currency. Its prices
        # come from a requisition estimate or a last purchase price, and a
        # requisition carries no currency of its own (docs/assumptions.md §52).
        currency=DEFAULT_CURRENCY,
        delivery_address=payload.delivery_address,
        terms=payload.terms,
        notes=payload.notes,
        assigned_vendor_user_id=assignee_id,
        source_type=PurchaseSourceType.PURCHASE_REQUISITION,
        source_id=group.source_requisition_id,
        acknowledge_duplicates=payload.acknowledge_duplicates,
        duplicate_acknowledgement_reason=payload.duplicate_acknowledgement_reason,
        lines=[
            PurchaseOrderLineIn(
                line_number=number,
                item_id=line.item_id,
                description=line.description,
                quantity=line.quantity,
                uom_id=line.uom_id,
                unit_price=line.unit_price,
                required_date=line.required_date,
                requisition_line_id=line.requisition_line_id,
            )
            for number, line in enumerate(group.lines, start=1)
        ],
    )


async def _conversion_duplicates(
    session: AsyncSession,
    tenant: TenantContext,
    plan: _ConversionPlan,
    payload: RequisitionConversionRequest,
) -> list[list[DuplicateCandidate]]:
    """Each proposed order's duplicate candidates, from one statement for them all."""
    if not plan.groups:
        return []
    proposed = [
        [
            ProposedLine(
                item_id=line.item_id,
                required_date=line.required_date or _group_expected_date(group, payload),
                requisition_line_id=line.requisition_line_id,
            )
            for line in group.lines
        ]
        for group in plan.groups
    ]
    every = [line for group_lines in proposed for line in group_lines]
    date_from, date_to = _date_envelope([line.required_date for line in every])
    rows = await PurchaseOrderRepository(session, tenant).open_lines_for_conversion_check(
        vendor_ids=[group.vendor_id for group in plan.groups],
        warehouse_ids=[group.warehouse_id for group in plan.groups],
        item_ids=[line.item_id for line in every],
        date_from=date_from,
        date_to=date_to,
        requisition_ids=[requisition.id for requisition in plan.requisitions],
        requisition_line_ids=[
            line.requisition_line_id for line in every if line.requisition_line_id is not None
        ],
        group_count=len(plan.groups),
    )
    candidates = [_as_candidate_input(row) for row in rows]
    return [
        _classify_for_lines(
            candidates,
            vendor_id=group.vendor_id,
            warehouse_id=group.warehouse_id,
            lines=proposed[index],
            source_type=PurchaseSourceType.PURCHASE_REQUISITION,
            source_id=group.source_requisition_id,
        )
        for index, group in enumerate(plan.groups)
    ]


def _conversion_result(
    plan: _ConversionPlan,
    payload: RequisitionConversionRequest,
    *,
    duplicates: Sequence[Sequence[DuplicateCandidate]],
    orders: Sequence[PurchaseOrder],
    converted: bool,
) -> RequisitionConversionResult:
    groups: list[RequisitionConversionGroup] = []
    for index, group in enumerate(plan.groups):
        vendor = plan.vendors[group.vendor_id]
        category = plan.categories.get(group.item_category_id) if group.item_category_id else None
        assignee_id, reason = plan.assignees[index]
        line_reads: list[RequisitionConversionLine] = []
        amounts: list[LineAmounts] = []
        for line in group.lines:
            item = plan.items[line.item_id]
            line_amounts = compute_line_amounts(
                quantity=line.quantity, unit_price=line.unit_price, tax_rate=item.gst_rate
            )
            amounts.append(line_amounts)
            line_reads.append(
                RequisitionConversionLine(
                    requisition_id=line.requisition_id,
                    requisition_code=line.requisition_code,
                    requisition_line_id=line.requisition_line_id,
                    line_number=line.line_number,
                    item_id=line.item_id,
                    item_code=item.code,
                    item_name=item.name,
                    description=line.description,
                    quantity=line.quantity,
                    uom_id=line.uom_id,
                    required_date=line.required_date,
                    unit_price=line.unit_price,
                    price_source=line.price_source,
                    vendor_source=line.vendor_source,
                    line_total=line_amounts.line_total,
                )
            )
        totals = sum_document_totals(amounts)
        order = orders[index] if index < len(orders) else None
        groups.append(
            RequisitionConversionGroup(
                vendor_id=vendor.id,
                vendor_code=vendor.code,
                vendor_name=vendor.name,
                item_category_id=group.item_category_id,
                item_category_code=category.code if category else None,
                item_category_name=category.name if category else None,
                warehouse_id=group.warehouse_id,
                source_requisition_id=group.source_requisition_id,
                assigned_vendor_user_id=assignee_id,
                assigned_vendor_user_name=plan.user_names.get(assignee_id) if assignee_id else None,
                assignee_reason=reason,
                currency=DEFAULT_CURRENCY,
                expected_delivery_date=_group_expected_date(group, payload),
                priority=_group_priority(group, payload),
                subtotal=totals.subtotal,
                tax_amount=totals.tax_amount,
                total_amount=totals.total_amount,
                lines=line_reads,
                duplicate_candidates=[_duplicate_read(c) for c in duplicates[index]],
                purchase_order_id=order.id if order is not None else None,
                purchase_order_code=order.code if order is not None else None,
            )
        )
    return RequisitionConversionResult(
        requisitions=[RequisitionSummary.model_validate(r) for r in plan.requisitions],
        groups=groups,
        unresolved_lines=plan.unresolved,
        skipped_line_count=plan.skipped_line_count,
        max_orders=MAX_ORDERS_PER_CONVERSION,
        requires_acknowledgement=any(duplicates),
        converted=converted,
    )


async def preview_requisition_conversion(
    session: AsyncSession,
    tenant: TenantContext,
    requisition_ids: Sequence[uuid.UUID],
    payload: RequisitionConversionRequest,
) -> RequisitionConversionResult:
    """What converting these requisitions would raise. Read-only.

    Every proposed order with its vendor, category, assignee, prices and their
    sources, and the open orders it would collide with; every line that cannot
    be converted, with every reason. Nothing is locked or written, so the buyer
    can change the overrides and ask again as often as they like.
    """
    plan = await _plan_conversion(session, tenant, requisition_ids, payload, lock=False)
    duplicates = await _conversion_duplicates(session, tenant, plan, payload)
    return _conversion_result(plan, payload, duplicates=duplicates, orders=[], converted=False)


async def convert_requisitions_to_orders(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    requisition_ids: Sequence[uuid.UUID],
    payload: RequisitionConversionRequest,
) -> RequisitionConversionResult:
    """Raise one DRAFT purchase order per (vendor, procurement category, warehouse).

    One transaction: every requisition and line is locked, every line resolved,
    every order created through the same body a buyer's own order uses (the
    duplicate refusal included) and linked back to its requisition, which moves
    APPROVED to CONVERTED.

    ASSUMPTION: a line that cannot be resolved refuses the whole conversion and
    nothing is written (docs/assumptions.md §51), and the orders are raised as
    DRAFT, going through submit, approve and send like any other
    (docs/assumptions.md §53).
    """
    async with transaction(session):
        plan = await _plan_conversion(session, tenant, requisition_ids, payload, lock=True)
        if plan.unresolved:
            raise BusinessRuleError(
                f"{len(plan.unresolved)} line(s) cannot be converted, so no purchase order was "
                "raised: "
                + "; ".join(
                    f"{u.requisition_code} line {u.line_number} ({u.item_code}): "
                    + " ".join(u.reasons)
                    for u in plan.unresolved
                ),
                details={"unresolved_lines": [u.model_dump(mode="json") for u in plan.unresolved]},
            )
        if not plan.groups:
            codes = ", ".join(requisition.code for requisition in plan.requisitions)
            raise BusinessRuleError(
                f"Nothing is left to order on {codes}: every line is fully ordered, cancelled "
                "or excluded.",
                details={"requisitions": [r.code for r in plan.requisitions]},
            )
        assert_within_order_limit(len(plan.groups))
        oversized = [group for group in plan.groups if len(group.lines) > MAX_LINES_PER_DOCUMENT]
        if oversized:
            raise BusinessRuleError(
                f"A purchase order cannot have more than {MAX_LINES_PER_DOCUMENT} lines; convert "
                "fewer requisitions at once.",
                details={"max_lines_per_document": MAX_LINES_PER_DOCUMENT},
            )

        # T9: every duplicate lock the orders below will take, taken once here
        # in one sorted order, so two conversions sharing a vendor and warehouse
        # queue rather than deadlock. Re-taking one inside an order is a no-op.
        # A bounded loop: at most MAX_ORDERS_PER_CONVERSION keys.
        for vendor_id, warehouse_id in sorted(
            {(group.vendor_id, group.warehouse_id) for group in plan.groups},
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            await advisory_xact_lock(
                session, tenant.company_id, "po-duplicate", vendor_id, warehouse_id
            )

        # One order per group, each a bounded run of writes (see
        # MAX_ORDERS_PER_CONVERSION) - the precedent ``_link_source_documents``
        # and ``_raise_inspections`` set for per-document work inside one
        # transaction.
        orders: list[PurchaseOrder] = []
        duplicates: list[list[DuplicateCandidate]] = []
        for index, group in enumerate(plan.groups):
            assignee_id, _ = plan.assignees[index]
            order, _, candidates = await _create_purchase_order(
                session,
                tenant,
                principal,
                _conversion_order_payload(
                    group, payload, po_date=plan.po_date, assignee_id=assignee_id
                ),
                vendor=plan.vendors[group.vendor_id],
                items=plan.items,
                item_category_id=group.item_category_id,
            )
            orders.append(order)
            duplicates.append(candidates)

        await _audit_conversion(session, tenant, plan, orders)

    return _conversion_result(plan, payload, duplicates=duplicates, orders=orders, converted=True)


async def _audit_conversion(
    session: AsyncSession,
    tenant: TenantContext,
    plan: _ConversionPlan,
    orders: Sequence[PurchaseOrder],
) -> None:
    """One summary row per requisition: which orders it was split into, and why each.

    Each order already has its own CREATE row; this is the requisition's side
    of the same event, carrying every group's vendor, category and assignee in
    ``context``. Written with one flush for the whole set.
    """
    for requisition in plan.requisitions:
        raised = [
            (group, order, plan.assignees[index])
            for index, (group, order) in enumerate(zip(plan.groups, orders, strict=True))
            if requisition.id in group.requisition_ids
        ]
        if not raised:
            continue
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_REQUISITION,
            entity_id=requisition.id,
            entity_code=requisition.code,
            summary=f"Split into {', '.join(order.code for _, order, _ in raised)}",
            user_id=tenant.user_id,
            context={
                "purchase_orders": [
                    {
                        "purchase_order_id": order.id,
                        "purchase_order_code": order.code,
                        "vendor_id": group.vendor_id,
                        "vendor_code": plan.vendors[group.vendor_id].code,
                        "item_category_id": group.item_category_id,
                        "warehouse_id": group.warehouse_id,
                        "assigned_vendor_user_id": assignee_id,
                        "assignee_reason": reason,
                        "line_count": sum(
                            1 for line in group.lines if line.requisition_id == requisition.id
                        ),
                    }
                    for group, order, (assignee_id, reason) in raised
                ]
            },
            flush=False,
        )
    await session.flush()


# =============================================================================
# Goods receipt
# =============================================================================


def receipt_detail(receipt: GoodsReceipt, lines: Sequence[GoodsReceiptLine]) -> GoodsReceiptDetail:
    detail = GoodsReceiptDetail.model_validate(receipt)
    detail.lines = [GoodsReceiptLineRead.model_validate(line) for line in lines]
    return detail


@dataclass(frozen=True, slots=True)
class _ReceiptContext:
    """Everything the posting path needs, fetched once and validated together."""

    receipt: GoodsReceipt
    order: PurchaseOrder
    lines: list[GoodsReceiptLine]
    order_lines: dict[uuid.UUID, PurchaseOrderLine]
    items: dict[uuid.UUID, PurchasableItem]


async def create_goods_receipt(
    session: AsyncSession, tenant: TenantContext, payload: GoodsReceiptCreate
) -> tuple[GoodsReceipt, list[GoodsReceiptLine]]:
    """Record a delivery as a DRAFT. No stock moves until it is posted.

    The tolerance is checked here as well as at posting, so the storekeeper is
    told at the gate rather than after keying a whole delivery note.
    """
    repo = GoodsReceiptRepository(session, tenant)
    line_repo = GoodsReceiptLineRepository(session, tenant)
    order_repo = PurchaseOrderRepository(session, tenant)
    order_line_repo = PurchaseOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await order_repo.get(payload.purchase_order_id)
        if order is None:
            raise NotFoundError("Purchase order", payload.purchase_order_id)
        assert_warehouse_scope(tenant, order.warehouse_id)
        if PurchaseOrderStatus(order.status) not in RECEIVABLE_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; goods can only be received "
                "against an order that has been sent to the vendor.",
                details={"status": str(order.status), "code": order.code},
            )

        order_lines = {line.id: line for line in await order_line_repo.list_for_order(order.id)}
        items = await _load_items(session, tenant, [line.item_id for line in order_lines.values()])

        receipt_date = payload.receipt_date or today()
        code = await next_document_number(session, tenant.company_id, DocTypes.GRN, on=receipt_date)
        receipt = GoodsReceipt(
            code=code,
            purchase_order_id=order.id,
            vendor_id=order.vendor_id,
            warehouse_id=order.warehouse_id,
            receipt_date=receipt_date,
            status=GoodsReceiptStatus.DRAFT,
            vendor_delivery_note=payload.vendor_delivery_note,
            vendor_invoice_number=payload.vendor_invoice_number,
            gate_entry_number=payload.gate_entry_number,
            vehicle_number=payload.vehicle_number,
            transporter_id=payload.transporter_id,
            notes=payload.notes,
        )
        repo.add(receipt)
        await session.flush()

        lines: list[GoodsReceiptLine] = []
        for payload_line in payload.lines:
            order_line = order_lines.get(payload_line.purchase_order_line_id)
            if order_line is None:
                raise ValidationError(
                    f"Line {payload_line.line_number} references a purchase order line that is "
                    f"not on {order.code}.",
                    details={
                        "line_number": payload_line.line_number,
                        "purchase_order_line_id": str(payload_line.purchase_order_line_id),
                    },
                )
            item = items[order_line.item_id]
            if item.is_serial_tracked and not payload_line.serial_numbers:
                raise BusinessRuleError(
                    f"Item {item.code} is serial tracked; name one serial per accepted unit "
                    f"on line {payload_line.line_number}.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={"item_code": item.code, "item_id": str(item.id)},
                )
            if not item.is_serial_tracked and payload_line.serial_numbers:
                raise BusinessRuleError(
                    f"Line {payload_line.line_number}: {item.code} is not serial tracked; "
                    "remove the serial numbers.",
                    details={"item_code": item.code, "line_number": payload_line.line_number},
                )
            if item.is_batch_tracked and not (payload_line.batch_number or "").strip():
                raise BusinessRuleError(
                    f"Item {item.code} is batch tracked; the vendor's lot number is required "
                    f"on line {payload_line.line_number}.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"item_code": item.code, "line_number": payload_line.line_number},
                )
            assert_within_over_receipt_tolerance(
                item_code=item.code,
                line_number=payload_line.line_number,
                ordered_quantity=order_line.quantity,
                already_received=order_line.received_quantity,
                incoming_quantity=payload_line.received_quantity,
            )

            requires_qc = (
                order_line.requires_qc
                if payload_line.requires_qc is None
                else payload_line.requires_qc
            )
            # Nothing was taken into stock, so there is nothing to inspect. A
            # line left flagged for inspection with no accepted quantity gets no
            # inspection raised at posting - the posting path only raises them
            # for quantity it actually put on QC hold - and then holds the
            # purchase order in QC_PENDING forever, because only an inspection
            # decision can leave that state.
            if to_quantity(payload_line.accepted_quantity) <= 0:
                requires_qc = False
            # ASSUMPTION: goods held for incoming inspection are received at
            # warehouse level, never into a bin. The inspection - and the
            # decision that releases, damages or scraps the lot - has no bin
            # of its own and moves stock at the unbinned position, so a lot held
            # in a bin could never be decided ('Only 0 of X is in QC hold').
            # Released stock is put away into its bin afterwards with a bin move
            # (docs/assumptions.md §E8-7).
            if requires_qc and payload_line.location_id is not None:
                raise BusinessRuleError(
                    f"Line {payload_line.line_number}: {item.code} is held for incoming "
                    "inspection, so it is received at warehouse level. Put it away into a "
                    "bin with a bin move once it is released.",
                    details={
                        "line_number": payload_line.line_number,
                        "item_code": item.code,
                        "location_id": str(payload_line.location_id),
                    },
                )
            taxable = to_money(
                Decimal(order_line.quantity) * Decimal(order_line.unit_price)
                - Decimal(order_line.discount)
            )
            line = line_repo.add(
                GoodsReceiptLine(
                    goods_receipt_id=receipt.id,
                    line_number=payload_line.line_number,
                    purchase_order_line_id=order_line.id,
                    item_id=order_line.item_id,
                    description=order_line.description or item.name,
                    received_quantity=to_quantity(payload_line.received_quantity),
                    accepted_quantity=to_quantity(payload_line.accepted_quantity),
                    rejected_quantity=to_quantity(payload_line.rejected_quantity),
                    uom_id=order_line.uom_id,
                    unit_cost=receipt_unit_cost(
                        quantity=Decimal(order_line.quantity),
                        taxable_amount=taxable,
                        fallback_unit_price=Decimal(order_line.unit_price),
                    ),
                    batch_number=(payload_line.batch_number or "").strip().upper() or None,
                    serial_numbers=payload_line.serial_numbers or None,
                    manufactured_on=payload_line.manufactured_on,
                    expires_on=payload_line.expires_on,
                    location_id=payload_line.location_id,
                    requires_qc=requires_qc,
                    qc_status=(
                        ReceiptLineQcStatus.PENDING
                        if requires_qc
                        else ReceiptLineQcStatus.NOT_REQUIRED
                    ),
                    rejection_reason=payload_line.rejection_reason,
                    notes=payload_line.notes,
                )
            )
            lines.append(line)

        receipt.total_received_quantity = to_quantity(
            sum((Decimal(line.received_quantity) for line in lines), ZERO)
        )
        receipt.total_accepted_quantity = to_quantity(
            sum((Decimal(line.accepted_quantity) for line in lines), ZERO)
        )
        receipt.total_rejected_quantity = to_quantity(
            sum((Decimal(line.rejected_quantity) for line in lines), ZERO)
        )
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_GOODS_RECEIPT,
            entity_id=receipt.id,
            entity_code=receipt.code,
            summary=(
                f"Recorded {receipt.code} against {order.code}: "
                f"{receipt.total_received_quantity} received, "
                f"{receipt.total_rejected_quantity} rejected"
            ),
            user_id=tenant.user_id,
            new_values={
                "purchase_order_id": order.id,
                "vendor_id": receipt.vendor_id,
                "warehouse_id": receipt.warehouse_id,
                "line_count": len(lines),
                "total_received_quantity": receipt.total_received_quantity,
            },
        )

    return receipt, lines


async def _resolve_batches(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[GoodsReceiptLine],
    *,
    vendor_id: uuid.UUID,
    received_on: date,
) -> None:
    """Attach a ``stock_batches`` row to every batch-tracked receipt line.

    Existing lots are resolved in one statement. A genuinely new lot is created
    through ``inventory.TraceabilityService`` rather than inserted here, so the
    duplicate check and the audit entry for a batch stay in the module that owns
    the table - one call per new lot on the document, not one per line.
    """
    tracked = [line for line in lines if line.batch_number]
    if not tracked:
        return

    lookup = BatchLookupRepository(session, tenant)
    known = await lookup.by_numbers([(line.item_id, line.batch_number or "") for line in tracked])

    traceability = TraceabilityService(session, tenant)
    for line in tracked:
        key = (line.item_id, line.batch_number or "")
        batch_id = known.get(key)
        if batch_id is None:
            created = await traceability.create_batch(
                StockBatchCreate(
                    item_id=line.item_id,
                    batch_number=line.batch_number or "",
                    vendor_id=vendor_id,
                    manufactured_on=line.manufactured_on,
                    expires_on=line.expires_on,
                    received_on=received_on,
                    initial_quantity=Decimal(line.accepted_quantity),
                    unit_cost=Decimal(line.unit_cost),
                )
            )
            batch_id = created.id
            known[key] = batch_id
        line.batch_id = batch_id


async def _resolve_serials(
    session: AsyncSession,
    tenant: TenantContext,
    receipt: GoodsReceipt,
    lines: Sequence[GoodsReceiptLine],
    items: dict[uuid.UUID, PurchasableItem],
) -> None:
    """Adopt pre-registered units and mint the rest for one receipt.

    A serial may be registered at goods-in before the paperwork is keyed. The
    adoption/mint rule belongs to the inventory tracker, not here - this only
    answers the receipt-specific questions: is the item tracked, and does the
    count match what was accepted?
    """
    traceability = TraceabilityService(session, tenant)
    seen: set[str] = set()

    for line in lines:
        numbers = list(line.serial_numbers or [])
        accepted = Decimal(line.accepted_quantity)
        if accepted <= 0:
            if numbers:
                raise BusinessRuleError(
                    f"Line {line.line_number} has serials but no accepted quantity."
                )
            continue
        item = items[line.item_id]
        if item.is_serial_tracked and not numbers:
            raise BusinessRuleError(
                f"Line {line.line_number}: {item.code} is serial tracked; name one serial "
                "per accepted unit.",
                code=ErrorCode.SERIAL_REQUIRED,
                details={"line_number": line.line_number, "item_code": item.code},
            )
        if not item.is_serial_tracked:
            if numbers:
                raise BusinessRuleError(
                    f"Line {line.line_number}: {item.code} is not serial tracked."
                )
            continue
        if Decimal(len(numbers)) != accepted:
            raise BusinessRuleError(
                f"Line {line.line_number}: {len(numbers)} serials were captured for "
                f"{accepted} accepted units.",
                code=ErrorCode.SERIAL_REQUIRED,
                details={"line_number": line.line_number, "item_code": item.code},
            )
        repeated = seen.intersection(numbers)
        if repeated:
            raise BusinessRuleError(
                f"Serial {sorted(repeated)[0]} appears on more than one receipt line.",
                details={"serial_number": sorted(repeated)[0]},
            )
        seen.update(numbers)
        line.serial_ids = await traceability.resolve_serial_ids(
            line.item_id,
            numbers,
            batch_id=line.batch_id,
            warehouse_id=receipt.warehouse_id,
            location_id=line.location_id,
            received_on=receipt.receipt_date,
            unit_cost=Decimal(line.unit_cost),
            source=receipt.code,
        )

    await session.flush()


async def post_goods_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    receipt_id: uuid.UUID,
    *,
    quality: QualityPort | None = None,
) -> GoodsReceiptPostResult:
    """Post a receipt: move the stock, route QC, advance the order. One transaction.

    In order:

    1. Lock the receipt and its purchase order, then every order line in primary
       key order, so two deliveries against the same order serialise rather than
       both validating against stale received quantities.
    2. Re-check the over-receipt tolerance against the *locked* quantities. The
       check at draft time was advisory; this one is the gate.
    3. Post every accepted quantity through ``inventory.post_movements`` as one
       batch, routed to ``QC_HOLD`` where the line requires inspection and to
       ``AVAILABLE`` where it does not.
    4. Advance the purchase order and its lines through the state machine.
    5. Raise the incoming inspections, or leave the lines flagged for quality.
    6. Write the audit entries - all of it inside the same transaction.

    Rejected quantity is deliberately *not* posted to inventory. Goods refused
    at the gate never entered the company's custody; they leave on the lorry
    they arrived on. Material accepted and only later found defective is sent
    back with a purchase return, which posts the offsetting movement.
    """
    receipt_repo = GoodsReceiptRepository(session, tenant)
    receipt_line_repo = GoodsReceiptLineRepository(session, tenant)
    order_repo = PurchaseOrderRepository(session, tenant)
    order_line_repo = PurchaseOrderLineRepository(session, tenant)

    quality_port = quality if quality is not None else resolve_quality_port()

    async with transaction(session):
        receipt = await receipt_repo.lock_for_update(receipt_id)
        if receipt is None:
            raise NotFoundError("Goods receipt", receipt_id)
        assert_warehouse_scope(tenant, receipt.warehouse_id)
        # Fail before any work: the state machine is the authority on whether
        # this document can be posted at all.
        GOODS_RECEIPT_LIFECYCLE.assert_transition(
            GoodsReceiptStatus(receipt.status), GoodsReceiptStatus.POSTED
        )

        order = await order_repo.lock_for_update(receipt.purchase_order_id)
        if order is None:  # pragma: no cover - FK guarantees the row exists
            raise NotFoundError("Purchase order", receipt.purchase_order_id)
        if PurchaseOrderStatus(order.status) not in RECEIVABLE_PO_STATUSES:
            raise BusinessRuleError(
                f"Purchase order {order.code} is {order.status}; {receipt.code} cannot be posted "
                "against it.",
                details={"status": str(order.status)},
            )

        lines = await receipt_line_repo.list_for_receipt(receipt.id)
        if not lines:
            raise BusinessRuleError(f"Goods receipt {receipt.code} has no lines to post.")

        order_lines = {line.id: line for line in await order_line_repo.lock_for_order(order.id)}
        items = await _load_items(session, tenant, [line.item_id for line in lines])

        context = _ReceiptContext(
            receipt=receipt, order=order, lines=lines, order_lines=order_lines, items=items
        )
        _validate_receipt_against_order(context)
        _clear_qc_on_unaccepted_lines(context)

        await _resolve_batches(
            session,
            tenant,
            lines,
            vendor_id=receipt.vendor_id,
            received_on=receipt.receipt_date,
        )
        await _resolve_serials(session, tenant, receipt, lines, context.items)

        movements = _build_receipt_movements(context)
        transactions = await post_movements(session, tenant, movements) if movements else []

        _apply_receipt_to_order_lines(context)
        await session.flush()

        qc_lines = [
            line for line in lines if line.requires_qc and Decimal(line.accepted_quantity) > 0
        ]
        inspections_created = False
        if quality_port is not None and qc_lines:
            inspections_created = await _raise_inspections(
                session, tenant, context, qc_lines, quality_port
            )

        receipt.posted_at = utcnow()
        receipt.posted_by_id = principal.user_id
        await _transition(
            session,
            tenant,
            machine=GOODS_RECEIPT_LIFECYCLE,
            entity=receipt,
            entity_type=AUDIT_GOODS_RECEIPT,
            entity_code=receipt.code,
            target=GoodsReceiptStatus.POSTED,
            reason=f"Posted against {order.code}",
            status_enum=GoodsReceiptStatus,
        )

        await _advance_order_after_receipt(session, tenant, context)
        # The proposal's "Material received" to the vendor, in this transaction:
        # its users who may open the order, through the portal's own scope rule.
        await notify_vendor_about_order(
            session,
            tenant,
            order,
            permission=Permission.VENDOR_PORTAL_ACCESS,
            notification_type=NotificationType.GOODS_RECEIPT_POSTED,
            title=f"Material received against {order.code}",
            body=(
                f"{receipt.code}: {_quantity_text(receipt.total_accepted_quantity)} accepted, "
                f"{_quantity_text(receipt.total_rejected_quantity)} rejected."
            ),
            entity_type=AUDIT_GOODS_RECEIPT,
            entity_id=receipt.id,
            action_url=vendor_order_url(order.id),
        )

        # The products module documents these two columns as written by goods
        # receipt posting (see ItemVendorLink.last_purchase_price). One
        # statement for every item on the receipt.
        await ItemVendorLinkRepository(session, tenant).record_last_purchase(
            receipt.vendor_id,
            {
                line.item_id: Decimal(line.unit_cost)
                for line in lines
                if Decimal(line.accepted_quantity) > 0
            },
            receipt.receipt_date,
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_GOODS_RECEIPT,
            entity_id=receipt.id,
            entity_code=receipt.code,
            summary=(
                f"Posted {receipt.code}: {receipt.total_accepted_quantity} accepted into "
                f"{len(movements)} stock position(s), "
                f"{receipt.total_rejected_quantity} rejected"
            ),
            user_id=tenant.user_id,
            new_values={
                "purchase_order_code": order.code,
                "purchase_order_status": str(order.status),
                "movements": len(movements),
                "qc_hold_lines": len(qc_lines),
                "qc_inspections_created": inspections_created,
            },
        )

    return GoodsReceiptPostResult(
        receipt=receipt_detail(receipt, lines),
        purchase_order_status=PurchaseOrderStatus(order.status),
        movements=[
            PostedMovement(
                transaction_id=txn.id,
                item_id=txn.item_id,
                warehouse_id=txn.warehouse_id,
                quantity=Decimal(txn.quantity),
                stock_state=str(txn.stock_state),
                batch_id=txn.batch_id,
            )
            for txn in transactions
        ],
        qc_hold_line_count=len(qc_lines),
        qc_inspections_created=inspections_created,
    )


def _validate_receipt_against_order(context: _ReceiptContext) -> None:
    """Re-check every line against the locked order quantities."""
    for line in context.lines:
        order_line = context.order_lines.get(line.purchase_order_line_id)
        if order_line is None:
            raise BusinessRuleError(
                f"Line {line.line_number} of {context.receipt.code} references a purchase order "
                f"line that is no longer on {context.order.code}.",
                details={"line_number": line.line_number},
            )
        if PurchaseOrderLineStatus(order_line.status) is PurchaseOrderLineStatus.CANCELLED:
            raise BusinessRuleError(
                f"Line {line.line_number}: purchase order line {order_line.line_number} has been "
                "cancelled and cannot be received.",
                details={"line_number": line.line_number},
            )
        item = context.items[line.item_id]
        assert_within_over_receipt_tolerance(
            item_code=item.code,
            line_number=line.line_number,
            ordered_quantity=order_line.quantity,
            already_received=order_line.received_quantity,
            incoming_quantity=line.received_quantity,
        )
        if item.is_batch_tracked and not line.batch_number:
            raise BusinessRuleError(
                f"Item {item.code} is batch tracked; line {line.line_number} has no lot number.",
                code=ErrorCode.BATCH_REQUIRED,
                details={"item_code": item.code, "line_number": line.line_number},
            )


def _clear_qc_on_unaccepted_lines(context: _ReceiptContext) -> None:
    """Drop the inspection flag from lines that accepted nothing.

    Goods refused at the gate never entered the company's custody, so no
    quantity is put on QC hold and no inspection is raised for them. Leaving
    such a line flagged PENDING puts a work item in quality's inbound queue that
    has nothing to inspect, and - because the order-level count reads exactly
    that flag - holds the purchase order in QC_PENDING, which only an inspection
    decision can leave. Applied here as well as at draft time so a receipt keyed
    before this rule existed still posts to a reachable state.
    """
    for line in context.lines:
        if line.requires_qc and to_quantity(Decimal(line.accepted_quantity)) <= 0:
            line.requires_qc = False
            line.qc_status = ReceiptLineQcStatus.NOT_REQUIRED


def _build_receipt_movements(context: _ReceiptContext) -> list[MovementRequest]:
    """One movement per accepted line, or per unit on a serialised line.

    Built as a batch so the inventory engine locks every affected balance row in
    primary-key order - the deadlock-free pattern database.md section 4.4
    requires for a multi-line document. A serialised line posts one movement per
    unit because the unit's lifecycle is decided per row, and each unit is
    physically the thing being received.
    """
    movements: list[MovementRequest] = []
    for line in context.lines:
        accepted = to_quantity(Decimal(line.accepted_quantity))
        if accepted <= 0:
            continue
        state = StockState.QC_HOLD if line.requires_qc else StockState.AVAILABLE
        line.posted_stock_state = str(state)
        reason = f"Goods receipt {context.receipt.code} against {context.order.code}" + (
            " (held for incoming inspection)" if line.requires_qc else ""
        )
        serial_ids = line.serial_ids or []
        if serial_ids:
            for serial_id in serial_ids:
                movements.append(
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=context.receipt.warehouse_id,
                        quantity=Decimal("1"),
                        transaction_type=TransactionType.PURCHASE_RECEIPT,
                        reference_type=MOVEMENT_REFERENCE_GRN,
                        reference_id=context.receipt.id,
                        reference_code=context.receipt.code,
                        reason=reason,
                        location_id=line.location_id,
                        batch_id=line.batch_id,
                        serial_id=serial_id,
                        unit_cost=Decimal(line.unit_cost),
                        stock_state=state,
                    )
                )
        else:
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=context.receipt.warehouse_id,
                    quantity=accepted,
                    transaction_type=TransactionType.PURCHASE_RECEIPT,
                    reference_type=MOVEMENT_REFERENCE_GRN,
                    reference_id=context.receipt.id,
                    reference_code=context.receipt.code,
                    reason=reason,
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                    unit_cost=Decimal(line.unit_cost),
                    stock_state=state,
                )
            )
    return movements


def _apply_receipt_to_order_lines(context: _ReceiptContext) -> None:
    """Roll the receipt into the order lines' running totals."""
    for line in context.lines:
        order_line = context.order_lines[line.purchase_order_line_id]
        order_line.received_quantity = to_quantity(
            Decimal(order_line.received_quantity) + Decimal(line.received_quantity)
        )
        order_line.accepted_quantity = to_quantity(
            Decimal(order_line.accepted_quantity) + Decimal(line.accepted_quantity)
        )
        order_line.rejected_quantity = to_quantity(
            Decimal(order_line.rejected_quantity) + Decimal(line.rejected_quantity)
        )
        order_line.status = po_line_status_for(
            quantity=order_line.quantity, received_quantity=order_line.received_quantity
        )


async def _raise_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    context: _ReceiptContext,
    qc_lines: Sequence[GoodsReceiptLine],
    quality_port: QualityPort,
) -> bool:
    """Ask the quality module for an inspection per held line.

    One call per held line because an inspection is per lot and per line; there
    is no batch form in the contract. A failure here is not swallowed - the
    whole receipt rolls back rather than leaving quantity on QC hold that
    nobody has been asked to inspect.
    """
    for line in qc_lines:
        inspection = await quality_port(
            session,
            tenant,
            item_id=line.item_id,
            quantity=Decimal(line.accepted_quantity),
            warehouse_id=context.receipt.warehouse_id,
            reference_type=MOVEMENT_REFERENCE_GRN,
            reference_id=context.receipt.id,
            reference_code=context.receipt.code,
            vendor_id=context.receipt.vendor_id,
            batch_id=line.batch_id,
            **({"serial_ids": line.serial_ids} if line.serial_ids else {}),
        )
        line.qc_inspection_id = getattr(inspection, "id", None)
        line.qc_status = ReceiptLineQcStatus.PENDING
    await session.flush()
    return True


async def _advance_order_after_receipt(
    session: AsyncSession, tenant: TenantContext, context: _ReceiptContext
) -> None:
    """Move the purchase order to where its quantities now say it is.

    A short receipt leaves it PARTIALLY_RECEIVED. A complete one goes to
    RECEIVED and then straight on: to QC_PENDING when any line is held for
    inspection, otherwise to GRN_COMPLETED. Each hop is a separate audited
    transition, so the document's history reads as what actually happened.
    """
    order = context.order
    all_lines = list(context.order_lines.values())
    fully_received = order_is_fully_received(
        [
            (
                Decimal(line.quantity),
                Decimal(line.received_quantity),
                PurchaseOrderLineStatus(line.status)
                in (PurchaseOrderLineStatus.CLOSED, PurchaseOrderLineStatus.CANCELLED),
            )
            for line in all_lines
        ]
    )

    target = (
        PurchaseOrderStatus.RECEIVED if fully_received else PurchaseOrderStatus.PARTIALLY_RECEIVED
    )
    if PurchaseOrderStatus(order.status) != target:
        await _transition(
            session,
            tenant,
            machine=PURCHASE_ORDER_LIFECYCLE,
            entity=order,
            entity_type=AUDIT_PURCHASE_ORDER,
            entity_code=order.code,
            target=target,
            reason=f"Goods receipt {context.receipt.code} posted",
            status_enum=PurchaseOrderStatus,
        )

    if not fully_received:
        return

    awaiting_qc = await GoodsReceiptLineRepository(session, tenant).qc_pending_count(order.id)
    follow_on = PurchaseOrderStatus.QC_PENDING if awaiting_qc else PurchaseOrderStatus.GRN_COMPLETED
    await _transition(
        session,
        tenant,
        machine=PURCHASE_ORDER_LIFECYCLE,
        entity=order,
        entity_type=AUDIT_PURCHASE_ORDER,
        entity_code=order.code,
        target=follow_on,
        reason=(
            f"{awaiting_qc} line(s) awaiting incoming inspection"
            if awaiting_qc
            else "All receipts posted and no inspection required"
        ),
        status_enum=PurchaseOrderStatus,
    )


# =============================================================================
# The incoming-inspection verdict - called back by quality
# =============================================================================
#
# Quality decides the inspection a goods receipt raised and moves the held stock
# itself. It then tells this module through ``quality.owners``, in the same
# transaction, so the receipt line and the purchase order follow the verdict
# without anything here moving stock a second time. Nothing in this module may
# release QC_HOLD stock: the inspection is the one release path.


class IncomingInspectionVerdict(Protocol):
    """What procurement reads off a decided incoming inspection.

    A protocol rather than quality's own type, so this module does not import
    the module it reaches through a port.
    """

    @property
    def inspection_id(self) -> uuid.UUID: ...
    @property
    def inspection_code(self) -> str: ...
    @property
    def reference_id(self) -> uuid.UUID | None: ...
    @property
    def accepted_quantity(self) -> Decimal: ...
    @property
    def refused_quantity(self) -> Decimal: ...
    @property
    def is_settled(self) -> bool: ...
    @property
    def reason(self) -> str: ...


async def lock_for_incoming_inspection(
    session: AsyncSession, tenant: TenantContext, reference_id: uuid.UUID
) -> None:
    """Take the purchase order's row lock before quality moves any stock.

    Quality calls this right after locking the inspection, so a decision locks
    inspection -> purchase order -> balances. Posting a second delivery takes
    purchase order -> balances, the same order, so the two queue instead of
    deadlocking. A reference that names no receipt (an inspection raised by hand)
    locks nothing.
    """
    receipt = await GoodsReceiptRepository(session, tenant).get(reference_id)
    if receipt is not None:
        await PurchaseOrderRepository(session, tenant).lock_for_update(receipt.purchase_order_id)


async def record_incoming_inspection_outcome(
    session: AsyncSession, tenant: TenantContext, verdict: IncomingInspectionVerdict
) -> bool:
    """Write an incoming inspection's verdict back to its receipt line and order.

    Once nothing of the lot is held any more, the line goes PASSED or FAILED
    (:func:`~src.modules.procurement.service_rules.receipt_line_qc_status`).
    When that leaves the purchase order with no line still PENDING, the order
    moves QC_PENDING -> QC_COMPLETED -> GRN_COMPLETED through the state machine,
    each hop audited. A purchase order still short of a delivery stays where it
    is: when its last receipt posts, ``_advance_order_after_receipt`` counts the
    lines still pending and finds none.

    Moves no stock - quality already did. Idempotent: a line already decided is
    left alone, so the reconciliation sweep can call this as often as it likes.
    Returns whether anything changed.
    """
    if verdict.reference_id is None:
        return False

    order_repo = PurchaseOrderRepository(session, tenant)
    line_repo = GoodsReceiptLineRepository(session, tenant)

    async with transaction(session):
        receipt = await GoodsReceiptRepository(session, tenant).get(verdict.reference_id)
        if receipt is None or GoodsReceiptStatus(receipt.status) is not GoodsReceiptStatus.POSTED:
            log.info(
                "procurement.inspection_names_no_posted_receipt",
                inspection_id=str(verdict.inspection_id),
                reference_id=str(verdict.reference_id),
            )
            return False

        # The order is the serialisation point: two inspections of one order
        # decided at once must not both read the other's line as still pending.
        order = await order_repo.lock_for_update(receipt.purchase_order_id)
        if order is None:  # pragma: no cover - the foreign key guarantees the row
            return False

        changed = False
        line = await line_repo.for_inspection(receipt.id, verdict.inspection_id)
        new_status = receipt_line_qc_status(
            settled=verdict.is_settled, refused_quantity=verdict.refused_quantity
        )
        if (
            line is not None
            and new_status is not None
            and ReceiptLineQcStatus(line.qc_status) is ReceiptLineQcStatus.PENDING
        ):
            line.qc_status = new_status
            line_repo.touch(line)
            await session.flush()
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.QC_DECISION,
                entity_type=AUDIT_GOODS_RECEIPT,
                entity_id=receipt.id,
                entity_code=receipt.code,
                summary=(
                    f"Line {line.line_number} of {receipt.code} "
                    f"{'passed' if new_status is ReceiptLineQcStatus.PASSED else 'failed'} "
                    f"incoming inspection {verdict.inspection_code}"
                ),
                user_id=tenant.user_id,
                old_values={"line_number": line.line_number, "qc_status": "PENDING"},
                new_values={"line_number": line.line_number, "qc_status": str(new_status)},
                context={
                    "qc_inspection_id": verdict.inspection_id,
                    "qc_inspection_code": verdict.inspection_code,
                    "accepted_quantity": verdict.accepted_quantity,
                    "refused_quantity": verdict.refused_quantity,
                    "reason": verdict.reason,
                },
            )
            changed = True

        if await _complete_order_inspection(
            session, tenant, order, reason=f"Inspection {verdict.inspection_code} decided"
        ):
            changed = True

    return changed


async def _complete_order_inspection(
    session: AsyncSession, tenant: TenantContext, order: PurchaseOrder, *, reason: str
) -> bool:
    """Take a QC_PENDING order on to GRN_COMPLETED once no line awaits a verdict."""
    if PurchaseOrderStatus(order.status) is not PurchaseOrderStatus.QC_PENDING:
        return False
    if await GoodsReceiptLineRepository(session, tenant).qc_pending_count(order.id):
        return False

    await _transition(
        session,
        tenant,
        machine=PURCHASE_ORDER_LIFECYCLE,
        entity=order,
        entity_type=AUDIT_PURCHASE_ORDER,
        entity_code=order.code,
        target=PurchaseOrderStatus.QC_COMPLETED,
        reason=f"{reason}; no received line awaits inspection",
        status_enum=PurchaseOrderStatus,
        action=AuditAction.QC_DECISION,
    )
    await _transition(
        session,
        tenant,
        machine=PURCHASE_ORDER_LIFECYCLE,
        entity=order,
        entity_type=AUDIT_PURCHASE_ORDER,
        entity_code=order.code,
        target=PurchaseOrderStatus.GRN_COMPLETED,
        reason="Every receipt posted and every held line inspected",
        status_enum=PurchaseOrderStatus,
    )
    return True


async def awaiting_incoming_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    after: uuid.UUID | None,
    limit: int,
) -> list[tuple[str, uuid.UUID]]:
    """Receipts with a line still waiting on its inspection's verdict.

    One keyset page for ``quality.owners.reconcile_owners``: the sweep reads the
    verdicts these receipts' inspections already carry and replays them through
    :func:`record_incoming_inspection_outcome`.
    """
    receipt_ids = await GoodsReceiptLineRepository(session, tenant).receipts_awaiting_inspection(
        after=after, limit=limit
    )
    return [(MOVEMENT_REFERENCE_GRN, receipt_id) for receipt_id in receipt_ids]


async def cancel_goods_receipt(
    session: AsyncSession, tenant: TenantContext, receipt_id: uuid.UUID, *, reason: str
) -> GoodsReceipt:
    """Cancel a draft receipt. A posted one is reversed by a purchase return."""
    repo = GoodsReceiptRepository(session, tenant)

    async with transaction(session):
        receipt = await repo.lock_for_update(receipt_id)
        if receipt is None:
            raise NotFoundError("Goods receipt", receipt_id)
        assert_warehouse_scope(tenant, receipt.warehouse_id)
        if GoodsReceiptStatus(receipt.status) is GoodsReceiptStatus.POSTED:
            raise BusinessRuleError(
                f"Goods receipt {receipt.code} has been posted and moved stock; raise a purchase "
                "return to send the material back.",
                details={"status": str(receipt.status)},
            )
        receipt.cancellation_reason = reason
        await _transition(
            session,
            tenant,
            machine=GOODS_RECEIPT_LIFECYCLE,
            entity=receipt,
            entity_type=AUDIT_GOODS_RECEIPT,
            entity_code=receipt.code,
            target=GoodsReceiptStatus.CANCELLED,
            reason=reason,
            status_enum=GoodsReceiptStatus,
            action=AuditAction.CANCEL,
        )

    return receipt


async def list_goods_receipts(
    session: AsyncSession,
    tenant: TenantContext,
    filters: GoodsReceiptFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[GoodsReceiptSummary], OffsetPagination]:
    rows, pagination = await GoodsReceiptRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [GoodsReceiptSummary.model_validate(row) for row in rows], pagination


async def get_goods_receipt_detail(
    session: AsyncSession, tenant: TenantContext, receipt_id: uuid.UUID
) -> GoodsReceiptDetail:
    receipt = await GoodsReceiptRepository(session, tenant).get(receipt_id)
    if receipt is None:
        raise NotFoundError("Goods receipt", receipt_id)
    lines = await GoodsReceiptLineRepository(session, tenant).list_for_receipt(receipt_id)
    return receipt_detail(receipt, lines)


# =============================================================================
# Purchase return
# =============================================================================


def return_detail(
    purchase_return: PurchaseReturn, lines: Sequence[PurchaseReturnLine]
) -> PurchaseReturnDetail:
    detail = PurchaseReturnDetail.model_validate(purchase_return)
    detail.lines = [PurchaseReturnLineRead.model_validate(line) for line in lines]
    return detail


async def create_purchase_return(
    session: AsyncSession, tenant: TenantContext, payload: PurchaseReturnCreate
) -> tuple[PurchaseReturn, list[PurchaseReturnLine]]:
    """Raise a DRAFT return against the receipt lines it reverses.

    Bounded by what was actually accepted on each receipt line: returning more
    than came in would drive the position negative and leave the ledger
    describing goods that were never received.
    """
    repo = PurchaseReturnRepository(session, tenant)
    line_repo = PurchaseReturnLineRepository(session, tenant)
    receipt_repo = GoodsReceiptRepository(session, tenant)
    receipt_line_repo = GoodsReceiptLineRepository(session, tenant)

    async with transaction(session):
        receipt = await receipt_repo.get(payload.goods_receipt_id)
        if receipt is None:
            raise NotFoundError("Goods receipt", payload.goods_receipt_id)
        assert_warehouse_scope(tenant, receipt.warehouse_id)
        if GoodsReceiptStatus(receipt.status) is not GoodsReceiptStatus.POSTED:
            raise BusinessRuleError(
                f"Goods receipt {receipt.code} is {receipt.status}; only material that was "
                "actually received can be returned.",
                details={"status": str(receipt.status)},
            )

        receipt_lines = {
            line.id: line
            for line in await receipt_line_repo.lock_many(
                [line.goods_receipt_line_id for line in payload.lines]
            )
        }
        items = await _load_items(
            session, tenant, [line.item_id for line in receipt_lines.values()]
        )
        pool_facts = await _return_pool_facts(
            session, tenant, receipt, list(receipt_lines.values())
        )

        return_date = payload.return_date or today()
        code = await next_document_number(
            session, tenant.company_id, DocTypes.PURCHASE_RETURN, on=return_date
        )
        purchase_return = PurchaseReturn(
            code=code,
            vendor_id=receipt.vendor_id,
            goods_receipt_id=receipt.id,
            purchase_order_id=receipt.purchase_order_id,
            warehouse_id=receipt.warehouse_id,
            return_date=return_date,
            status=PurchaseReturnStatus.DRAFT,
            reason=payload.reason,
            debit_note_number=payload.debit_note_number,
            notes=payload.notes,
        )
        repo.add(purchase_return)
        await session.flush()

        lines: list[PurchaseReturnLine] = []
        amounts: list[LineAmounts] = []
        for payload_line in payload.lines:
            receipt_line = receipt_lines.get(payload_line.goods_receipt_line_id)
            if receipt_line is None or receipt_line.goods_receipt_id != receipt.id:
                raise ValidationError(
                    f"Line {payload_line.line_number} references a receipt line that is not on "
                    f"{receipt.code}.",
                    details={"line_number": payload_line.line_number},
                )
            returnable = Decimal(receipt_line.accepted_quantity) - Decimal(
                receipt_line.returned_quantity
            )
            if Decimal(payload_line.quantity) > returnable:
                raise BusinessRuleError(
                    f"Line {payload_line.line_number}: only {to_quantity(returnable)} of "
                    f"{items[receipt_line.item_id].code} from {receipt.code} is still returnable.",
                    details={
                        "line_number": payload_line.line_number,
                        "returnable_quantity": str(to_quantity(returnable)),
                        "requested_quantity": str(to_quantity(payload_line.quantity)),
                    },
                )

            item = items[receipt_line.item_id]
            # Which pool the goods leave: what the inspection did with them, or
            # the pool the caller named, bounded by what that pool still holds.
            stock_state = resolve_return_pool(
                line_number=payload_line.line_number,
                item_code=item.code,
                receipt_code=receipt.code,
                quantity=Decimal(payload_line.quantity),
                requested_pool=payload_line.stock_state,
                facts=pool_facts[receipt_line.id],
            )
            line_amounts = compute_line_amounts(
                quantity=payload_line.quantity,
                unit_price=Decimal(receipt_line.unit_cost),
                tax_rate=item.gst_rate,
            )
            amounts.append(line_amounts)
            lines.append(
                line_repo.add(
                    PurchaseReturnLine(
                        purchase_return_id=purchase_return.id,
                        line_number=payload_line.line_number,
                        goods_receipt_line_id=receipt_line.id,
                        item_id=receipt_line.item_id,
                        description=receipt_line.description or item.name,
                        quantity=to_quantity(payload_line.quantity),
                        uom_id=receipt_line.uom_id,
                        unit_price=Decimal(receipt_line.unit_cost),
                        tax_rate=item.gst_rate,
                        tax_amount=line_amounts.tax_amount,
                        line_total=line_amounts.line_total,
                        batch_id=receipt_line.batch_id,
                        location_id=receipt_line.location_id,
                        stock_state=stock_state,
                        reason=payload_line.reason or payload.reason,
                        notes=payload_line.notes,
                    )
                )
            )

        totals = sum_document_totals(amounts)
        purchase_return.subtotal = totals.subtotal
        purchase_return.tax_amount = totals.tax_amount
        purchase_return.total_amount = totals.total_amount
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_PURCHASE_RETURN,
            entity_id=purchase_return.id,
            entity_code=purchase_return.code,
            summary=(
                f"Raised {purchase_return.code} against {receipt.code}: "
                f"{len(lines)} line(s), {purchase_return.total_amount}"
            ),
            user_id=tenant.user_id,
            new_values={
                "goods_receipt_id": receipt.id,
                "vendor_id": purchase_return.vendor_id,
                "reason": purchase_return.reason,
                "line_count": len(lines),
            },
        )

    return purchase_return, lines


async def post_purchase_return(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    return_id: uuid.UUID,
) -> PurchaseReturnPostResult:
    """Post the return: one negative movement per line, against the same lot.

    ``PURCHASE_RETURN`` is a decreasing transaction type, so the quantity passed
    is a magnitude and the engine applies the sign (see CLAUDE.md - the type
    carries the sign, never the caller's arithmetic).
    """
    repo = PurchaseReturnRepository(session, tenant)
    line_repo = PurchaseReturnLineRepository(session, tenant)
    receipt_line_repo = GoodsReceiptLineRepository(session, tenant)
    order_line_repo = PurchaseOrderLineRepository(session, tenant)

    async with transaction(session):
        purchase_return = await repo.lock_for_update(return_id)
        if purchase_return is None:
            raise NotFoundError("Purchase return", return_id)
        assert_warehouse_scope(tenant, purchase_return.warehouse_id)
        PURCHASE_RETURN_LIFECYCLE.assert_transition(
            PurchaseReturnStatus(purchase_return.status), PurchaseReturnStatus.POSTED
        )

        lines = await line_repo.list_for_return(purchase_return.id)
        if not lines:
            raise BusinessRuleError(f"Purchase return {purchase_return.code} has no lines to post.")

        receipt_lines = {
            line.id: line
            for line in await receipt_line_repo.lock_many(
                [line.goods_receipt_line_id for line in lines]
            )
        }
        items = await _load_items(session, tenant, [line.item_id for line in lines])
        receipt = await GoodsReceiptRepository(session, tenant).get(
            purchase_return.goods_receipt_id
        )
        if receipt is None:
            raise NotFoundError("Goods receipt", purchase_return.goods_receipt_id)
        # The authoritative pass: the receipt lines are locked now, so the
        # per-pool balances are re-derived here rather than trusted from the
        # draft - another return may have posted since it was raised.
        pool_facts = await _return_pool_facts(
            session, tenant, receipt, list(receipt_lines.values())
        )

        # Locked here, before any movement is posted, for two reasons. The
        # returned quantity below is a read-modify-write on a column with no
        # version counter, and nothing else in this path serialises two returns
        # against the same order line: they touch different receipt lines, and
        # when the two deliveries arrived under different lots they touch
        # different balance rows too. Taking the lock before post_movements also
        # keeps the acquisition order the same as the receipt path's - order
        # lines, then inventory balances - so the two cannot deadlock.
        order_lines = {
            line.id: line
            for line in await order_line_repo.lock_many(
                [receipt_lines[line.goods_receipt_line_id].purchase_order_line_id for line in lines]
            )
        }
        order_lines_by_receipt: dict[uuid.UUID, PurchaseOrderLine] = {}
        for receipt_line in receipt_lines.values():
            order_line = order_lines.get(receipt_line.purchase_order_line_id)
            if order_line is None:
                raise NotFoundError("Purchase order line", receipt_line.purchase_order_line_id)
            order_lines_by_receipt[receipt_line.id] = order_line

        movements: list[MovementRequest] = []
        for line in lines:
            receipt_line = receipt_lines[line.goods_receipt_line_id]
            returnable = Decimal(receipt_line.accepted_quantity) - Decimal(
                receipt_line.returned_quantity
            )
            if Decimal(line.quantity) > returnable:
                raise BusinessRuleError(
                    f"Line {line.line_number}: only {to_quantity(returnable)} of "
                    f"{items[line.item_id].code} is still returnable against this receipt.",
                    details={
                        "line_number": line.line_number,
                        "returnable_quantity": str(to_quantity(returnable)),
                    },
                )
            # Bounded per pool as well as in aggregate: the draft named the pool
            # when it was raised, and what that pool still holds is checked
            # again under the lock.
            stock_state = resolve_return_pool(
                line_number=line.line_number,
                item_code=items[line.item_id].code,
                receipt_code=receipt.code,
                quantity=Decimal(line.quantity),
                requested_pool=str(line.stock_state),
                facts=pool_facts[receipt_line.id],
            )
            movements.append(
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=purchase_return.warehouse_id,
                    quantity=to_quantity(Decimal(line.quantity)),
                    transaction_type=TransactionType.PURCHASE_RETURN,
                    reference_type=MOVEMENT_REFERENCE_RETURN,
                    reference_id=purchase_return.id,
                    reference_code=purchase_return.code,
                    reason=f"{purchase_return.code}: {line.reason or purchase_return.reason}",
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                    unit_cost=Decimal(line.unit_price),
                    # Returned stock leaves the pool the inspection put it in:
                    # AVAILABLE for released goods, DAMAGED for a rejected lot.
                    # Same batch, no location - the grain quality's decision
                    # movement wrote to.
                    stock_state=StockState(stock_state),
                )
            )

        transactions = await post_movements(session, tenant, movements)

        for line in lines:
            receipt_line = receipt_lines[line.goods_receipt_line_id]
            receipt_line.returned_quantity = to_quantity(
                Decimal(receipt_line.returned_quantity) + Decimal(line.quantity)
            )
            order_line = order_lines_by_receipt[receipt_line.id]
            order_line.returned_quantity = to_quantity(
                Decimal(order_line.returned_quantity) + Decimal(line.quantity)
            )

        purchase_return.posted_at = utcnow()
        purchase_return.posted_by_id = principal.user_id
        await session.flush()

        await _transition(
            session,
            tenant,
            machine=PURCHASE_RETURN_LIFECYCLE,
            entity=purchase_return,
            entity_type=AUDIT_PURCHASE_RETURN,
            entity_code=purchase_return.code,
            target=PurchaseReturnStatus.POSTED,
            reason=purchase_return.reason,
            status_enum=PurchaseReturnStatus,
        )

        from src.modules.finance.service import post_purchase_return_debit_note

        debit_note_journal_id = await post_purchase_return_debit_note(
            session,
            tenant,
            purchase_return_id=purchase_return.id,
            code=purchase_return.code,
            vendor_id=purchase_return.vendor_id,
            entry_date=purchase_return.return_date,
            currency=purchase_return.currency,
            subtotal=Decimal(purchase_return.subtotal),
            tax_amount=Decimal(purchase_return.tax_amount),
            total_amount=Decimal(purchase_return.total_amount),
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_PURCHASE_RETURN,
            entity_id=purchase_return.id,
            entity_code=purchase_return.code,
            summary=(
                f"Posted {purchase_return.code}: {len(movements)} line(s) returned to the vendor"
            ),
            user_id=tenant.user_id,
            new_values={
                "movements": len(movements),
                "total_amount": purchase_return.total_amount,
                "reason": purchase_return.reason,
                "debit_note_journal_id": debit_note_journal_id,
            },
        )

    return PurchaseReturnPostResult(
        purchase_return=return_detail(purchase_return, lines),
        debit_note_journal_id=debit_note_journal_id,
        movements=[
            PostedMovement(
                transaction_id=txn.id,
                item_id=txn.item_id,
                warehouse_id=txn.warehouse_id,
                quantity=Decimal(txn.quantity),
                stock_state=str(txn.stock_state),
                batch_id=txn.batch_id,
            )
            for txn in transactions
        ],
    )


async def cancel_purchase_return(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID, *, reason: str
) -> PurchaseReturn:
    repo = PurchaseReturnRepository(session, tenant)

    async with transaction(session):
        purchase_return = await repo.lock_for_update(return_id)
        if purchase_return is None:
            raise NotFoundError("Purchase return", return_id)
        assert_warehouse_scope(tenant, purchase_return.warehouse_id)
        purchase_return.cancellation_reason = reason
        await _transition(
            session,
            tenant,
            machine=PURCHASE_RETURN_LIFECYCLE,
            entity=purchase_return,
            entity_type=AUDIT_PURCHASE_RETURN,
            entity_code=purchase_return.code,
            target=PurchaseReturnStatus.CANCELLED,
            reason=reason,
            status_enum=PurchaseReturnStatus,
            action=AuditAction.CANCEL,
        )

    return purchase_return


async def list_purchase_returns(
    session: AsyncSession,
    tenant: TenantContext,
    filters: PurchaseReturnFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[PurchaseReturnSummary], OffsetPagination]:
    rows, pagination = await PurchaseReturnRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [PurchaseReturnSummary.model_validate(row) for row in rows], pagination


async def get_purchase_return_detail(
    session: AsyncSession, tenant: TenantContext, return_id: uuid.UUID
) -> PurchaseReturnDetail:
    purchase_return = await PurchaseReturnRepository(session, tenant).get(return_id)
    if purchase_return is None:
        raise NotFoundError("Purchase return", return_id)
    lines = await PurchaseReturnLineRepository(session, tenant).list_for_return(return_id)
    return return_detail(purchase_return, lines)


# =============================================================================
# Approvals queue
# =============================================================================


async def pending_approvals(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    filters: PendingApprovalFilters,
    params: PageParams,
) -> tuple[list[PendingApproval], OffsetPagination]:
    """Documents waiting on *this* principal's decision.

    Filtered by what the caller can actually approve rather than by what exists:
    a queue full of items somebody cannot action is a queue they stop reading.
    """
    return await PendingApprovalRepository(session, tenant).list_pending(
        params,
        filters=filters,
        include_requisitions=principal.has(Permission.PURCHASE_REQUISITION_APPROVE),
        include_orders=principal.has(Permission.PURCHASE_ORDER_APPROVE),
    )


# =============================================================================
# Planning contract: what MRP nets incoming supply against
# =============================================================================
#
# ``src.modules.mrp.repository`` resolves this function by name at call time -
# it never imports these tables and never guesses at their columns. The
# signature below IS that contract; changing it silently turns MRP's netting
# stage off, which shows up only as a warning on the run and a plan that
# over-orders. See the module docstring in ``src/modules/mrp/repository.py``.


async def incoming_quantities(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_ids: Sequence[uuid.UUID],
    warehouse_ids: Sequence[uuid.UUID] | None = None,
    upto: date,
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """Committed purchase-order balances still expected, by promised arrival date.

    The netting input for master rules section 14's "net against confirmed
    incoming POs (**by expected date**)". Read-only by construction: no status
    moves, no stock moves, and therefore no audit row - there is nothing to
    audit about answering a question.

    Each item maps to its outstanding balances as ``(due_date, quantity)``
    tranches in date order, all due on or before ``upto``. The dates are the
    contract: the caller nets a requirement against the tranches that arrive
    before it is needed, not against the whole horizon. A single summed figure
    per item - what this returned before - cannot express that, and a plan built
    on one silently netted demand due this week against an order landing next
    month.

    Which orders count is :data:`~src.modules.procurement.repository.
    NETTABLE_PO_STATUSES` (approved and beyond, not yet arrived) crossed with
    :data:`~src.modules.procurement.repository.NETTABLE_PO_LINE_STATUSES`
    (lines still expecting a delivery); both constants carry the argument for
    their membership.

    ``warehouse_ids`` of ``None`` means the whole company, which is what a run
    that was not scoped to warehouses plans against. ``item_ids`` empty returns
    an empty mapping. Items with nothing on order are absent from the result;
    the planner reads it with ``incoming.get(item_id, ())``, so absence and an
    empty schedule mean the same thing.
    """
    return await PurchaseOrderLineRepository(session, tenant).incoming_by_item(
        item_ids, warehouse_ids=warehouse_ids, upto=upto
    )


__all__ = [
    "AUDIT_GOODS_RECEIPT",
    "AUDIT_PURCHASE_ORDER",
    "AUDIT_PURCHASE_RETURN",
    "AUDIT_QUOTATION",
    "AUDIT_REQUISITION",
    "AUDIT_RFQ",
    "IncomingInspectionVerdict",
    "QualityPort",
    "awaiting_incoming_inspections",
    "can_approve",
    "cancel_goods_receipt",
    "cancel_purchase_return",
    "check_duplicates",
    "compare_quotations",
    "convert_requisitions_to_orders",
    "create_goods_receipt",
    "create_purchase_order",
    "create_purchase_return",
    "create_quotation",
    "create_requisition",
    "create_requisition_for_planning",
    "create_requisition_from_mrp",
    "create_rfq",
    "decide_quotation",
    "delete_draft_requisition",
    "describe_purchase_order",
    "find_duplicate_candidates",
    "find_duplicates_for_lines",
    "get_goods_receipt_detail",
    "get_purchase_order_detail",
    "get_purchase_return_detail",
    "get_quotation_detail",
    "get_requisition_detail",
    "get_rfq_detail",
    "incoming_quantities",
    "list_goods_receipts",
    "list_purchase_orders",
    "list_purchase_returns",
    "list_quotations",
    "list_requisitions",
    "list_rfqs",
    "list_vendor_assignees",
    "lock_for_incoming_inspection",
    "order_detail",
    "order_tracking",
    "pending_approvals",
    "post_goods_receipt",
    "post_purchase_return",
    "preview_requisition_conversion",
    "quotation_detail",
    "quotation_terms",
    "receipt_detail",
    "record_incoming_inspection_outcome",
    "record_shipment",
    "requisition_detail",
    "resolve_quality_port",
    "return_detail",
    "rfq_detail",
    "transition_purchase_order",
    "transition_requisition",
    "transition_rfq",
    "update_purchase_order",
    "update_quotation",
    "update_requisition",
    "update_rfq",
]
